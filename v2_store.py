"""SQLite persistence for causal Bollinger v2 forward-shadow observations.

Predictions are immutable observations made at a closed decision candle.  Their
later outcomes are replayed only from candles that have themselves closed.  The
module owns no worker loop and accepts a duck-typed scoring callback so it does
not depend on a particular model implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Callable, Mapping, Sequence

from v2_engine import (
    BAR_MS,
    DEFAULT_COST,
    MAIN_SPEC,
    CandidateEvent,
    CostModel,
    LabelNotAvailableError,
    BarrierSpec,
    candidate_signals,
    indicators,
    label_candidate,
)


STREAM_ID = "bitstamp-btcusd-live-v2"
SOURCE = "true_forward_shadow"
DEFAULT_FETCH_BARS = 240
MAX_RECOVERY_FETCH_BARS = 10_000


def ensure_tables(db: sqlite3.Connection) -> None:
    """Create the namespaced v2 schema without changing existing project tables."""

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_predictions (
            event_id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL,
            spec_id TEXT NOT NULL,
            policy TEXT NOT NULL,
            cost_version TEXT NOT NULL,
            strategy TEXT NOT NULL,
            decision_ts INTEGER NOT NULL,
            fill_ts INTEGER NOT NULL,
            atr REAL NOT NULL,
            model_version TEXT,
            score REAL,
            threshold REAL,
            feature_version TEXT,
            features TEXT,
            accepted INTEGER NOT NULL CHECK (accepted IN (0,1)),
            created_ts INTEGER NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1)),
            resolved_ts INTEGER,
            resolution TEXT,
            UNIQUE(stream_id, spec_id, cost_version, strategy, decision_ts)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_samples (
            event_id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL,
            spec_id TEXT NOT NULL,
            policy TEXT NOT NULL,
            cost_version TEXT NOT NULL,
            strategy TEXT NOT NULL,
            fill_ts INTEGER NOT NULL,
            exit_ts INTEGER NOT NULL,
            label_available_ts INTEGER NOT NULL,
            source TEXT NOT NULL,
            net_return REAL NOT NULL,
            detail TEXT NOT NULL,
            created_ts INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_shadow_state (
            scope_id TEXT PRIMARY KEY,
            anchor_decision_ts INTEGER NOT NULL,
            last_examined_decision_ts INTEGER NOT NULL,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_executions (
            event_id TEXT PRIMARY KEY,
            model_version TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL,
            exit_ts_ms INTEGER NOT NULL,
            entry_mode TEXT NOT NULL,
            net_return REAL NOT NULL,
            pnl_usd REAL NOT NULL,
            cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
            exit_reason TEXT NOT NULL,
            FOREIGN KEY(event_id) REFERENCES v2_predictions(event_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_challenger_models (
            model_version TEXT PRIMARY KEY,
            cohort_id TEXT NOT NULL,
            family TEXT NOT NULL CHECK (family = 'weighted_interaction_logistic'),
            score_kind TEXT NOT NULL CHECK (score_kind = 'probability'),
            artifact_payload TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL UNIQUE,
            training_corpus_hash TEXT NOT NULL,
            training_cutoff_label_ts INTEGER NOT NULL CHECK (training_cutoff_label_ts >= 0),
            threshold REAL NOT NULL CHECK (threshold > 0 AND threshold < 1),
            status TEXT NOT NULL CHECK (status = 'frozen_shadow'),
            created_ts INTEGER NOT NULL CHECK (created_ts >= training_cutoff_label_ts)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_challenger_scores (
            event_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
            threshold REAL NOT NULL CHECK (threshold > 0 AND threshold < 1),
            would_accept INTEGER NOT NULL CHECK (would_accept IN (0,1)),
            execution_gate INTEGER NOT NULL CHECK (execution_gate IN (0,1)),
            accepted INTEGER NOT NULL CHECK (accepted IN (0,1)),
            feature_version TEXT NOT NULL,
            feature_digest TEXT NOT NULL,
            created_ts INTEGER NOT NULL CHECK (created_ts >= 0),
            PRIMARY KEY(event_id, model_version),
            FOREIGN KEY(event_id) REFERENCES v2_predictions(event_id),
            FOREIGN KEY(model_version) REFERENCES v2_challenger_models(model_version)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_challenger_portfolios (
            model_version TEXT PRIMARY KEY,
            initial_usd REAL NOT NULL CHECK (initial_usd > 0),
            cash_usd REAL NOT NULL CHECK (cash_usd >= 0),
            position TEXT,
            realized_pnl_usd REAL NOT NULL,
            completed_trades INTEGER NOT NULL CHECK (completed_trades >= 0),
            equity_usd REAL NOT NULL CHECK (equity_usd >= 0),
            peak_equity_usd REAL NOT NULL CHECK (peak_equity_usd >= 0),
            drawdown_pct REAL NOT NULL CHECK (drawdown_pct >= 0),
            day TEXT NOT NULL,
            day_start_equity REAL NOT NULL CHECK (day_start_equity >= 0),
            daily_halt INTEGER NOT NULL CHECK (daily_halt IN (0,1)),
            drawdown_halt INTEGER NOT NULL CHECK (drawdown_halt IN (0,1)),
            created_ts INTEGER NOT NULL CHECK (created_ts >= 0),
            updated_ts INTEGER NOT NULL CHECK (updated_ts >= created_ts),
            FOREIGN KEY(model_version) REFERENCES v2_challenger_models(model_version)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v2_challenger_executions (
            event_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL CHECK (entry_ts_ms >= 0),
            exit_ts_ms INTEGER,
            entry_reference REAL NOT NULL CHECK (entry_reference > 0),
            entry_price REAL NOT NULL CHECK (entry_price > 0),
            exit_price REAL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            cost_usd REAL NOT NULL CHECK (cost_usd > 0),
            proceeds_usd REAL,
            pnl_usd REAL,
            net_return REAL,
            score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
            threshold REAL NOT NULL CHECK (threshold > 0 AND threshold < 1),
            exit_reason TEXT,
            status TEXT NOT NULL CHECK (status IN ('open','closed')),
            created_ts INTEGER NOT NULL CHECK (created_ts >= 0),
            updated_ts INTEGER NOT NULL CHECK (updated_ts >= created_ts),
            PRIMARY KEY(event_id, model_version),
            FOREIGN KEY(event_id,model_version)
                REFERENCES v2_challenger_scores(event_id,model_version)
        )
        """
    )
    prediction_columns = {row[1] for row in db.execute("PRAGMA table_info(v2_predictions)")}
    if "feature_version" not in prediction_columns:
        db.execute("ALTER TABLE v2_predictions ADD COLUMN feature_version TEXT")
    if "features" not in prediction_columns:
        db.execute("ALTER TABLE v2_predictions ADD COLUMN features TEXT")
    if "resolution" not in prediction_columns:
        db.execute("ALTER TABLE v2_predictions ADD COLUMN resolution TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS v2_predictions_unresolved "
        "ON v2_predictions(stream_id,spec_id,cost_version,resolved,strategy)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS v2_samples_scope "
        "ON v2_samples(stream_id,spec_id,cost_version,source,label_available_ts)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS v2_executions_model "
        "ON v2_executions(model_version,exit_ts_ms)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS v2_challenger_models_cohort "
        "ON v2_challenger_models(cohort_id,status,created_ts)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS v2_challenger_scores_model "
        "ON v2_challenger_scores(model_version,created_ts,event_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS v2_challenger_executions_model "
        "ON v2_challenger_executions(model_version,status,entry_ts_ms)"
    )


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value.strip()


def _timestamp_ms(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer millisecond timestamp.")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite.")
    return float(value)


def record_execution(
    db: sqlite3.Connection,
    event_id: str,
    model_version: str,
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_mode: str,
    net_return: float,
    pnl_usd: float,
    cost_usd: float,
    exit_reason: str,
    *,
    manage_transaction: bool = True,
) -> bool:
    """Freeze one actual paper execution against its originating prediction.

    Returns ``True`` for a new row and ``False`` for an exact idempotent retry.
    A retry with different outcome data is rejected rather than overwriting the
    first observed result.  Standalone callers keep the historical atomic
    commit/rollback behavior.  A caller that already owns a wider transaction
    must pass ``manage_transaction=False`` so this write participates in that
    transaction instead of committing it early.
    """

    ensure_tables(db)
    event_id = _required_text(event_id, "event_id")
    model_version = _required_text(model_version, "model_version")
    entry_mode = _required_text(entry_mode, "entry_mode")
    exit_reason = _required_text(exit_reason, "exit_reason")
    entry_ts_ms = _timestamp_ms(entry_ts_ms, "entry_ts_ms")
    exit_ts_ms = _timestamp_ms(exit_ts_ms, "exit_ts_ms")
    if exit_ts_ms < entry_ts_ms:
        raise ValueError("exit_ts_ms cannot be earlier than entry_ts_ms.")
    net_return = _finite(net_return, "net_return")
    pnl_usd = _finite(pnl_usd, "pnl_usd")
    cost_usd = _finite(cost_usd, "cost_usd")
    if cost_usd <= 0:
        raise ValueError("cost_usd must be positive.")
    if not math.isclose(net_return, pnl_usd / cost_usd, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("net_return must equal pnl_usd / cost_usd.")

    frozen = (
        model_version,
        entry_ts_ms,
        exit_ts_ms,
        entry_mode,
        net_return,
        pnl_usd,
        cost_usd,
        exit_reason,
    )
    def persist() -> bool:
        prediction = db.execute(
            "SELECT model_version,fill_ts FROM v2_predictions WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if prediction is None:
            raise ValueError("Execution requires a matching v2 prediction event_id.")
        predicted_model, predicted_fill_ts = prediction
        if predicted_model is None or str(predicted_model) != model_version:
            raise ValueError("Execution model_version does not match the frozen prediction.")
        if entry_ts_ms < int(predicted_fill_ts):
            raise ValueError("entry_ts_ms cannot precede the prediction fill timestamp.")

        existing = db.execute(
            "SELECT model_version,entry_ts_ms,exit_ts_ms,entry_mode,net_return,"
            "pnl_usd,cost_usd,exit_reason FROM v2_executions WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if existing is not None:
            if tuple(existing) == frozen:
                return False
            raise ValueError("Execution outcome is already frozen and cannot be overwritten.")
        db.execute(
            "INSERT INTO v2_executions "
            "(event_id,model_version,entry_ts_ms,exit_ts_ms,entry_mode,net_return,"
            "pnl_usd,cost_usd,exit_reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, *frozen),
        )
        return True

    if not isinstance(manage_transaction, bool):
        raise ValueError("manage_transaction must be boolean.")
    if manage_transaction:
        with db:
            return persist()
    return persist()


def get_execution(db: sqlite3.Connection, event_id: str) -> dict[str, object] | None:
    """Return a frozen execution as a plain mapping, if one exists."""

    ensure_tables(db)
    event_id = _required_text(event_id, "event_id")
    row = db.execute(
        "SELECT event_id,model_version,entry_ts_ms,exit_ts_ms,entry_mode,net_return,"
        "pnl_usd,cost_usd,exit_reason FROM v2_executions WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    names = (
        "event_id", "model_version", "entry_ts_ms", "exit_ts_ms", "entry_mode",
        "net_return", "pnl_usd", "cost_usd", "exit_reason",
    )
    return dict(zip(names, row))


def _scope_id(spec: BarrierSpec, cost: CostModel, stream_id: str) -> str:
    return f"{stream_id}|{spec.spec_id}|{cost.version}"


def stable_event_id(
    strategy: str,
    decision_ts: int,
    *,
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
    stream_id: str = STREAM_ID,
) -> str:
    """Key a forward observation by its complete immutable label contract."""

    spec.validate()
    cost.validate()
    payload = (
        f"{spec.spec_id}|{cost.version}|{stream_id}|{strategy}|{int(decision_ts)}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _latest_closed_index(rows: Sequence[Mapping[str, object]], now_ms: int) -> int | None:
    if not isinstance(now_ms, int) or isinstance(now_ms, bool):
        raise ValueError("now_ms must be an integer millisecond timestamp.")
    result = None
    for index, row in enumerate(rows):
        timestamp = int(row["ts"])
        if timestamp + BAR_MS <= now_ms:
            result = index
        else:
            break
    return result


def required_fetch_count(
    db: sqlite3.Connection,
    now_ms: int,
    *,
    base_count: int = DEFAULT_FETCH_BARS,
    max_count: int = MAX_RECOVERY_FETCH_BARS,
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
    stream_id: str = STREAM_ID,
) -> int:
    """Expand the next fetch enough to recover unresolved H8 labels after sleep.

    The bounded retention limit protects the quote loop from a multi-year API
    replay.  Older locks are released with an explicit no-label resolution.
    """

    ensure_tables(db)
    if (not isinstance(now_ms, int) or isinstance(now_ms, bool)
            or not isinstance(base_count, int) or isinstance(base_count, bool)
            or not isinstance(max_count, int) or isinstance(max_count, bool)
            or not 100 <= base_count <= max_count):
        raise ValueError("Invalid v2 recovery fetch bounds.")
    spec.validate()
    cost.validate()
    latest_start = (now_ms // BAR_MS) * BAR_MS - BAR_MS
    earliest_recoverable = latest_start - (max_count - 1) * BAR_MS
    with db:
        db.execute(
            "UPDATE v2_predictions SET resolved=1,resolved_ts=?,resolution=? "
            "WHERE stream_id=? AND spec_id=? AND policy=? AND cost_version=? "
            "AND resolved=0 AND decision_ts<?",
            (
                now_ms,
                "history_retention_exceeded_no_label",
                stream_id,
                spec.spec_id,
                spec.policy,
                cost.version,
                earliest_recoverable,
            ),
        )
    oldest = db.execute(
        "SELECT MIN(decision_ts) FROM v2_predictions WHERE stream_id=? AND spec_id=? "
        "AND policy=? AND cost_version=? AND resolved=0",
        (stream_id, spec.spec_id, spec.policy, cost.version),
    ).fetchone()[0]
    if oldest is None or int(oldest) > latest_start:
        return base_count
    needed = (latest_start - int(oldest)) // BAR_MS + 1
    return max(base_count, min(max_count, int(needed)))


def _finite_optional(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite or None.")
    return float(value)


def _model_decision(
    model_state: Mapping[str, object] | Callable[..., object] | None,
    *,
    strategy: str,
    rows: Sequence[Mapping[str, object]],
    features: Mapping[str, Sequence[float | None]],
    decision_index: int,
) -> dict[str, object]:
    """Normalize a model/callback response into immutable prediction fields."""

    if model_state is None:
        return {"model_version": None, "score": None, "threshold": None,
                "feature_version": None, "features": None, "accepted": False}
    state: object = model_state
    if callable(state):
        state = state(
            strategy=strategy,
            rows=rows,
            features=features,
            decision_index=decision_index,
        )
    elif isinstance(state, Mapping) and strategy in state:
        state = state[strategy]
        if callable(state):
            state = state(
                strategy=strategy,
                rows=rows,
                features=features,
                decision_index=decision_index,
            )
    if state is None:
        return {"model_version": None, "score": None, "threshold": None,
                "feature_version": None, "features": None, "accepted": False}
    if isinstance(state, (int, float)) and not isinstance(state, bool):
        score = _finite_optional(state, "score")
        return {"model_version": None, "score": score, "threshold": None,
                "feature_version": None, "features": None, "accepted": False}
    if not isinstance(state, Mapping):
        raise ValueError("Model callback must return a mapping, score, or None.")

    scorer = state.get("score_callback")
    if callable(scorer):
        score_value = scorer(
            strategy=strategy,
            rows=rows,
            features=features,
            decision_index=decision_index,
        )
    else:
        score_value = state.get("score")
    score = _finite_optional(score_value, "score")
    threshold = _finite_optional(state.get("threshold"), "threshold")
    if score is not None and not (0 <= score <= 1):
        raise ValueError("score must be in [0, 1].")
    if threshold is not None and not (0 <= threshold <= 1):
        raise ValueError("threshold must be in [0, 1].")
    version_value = state.get("model_version", state.get("version"))
    model_version = None if version_value is None else str(version_value)
    feature_version_value = state.get("feature_version")
    feature_version = None if feature_version_value is None else str(feature_version_value)
    raw_features = state.get("features")
    if raw_features is None:
        frozen_features = None
    else:
        if (not isinstance(raw_features, (list, tuple))
                or not raw_features
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in raw_features)):
            raise ValueError("features must be a non-empty finite numeric sequence.")
        frozen_features = [float(value) for value in raw_features]
    explicit_accept = state.get("accepted", state.get("accept"))
    if explicit_accept is None:
        accepted = bool(state.get("eligible", False)) and score is not None and threshold is not None and score >= threshold
    elif isinstance(explicit_accept, bool):
        accepted = explicit_accept
    else:
        raise ValueError("accepted must be boolean when supplied.")
    return {
        "model_version": model_version,
        "score": score,
        "threshold": threshold,
        "feature_version": feature_version,
        "features": frozen_features,
        "accepted": accepted,
    }


def record_decisions(
    db: sqlite3.Connection,
    rows: Sequence[Mapping[str, object]],
    model_state: Mapping[str, object] | Callable[..., object] | None,
    now_ms: int,
    *,
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
    stream_id: str = STREAM_ID,
    after_insert: Callable[..., object] | None = None,
) -> list[str]:
    """Record candidates on only the latest closed bar, never historical backfill."""

    ensure_tables(db)
    spec.validate()
    cost.validate()
    if after_insert is not None and not callable(after_insert):
        raise ValueError("after_insert must be callable or None.")
    latest = _latest_closed_index(rows, now_ms)
    if latest is None:
        return []
    decision_ts = int(rows[latest]["ts"])
    scope = _scope_id(spec, cost, stream_id)
    state = db.execute(
        "SELECT anchor_decision_ts,last_examined_decision_ts FROM v2_shadow_state WHERE scope_id=?",
        (scope,),
    ).fetchone()
    if state is not None and decision_ts <= int(state[1]):
        return []

    # Slicing is a hard causality boundary: neither signals nor the score callback
    # receives a forming candle or any later data supplied by a replay caller.
    causal_rows = list(rows[: latest + 1])
    f = indicators(causal_rows, spec)
    signals = candidate_signals(causal_rows, latest, spec, f)
    recorded = []
    with db:
        if state is None:
            db.execute(
                "INSERT INTO v2_shadow_state VALUES (?,?,?,?,?)",
                (scope, decision_ts, decision_ts, now_ms, now_ms),
            )
        else:
            db.execute(
                "UPDATE v2_shadow_state SET last_examined_decision_ts=?,updated_ts=? WHERE scope_id=?",
                (decision_ts, now_ms, scope),
            )

        for strategy in signals:
            unresolved = db.execute(
                "SELECT 1 FROM v2_predictions WHERE stream_id=? AND spec_id=? "
                "AND policy=? AND cost_version=? AND strategy=? AND resolved=0 LIMIT 1",
                (stream_id, spec.spec_id, spec.policy, cost.version, strategy),
            ).fetchone()
            if unresolved:
                continue
            atr = f["atr"][latest]
            if atr is None or not math.isfinite(float(atr)) or float(atr) <= 0:
                continue
            frozen = _model_decision(
                model_state,
                strategy=strategy,
                rows=causal_rows,
                features=f,
                decision_index=latest,
            )
            event_id = stable_event_id(
                strategy, decision_ts, spec=spec, cost=cost, stream_id=stream_id)
            cursor = db.execute(
                "INSERT OR IGNORE INTO v2_predictions "
                "(event_id,stream_id,spec_id,policy,cost_version,strategy,decision_ts,fill_ts,atr,"
                "model_version,score,threshold,feature_version,features,accepted,created_ts,resolved,resolved_ts,resolution) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)",
                (
                    event_id,
                    stream_id,
                    spec.spec_id,
                    spec.policy,
                    cost.version,
                    strategy,
                    decision_ts,
                    decision_ts + BAR_MS,
                    float(atr),
                    frozen["model_version"],
                    frozen["score"],
                    frozen["threshold"],
                    frozen["feature_version"],
                    (json.dumps(frozen["features"], separators=(",", ":"), allow_nan=False)
                     if frozen["features"] is not None else None),
                    int(bool(frozen["accepted"])),
                    now_ms,
                ),
            )
            if cursor.rowcount:
                if after_insert is not None:
                    # The hook deliberately participates in this transaction.
                    # A challenger failure therefore cannot leave a production
                    # prediction or shadow cursor without its paired score.
                    after_insert(
                        db=db,
                        event_id=event_id,
                        strategy=strategy,
                        decision_ts=decision_ts,
                        created_ts=now_ms,
                        frozen=dict(frozen),
                    )
                recorded.append(event_id)
    return recorded


def replay_labels(
    db: sqlite3.Connection,
    rows: Sequence[Mapping[str, object]],
    now_ms: int,
    *,
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
    stream_id: str = STREAM_ID,
) -> list[str]:
    """Resolve previously observed events from their exact stored H8 window."""

    ensure_tables(db)
    spec.validate()
    cost.validate()
    if not isinstance(now_ms, int) or isinstance(now_ms, bool):
        raise ValueError("now_ms must be an integer millisecond timestamp.")
    index_by_ts = {int(row["ts"]): index for index, row in enumerate(rows)}
    earliest_ts = min(index_by_ts) if index_by_ts else None
    pending = db.execute(
        "SELECT event_id,strategy,decision_ts,fill_ts,atr,model_version,score,threshold,"
        "feature_version,features,accepted,created_ts "
        "FROM v2_predictions WHERE stream_id=? AND spec_id=? AND policy=? AND cost_version=? "
        "AND resolved=0 ORDER BY fill_ts,strategy",
        (stream_id, spec.spec_id, spec.policy, cost.version),
    ).fetchall()
    resolved = []
    for row in pending:
        (
            event_id,
            strategy,
            decision_ts,
            fill_ts,
            atr,
            model_version,
            score,
            threshold,
            feature_version,
            features_json,
            accepted,
            prediction_created_ts,
        ) = row
        decision_index = index_by_ts.get(int(decision_ts))
        fill_index = index_by_ts.get(int(fill_ts))
        if decision_index is None or fill_index is None or fill_index != decision_index + 1:
            # A bounded provider response can exclude the event even after
            # required_fetch_count requested a larger window.  Once the fill
            # candle is final, release this strategy lock explicitly instead
            # of leaving an observation that can never be labeled.
            if (earliest_ts is not None
                    and (int(decision_ts) < earliest_ts or int(fill_ts) < earliest_ts)
                    and int(fill_ts) + BAR_MS <= now_ms):
                with db:
                    db.execute(
                        "UPDATE v2_predictions SET resolved=1,resolved_ts=?,resolution=? "
                        "WHERE event_id=? AND resolved=0",
                        (now_ms, "replay_window_excluded_prediction_no_label", event_id),
                    )
            continue
        # A zero-volume opening candle is not an executable fill.  Close the
        # prediction lock after that candle is final, but never manufacture a
        # trade label from it.
        if float(rows[fill_index]["volume"]) <= 0:
            if int(fill_ts) + BAR_MS <= now_ms:
                with db:
                    db.execute(
                        "UPDATE v2_predictions SET resolved=1,resolved_ts=?,resolution=? "
                        "WHERE event_id=? AND resolved=0",
                        (now_ms, "no_fill_zero_volume", event_id),
                    )
            continue
        candidate = CandidateEvent(
            event_id=str(event_id),
            strategy=str(strategy),
            decision_index=decision_index,
            decision_ts=int(decision_ts),
            fill_index=fill_index,
            fill_ts=int(fill_ts),
            atr=float(atr),
            spec_id=spec.spec_id,
            policy=spec.policy,
            dataset_hash=stream_id,
        )
        try:
            label = label_candidate(rows, candidate, spec, cost)
        except LabelNotAvailableError:
            continue
        if int(label["label_available_ts"]) > now_ms:
            continue
        prediction = {
            "model_version": model_version,
            "score": score,
            "threshold": threshold,
            "feature_version": feature_version,
            "accepted": bool(accepted),
            "created_ts": int(prediction_created_ts),
        }
        detail = dict(label)
        detail.pop("_fill_index", None)
        detail.pop("_exit_index", None)
        metadata = dict(detail["metadata"])
        metadata.update(
            {
                "source": SOURCE,
                "stream_id": stream_id,
                "model_version": model_version,
                "prediction": prediction,
            }
        )
        detail["metadata"] = metadata
        if features_json is not None:
            features = json.loads(features_json)
            if (not isinstance(features, list) or not features
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in features)):
                raise ValueError("Stored prediction features are invalid.")
            detail["x"] = [float(value) for value in features]
        encoded = json.dumps(detail, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with db:
            db.execute(
                "INSERT OR IGNORE INTO v2_samples "
                "(event_id,stream_id,spec_id,policy,cost_version,strategy,fill_ts,exit_ts,"
                "label_available_ts,source,net_return,detail,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    stream_id,
                    spec.spec_id,
                    spec.policy,
                    cost.version,
                    strategy,
                    fill_ts,
                    label["exit_ts"],
                    label["label_available_ts"],
                    SOURCE,
                    label["net_return"],
                    encoded,
                    now_ms,
                ),
            )
            db.execute(
                "UPDATE v2_predictions SET resolved=1,resolved_ts=?,resolution=? "
                "WHERE event_id=? AND resolved=0",
                (now_ms, "labeled", event_id),
            )
        resolved.append(str(event_id))
    return resolved


def status(
    db: sqlite3.Connection,
    *,
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
    stream_id: str = STREAM_ID,
) -> dict[str, object]:
    """Return current-scope forward-shadow counts; old specs remain isolated."""

    ensure_tables(db)
    values = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(resolved),0),COALESCE(SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END),0) "
        "FROM v2_predictions WHERE stream_id=? AND spec_id=? AND policy=? AND cost_version=?",
        (stream_id, spec.spec_id, spec.policy, cost.version),
    ).fetchone()
    sample_count = db.execute(
        "SELECT COUNT(*) FROM v2_samples WHERE stream_id=? AND spec_id=? AND policy=? "
        "AND cost_version=? AND source=?",
        (stream_id, spec.spec_id, spec.policy, cost.version, SOURCE),
    ).fetchone()[0]
    execution = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(e.pnl_usd),0),COALESCE(SUM(e.cost_usd),0) "
        "FROM v2_executions e JOIN v2_predictions p ON p.event_id=e.event_id "
        "WHERE p.stream_id=? AND p.spec_id=? AND p.policy=? AND p.cost_version=?",
        (stream_id, spec.spec_id, spec.policy, cost.version),
    ).fetchone()
    scope = _scope_id(spec, cost, stream_id)
    anchor = db.execute(
        "SELECT anchor_decision_ts,last_examined_decision_ts FROM v2_shadow_state WHERE scope_id=?",
        (scope,),
    ).fetchone()
    return {
        "stream_id": stream_id,
        "spec_id": spec.spec_id,
        "policy": spec.policy,
        "cost_version": cost.version,
        "predictions": int(values[0]),
        "resolved": int(values[1]),
        "unresolved": int(values[2]),
        "samples": int(sample_count),
        "true_forward": int(sample_count),
        "executions": int(execution[0]),
        "execution_pnl_usd": float(execution[1]),
        "execution_cost_usd": float(execution[2]),
        "anchor_decision_ts": int(anchor[0]) if anchor else None,
        "last_examined_decision_ts": int(anchor[1]) if anchor else None,
    }


__all__ = [
    "STREAM_ID",
    "SOURCE",
    "ensure_tables",
    "stable_event_id",
    "required_fetch_count",
    "record_decisions",
    "record_execution",
    "get_execution",
    "replay_labels",
    "status",
]
