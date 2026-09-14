"""Bollinger 15M v2 paper policy and shadow-learning integration.

This module is intentionally paper-only.  It consumes public market data, writes
local SQLite state and never contains an authenticated exchange/order endpoint.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Mapping, Sequence
import uuid

from strategies import BAR_LABEL, BAR_MS, BAR_SECONDS, FRESH_WINDOW_SECONDS
from v2_engine import (
    DEFAULT_COST, EXECUTION_POLICY_VERSION, MAIN_SPEC, MAX_ENTRY_SPREAD,
    MIN_TARGET_NET_RETURN, POLICY, candidate_signals, indicators,
)
import v2_model
import v2_store
import v2_challengers
import paper_v3


ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / 'state/bollinger-v2-model.json'
TRAINING_REPORT_PATH = ROOT / 'reports/bollinger-v2-training.json'
TRAINING_DATA_PATH = ROOT / 'data/bitstamp-btc-usd-15m-200000.csv'
AUTO_RETRAIN_LOCK = ROOT / 'state/bollinger-v2-retrain.lock'
AUTO_RETRAIN_LOG = ROOT / 'state/bollinger-v2-retrain.log'
MODEL_UPDATE_LOCK = ROOT / 'state/bollinger-v2-model-update.lock'
PORTFOLIOS_INITIALIZED_KEY = 'v2_portfolios_initialized'
PORTFOLIOS = ('trend', 'breakout', 'reversion',
              'learned_trend', 'learned_breakout', 'learned_reversion')
BASE_PORTFOLIOS = ('trend', 'breakout', 'reversion')
ACCOUNT_FOR_STRATEGY = {'breakout': 'learned_breakout',
                        'reentry': 'learned_reversion'}

# Only a single tiny paper probe is allowed while the model remains in shadow.
PROBE_RISK_FRACTION = .0001
PROBE_ALLOCATION_CAP = .005
NORMAL_RISK_FRACTION = .001
NORMAL_ALLOCATION_CAP = .05
POSITION_HOLD_SECONDS = MAIN_SPEC.horizon_bars * BAR_SECONDS
AUTO_RETRAIN_CASH_BATCH = 25
AUTO_RETRAIN_FAILED_EVIDENCE = 100
AUTO_RETRAIN_LOCK_MAX_AGE_SECONDS = 2 * 60 * 60
AUTO_RETRAIN_CHECK_INTERVAL_SECONDS = 15 * 60
AUTO_RETRAIN_CHECK_STATE_KEY = 'v2_last_auto_retrain_check'


def _get(db: sqlite3.Connection, key: str, default=None):
    row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put(db: sqlite3.Connection, key: str, value) -> None:
    db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
               (key, json.dumps(value, allow_nan=False)))


def _event(db: sqlite3.Connection, now: float, strategy: str,
           kind: str, detail: Mapping[str, object]) -> None:
    db.execute('INSERT INTO events(ts,strategy,kind,detail) VALUES (?,?,?,?)',
               (now, strategy, kind,
                json.dumps(dict(detail), allow_nan=False)))


def load_artifact(path: Path = MODEL_PATH) -> dict[str, object] | None:
    try:
        return v2_model.load_model(path)
    except ValueError:
        return None


def _read_training_report(path: Path = TRAINING_REPORT_PATH) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, allow_nan=False),
        encoding='utf-8')
    temporary.replace(path)


def acquire_model_update_lock(owner: str) -> str | None:
    """Acquire the cross-process model/report writer fence."""
    MODEL_UPDATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    if MODEL_UPDATE_LOCK.is_file():
        try:
            age = max(0., now - MODEL_UPDATE_LOCK.stat().st_mtime)
        except OSError:
            return None
        if age <= AUTO_RETRAIN_LOCK_MAX_AGE_SECONDS:
            return None
        MODEL_UPDATE_LOCK.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    payload = {'token': token, 'owner': str(owner), 'pid': os.getpid(), 'ts': now}
    try:
        with MODEL_UPDATE_LOCK.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, indent=2, allow_nan=False)
    except FileExistsError:
        return None
    return token


def release_model_update_lock(token: str) -> None:
    """Release only the exact fence acquired by this process."""
    try:
        payload = json.loads(MODEL_UPDATE_LOCK.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(payload, dict) and payload.get('token') == token:
        MODEL_UPDATE_LOCK.unlink(missing_ok=True)


def forward_training_samples(db: sqlite3.Connection) -> list[dict[str, object]]:
    """Return complete, current-contract forward labels for the next offline fit."""
    v2_store.ensure_tables(db)
    records = db.execute(
        'SELECT s.event_id,s.detail,p.feature_version,p.resolution '
        'FROM v2_samples s JOIN v2_predictions p ON p.event_id=s.event_id '
        'WHERE s.stream_id=? AND s.spec_id=? AND s.policy=? AND s.cost_version=? '
        'AND s.source=? AND p.resolved=1 AND p.resolution=? '
        'ORDER BY s.fill_ts,s.event_id',
        (v2_store.STREAM_ID, MAIN_SPEC.spec_id, POLICY, DEFAULT_COST.version,
         v2_store.SOURCE, 'labeled')).fetchall()
    samples = []
    rejected = []
    for event_id, encoded, feature_version, _resolution in records:
        try:
            sample = json.loads(encoded)
        except (TypeError, json.JSONDecodeError):
            rejected.append({'event_id': event_id, 'reason': 'invalid_json'})
            continue
        metadata = sample.get('metadata') if isinstance(sample, dict) else None
        vector = sample.get('x') if isinstance(sample, dict) else None
        prediction = metadata.get('prediction') if isinstance(metadata, dict) else None
        valid_vector = (
            isinstance(vector, list) and len(vector) == len(v2_model.FEATURE_NAMES)
            and all(not isinstance(value, bool) and isinstance(value, (int, float))
                    and math.isfinite(value) for value in vector))
        if (not isinstance(sample, dict) or sample.get('id') != event_id
                or feature_version != v2_model.FEATURE_VERSION or not valid_vector
                or not isinstance(metadata, dict)
                or metadata.get('spec_id') != MAIN_SPEC.spec_id
                or metadata.get('policy') != POLICY
                or metadata.get('cost_version') != DEFAULT_COST.version
                or metadata.get('source') != v2_store.SOURCE
                or not isinstance(prediction, dict)
                or prediction.get('feature_version') != v2_model.FEATURE_VERSION):
            rejected.append({'event_id': event_id, 'reason': 'incompatible_contract'})
            continue
        clean = dict(sample)
        clean['source'] = v2_store.SOURCE
        try:
            # Reuse the trainer's canonical contract so one incomplete row is
            # quarantined here instead of aborting every later batch fit.
            v2_model._canonical_forward_sample(clean, len(samples))
        except ValueError as exc:
            rejected.append({
                'event_id': event_id,
                'reason': 'trainer_contract_rejected',
                'detail': str(exc),
            })
            continue
        samples.append(clean)
    rejection_state = {
        'count': len(rejected), 'rejected': rejected,
        'valid_count': len(samples), 'checked_ts': time.time(),
    }
    previous_rejections = _get(db, 'v2_forward_training_rejections')
    with db:
        _put(db, 'v2_forward_training_rejections', rejection_state)
        if rejected and (
                not isinstance(previous_rejections, dict)
                or previous_rejections.get('rejected') != rejected):
            _event(db, time.time(), 'system', 'v2_forward_samples_quarantined',
                   rejection_state)
    return samples


def prequential_evidence(db: sqlite3.Connection, model_version: str) -> list[dict[str, object]]:
    """Build immutable promotion evidence for exactly one deployed model version."""
    v2_store.ensure_tables(db)
    rows = db.execute(
        'SELECT p.event_id,p.decision_ts,p.created_ts,s.label_available_ts,'
        'p.model_version,p.score,p.accepted,s.net_return,'
        'e.net_return,e.exit_ts_ms,e.entry_mode,e.exit_reason '
        'FROM v2_predictions p JOIN v2_samples s ON s.event_id=p.event_id '
        'LEFT JOIN v2_executions e ON e.event_id=p.event_id '
        'WHERE p.stream_id=? AND p.spec_id=? AND p.policy=? AND p.cost_version=? '
        'AND s.source=? AND p.resolution=? AND p.model_version=? '
        'ORDER BY p.decision_ts,p.event_id',
        (v2_store.STREAM_ID, MAIN_SPEC.spec_id, POLICY, DEFAULT_COST.version,
         v2_store.SOURCE, 'labeled', str(model_version))).fetchall()
    return [{
        'event_id': event_id,
        'decision_ts': decision_ts,
        'prediction_ts': prediction_ts,
        'label_available_ts': label_available_ts,
        'model_version': frozen_version,
        'probability': score,
        'accepted': bool(accepted),
        'net_return': net_return,
        'source': 'paper_v2_prequential',
        'executed': (execution_return is not None
                     and execution_exit_reason != 'policy_migration_censored'),
        'execution_net_return': execution_return,
        'execution_label_available_ts': execution_exit_ts,
        'execution_mode': execution_mode,
        'execution_exit_reason': execution_exit_reason,
    } for (event_id, decision_ts, prediction_ts, label_available_ts,
           frozen_version, score, accepted, net_return, execution_return,
           execution_exit_ts, execution_mode, execution_exit_reason) in rows]


def refresh_promotion(
        db: sqlite3.Connection,
        artifact: Mapping[str, object] | None = None,
        report_path: Path = TRAINING_REPORT_PATH,
        model_path: Path = MODEL_PATH,
        update_lock_token: str | None = None) -> dict[str, object] | None:
    """Re-evaluate promotion after labels arrive without changing model weights."""
    current = dict(artifact) if artifact is not None else load_artifact(model_path)
    owned_token = None
    if update_lock_token is None:
        owned_token = acquire_model_update_lock('promotion_refresh')
        if owned_token is None:
            return current
        update_lock_token = owned_token
    try:
        # Re-read only after acquiring the fence.  A worker that loaded an older
        # artifact before a background fit may never overwrite the newer file.
        on_disk = load_artifact(model_path)
        if on_disk is not None:
            current = on_disk
        report = _read_training_report(report_path)
        if current is None or report is None:
            return current
        if report.get('model_version') != current.get('model_version'):
            return current
        walk_forward = report.get('walk_forward')
        if not isinstance(walk_forward, dict):
            return current
        evidence = prequential_evidence(db, str(current['model_version']))
        promotion = v2_model.evaluate_promotion(current, walk_forward, evidence)
        current['promotion'] = promotion
        current['eligible'] = bool(promotion['eligible'])
        current['status'] = 'paper_eligible' if current['eligible'] else 'shadow'
        v2_model.save_model(model_path, current)
        updated_report = dict(report)
        updated_report.update(
            promotion=promotion,
            eligible=current['eligible'],
            status=current['status'],
            promotion_refreshed_ts=int(time.time() * 1000))
        _write_json_atomic(report_path, updated_report)
        with db:
            _put(db, 'v2_last_promotion_refresh', {
                'ts': time.time(),
                'model_version': current['model_version'],
                'valid_forward': promotion['prequential']['valid_count'],
                'accepted_forward': promotion['prequential']['accepted_count'],
                'eligible': current['eligible'],
                'failed_checks': promotion['failed_checks'],
            })
        return current
    finally:
        if owned_token is not None:
            release_model_update_lock(owned_token)


def _auto_retrain_plan(
        artifact: Mapping[str, object] | None,
        current_forward: int,
        trained_forward: int,
        version_evidence: int) -> dict[str, object]:
    new_forward = max(0, int(current_forward) - int(trained_forward))
    if artifact is None:
        return {'due': False, 'reason': 'model_unavailable', 'new_forward': new_forward}
    if bool(artifact.get('eligible')):
        return {'due': False, 'reason': 'model_paper_eligible', 'new_forward': new_forward}
    selection = artifact.get('selection')
    cash_selected = not isinstance(selection, Mapping) or bool(selection.get('cash_selected', True))
    if cash_selected:
        due = new_forward >= AUTO_RETRAIN_CASH_BATCH
        reason = 'cash_model_new_forward_batch' if due else 'collecting_cash_model_labels'
    else:
        # Freeze a non-cash shadow model long enough to obtain the 50 observations
        # required by the promotion contract.  If it still fails after 100, its
        # mistakes become the next fit instead of weakening the gate.
        due = (version_evidence >= AUTO_RETRAIN_FAILED_EVIDENCE and new_forward > 0)
        reason = 'failed_shadow_evidence_retrain' if due else 'collecting_version_frozen_evidence'
    return {
        'due': due,
        'reason': reason,
        'new_forward': new_forward,
        'current_forward': int(current_forward),
        'trained_forward': int(trained_forward),
        'version_evidence': int(version_evidence),
        'cash_selected': cash_selected,
    }


def auto_retrain_status(
        db: sqlite3.Connection,
        artifact: Mapping[str, object] | None = None) -> dict[str, object]:
    current = artifact if artifact is not None else load_artifact()
    report = _read_training_report() or {}
    label_contract = report.get('label_contract')
    trained_forward = (int(label_contract.get('forward_training_events', 0))
                       if isinstance(label_contract, dict) else 0)
    store_status = v2_store.status(db)
    current_forward = len(forward_training_samples(db))
    version_evidence = (len(prequential_evidence(db, str(current['model_version'])))
                        if current is not None else 0)
    plan = _auto_retrain_plan(
        current, current_forward, trained_forward, version_evidence)
    # A challenger and its control must see the same untouched future stream.
    # Hold the control artifact fixed until the precommitted challenger evidence
    # quota is complete; otherwise a 25-label refit would mix control versions.
    if current is not None and not bool(current.get('eligible')):
        for challenger_version in v2_challengers.registered_model_versions(db):
            try:
                challenger = v2_challengers.load_model(db, challenger_version)
                if (challenger.get('control_model_version')
                        != current.get('model_version')):
                    continue
                comparison = v2_challengers.evaluate_model(
                    db, challenger_version)
            except ValueError:
                # The read-only challenger status reports the invalid artifact;
                # it may never influence control retraining or capital.
                continue
            if comparison.get('status') == 'collecting':
                plan.update(
                    due=False,
                    reason='challenger_control_frozen_for_fair_forward_test',
                    challenger_model_version=challenger_version,
                    challenger_matched_future_events=int(
                        comparison.get('matched_future_events', 0)),
                    challenger_required_future_events=(
                        v2_challengers.MIN_MATCHED_FUTURE_EVENTS),
                    challenger_would_accept=int(comparison.get('would_accept', 0)),
                    challenger_required_accepts=v2_challengers.MIN_WOULD_ACCEPT,
                )
                break
    plan['lock_active'] = AUTO_RETRAIN_LOCK.is_file()
    plan['raw_forward_labels'] = int(store_status['true_forward'])
    plan['quarantined_forward_labels'] = max(
        0, int(store_status['true_forward']) - current_forward)
    plan['batch_for_cash_model'] = AUTO_RETRAIN_CASH_BATCH
    plan['failed_shadow_evidence_threshold'] = AUTO_RETRAIN_FAILED_EVIDENCE
    return plan


def maybe_start_auto_retrain(
        db: sqlite3.Connection,
        artifact: Mapping[str, object] | None,
        now: float) -> dict[str, object]:
    """Launch a rare offline refit without blocking quote protection checks."""
    plan = auto_retrain_status(db, artifact)
    if not plan['due']:
        return plan
    if AUTO_RETRAIN_LOCK.is_file():
        age = max(0., now - AUTO_RETRAIN_LOCK.stat().st_mtime)
        if age <= AUTO_RETRAIN_LOCK_MAX_AGE_SECONDS:
            return {**plan, 'started': False, 'reason': 'retrain_already_running'}
        AUTO_RETRAIN_LOCK.unlink(missing_ok=True)
    AUTO_RETRAIN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    marker = {**plan, 'requested_ts': now, 'model_version': artifact['model_version']}
    try:
        with AUTO_RETRAIN_LOCK.open('x', encoding='utf-8') as stream:
            json.dump(marker, stream, indent=2, allow_nan=False)
    except FileExistsError:
        return {**plan, 'started': False, 'reason': 'retrain_already_running'}
    command = [
        sys.executable, str((ROOT / 'agent.py').resolve()),
        'train-bollinger-v2', '--data', str(TRAINING_DATA_PATH.resolve()),
        '--model', str(MODEL_PATH.resolve()), '--report', str(TRAINING_REPORT_PATH.resolve()),
        '--auto-lock', str(AUTO_RETRAIN_LOCK.resolve()),
    ]
    try:
        with AUTO_RETRAIN_LOG.open('ab') as stream:
            process = subprocess.Popen(
                command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=stream,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')
    except OSError:
        AUTO_RETRAIN_LOCK.unlink(missing_ok=True)
        raise
    launched = {**marker, 'started': True, 'pid': process.pid}
    _write_json_atomic(AUTO_RETRAIN_LOCK, launched)
    with db:
        _put(db, 'v2_last_auto_retrain', launched)
        _event(db, now, 'system', 'v2_auto_retrain_started', launched)
    return launched


def _auto_retrain_check_due(db: sqlite3.Connection, now: float) -> bool:
    """Rate-limit background-fit checks while still retrying failed child fits."""
    last_check = _get(db, AUTO_RETRAIN_CHECK_STATE_KEY)
    if isinstance(last_check, bool) or not isinstance(last_check, (int, float)):
        return True
    if not math.isfinite(float(last_check)):
        return True
    return now - float(last_check) >= AUTO_RETRAIN_CHECK_INTERVAL_SECONDS


def model_callback(
        artifact: Mapping[str, object] | None,
        quote_snapshot: Mapping[str, float] | None = None):
    """Return a causal scorer whose full feature vector is frozen by v2_store."""
    if artifact is None:
        return None

    def score(*, strategy, rows, features, decision_index):
        # v2_model independently slices at decision_index, so callers cannot
        # accidentally expose a later bar through the shared indicator object.
        vector = v2_model.causal_feature_vector(
            rows, decision_index, strategy, MAIN_SPEC)
        review = v2_model.assess(artifact, vector)
        decision_close_ts = float(rows[decision_index]['ts']) / 1000 + BAR_SECONDS
        atr = features['atr'][decision_index]
        execution_gate = bool(
            quote_snapshot is not None
            and float(quote_snapshot['timestamp']) >= decision_close_ts
            and _spread(quote_snapshot) <= MAX_ENTRY_SPREAD
            and atr is not None
            and size_position(1., float(quote_snapshot['ask']), float(atr),
                              probe=False) is not None)
        return {
            'model_version': artifact['model_version'],
            'score': review['probability'],
            'threshold': review['threshold'],
            # This is the frozen shadow decision.  Real paper capital below also
            # requires artifact.eligible, which historical data cannot grant.
            'accepted': bool(review['would_accept'] and execution_gate),
            'feature_version': v2_model.FEATURE_VERSION,
            'features': vector,
        }
    return score


def _spread(q: Mapping[str, float]) -> float:
    return (float(q['ask']) - float(q['bid'])) / float(q['bid'])


def size_position(
        cash: float, ask: float, atr: float, *, probe: bool,
        risk_fraction_override: float | None = None,
        allocation_cap_override: float | None = None) -> dict[str, float] | None:
    """Size one v2 event and require useful room after all configured costs."""
    values = (cash, ask, atr)
    if not all(math.isfinite(value) and value > 0 for value in values):
        return None
    fee = DEFAULT_COST.fee_each_side
    slip = DEFAULT_COST.slippage_each_side
    entry_reference = ask
    entry = entry_reference * (1 + slip)
    stop = entry_reference - MAIN_SPEC.stop_atr * atr
    target = entry_reference + MAIN_SPEC.target_atr * atr
    if stop <= 0:
        return None
    unit_cost = entry * (1 + fee)
    stop_cash = stop * (1 - slip) * (1 - fee)
    target_cash = target * (1 - slip) * (1 - fee)
    planned_loss = unit_cost - stop_cash
    target_net_return = target_cash / unit_cost - 1
    if planned_loss <= 0 or target_net_return < MIN_TARGET_NET_RETURN:
        return None
    risk_fraction = (PROBE_RISK_FRACTION if probe else NORMAL_RISK_FRACTION)
    allocation_cap = (PROBE_ALLOCATION_CAP if probe else NORMAL_ALLOCATION_CAP)
    if risk_fraction_override is not None:
        risk_fraction = float(risk_fraction_override)
    if allocation_cap_override is not None:
        allocation_cap = float(allocation_cap_override)
    if (not math.isfinite(risk_fraction) or not 0 < risk_fraction <= 1
            or not math.isfinite(allocation_cap) or not 0 < allocation_cap <= 1):
        return None
    quantity = min(cash * risk_fraction / planned_loss,
                   cash * allocation_cap / unit_cost)
    if not math.isfinite(quantity) or quantity <= 0:
        return None
    return {
        'quantity': quantity,
        'entry_reference': entry_reference,
        'entry': entry,
        'stop': stop,
        'target': target,
        'cost': quantity * unit_cost,
        'planned_loss_usd': quantity * planned_loss,
        'target_net_return': target_net_return,
        'risk_fraction': risk_fraction,
        'allocation_cap': allocation_cap,
        'fee': fee,
        'slippage': slip,
    }


def challenger_after_insert_callback(
        db: sqlite3.Connection,
        artifact: Mapping[str, object] | None,
        quote_snapshot: Mapping[str, float] | None):
    """Pair frozen challenger scores with a newly inserted control prediction.

    The returned hook is invoked by ``v2_store.record_decisions`` inside the
    control prediction transaction.  Scores and isolated micro-paper entries are
    therefore atomic.  They never touch the main $1,000 portfolio or production
    ``v2_executions``; an invalid matching artifact rolls the observation back.
    """
    if artifact is None or quote_snapshot is None:
        return None
    control_version = artifact.get('model_version')
    if not isinstance(control_version, str) or not control_version:
        return None
    matching = []
    for version in v2_challengers.registered_model_versions(db):
        challenger = v2_challengers.load_model(db, version)
        if challenger.get('control_model_version') == control_version:
            matching.append(version)
    if not matching:
        return None

    def after_insert(*, db, event_id, strategy, decision_ts, created_ts, frozen):
        if frozen.get('model_version') != control_version:
            raise ValueError('Challenger control model does not match the frozen prediction.')
        row = db.execute(
            'SELECT atr FROM v2_predictions WHERE event_id=?',
            (event_id,)).fetchone()
        if row is None:
            raise ValueError('Challenger hook cannot find the frozen prediction.')
        atr = float(row[0])
        decision_close_ts = float(decision_ts) / 1000 + BAR_SECONDS
        execution_gate = bool(
            float(quote_snapshot['timestamp']) >= decision_close_ts
            and _spread(quote_snapshot) <= MAX_ENTRY_SPREAD
            and size_position(1., float(quote_snapshot['ask']), atr,
                              probe=False) is not None)
        for version in matching:
            v2_challengers.record_event_score(
                db, event_id, version,
                execution_gate=execution_gate,
                created_ts=int(created_ts),
                manage_transaction=False)
            v2_challengers.ensure_paper_portfolio(
                db, version, int(created_ts), manage_transaction=False)
            portfolio = v2_challengers.paper_portfolio_snapshot(db, version)
            if portfolio is None:
                raise ValueError('Challenger paper portfolio initialization failed.')
            plan = size_position(
                float(portfolio['cash_usd']), float(quote_snapshot['ask']), atr,
                probe=False,
                risk_fraction_override=v2_challengers.PAPER_RISK_FRACTION,
                allocation_cap_override=v2_challengers.PAPER_ALLOCATION_CAP)
            if plan is not None:
                v2_challengers.open_paper_position(
                    db, event_id, version,
                    strategy=str(strategy), plan=plan,
                    bid=float(quote_snapshot['bid']),
                    quote_ts_ms=int(round(float(quote_snapshot['timestamp']) * 1000)),
                    decision_close_ts_ms=int(decision_ts) + BAR_MS,
                    now_ms=int(created_ts), manage_transaction=False)

    return after_insert


def _liquidation_value(position: Mapping[str, float], bid: float) -> float:
    slip = float(position.get('slippage', DEFAULT_COST.slippage_each_side))
    fee = float(position.get('fee', DEFAULT_COST.fee_each_side))
    return float(position['quantity']) * bid * (1 - slip) * (1 - fee)


def _close(db, state, name, position, q, now, reason):
    slip = float(position.get('slippage', DEFAULT_COST.slippage_each_side))
    fee = float(position.get('fee', DEFAULT_COST.fee_each_side))
    price = float(q['bid']) * (1 - slip)
    proceeds = float(position['quantity']) * price * (1 - fee)
    pnl = proceeds - float(position['cost'])
    state['cash_usd'] += proceeds
    state['realized_pnl_usd'] = state.get('realized_pnl_usd', 0.) + pnl
    state['completed_trades'] = state.get('completed_trades', 0) + 1
    state['position'] = None
    execution_recorded = False
    prediction_event_id = position.get('prediction_event_id')
    model_version = position.get('model_version')
    if prediction_event_id and model_version and float(position['cost']) > 0:
        try:
            execution_recorded = v2_store.record_execution(
                db,
                str(prediction_event_id),
                str(model_version),
                int(position.get('entry_ts_ms', round(float(position['ts']) * 1000))),
                int(round(now * 1000)),
                str(position.get('entry_mode', 'unknown')),
                pnl / float(position['cost']),
                pnl,
                float(position['cost']),
                str(reason),
                manage_transaction=False,
            )
        except ValueError as exc:
            _event(db, now, name, 'v2_execution_record_failed', {
                'prediction_event_id': prediction_event_id,
                'reason': str(exc),
            })
    _event(db, now, name, 'sell', {
        'price': price, 'quantity': position['quantity'], 'pnl_usd': pnl,
        'reason': reason, 'entry_ts': position['ts'],
        'entry_mode': position.get('entry_mode'),
        'candidate_strategy': position.get('candidate_strategy'),
        'execution_sample_recorded': execution_recorded,
        'learning_sample_recorded': False,
        'learning_note': 'Outcome labels come from closed-candle v2 shadow replay.'})
    return execution_recorded


def _initial_state(initial: float, candle: int | None, day: str) -> dict[str, object]:
    return {
        'initial_usd': initial, 'cash_usd': initial, 'position': None,
        'last_candle': candle, 'day': day, 'day_start_equity': initial,
        'daily_halt': False, 'drawdown_halt': False,
        'realized_pnl_usd': 0., 'completed_trades': 0,
        'equity_usd': initial, 'peak_equity_usd': initial,
        'drawdown_pct': 0., 'policy_id': POLICY,
        'bar_seconds': BAR_SECONDS,
    }


def _is_pristine_v2_session(db: sqlite3.Connection) -> bool:
    """Recognize only a never-run v2 database as eligible for initial funding."""
    v2_store.ensure_tables(db)
    paper_v3.ensure_tables(db)
    if any(_get(db, name) is not None for name in PORTFOLIOS):
        return False
    allowed_keys = ('policy_epoch', 'stop', 'exploration_enabled')
    placeholders = ','.join('?' for _ in allowed_keys)
    if db.execute(
            f'SELECT 1 FROM state WHERE key NOT IN ({placeholders}) LIMIT 1',
            allowed_keys).fetchone() is not None:
        return False
    if db.execute('SELECT 1 FROM events LIMIT 1').fetchone() is not None:
        return False
    for table in ('v2_predictions', 'v2_samples', 'v2_shadow_state',
                  'v2_executions', 'v2_challenger_models',
                  'v2_challenger_scores', 'v2_challenger_portfolios',
                  'v2_challenger_executions', 'v3_paper_state',
                  'v3_paper_decisions', 'v3_paper_executions'):
        if db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is not None:
            return False
    return True


def tick(db: sqlite3.Connection, q: Mapping[str, float],
         rows: Sequence[Mapping[str, object]] | None, now: float,
         budgets: Mapping[str, float]) -> bool:
    """Run one v2 paper update; base rules never consume paper capital."""
    from paper import check_quote
    check_quote(q, now)
    if _get(db, 'stop', False):
        return False
    if _get(db, 'policy_epoch') != POLICY:
        raise ValueError('Portföy durumu Bollinger 15 dakika v2 politikasıyla uyuşmuyor.')
    pristine_initialization = _is_pristine_v2_session(db)
    v2_challengers.paper_tick(db, q, int(round(now * 1000)))
    paper_v3.tick(db, q, rows, int(round(now * 1000)))

    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    candle = int(rows[-1]['ts']) if rows else None
    fresh = bool(rows and 0 <= now - (candle / 1000 + BAR_SECONDS) <= FRESH_WINDOW_SECONDS)
    artifact = load_artifact()
    resolved = []
    decision_ids = []
    if rows:
        resolved = v2_store.replay_labels(db, rows, int(now * 1000))
        previous = _get(db, 'v2_last_candle')
        if previous is None:
            with db:
                _put(db, 'v2_last_candle', candle)
        elif candle > previous:
            if fresh and float(rows[-1]['volume']) > 0 and artifact is not None:
                challenger_hook = challenger_after_insert_callback(db, artifact, q)
                decision_kwargs = ({'after_insert': challenger_hook}
                                   if challenger_hook is not None else {})
                decision_ids = v2_store.record_decisions(
                    db, rows, model_callback(artifact, q), int(now * 1000),
                    **decision_kwargs)
            with db:
                _put(db, 'v2_last_candle', candle)
    if resolved:
        artifact = refresh_promotion(db, artifact)
        with db:
            _event(db, now, 'system', 'v2_shadow_labels_resolved', {
                'count': len(resolved), 'event_ids': resolved,
                'model_version': artifact.get('model_version') if artifact else None})

    decisions = []
    if decision_ids:
        marks = ','.join('?' for _ in decision_ids)
        decisions = db.execute(
            'SELECT event_id,strategy,atr,score,threshold,accepted,model_version '
            f'FROM v2_predictions WHERE event_id IN ({marks}) ORDER BY strategy',
            decision_ids).fetchall()

    execution_recorded = False
    with db:
        portfolio_states = {name: _get(db, name) for name in PORTFOLIOS}
        initialization_marker = _get(db, PORTFOLIOS_INITIALIZED_KEY, False)
        # Only a genuinely pristine v2 session receives the configured $1,000.
        # Once any book or session evidence exists, a missing row is recovery
        # and must come back with zero capital.
        # First mark-to-market and close positions.  This path remains active even
        # when candle retrieval failed, provided the quote is fresh.
        for name in PORTFOLIOS:
            state = portfolio_states[name]
            if state is None:
                initial = float(budgets[name]) if pristine_initialization else 0.
                state = _initial_state(initial, candle, day)
                _event(db, now, name, 'initialized', {
                    'initial_usd': initial, 'anchor_candle': candle,
                    'policy_id': POLICY,
                    'recovered_missing_row': not pristine_initialization})
            position = state.get('position')
            equity = state['cash_usd'] + (
                _liquidation_value(position, float(q['bid'])) if position else 0.)
            initial = float(state.get('initial_usd', budgets[name]))
            state['peak_equity_usd'] = max(
                float(state.get('peak_equity_usd', max(initial, equity))), equity)
            state['drawdown_pct'] = (
                (1 - equity / state['peak_equity_usd']) * 100
                if state['peak_equity_usd'] > 0 else 0.)
            state['drawdown_halt'] = bool(state.get('drawdown_halt', False)
                                           or state['drawdown_pct'] >= 8)
            if state.get('day') != day:
                state.update(day=day, day_start_equity=equity, daily_halt=False)
            if equity <= float(state['day_start_equity']) * .98:
                state['daily_halt'] = True
            reason = None
            if position:
                if state['drawdown_halt']:
                    reason = 'total_drawdown_halt'
                elif state['daily_halt']:
                    reason = 'daily_loss_halt'
                elif float(q['bid']) <= float(position['stop']):
                    reason = 'stop'
                elif float(q['bid']) >= float(position['target']):
                    reason = 'target'
                elif now >= float(position['expires_ts']):
                    reason = 'v2_h8_timeout'
            if reason:
                execution_recorded = (
                    _close(db, state, name, position, q, now, reason)
                    or execution_recorded)
            position = state.get('position')
            state['equity_usd'] = state['cash_usd'] + (
                _liquidation_value(position, float(q['bid'])) if position else 0.)
            state['peak_equity_usd'] = max(state['peak_equity_usd'], state['equity_usd'])
            state['drawdown_pct'] = (
                (1 - state['equity_usd'] / state['peak_equity_usd']) * 100
                if state['peak_equity_usd'] > 0 else 0.)
            state['policy_id'] = POLICY
            state['bar_seconds'] = BAR_SECONDS
            state['last_candle'] = candle
            state['capital_mode'] = 'shadow_no_capital' if name in BASE_PORTFOLIOS else 'model_gated'
            _put(db, name, state)

        if not initialization_marker:
            _put(db, PORTFOLIOS_INITIALIZED_KEY, {
                'policy_id': POLICY,
                'initialized_ts': now,
            })

        if decision_ids:
            _event(db, now, 'system', 'v2_shadow_decisions', {
                'event_ids': decision_ids,
                'capital_for_base_rules': False,
                'model_status': artifact.get('status') if artifact else 'unavailable'})

        active_probe = any(
            ((_get(db, name) or {}).get('position') or {}).get('entry_mode') == 'v2_micro_probe'
            for name in PORTFOLIOS)
        artifact_model_version = artifact.get('model_version') if artifact else None
        normal_decision_exists = bool(
            artifact and artifact.get('eligible')
            and artifact_model_version is not None
            and any(bool(row[5]) and row[6] == artifact_model_version
                    for row in decisions))
        # In shadow, use the single micro slot on a model-accepted observation
        # before probing a rejected one so promotion can collect actual P&L.
        decisions.sort(key=lambda row: not bool(row[5]))
        opened = False
        for event_id, strategy, atr, score, threshold, accepted, model_version in decisions:
            account = ACCOUNT_FOR_STRATEGY.get(strategy)
            if account is None:
                continue
            state = _get(db, account)
            prediction_matches_artifact = bool(
                artifact_model_version is not None
                and model_version == artifact_model_version)
            normal = bool(
                artifact and artifact.get('eligible') and accepted
                and prediction_matches_artifact)
            probe = bool(_get(db, 'exploration_enabled', False)
                         and not active_probe and not normal_decision_exists
                         and prediction_matches_artifact)
            if state.get('position') or state.get('daily_halt') or state.get('drawdown_halt'):
                continue
            if not normal and not probe:
                continue
            decision_close_ts = candle / 1000 + BAR_SECONDS
            if float(q['timestamp']) < decision_close_ts:
                _event(db, now, account, 'entry_blocked', {
                    'reason': 'quote_precedes_decision_close',
                    'quote_timestamp': q['timestamp'],
                    'decision_close_timestamp': decision_close_ts,
                    'event_id': event_id})
                continue
            if _spread(q) > MAX_ENTRY_SPREAD:
                _event(db, now, account, 'entry_blocked', {
                    'reason': 'spread_above_v2_limit', 'spread': _spread(q),
                    'limit': MAX_ENTRY_SPREAD, 'event_id': event_id})
                continue
            position = size_position(float(state['cash_usd']), float(q['ask']),
                                     float(atr), probe=not normal)
            if position is None:
                _event(db, now, account, 'entry_blocked', {
                    'reason': 'insufficient_cost_adjusted_target_room',
                    'minimum_target_net_return': MIN_TARGET_NET_RETURN,
                    'event_id': event_id})
                continue
            position.update({
                'ts': now, 'entry_ts_ms': int(round(now * 1000)),
                'expires_ts': now + POSITION_HOLD_SECONDS,
                'entry_mode': 'model' if normal else 'v2_micro_probe',
                'candidate_strategy': strategy, 'prediction_event_id': event_id,
                'model_version': model_version, 'model_score': score,
                'model_threshold': threshold, 'policy_id': POLICY,
                'bar_seconds': BAR_SECONDS,
                'quote_timestamp': q['timestamp'],
                'decision_close_timestamp': decision_close_ts,
                'execution_policy_version': EXECUTION_POLICY_VERSION,
            })
            state['cash_usd'] -= position['cost']
            state['position'] = position
            state['equity_usd'] = (
                state['cash_usd'] + _liquidation_value(position, float(q['bid'])))
            state['peak_equity_usd'] = max(
                float(state.get('peak_equity_usd', state['equity_usd'])),
                state['equity_usd'])
            state['drawdown_pct'] = (
                (1 - state['equity_usd'] / state['peak_equity_usd']) * 100
                if state['peak_equity_usd'] > 0 else 0.)
            if state['equity_usd'] <= float(state['day_start_equity']) * .98:
                state['daily_halt'] = True
            state['drawdown_halt'] = bool(
                state.get('drawdown_halt', False) or state['drawdown_pct'] >= 8)
            _put(db, account, state)
            _event(db, now, account, 'buy', position)
            opened = True
            if not normal:
                active_probe = True
                break

        # Base books are retained for audit/accounting but never spend cash in v2.
        if decision_ids and not opened:
            _event(db, now, 'system', 'v2_capital_kept_cash', {
                'reason': 'model_not_promoted_or_cost_gate',
                'event_ids': decision_ids})
        _put(db, 'last_quote', dict(q))
        _put(db, 'last_success', now)
    if execution_recorded:
        artifact = refresh_promotion(db, artifact)
    if resolved or execution_recorded or _auto_retrain_check_due(db, now):
        with db:
            _put(db, AUTO_RETRAIN_CHECK_STATE_KEY, now)
        maybe_start_auto_retrain(db, artifact, now)
    return True


def status(db: sqlite3.Connection) -> dict[str, object]:
    artifact = load_artifact()
    model_summary = None
    if artifact:
        model_summary = {
            'status': artifact['status'], 'eligible': artifact['eligible'],
            'model_version': artifact['model_version'],
            'training_events': artifact['fitted']['training_events'],
            'selection': artifact['selection'],
            'promotion': artifact.get('promotion'),
        }
    return {
        'policy_id': POLICY,
        'bar': BAR_LABEL,
        'bar_seconds': BAR_SECONDS,
        'base_portfolios': 'shadow_no_capital',
        'normal_capital': 'eligible model decisions only',
        'execution_policy': {
            'version': EXECUTION_POLICY_VERSION,
            'maximum_entry_spread': MAX_ENTRY_SPREAD,
            'quote_must_follow_decision_close': True,
            'promotion_return_source': 'frozen_actual_paper_execution',
        },
        'micro_probe': {
            'enabled': _get(db, 'exploration_enabled', False),
            'one_at_a_time': True,
            'risk_fraction': PROBE_RISK_FRACTION,
            'allocation_cap': PROBE_ALLOCATION_CAP,
            'horizon_bars': MAIN_SPEC.horizon_bars,
            'horizon_seconds': POSITION_HOLD_SECONDS,
            'minimum_target_net_return': MIN_TARGET_NET_RETURN,
        },
        'shadow_learning': v2_store.status(db),
        'challengers': v2_challengers.status(db),
        'v3_test': paper_v3.status(db),
        'automatic_retraining': auto_retrain_status(db, artifact),
        'model': model_summary,
    }


def migrate(path=None) -> dict[str, object]:
    """Atomically carry the existing six books into bollinger_15m_v2."""
    import paper
    target = paper.DB if path is None else path
    control_lock = paper.acquire_control_lock()
    if control_lock is None:
        raise ValueError('Başlatma veya başka bir politika geçişi devam ediyor.')
    migration_lock = paper.acquire_lock()
    if migration_lock is None:
        control_lock.close()
        raise ValueError('v2 geçişi için önce paper-stop ile workerı durdurun.')
    db = None
    try:
        db = paper.connect(target)
        if _get(db, 'policy_epoch') == POLICY:
            result = _get(db, 'policy_cutover_v2')
            print('Bollinger 15 dakika v2 geçişi daha önce tamamlanmış.')
            return result
        previous_policy = _get(db, 'policy_epoch')
        if previous_policy not in (None, 'bollinger_15m_v1'):
            raise ValueError('Önce mevcut eski politika geçişini tamamlayın.')
        existing = {name: _get(db, name) for name in PORTFOLIOS}
        q = paper.quote()
        now = time.time()
        paper.check_quote(q, now)
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if all(state is None for state in existing.values()):
            existing = {name: _initial_state(float(paper.DEFAULT_BUDGETS[name]), None, day)
                        for name in PORTFOLIOS}
        else:
            # A damaged/partial legacy DB must not mint the missing allocations.
            # Keep absent books as zero-capital audit placeholders.
            recovered = {}
            for name, state in existing.items():
                if state is None:
                    recovered[name] = _initial_state(0., None, day)
                    continue
                if (not isinstance(state, Mapping)
                        or any(key not in state for key in
                               ('initial_usd', 'cash_usd', 'position'))):
                    raise ValueError(
                        f'{name} portföy durumu bozuk; v2 geçişi güvenli biçimde tamamlanamadı.')
                recovered[name] = dict(state)
            existing = recovered
        with db:
            v2_store.ensure_tables(db)
            _put(db, 'archive_bollinger_v1_before_v2', {
                'archived_at': now, 'policy_id': previous_policy,
                'portfolios': existing})
            for name, state in existing.items():
                position = state.get('position')
                if position:
                    _close(db, state, name, position, q, now,
                           'policy_migration_censored')
                state['last_candle'] = None
                state['policy_id'] = POLICY
                state['bar_seconds'] = BAR_SECONDS
                state['equity_usd'] = state['cash_usd']
                state['peak_equity_usd'] = max(
                    state.get('peak_equity_usd', state['equity_usd']),
                    state['equity_usd'])
                state['drawdown_pct'] = (
                    (1 - state['equity_usd'] / state['peak_equity_usd']) * 100
                    if state['peak_equity_usd'] > 0 else 0.)
                state['policy_cutover_v2_equity_usd'] = state['equity_usd']
                state['capital_mode'] = ('shadow_no_capital' if name in BASE_PORTFOLIOS
                                         else 'model_gated')
                _put(db, name, state)
            equity = sum(float((_get(db, name) or {}).get('equity_usd', 0.))
                         for name in PORTFOLIOS)
            cutover = {
                'ts': now, 'from_policy': previous_policy, 'to_policy': POLICY,
                'equity_usd': equity,
                'original_initial_usd': sum(
                    float(state.get('initial_usd', 0.)) for state in existing.values()),
                'quote': q, 'base_portfolios_capital_enabled': False,
                'model_path': str(MODEL_PATH.resolve()),
            }
            _put(db, 'policy_cutover_v2', cutover)
            _put(db, 'policy_epoch', POLICY)
            _put(db, PORTFOLIOS_INITIALIZED_KEY, {
                'policy_id': POLICY,
                'initialized_ts': now,
            })
            _put(db, 'v2_last_candle', None)
            _put(db, 'last_quote', q)
            _put(db, 'error', None)
            _event(db, now, 'system', 'policy_migration_v2', cutover)
        print(f'Bollinger 15 dakika v2 geçişi tamamlandı. Devreden sanal bakiye: {equity:.6f} USD.')
        return cutover
    finally:
        if db is not None:
            db.close()
        migration_lock.close()
        control_lock.close()


__all__ = [
    'POLICY', 'MODEL_PATH', 'TRAINING_REPORT_PATH', 'TRAINING_DATA_PATH',
    'tick', 'status', 'migrate', 'load_artifact', 'model_callback',
    'size_position', 'challenger_after_insert_callback',
    'forward_training_samples', 'prequential_evidence',
    'refresh_promotion', 'auto_retrain_status', 'maybe_start_auto_retrain',
]
