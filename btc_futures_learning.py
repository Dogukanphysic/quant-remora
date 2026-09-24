"""Offline, shadow-only learning from the immutable BTC Futures event ledger.

Only the learning database is writable. Wallet changes are unverified account
proxies, never verified trade PnL. No exchange client, credentials or live worker
is imported. Labels mean a negative (1) or positive (0) account wallet change.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from btc_futures_small_sample import summarize as summarize_small_sample

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "state/btc-futures-live.sqlite3"
DB = ROOT / "state/btc-futures-mainnet-learning.sqlite3"
AUTHORITY = {"automatic_activation": False, "decision_authority": False,
             "real_orders_enabled": False, "mainnet_candidate_active": False}
PROXY = "account_wallet_delta_unverified"
ALGORITHM = "regularized-logistic-loss-v1"
PRE_ENTRY_SCHEMA = "pre_entry_v1"
CONTROL_DOMAIN_SCHEMA = "entry_control_domains_v1"
MAX_MODEL_AGE_SECONDS = 7 * 24 * 3600
MAX_SOURCE_AGE_SECONDS = 120
DECISION_NUMERIC = ("rsi", "lower", "upper", "entry_limit", "atr", "macd",
                    "macd_signal", "macd_histogram", "recovery_confirmed",
                    "trend_confirmed", "histogram_rising", "raw_enter", "enter",
                    "lower_zone_reached", "lower_touched", "volume_confirmed",
                    "squeeze", "breakout", "ema50", "ema200")
ENTRY_STABLE = ("schema", "decision", "bar", "captured_at", "wallet_before_usdt",
                "quantity", "risk_profile", "leverage")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid numeric value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid numeric value") from None
    if not result.is_finite() or abs(result) > Decimal("1e15"):
        raise ValueError("nonfinite or out-of-range numeric value")
    return result


def _number(value):
    return float(_decimal(value))


def _positive(value):
    result = _decimal(value)
    if result <= 0:
        raise ValueError("nonpositive value")
    return result


def _path(value):
    return Path(value).expanduser().resolve()


def _guard(source_path, learning_path, report_path=None):
    paths = [_path(source_path), _path(learning_path)]
    if report_path is not None:
        paths.append(_path(report_path))
    for index, first in enumerate(paths):
        for second in paths[index + 1:]:
            same = os.path.normcase(str(first)) == os.path.normcase(str(second))
            if first.exists() and second.exists():
                same = same or os.path.samefile(first, second)
            if same:
                raise ValueError("source, learning database and report paths must differ")
            # SQLite sidecar files must not be treated as report/learning outputs.
            if any(str(first).lower() == (str(second) + suffix).lower() or
                   str(second).lower() == (str(first) + suffix).lower()
                   for suffix in ("-wal", "-shm", "-journal")):
                raise ValueError("output path overlaps a SQLite sidecar")
    return paths


def _readonly(path):
    connection = sqlite3.connect(_path(path).as_uri() + "?mode=ro", uri=True,
                                 timeout=10)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _connect(path):
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    existing = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if existing and "meta" not in existing:
        db.close()
        raise ValueError("learning path contains an unrelated database")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts REAL NOT NULL,
            kind TEXT NOT NULL,payload_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,entry_event_id INTEGER,
            activated_event_id INTEGER,closed_event_id INTEGER,opened_at REAL,
            closed_at REAL,features TEXT NOT NULL,label INTEGER,
            wallet_delta_proxy_usdt TEXT NOT NULL,quality TEXT NOT NULL,
            provenance TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,dataset_hash TEXT UNIQUE,
            created_at REAL,trained_sample_count INTEGER,model TEXT,validation TEXT);
        CREATE TABLE IF NOT EXISTS quarantine(id TEXT PRIMARY KEY,reason TEXT,
            detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pre_entry_samples(sample_id TEXT PRIMARY KEY,
            features TEXT NOT NULL,context_digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pre_entry_models(id TEXT PRIMARY KEY,
            dataset_hash TEXT UNIQUE,artifact TEXT NOT NULL,artifact_digest TEXT NOT NULL,
            created_at REAL,source_fingerprint TEXT,trained_through_event_id INTEGER,
            trained_through_at REAL);
        CREATE TABLE IF NOT EXISTS forward_predictions(id TEXT PRIMARY KEY,
            entry_event_id INTEGER UNIQUE,closed_event_id INTEGER UNIQUE,model_id TEXT,
            prediction TEXT NOT NULL,label INTEGER,wallet_delta_proxy_usdt TEXT,
            quality TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trade_journal(id TEXT PRIMARY KEY,
            source_fingerprint TEXT,entry_event_id INTEGER,activated_event_id INTEGER,
            closed_event_id INTEGER,entry_client_id TEXT,status TEXT,evaluation_status TEXT,
            opened_at REAL,closed_at REAL,entry_quantity TEXT,entry_price TEXT,leverage REAL,
            entry_fill_status TEXT,exit_fill_status TEXT,wallet_delta_proxy_usdt TEXT,
            sample_id TEXT,detail TEXT,valid INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS order_observations(id TEXT PRIMARY KEY,
            event_id INTEGER,cycle_id TEXT,role TEXT,status TEXT,client_id TEXT,
            quantity TEXT,executed_quantity TEXT,average_price TEXT,evidence TEXT,
            event_hash TEXT,observed_at REAL,valid INTEGER NOT NULL);
    """)
    marker = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if marker and marker[0] != "1":
        db.close()
        raise ValueError("unsupported learning schema")
    _put(db, "schema", 1)
    db.commit()
    return db


def _put(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, _json(value)))


def _get(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _source(path):
    with closing(_readonly(path)) as source:
        source.execute("BEGIN")
        row = source.execute("SELECT value FROM state WHERE id=1").fetchone()
        if not row:
            raise ValueError("source state missing")
        state = json.loads(row[0])
        if not isinstance(state, dict):
            raise ValueError("source state malformed")
        rows = source.execute("SELECT id,ts,kind,payload FROM events ORDER BY id").fetchall()
    events = []
    for event_id, ts, kind, payload in rows:
        stamp = _number(ts)
        if (stamp < 0 or not isinstance(event_id, int) or event_id <= 0 or
                not isinstance(kind, str) or not kind):
            raise ValueError("source event identity malformed")
        # Hash raw immutable payload, not a normalized or redacted approximation.
        event = {"id": event_id, "ts": stamp, "kind": kind,
                 "hash": _hash([event_id, ts, kind, payload])}
        try:
            event["payload"] = json.loads(payload)
            if not isinstance(event["payload"], dict):
                raise ValueError("event payload is not an object")
        except (ValueError, TypeError):
            event["payload"] = None
        events.append(event)
    # Identity is hashed internally; neither reports nor samples store raw identity.
    binding = _hash([os.path.normcase(str(_path(path))), state.get("identity"),
                     state.get("epoch"), state.get("account_epoch"),
                     state.get("initial_wallet_usdt")])
    return state, events, binding


def _features(intent, activated, closed):
    entry = intent.get("learning_entry")
    activation_entry = activated.get("learning_entry")
    outcome = closed.get("learning_outcome")
    if entry is None and activation_entry is not None:
        raise ValueError("enriched activation missing entry snapshot")
    if entry is not None:
        if not isinstance(entry, dict) or entry.get("schema") != 1:
            raise ValueError("unsupported entry snapshot")
        if not isinstance(entry.get("decision"), dict):
            raise ValueError("missing entry decision")
        for other in (activation_entry, outcome.get("entry") if isinstance(outcome, dict) else None):
            if other is not None and (not isinstance(other, dict) or
                    any(other.get(key) != value for key, value in entry.items())):
                raise ValueError("entry snapshot conflict")
        captured_at = _number(entry["captured_at"])
        bar = _number(entry["bar"])
        if captured_at < 0 or bar < 0 or bar > captured_at * 1000:
            raise ValueError("entry bar is later than its snapshot")
        _positive(entry["quantity"])
        _positive(entry["leverage"])
        _decimal(entry["wallet_before_usdt"])
        if _decimal(entry["quantity"]) != _decimal(intent["quantity"]):
            raise ValueError("entry snapshot quantity conflict")
    elif outcome is not None and (not isinstance(outcome, dict) or outcome.get("entry") is not None):
        raise ValueError("outcome snapshot has no entry provenance")
    quantity = _positive(activated["quantity"])
    if quantity != _positive(intent["quantity"]):
        raise ValueError("entry and activation quantity conflict")
    price = _positive(activated["entry"])
    stop = _positive(activated["stop"])
    target = _positive(activated["target"])
    if not stop < price < target:
        raise ValueError("invalid long entry barriers")
    features = {"entry_price": float(price), "quantity": float(quantity),
                "stop_distance_fraction": float((price - stop) / price),
                "target_distance_fraction": float((target - price) / price),
                "reward_risk_ratio": float((target - price) / (price - stop))}
    risk = activated.get("risk_profile", "unknown")
    if isinstance(risk, str) and len(risk) <= 64:
        features["risk_profile=" + risk] = 1.0
    for key in ("stop_atr", "target_atr"):
        if activated.get(key) is not None:
            features[key] = _number(activated[key])
    if entry is not None:
        features["leverage"] = _number(entry["leverage"])
        decision = entry["decision"]
        for key in DECISION_NUMERIC:
            value = decision.get(key)
            if value is not None:
                features["decision." + key] = float(value) if isinstance(value, bool) else _number(value)
        regime = decision.get("entry_regime")
        if isinstance(regime, str) and len(regime) <= 64:
            features["decision.entry_regime=" + regime] = 1.0
    # Outcome metadata is deliberately never passed into the feature vector.
    for value in features.values():
        _number(value)
    return features, entry, outcome


def _reconstruct(state, events, binding):
    samples, quarantine = [], []
    intent = active = None
    ambiguous = False
    seen_clients = set()
    previous_wallet = None
    previous_close_at = None
    if events and events[0]["id"] == 1:
        try:
            previous_wallet = _decimal(state.get("initial_wallet_usdt"))
        except ValueError:
            pass

    def reject(reason, ids):
        detail = {"event_ids": ids}
        quarantine.append((_hash([binding, reason, ids]), reason, _json(detail)))

    for event in events:
        kind, payload = event["kind"], event["payload"]
        if payload is None:
            reject("malformed_event", [event["id"]])
            if kind in {"entry_intent", "long_activated", "round_trip_closed"}:
                ambiguous = True
                previous_wallet = None
            continue
        if kind == "entry_intent":
            if intent is not None or active is not None or ambiguous:
                reject("overlapping_entries", [e["id"] for e in (intent, active, event) if e])
                ambiguous = True
            client = payload.get("client_id")
            if not isinstance(client, str) or not client or client in seen_clients:
                reject("missing_or_reused_client_id", [event["id"]])
                ambiguous = True
            if isinstance(client, str):
                seen_clients.add(client)
            intent = event
        elif kind in {"unsent_entry_recovered", "entry_terminal_without_position"}:
            client = payload.get("client_id", payload.get("clientOrderId"))
            terminal = (kind == "unsent_entry_recovered" or
                        payload.get("status") in {"CANCELED", "EXPIRED", "REJECTED"})
            if (terminal and intent is not None and active is None and not ambiguous and
                    client == intent["payload"].get("client_id")):
                intent = None
            else:
                reject("unmatched_terminal_entry", [event["id"]])
        elif kind == "long_activated":
            if intent is None or active is not None:
                reject("unpaired_activation", [event["id"]])
                ambiguous = True
            active = event
        elif kind == "round_trip_closed":
            ids = [e["id"] for e in (intent, active, event) if e]
            try:
                wallet_after = _decimal(payload["wallet_usdt"])
            except (ValueError, KeyError):
                wallet_after = None
            if ambiguous or intent is None or active is None:
                reject("ambiguous_or_missing_cycle", ids)
            else:
                try:
                    if not intent["ts"] <= active["ts"] <= event["ts"]:
                        raise ValueError("nonchronological cycle")
                    if previous_close_at is not None and intent["ts"] < previous_close_at:
                        raise ValueError("nonchronological adjacent cycles")
                    features, entry, outcome = _features(intent["payload"], active["payload"], payload)
                    if entry is not None and _number(entry["captured_at"]) > intent["ts"]:
                        raise ValueError("entry snapshot captured after intent")
                    wallet_before = _decimal(entry["wallet_before_usdt"]) if entry else previous_wallet
                    if wallet_before is None or wallet_after is None:
                        raise ValueError("wallet baseline missing")
                    delta = wallet_after - wallet_before
                    if outcome is not None:
                        if (not isinstance(outcome, dict) or outcome.get("schema") != 1 or
                                outcome.get("pnl_basis") != PROXY or
                                outcome.get("execution_verified") is not False or
                                (entry is not None and outcome.get("entry") is None)):
                            raise ValueError("unsupported outcome evidence")
                        if _decimal(outcome["wallet_after_usdt"]) != wallet_after:
                            raise ValueError("outcome wallet conflict")
                        if outcome.get("wallet_delta_proxy_usdt") is not None and _decimal(outcome["wallet_delta_proxy_usdt"]) != delta:
                            raise ValueError("outcome delta conflict")
                    label = 1 if delta < 0 else 0 if delta > 0 else None
                    quality = "entry_snapshot_wallet_proxy" if entry else "legacy_wallet_proxy"
                    provenance = {"source_fingerprint": binding, "event_ids": ids,
                                  "event_hashes": [intent["hash"], active["hash"], event["hash"]],
                                  "pnl_basis": PROXY, "execution_verified": False,
                                  "feature_basis": "entry_only", "zero_label_excluded": label is None,
                                  "wallet_before_usdt": str(wallet_before), "wallet_after_usdt": str(wallet_after),
                                  "exit_reason": (outcome or {}).get("exit_reason", "legacy_unknown")}
                    samples.append({"id": _hash([binding, ids]), "entry_event_id": intent["id"],
                                    "activated_event_id": active["id"], "closed_event_id": event["id"],
                                    "opened_at": intent["ts"], "closed_at": event["ts"],
                                    "features": features, "label": label,
                                    "entry_context": entry,
                                    "wallet_delta_proxy_usdt": str(delta), "quality": quality,
                                    "provenance": provenance})
                except (ValueError, TypeError, KeyError, OverflowError) as exc:
                    reject(str(exc) if isinstance(exc, ValueError) else "missing_or_invalid_cycle_field", ids)
            previous_wallet = wallet_after
            previous_close_at = event["ts"]
            intent = active = None
            ambiguous = False
    return samples, quarantine


def _journal_empty(valid=False):
    return {"valid": valid, "cycle_count": 0, "closed_cycle_count": 0,
            "open_cycle_count": 0, "pending_cycle_count": 0,
            "terminal_without_position_count": 0, "evaluated_proxy_count": 0,
            "unknown_evaluation_count": 0, "order_observation_count": 0,
            "current_trade": None, "recent_trades": [],
            "verified_net_pnl_count": 0, "coverage_basis": "recorded_source_events_and_current_state"}


def _journal_text(value):
    return value if isinstance(value, str) and 0 < len(value) <= 160 else None


def _journal_number(value):
    try:
        return str(_decimal(value))
    except (ValueError, TypeError):
        return None


def _sync_journal(db, state, events, binding, samples):
    """Project every recorded attempt/position/close, including unusable samples.

    This is an observational journal, not an exchange fill reconciliation. Raw
    account identity, arbitrary error text and full exchange payloads are omitted.
    Missing order status, price, costs and PnL stay unknown. Current state can
    describe a current position but never rewrites a historical entry snapshot.
    """
    cycles, unresolved, clients, observations = [], [], {}, []
    historical_leverage = None
    sample_by_close = {sample["closed_event_id"]: sample for sample in samples}
    terminal_states = {"CANCELED", "EXPIRED", "REJECTED"}
    order_states = terminal_states | {"NEW", "PARTIALLY_FILLED", "FILLED", "PENDING_CANCEL", "UNKNOWN"}

    def new_cycle(event, client=None, context=None):
        context = context if isinstance(context, dict) else {}
        row = {"id": _hash([binding, "cycle", event["id"] if event else "state", client]),
               "source_fingerprint": binding, "entry_event_id": None,
               "activated_event_id": None, "closed_event_id": None,
               "entry_client_id": client, "status": "unknown", "evaluation_status": "execution_unknown",
               "opened_at": event["ts"] if event else None, "closed_at": None,
               "entry_quantity": None, "entry_price": None,
               "leverage": _journal_number(context.get("leverage")) or historical_leverage,
               "entry_fill_status": "UNKNOWN", "exit_fill_status": "UNKNOWN",
               "wallet_delta_proxy_usdt": None, "sample_id": None,
               "detail": {"event_ids": [], "pnl_basis": None, "verified_net_pnl_usdt": None,
                          "fees_usdt": None, "funding_usdt": None,
                          "execution_reconciled": False, "exit_price": None,
                          "signal_profile": _journal_text(context.get("signal_profile")),
                          "risk_profile": _journal_text(context.get("risk_profile")),
                          "projection_basis": "source_events" if event else "source_current_state"},
               "valid": 1}
        cycles.append(row)
        unresolved.append(row)
        if client:
            clients.setdefault(client, []).append(row)
        return row

    def owner(payload, *, entry_role=False):
        client = _journal_text(payload.get("entry_client_id"))
        if client is None and entry_role:
            client = _journal_text(payload.get("client_id", payload.get("clientOrderId")))
        if client:
            matches = clients.get(client, [])
            return matches[0] if len(matches) == 1 else None
        return unresolved[0] if len(unresolved) == 1 else None

    def attach(row, event):
        if row is not None and event["id"] not in row["detail"]["event_ids"]:
            row["detail"]["event_ids"].append(event["id"])

    def observe(event, row, role, status, payload, basis):
        values = payload if isinstance(payload, dict) else {}
        client = _journal_text(values.get("client_id", values.get("clientOrderId")))
        detail = {"basis": basis, "source_kind": event["kind"] if event else "current_state",
                  "observation_source": _journal_text(values.get("observation_source")),
                  "order_type": _journal_text(values.get("order_type", values.get("type"))),
                  "side": _journal_text(values.get("side")), "pnl_verified": False,
                  "evaluation_status": ("recorded_fill_without_cost_reconciliation" if status in {"FILLED", "PARTIALLY_FILLED"}
                                        else "order_state_unknown" if status == "UNKNOWN" else "recorded_order_or_position_state")}
        error_code = values.get("error_code", values.get("code"))
        if isinstance(error_code, (int, str)) and not isinstance(error_code, bool):
            detail["error_code"] = str(error_code)[:32]
        observations.append({"id": _hash([binding, "observation", event["id"] if event else "state",
                                           role, client, row["id"] if row else None]),
                             "event_id": event["id"] if event else None,
                             "cycle_id": row["id"] if row else None, "role": role, "status": status,
                             "client_id": client,
                             "quantity": _journal_number(values.get("quantity", values.get("origQty"))),
                             "executed_quantity": _journal_number(values.get("executed_quantity", values.get("executedQty"))),
                             "average_price": _journal_number(values.get("average_price", values.get("avgPrice"))),
                             "evidence": detail, "event_hash": event["hash"] if event else None,
                             "observed_at": event["ts"] if event else _journal_number(state.get("last_poll")),
                             "valid": 1})
        if row is not None and event is not None:
            attach(row, event)

    for event in events:
        kind, payload = event["kind"], event["payload"]
        if kind in {"configured", "open_position_leverage_migrated", "leverage_contract_migrated"}:
            if isinstance(payload, dict):
                historical_leverage = (_journal_number(payload.get("leverage", payload.get("new_leverage", payload.get("to"))))
                                       or historical_leverage)
        if payload is None:
            if kind in {"entry_intent", "long_activated", "round_trip_closed", "order_observation",
                        "unsent_entry_recovered", "entry_terminal_without_position"}:
                row = new_cycle(event)
                row["detail"]["issue"] = "malformed_source_event"
                observe(event, row, "unknown", "UNKNOWN", {}, "malformed_source_event")
            continue
        if kind == "entry_intent":
            client = _journal_text(payload.get("client_id"))
            row = new_cycle(event, client, payload.get("learning_entry"))
            row.update(entry_event_id=event["id"], status="pending", evaluation_status="awaiting_entry_outcome",
                       entry_quantity=_journal_number(payload.get("quantity")))
            if len(unresolved) > 1:
                for pending in unresolved:
                    pending["detail"]["pairing_issue"] = "overlapping_unresolved_attempts"
            observe(event, row, "entry", "INTENT_RECORDED", payload, "intent_only")
        elif kind == "long_activated":
            row = owner(payload, entry_role=True)
            if row is not None and row["status"] in {"closed", "unsent", "canceled", "expired", "rejected"}:
                row = None
            if row is None:
                row = new_cycle(event, _journal_text(payload.get("entry_client_id", payload.get("client_id"))),
                                payload.get("learning_entry"))
                row["detail"]["pairing_issue"] = "missing_or_ambiguous_entry"
            if row["activated_event_id"] is not None:
                row["detail"]["pairing_issue"] = "duplicate_activation"
            else:
                row["activated_event_id"] = event["id"]
            row.update(status="open", evaluation_status="awaiting_close",
                       entry_quantity=_journal_number(payload.get("quantity")),
                       entry_price=_journal_number(payload.get("entry")))
            if row["entry_fill_status"] not in {"FILLED", "PARTIALLY_FILLED"}:
                row["entry_fill_status"] = "POSITION_CONFIRMED"
            observe(event, row, "entry", "POSITION_CONFIRMED",
                    {**payload, "average_price": payload.get("entry")}, "worker_position_activation")
        elif kind == "round_trip_closed":
            sample = sample_by_close.get(event["id"])
            matches = [cycle for cycle in cycles if sample and cycle["entry_event_id"] == sample["entry_event_id"]]
            row = matches[0] if len(matches) == 1 else owner(payload)
            if row is None or row["closed_event_id"] is not None:
                row = new_cycle(event, _journal_text(payload.get("entry_client_id")))
                row["detail"]["pairing_issue"] = "missing_or_ambiguous_open_cycle"
            row.update(closed_event_id=event["id"], closed_at=event["ts"], status="closed",
                       evaluation_status="quarantined_no_training_sample")
            if sample:
                row.update(sample_id=sample["id"], wallet_delta_proxy_usdt=sample["wallet_delta_proxy_usdt"],
                           evaluation_status="neutral_wallet_proxy" if sample["label"] is None else "evaluated_wallet_proxy")
                row["detail"].update(pnl_basis=PROXY, label=sample["label"], feature_quality=sample["quality"])
                row["detail"]["evaluation_reason"] = (
                    "negative_account_wallet_proxy" if sample["label"] == 1 else
                    "positive_account_wallet_proxy" if sample["label"] == 0 else "neutral_account_wallet_proxy")
            else:
                exclusions = []
                for reason, detail in db.execute("SELECT reason,detail FROM quarantine"):
                    if event["id"] in json.loads(detail).get("event_ids", []):
                        exclusions.append(reason)
                row["detail"]["training_exclusion_reasons"] = sorted(set(exclusions)) or ["missing_unambiguous_complete_cycle"]
            exit_order = payload.get("exit_order")
            if not isinstance(exit_order, dict):
                exit_order = {}
            exit_status = exit_order.get("status") if exit_order.get("status") in order_states else "UNKNOWN"
            if exit_status != "UNKNOWN" or row["exit_fill_status"] == "UNKNOWN":
                row["exit_fill_status"] = exit_status
            observe(event, row, "exit", exit_status, exit_order,
                    "recorded_exit_order_status" if exit_order else "closed_cycle_without_exit_order")
            if row in unresolved:
                unresolved.remove(row)
        elif kind in {"unsent_entry_recovered", "entry_terminal_without_position"}:
            row = owner(payload, entry_role=True)
            if row is None:
                row = new_cycle(event, _journal_text(payload.get("client_id", payload.get("clientOrderId"))))
                row["detail"]["pairing_issue"] = "terminal_without_matching_intent"
            status = "NOT_FOUND" if kind == "unsent_entry_recovered" else payload.get("status", "UNKNOWN")
            row["entry_fill_status"] = status if status in terminal_states | {"NOT_FOUND"} else "UNKNOWN"
            if status in terminal_states | {"NOT_FOUND"}:
                row.update(status="unsent" if status == "NOT_FOUND" else status.lower(),
                           evaluation_status="terminal_without_position", closed_at=event["ts"])
                if row in unresolved:
                    unresolved.remove(row)
            observe(event, row, "entry", row["entry_fill_status"], payload, "worker_terminal_without_position")
        elif kind == "order_observation":
            role = payload.get("role") if payload.get("role") in {"entry", "stop", "target", "exit"} else "unknown"
            row = owner(payload, entry_role=role == "entry")
            status = payload.get("status") if payload.get("status") in order_states else "UNKNOWN"
            if row is None and role == "entry":
                row = new_cycle(event, _journal_text(payload.get("entry_client_id", payload.get("client_id"))))
                row["detail"]["pairing_issue"] = "order_observation_without_matching_intent"
            observe(event, row, role, status, payload, "recorded_order_status")
            if row is not None:
                if role == "entry":
                    row["entry_fill_status"] = status
                    if status in {"FILLED", "PARTIALLY_FILLED"} and row["activated_event_id"] is None:
                        row.update(status="entry_fill_reported", evaluation_status="awaiting_position_reconciliation")
                    elif status in terminal_states and row["activated_event_id"] is None:
                        row.update(status="entry_terminal_reported", evaluation_status="awaiting_position_reconciliation")
                elif role in {"exit", "stop", "target"} and status in {"FILLED", "PARTIALLY_FILLED"}:
                    row["exit_fill_status"] = status
                    if row["closed_event_id"] is None:
                        row["evaluation_status"] = "awaiting_close_reconciliation"
        elif kind in {"worker_failed", "read_timestamp_retry_started", "read_transport_retry_started"}:
            row = owner(payload)
            observe(event, row, "unknown", "UNKNOWN", payload, "worker_error_not_order_outcome")

    # Current state supplements the current cycle only; it is never training X/Y.
    phase = state.get("phase")
    current_client = _journal_text(state.get("entry_client_id") or state.get("pending_entry"))
    current = owner({"entry_client_id": current_client}) if current_client else (
        unresolved[0] if len(unresolved) == 1 else None)
    try:
        has_position = phase == "long" and _decimal(state.get("quantity")) > 0
    except ValueError:
        has_position = False
    if current is not None and current["status"] in {"closed", "unsent", "canceled", "expired", "rejected"}:
        current = None
    if phase == "long" or state.get("pending_entry"):
        if current is None:
            current = new_cycle(None, current_client)
            current["detail"]["pairing_issue"] = "current_state_without_complete_event_cycle"
        current["detail"]["current_state"] = {
            "phase": _journal_text(phase), "quantity": _journal_number(state.get("quantity")),
            "entry_price": _journal_number(state.get("entry_price")),
            "leverage": _journal_number(state.get("leverage")),
            "last_poll": _journal_number(state.get("last_poll")), "halted": _journal_text(state.get("halted")),
            "last_error": _journal_text(state.get("last_error"))}
        if has_position:
            current["status"] = "open"
            if current["activated_event_id"] is None:
                current["evaluation_status"] = "open_position_missing_activation_event"
                current["entry_fill_status"] = "POSITION_OBSERVED_IN_STATE"
                # Clearly marked state-only evidence, not a reconstructed fill.
                current["entry_quantity"] = _journal_number(state.get("quantity"))
                current["entry_price"] = _journal_number(state.get("entry_price"))
            for role, key in (("stop", "stop_client_id"), ("target", "target_client_id")):
                client = _journal_text(state.get(key))
                if client and not any(item["client_id"] == client for item in observations):
                    observe(None, current, role, "UNKNOWN", {"client_id": client}, "current_state_identifier_only")
        elif phase == "long":
            current.update(status="unknown", evaluation_status="source_state_event_disagreement")
    elif current is not None:
        current["detail"]["current_state_phase"] = _journal_text(phase)
        if phase == "cash" and current["status"] in {"pending", "open", "entry_fill_reported"}:
            current.update(status="unknown", evaluation_status="source_state_event_disagreement")
    if phase == "cash" and not state.get("pending_entry"):
        for row in unresolved:
            if row["status"] in {"pending", "open", "entry_fill_reported", "entry_terminal_reported"}:
                row.update(status="unknown", evaluation_status="source_state_event_disagreement")
                row["detail"]["current_state_phase"] = "cash"

    db.execute("UPDATE trade_journal SET valid=0")
    db.execute("UPDATE order_observations SET valid=0")
    for row in cycles:
        detail = _json(row["detail"])
        values = [detail if key == "detail" else value for key, value in row.items()]
        db.execute("INSERT OR REPLACE INTO trade_journal VALUES (" + ",".join("?" for _ in values) + ")", values)
    for row in observations:
        values = [_json(value) if key == "evidence" else value for key, value in row.items()]
        db.execute("INSERT OR REPLACE INTO order_observations VALUES (" + ",".join("?" for _ in values) + ")", values)
    report = _journal_empty(valid=True)
    report.update(cycle_count=len(cycles), closed_cycle_count=sum(row["status"] == "closed" for row in cycles),
                  open_cycle_count=sum(row["status"] == "open" for row in cycles),
                  pending_cycle_count=sum(row["status"] in {"pending", "entry_fill_reported", "entry_terminal_reported"} for row in cycles),
                  terminal_without_position_count=sum(row["evaluation_status"] == "terminal_without_position" for row in cycles),
                  evaluated_proxy_count=sum(row["sample_id"] is not None for row in cycles),
                  unknown_evaluation_count=sum(row["evaluation_status"] not in {"evaluated_wallet_proxy", "neutral_wallet_proxy",
                                                                              "terminal_without_position", "awaiting_entry_outcome",
                                                                              "awaiting_close"} for row in cycles),
                  order_observation_count=len(observations), current_trade=current,
                  recent_trades=cycles[-10:])
    return report


def _fit(samples):
    names = sorted({name for sample in samples for name in sample["features"]})
    means = [sum(row["features"].get(name, 0.0) for row in samples) / len(samples) for name in names]
    scales = [max(1e-9, math.sqrt(sum((row["features"].get(name, 0.0) - mean) ** 2
               for row in samples) / len(samples))) for name, mean in zip(names, means)]
    xs = [[1.0] + [max(-8.0, min(8.0, (row["features"].get(name, 0.0) - mean) / scale))
                    for name, mean, scale in zip(names, means, scales)] for row in samples]
    weights = [0.0] * (len(names) + 1)
    for _ in range(240):
        gradient = [0.0] * len(weights)
        for x, row in zip(xs, samples):
            prediction = _sigmoid(sum(a * b for a, b in zip(weights, x)))
            for index, value in enumerate(x):
                gradient[index] += (prediction - row["label"]) * value / len(samples)
        for index in range(len(weights)):
            weights[index] -= 0.08 * (gradient[index] + 0.25 * weights[index])
    return {"algorithm": ALGORITHM, "feature_names": names, "means": means,
            "scales": scales, "weights": weights, "regularization": 0.25,
            "label": "negative_account_wallet_proxy", **AUTHORITY}


def _sigmoid(value):
    return 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, value))))


def predict(model, features):
    """Shadow probability of a negative wallet proxy, not a trade recommendation."""
    values = [1.0] + [max(-8.0, min(8.0, (_number(features.get(name, 0.0)) - mean) / scale))
                      for name, mean, scale in zip(model["feature_names"], model["means"], model["scales"])]
    return _sigmoid(sum(weight * value for weight, value in zip(model["weights"], values)))


def _validate(samples):
    predictions = []
    for index in range(1, len(samples)):
        # Prior closes must precede this observation's entry, not merely its exit.
        prior = [row for row in samples[:index] if row["closed_at"] <= samples[index]["opened_at"]]
        if not prior:
            continue
        probability = predict(_fit(prior), samples[index]["features"])
        predictions.append({"sample_id": samples[index]["id"], "trained_sample_count": len(prior),
                            "loss_probability": probability, "label": samples[index]["label"]})
    count = len(predictions)
    return {"method": "chronological_prequential", "evaluation_scope": "retrospective_replay",
            "prediction_count": count,
            "predictions": predictions, "skipped_count": len(samples) - count,
            "brier_score": sum((row["loss_probability"] - row["label"]) ** 2 for row in predictions) / count if count else None,
            "status": "insufficient_evidence", "no_shuffled_split": True}


def _pre_entry_features(context):
    """Only values available before an order intent; never actual fill barriers."""
    if not isinstance(context, dict) or context.get("schema") != 1:
        raise ValueError("incompatible_entry_context")
    decision = context.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("missing_pre_entry_decision")
    ask = _positive(context["reference_ask"])
    atr = _decimal(context["atr"])
    if atr < 0:
        raise ValueError("negative_entry_atr")
    captured = _number(context["captured_at"])
    bar = _number(context["bar"])
    if captured < 0 or not 0 <= bar <= captured * 1000:
        raise ValueError("future_entry_bar")
    features = {"reference_ask": float(ask), "atr_fraction": float(atr / ask),
                "leverage": float(_positive(context["leverage"]))}
    for name in DECISION_NUMERIC:
        value = decision.get(name)
        if value is not None:
            features["decision." + name] = float(value) if isinstance(value, bool) else _number(value)
    for name, value in (("risk_profile", context.get("risk_profile")),
                        ("entry_regime", decision.get("entry_regime", "unknown")),
                        ("signal_profile", context.get("signal_profile", decision.get("signal_profile", "trend")))):
        if not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError("invalid_entry_category")
        features[name + "=" + value] = 1.0
    for value in features.values():
        _number(value)
    return features


def _context_digest(context):
    # The producer may attach the returned prediction to the original context.
    # Everything else in that original context is bound exactly, not reconstructed.
    return _hash({key: value for key, value in context.items() if key != "shadow_prediction"})


def _control_domain(context):
    features = _pre_entry_features(context)
    return {
        "leverage": format(_positive(context["leverage"]).normalize(), "f"),
        "risk_profile": next(name.split("=", 1)[1] for name in features if name.startswith("risk_profile=")),
        "signal_profile": next(name.split("=", 1)[1] for name in features if name.startswith("signal_profile=")),
    }


def _training_domains(samples):
    groups = {}
    for sample in samples:
        domain = _control_domain(sample["entry_context"])
        key = _json(domain)
        group = groups.setdefault(key, {**domain, "sample_count": 0, "label_classes": []})
        group["sample_count"] += 1
        group["label_classes"] = sorted(set(group["label_classes"] + [sample["label"]]))
    return [groups[key] for key in sorted(groups)]


def _control_evidence(artifact, context):
    """Eligibility for an explicit experimental adapter, never economic validation."""
    domain = _control_domain(context)
    result = {"schema": CONTROL_DOMAIN_SCHEMA, "requested_domain": domain,
              "domain_match": False, "domain_sample_count": 0, "label_classes": [],
              "economic_validation_passed": False}
    if artifact.get("control_domain_schema") != CONTROL_DOMAIN_SCHEMA:
        return {**result, "reason": "training_domain_metadata_unavailable"}
    for group in artifact.get("training_domains", []):
        if all(group.get(key) == value for key, value in domain.items()):
            return {**result, "domain_match": True,
                    "domain_sample_count": group["sample_count"],
                    "label_classes": group["label_classes"],
                    "reason": "matching_entry_domain"}
    return {**result, "reason": "no_matching_entry_domain"}


def _source_digest(db, through_event_id):
    return _hash(db.execute("SELECT id,payload_hash FROM events WHERE id<=? ORDER BY id",
                            (through_event_id,)).fetchall())


def _artifact(db, model_id, binding, at=None):
    row = db.execute("""SELECT dataset_hash,artifact,artifact_digest,created_at,
        source_fingerprint,trained_through_event_id,trained_through_at
        FROM pre_entry_models WHERE id=?""", (model_id,)).fetchone()
    if not row:
        raise ValueError("compatible_artifact_missing")
    data = json.loads(row[1])
    if not isinstance(data, dict) or _hash(data) != row[2]:
        raise ValueError("artifact_digest_mismatch")
    fields = ("dataset_hash", "created_at", "source_fingerprint", "trained_through_event_id", "trained_through_at")
    if any(data.get(name) != value for name, value in zip(fields, (row[0], *row[3:]))):
        raise ValueError("artifact_metadata_mismatch")
    if (data.get("model_id") != model_id or model_id != "pre-entry-" + row[0][:20] or
            data.get("feature_schema") != PRE_ENTRY_SCHEMA or data.get("algorithm") != ALGORITHM or
            data.get("source_fingerprint") != binding or
            any(data.get(key) is not False for key in AUTHORITY)):
        raise ValueError("artifact_binding_mismatch")
    cutoff = data["trained_through_event_id"]
    event = db.execute("SELECT ts,kind FROM events WHERE id=?", (cutoff,)).fetchone()
    if (not event or event[0] != data["trained_through_at"] or event[1] != "round_trip_closed" or
            data.get("source_digest") != _source_digest(db, cutoff)):
        raise ValueError("artifact_source_digest_mismatch")
    created = _number(data["created_at"])
    trained = _number(data["trained_through_at"])
    if created < trained:
        raise ValueError("artifact_created_before_training_outcome")
    model = data["model"]
    if "control_domain_schema" in data:
        domains = data.get("training_domains")
        if (data["control_domain_schema"] != CONTROL_DOMAIN_SCHEMA or
                not isinstance(domains, list) or not domains):
            raise ValueError("artifact_control_domains_invalid")
        seen_domains, domain_count = set(), 0
        for group in domains:
            if (not isinstance(group, dict) or type(group.get("sample_count")) is not int or
                    group["sample_count"] < 1 or group.get("label_classes") not in ([0], [1], [0, 1])):
                raise ValueError("artifact_control_domains_invalid")
            key = _json({name: group[name] for name in ("leverage", "risk_profile", "signal_profile")})
            if key in seen_domains or _positive(group["leverage"]) > 125:
                raise ValueError("artifact_control_domains_invalid")
            if any(not isinstance(group[name], str) or not group[name] for name in ("risk_profile", "signal_profile")):
                raise ValueError("artifact_control_domains_invalid")
            seen_domains.add(key)
            domain_count += group["sample_count"]
        if domain_count != data["trained_sample_count"]:
            raise ValueError("artifact_control_domains_count_mismatch")
    names = model["feature_names"]
    size = len(names)
    if (len(set(names)) != size or len(model["means"]) != size or len(model["scales"]) != size or
            len(model["weights"]) != size + 1 or model.get("algorithm") != ALGORITHM or
            any(model.get(key) is not False for key in AUTHORITY)):
        raise ValueError("artifact_model_shape_mismatch")
    allowed = {"reference_ask", "atr_fraction", "leverage", *("decision." + name for name in DECISION_NUMERIC)}
    if any(name not in allowed and not name.startswith(("risk_profile=", "entry_regime=", "signal_profile=")) for name in names):
        raise ValueError("artifact_feature_schema_mismatch")
    for value in model["weights"] + model["means"] + model["scales"]:
        _number(value)
    if any(value <= 0 for value in model["scales"]):
        raise ValueError("artifact_scale_invalid")
    if at is not None:
        at = _number(at)
        if created > at or trained > at:
            raise ValueError("future_artifact")
        if at - trained > MAX_MODEL_AGE_SECONDS:
            raise ValueError("stale_training_evidence")
    return data, row[2]


def predict_entry(learning_path, entry_context, now=None, *, source_path=None):
    """Read-only shadow score, suitable for persisting in an entry intent.

    This cannot grant trading authority. ``source_path`` lets the live producer
    additionally verify its current source binding before the next sidecar poll.
    """
    unavailable = {"available": False, "status": "unavailable", **AUTHORITY,
                   "feature_schema": PRE_ENTRY_SCHEMA}
    try:
        stamp = _number(time.time() if now is None else now)
        features = _pre_entry_features(entry_context)
        if _number(entry_context["captured_at"]) > stamp:
            raise ValueError("future_entry_context")
        with closing(_readonly(learning_path)) as db:
            db.execute("BEGIN")
            report = _get(db, "report", {})
            if _get(db, "conflict") or report.get("status") in {"source_error", "source_conflict"}:
                raise ValueError("source_unhealthy")
            model_id = _get(db, "pre_entry_model_id")
            if not model_id:
                raise ValueError("no_compatible_pre_entry_artifact")
            success = report.get("last_success_at")
            if success is None or not 0 <= stamp - _number(success) <= MAX_SOURCE_AGE_SECONDS:
                raise ValueError("stale_source_snapshot")
            binding = _get(db, "source_fingerprint")
            artifact, digest = _artifact(db, model_id, binding, stamp)
            if source_path is not None:
                _guard(source_path, learning_path)
                current_state, current, current_binding = _source(source_path)
                incoming = {event["id"]: event["hash"] for event in current}
                known = db.execute("SELECT id,payload_hash FROM events").fetchall()
                if current_binding != binding or any(incoming.get(event_id) != checksum for event_id, checksum in known):
                    raise ValueError("current_source_binding_or_events_changed")
                completed = current_state.get("completed_round_trips")
                if completed is not None and int(completed) != sum(event["kind"] == "round_trip_closed" for event in current):
                    raise ValueError("current_source_counter_conflict")
            return {"available": True, "status": "scored", **AUTHORITY,
                    "model_id": model_id, "feature_schema": PRE_ENTRY_SCHEMA,
                    "loss_probability": predict(artifact["model"], features),
                    "training_sample_count": artifact["trained_sample_count"],
                    "training_label_classes": artifact["label_classes"],
                    "single_label_class": len(artifact["label_classes"]) < 2,
                    "score_interpretation": "uncalibrated_exploratory_wallet_proxy_score",
                    "control_evidence": _control_evidence(artifact, entry_context),
                    "predicted_at": stamp, "context_digest": _context_digest(entry_context),
                    "feature_digest": _hash(features), "artifact_digest": digest,
                    "source_fingerprint": binding,
                    "trained_through_event_id": artifact["trained_through_event_id"],
                    "trained_through_at": artifact["trained_through_at"],
                    "model_created_at": artifact["created_at"], "pnl_basis": PROXY}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
        # Only controlled validation identifiers are safe to expose.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum() else "prediction_unavailable"
        return {**unavailable, "reason": reason}


def _readiness(model_id, compatible_count, classes, forward_count, extra_blockers=()):
    blockers = list(extra_blockers)
    if not model_id:
        blockers.append("legacy_features_only" if compatible_count == 0 else "no_compatible_pre_entry_model")
    if not forward_count:
        blockers.append("no_true_forward_predictions")
    if len(classes) < 2:
        blockers.append("single_label_class" if classes else "no_labeled_classes")
    blockers.extend(("no_verified_trade_pnl", "no_verified_fill_cost_funding_route", "no_frozen_policy_evaluation"))
    return {"ready_for_live": False, "technical_ready": bool(model_id) and not extra_blockers,
            "economic_evidence_ready": False, "blockers": blockers,
            "verified_evidence_route_supported": False, "automatic_promotion_supported": False,
            "blocking_capability": "Fill, fee and funding reconciliation and frozen-policy promotion are not implemented.",
            "required_economic_evidence": "Reconciled fills, fees, funding and attributable net trade PnL, followed by frozen-policy forward evaluation."}


def _early_learning(samples, *, valid=True):
    """Immediate descriptive evidence, separate from immutable model scores."""
    try:
        result = summarize_small_sample(samples)
    except (ValueError, TypeError, KeyError, OverflowError):
        result = {"status": "unavailable", "reason": "invalid_small_sample_evidence"}
        valid = False
    result.update(AUTHORITY)
    result["valid"] = valid
    result["training_cadence"] = {
        "first_training_at_closed_non_neutral_labels": 1,
        "retrain_every_new_closed_non_neutral_labels": 1,
        "fixed_days_required": 0,
        "unchanged_data_retrains": False,
        "pre_entry_requires_original_compatible_context": True,
    }
    return result


def _sync_pre_entry(db, samples, binding):
    compatible = []
    for sample in samples:
        context = sample.get("entry_context")
        if context is None:
            continue
        try:
            features = _pre_entry_features(context)
            context_digest = _context_digest(context)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
        compatible.append({**sample, "features": features, "context_digest": context_digest})
        db.execute("INSERT OR IGNORE INTO pre_entry_samples VALUES (?,?,?)",
                   (sample["id"], _json(features), context_digest))
    training = sorted((sample for sample in compatible if sample["label"] is not None),
                      key=lambda sample: (sample["closed_at"], sample["closed_event_id"]))
    model_id, classes, blockers = None, sorted({sample["label"] for sample in training}), []
    if training:
        dataset_hash = _hash([PRE_ENTRY_SCHEMA, ALGORITHM, CONTROL_DOMAIN_SCHEMA, binding,
                              [(sample["id"], sample["features"], sample["label"]) for sample in training]])
        model_id = "pre-entry-" + dataset_hash[:20]
        if not db.execute("SELECT 1 FROM pre_entry_models WHERE id=?", (model_id,)).fetchone():
            latest = max(training, key=lambda sample: sample["closed_event_id"])
            created = time.time()
            artifact = {"model_id": model_id, "dataset_hash": dataset_hash,
                        "feature_schema": PRE_ENTRY_SCHEMA, "algorithm": ALGORITHM,
                        "source_fingerprint": binding, "source_digest": _source_digest(db, latest["closed_event_id"]),
                        "trained_through_event_id": latest["closed_event_id"], "trained_through_at": latest["closed_at"],
                        "created_at": created, "trained_sample_count": len(training),
                        "control_domain_schema": CONTROL_DOMAIN_SCHEMA,
                        "training_domains": _training_domains(training),
                        "label_classes": classes, "model": _fit(training), **AUTHORITY}
            db.execute("INSERT INTO pre_entry_models VALUES (?,?,?,?,?,?,?,?)",
                       (model_id, dataset_hash, _json(artifact), _hash(artifact), created,
                        binding, latest["closed_event_id"], latest["closed_at"]))
        try:
            _artifact(db, model_id, binding, time.time())
        except (ValueError, TypeError, KeyError):
            blockers.append("invalid_or_stale_pre_entry_artifact")
            model_id = None
    _put(db, "pre_entry_model_id", model_id)
    # Resolve only predictions actually persisted before the source entry intent.
    # Replaying historical features through a later artifact never creates a row.
    for sample in compatible:
        prediction = sample["entry_context"].get("shadow_prediction")
        if not isinstance(prediction, dict) or prediction.get("available") is not True:
            continue
        try:
            predicted_at = _number(prediction["predicted_at"])
            if not _number(sample["entry_context"]["captured_at"]) <= predicted_at <= sample["opened_at"]:
                raise ValueError("forward_prediction_time_invalid")
            artifact, digest = _artifact(db, prediction["model_id"], binding, predicted_at)
            if (artifact["trained_through_event_id"] >= sample["entry_event_id"] or
                    artifact["trained_through_at"] > sample["opened_at"] or
                    prediction.get("feature_schema") != PRE_ENTRY_SCHEMA or
                    prediction.get("status") != "scored" or
                    prediction.get("context_digest") != sample["context_digest"] or
                    prediction.get("feature_digest") != _hash(sample["features"]) or
                    prediction.get("artifact_digest") != digest or
                    prediction.get("source_fingerprint") != binding or
                    prediction.get("trained_through_event_id") != artifact["trained_through_event_id"] or
                    prediction.get("trained_through_at") != artifact["trained_through_at"] or
                    prediction.get("model_created_at") != artifact["created_at"] or
                    any(prediction.get(key) is not False for key in AUTHORITY)):
                raise ValueError("forward_prediction_provenance_invalid")
            actual = _number(prediction["loss_probability"])
            expected = predict(artifact["model"], sample["features"])
            if not 0 <= actual <= 1 or abs(actual - expected) > 1e-12:
                raise ValueError("forward_prediction_score_invalid")
            db.execute("INSERT OR IGNORE INTO forward_predictions VALUES (?,?,?,?,?,?,?,?)",
                       (sample["id"], sample["entry_event_id"], sample["closed_event_id"],
                        prediction["model_id"], _json(prediction), sample["label"],
                        sample["wallet_delta_proxy_usdt"], PROXY))
        except (ValueError, TypeError, KeyError, OverflowError):
            db.execute("DELETE FROM forward_predictions WHERE id=?", (sample["id"],))
            db.execute("INSERT OR IGNORE INTO quarantine VALUES (?,?,?)",
                       (_hash(["forward", sample["id"]]), "invalid_forward_prediction",
                        _json({"sample_id": sample["id"], "entry_event_id": sample["entry_event_id"]})))
    forward_count = db.execute("SELECT COUNT(*) FROM forward_predictions").fetchone()[0]
    return {"pre_entry_model_id": model_id, "pre_entry_feature_schema": PRE_ENTRY_SCHEMA,
            "compatible_sample_count": len(compatible), "pre_entry_trained_sample_count": len(training),
            "pre_entry_label_classes": classes, "true_forward_proxy_count": forward_count,
            "true_forward_verified_count": 0,
            "readiness": _readiness(model_id, len(compatible), classes, forward_count, blockers)}


def _report(db):
    result = _get(db, "report", {"status": "collecting", "sample_count": 0,
                               "proxy_count": 0, "verified_count": 0, "model_id": None,
                               "trained_sample_count": 0, "label_classes": []})
    result.update(AUTHORITY)
    result.setdefault("pre_entry_model_id", None)
    result.setdefault("compatible_sample_count", 0)
    result.setdefault("true_forward_proxy_count", 0)
    result.setdefault("true_forward_verified_count", 0)
    result.setdefault("readiness", _readiness(None, 0, [], 0))
    result.setdefault("journal", _journal_empty())
    result.setdefault("early_learning", _early_learning([], valid=False))
    result.setdefault("execution_adapter", {"enabled_in_source": False, "last_decision": None})
    now = time.time()
    result["observed_at"] = now
    success = result.get("last_success_at")
    result["seconds_since_success"] = max(0.0, now - success) if success is not None else None
    result["health"] = ("error" if result["status"] in {"source_error", "source_conflict"}
                        else "healthy" if success and now - success <= 120 else "stale")
    result["early_learning"]["source_health"] = result["health"]
    result["execution_adapter"]["source_health"] = result["health"]
    result["early_learning"]["usable_for_review"] = (
        result["health"] == "healthy" and result["early_learning"].get("valid", False))
    if result["health"] != "healthy":
        result["readiness"] = {**result["readiness"], "technical_ready": False,
                               "blockers": list(dict.fromkeys(result["readiness"]["blockers"] + ["source_" + result["health"]]))}
    if result["pre_entry_model_id"]:
        try:
            _artifact(db, result["pre_entry_model_id"], _get(db, "source_fingerprint"), now)
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError, AttributeError):
            result["pre_entry_model_id"] = None
            result["readiness"] = {**result["readiness"], "technical_ready": False,
                                   "blockers": list(dict.fromkeys(result["readiness"]["blockers"] + ["invalid_or_stale_pre_entry_artifact"]))}
    return result


def status(learning_path=DB):
    """Read the separate learner status without modifying either database."""
    with closing(_readonly(learning_path)) as db:
        return _report(db)


def _invalidate(db, reason, conflict=False):
    previous = _get(db, "report", {})
    db.execute("UPDATE trade_journal SET valid=0")
    db.execute("UPDATE order_observations SET valid=0")
    if conflict:
        for sample_id, in db.execute("SELECT id FROM samples").fetchall():
            db.execute("INSERT OR IGNORE INTO quarantine VALUES (?,?,?)",
                       (_hash(["invalidated", sample_id]), "source_conflict", _json({"sample_id": sample_id})))
        db.execute("DELETE FROM samples")
        db.execute("DELETE FROM pre_entry_samples")
        db.execute("DELETE FROM forward_predictions")
        _put(db, "conflict", reason)
    _put(db, "pre_entry_model_id", None)
    report = {"status": "source_conflict" if conflict else "source_error", **AUTHORITY,
              "sample_count": 0, "proxy_count": 0, "verified_count": 0,
              "model_id": None, "trained_sample_count": 0, "label_classes": [],
              "training_runs": db.execute("SELECT COUNT(*) FROM models").fetchone()[0],
              "quarantined_count": db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
              "last_attempt_at": time.time(), "last_error_at": time.time(),
              "last_success_at": previous.get("last_success_at"), "error": reason,
              "validation": {"method": "chronological_prequential", "prediction_count": 0, "status": "invalidated"},
              "recent_outcomes": [], "learned_loss_pattern": "Current evidence is unavailable or invalidated; no active candidate."}
    report.update({"pre_entry_model_id": None, "compatible_sample_count": 0,
                   "true_forward_proxy_count": 0, "true_forward_verified_count": 0,
                   "readiness": _readiness(None, 0, [], 0, ("source_unhealthy",)),
                   "journal": _journal_empty(),
                   "early_learning": _early_learning([], valid=False),
                   "execution_adapter": {"enabled_in_source": False, "last_decision": None}})
    _put(db, "report", report)
    db.commit()
    return _report(db)


def sync(source_path=SOURCE, learning_path=DB):
    """Idempotently import complete cycles and fit one shadow candidate per dataset."""
    source_path, learning_path = _guard(source_path, learning_path)
    with closing(_connect(learning_path)) as db:
        db.execute("BEGIN IMMEDIATE")
        if _get(db, "conflict"):
            return _invalidate(db, _get(db, "conflict"), conflict=True)
        try:
            state, events, binding = _source(source_path)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # Never return exception text: it can contain private ledger payloads.
            return _invalidate(db, "source_read_or_schema_error")
        old_binding = _get(db, "source_fingerprint")
        if old_binding and old_binding != binding:
            return _invalidate(db, "source_binding_or_epoch_changed", conflict=True)
        known = {row[0]: row[1] for row in db.execute("SELECT id,payload_hash FROM events")}
        incoming = {event["id"]: event["hash"] for event in events}
        if any(incoming.get(event_id) != checksum for event_id, checksum in known.items()):
            return _invalidate(db, "source_event_changed_or_deleted", conflict=True)
        configured = [event for event in events if event["kind"] == "configured"]
        if len(configured) > 1:
            return _invalidate(db, "multiple_source_epochs", conflict=True)
        closes = sum(event["kind"] == "round_trip_closed" for event in events)
        if state.get("completed_round_trips") is not None:
            try:
                if int(state["completed_round_trips"]) != closes:
                    return _invalidate(db, "source_close_counter_conflict", conflict=True)
            except (ValueError, TypeError):
                return _invalidate(db, "source_close_counter_malformed", conflict=True)
        _put(db, "source_fingerprint", binding)
        samples, quarantine = _reconstruct(state, events, binding)
        db.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?)",
                       [(event["id"], event["ts"], event["kind"], event["hash"]) for event in events])
        db.executemany("INSERT OR IGNORE INTO quarantine VALUES (?,?,?)", quarantine)
        for sample in samples:
            db.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (sample["id"], sample["entry_event_id"], sample["activated_event_id"],
                        sample["closed_event_id"], sample["opened_at"], sample["closed_at"],
                        _json(sample["features"]), sample["label"], sample["wallet_delta_proxy_usdt"],
                        sample["quality"], _json(sample["provenance"])))
        training = sorted((row for row in samples if row["label"] is not None),
                          key=lambda row: (row["closed_at"], row["closed_event_id"]))
        model_id = None
        validation = {"method": "chronological_prequential", "prediction_count": 0,
                      "predictions": [], "status": "insufficient_evidence"}
        model = None
        if training:
            dataset_hash = _hash([ALGORITHM, [(row["id"], row["features"], row["label"]) for row in training]])
            model_id = "shadow-" + dataset_hash[:20]
            row = db.execute("SELECT model,validation FROM models WHERE dataset_hash=?", (dataset_hash,)).fetchone()
            if row:
                model, validation = json.loads(row[0]), json.loads(row[1])
            else:
                model, validation = _fit(training), _validate(training)
                db.execute("INSERT INTO models VALUES (?,?,?,?,?,?)",
                           (model_id, dataset_hash, time.time(), len(training), _json(model), _json(validation)))
        classes = sorted({sample["label"] for sample in training})
        loss_count = sum(row["label"] == 1 for row in training)
        reason = (f"{loss_count}/{len(training)} labeled account wallet proxies are negative. "
                  + ("Only one label class has been observed. " if len(classes) == 1 else "")
                  + "Transfers, funding, fees and other positions can affect these proxies; "
                    "this provisional fit does not establish a profitable or causal trading pattern.")
        previous = _get(db, "report", {})
        pre_entry_report = _sync_pre_entry(db, samples, binding)
        journal_report = _sync_journal(db, state, events, binding, samples)
        report = {"status": "trained_proxy_insufficient_evidence" if training else "collecting",
                  **AUTHORITY, "live_strategy": "unchanged", "pnl_basis": PROXY, "sample_count": len(samples),
                  "proxy_count": len(samples), "verified_count": 0,
                  "neutral_count": len(samples) - len(training), "model_id": model_id,
                  "trained_sample_count": len(training), "label_classes": classes,
                  "single_class": len(classes) == 1, "training_runs": db.execute("SELECT COUNT(*) FROM models").fetchone()[0],
                  "quarantined_count": db.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0],
                  "validation": validation, "learned_loss_pattern": reason,
                  "model_feature_names": model["feature_names"] if model else [],
                  "recent_entry_shadow_loss_probability": predict(model, training[-1]["features"]) if model else None,
                  "prediction_scope": "in_sample_entry_example_not_live_forecast",
                  "recent_outcomes": [{"sample_id": row["id"], "closed_at": row["closed_at"],
                                       "wallet_delta_proxy_usdt": row["wallet_delta_proxy_usdt"],
                                       "label": row["label"], "quality": row["quality"],
                                       "execution_verified": False, "pnl_basis": PROXY}
                                      for row in samples[-10:]],
                  "last_attempt_at": time.time(), "last_success_at": time.time(),
                  "last_error_at": previous.get("last_error_at"), "error": None}
        report.update(pre_entry_report)
        report["journal"] = journal_report
        report["early_learning"] = _early_learning(samples)
        report["execution_adapter"] = {
            "enabled_in_source": state.get("model_decisions_enabled") is True,
            "scope": "experimental_baseline_entry_accept_or_defer",
            "authority_location": "user_operated_execution_worker_not_learner",
            "last_decision": state.get("model_control_latest"),
            "economic_validation_passed": False,
        }
        _put(db, "report", report)
        db.commit()
        return _report(db)


@contextmanager
def _watch_lock(path):
    """Lifetime OS lock; process exit releases it, including after a crash."""
    lock_path = Path(str(_path(path)) + ".watch.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError:
        if not locked:
            raise ValueError("a learner watcher already owns this database") from None
        raise
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _write_report(path, report, source_path, learning_path):
    _guard(source_path, learning_path, path)
    path = _path(path)
    temporary = Path(str(path) + ".tmp")
    _guard(source_path, learning_path, temporary)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "status", "watch"))
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args(argv)
    _guard(args.source, args.db, args.report)
    if not math.isfinite(args.poll_seconds) or not 1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be finite and between 1 and 60")

    def run(*, emit=True):
        report = status(args.db) if args.command == "status" else sync(args.source, args.db)
        if args.report:
            _write_report(args.report, report, args.source, args.db)
        if emit:
            print(json.dumps(report, sort_keys=True, allow_nan=False), flush=True)
        return report

    if args.command != "watch":
        run()
        return
    _guard(args.source, args.db, Path(str(_path(args.db)) + ".watch.lock"))
    if args.report:
        _guard(args.report, Path(str(_path(args.db)) + ".watch.lock"))
    with _watch_lock(args.db):
        previous_fingerprint = None
        while True:
            report = run(emit=False)
            fingerprint = _hash({key: report.get(key) for key in (
                "status", "model_id", "sample_count", "quarantined_count", "error",
                "pre_entry_model_id", "true_forward_proxy_count", "readiness")}
                | {"journal": {key: report.get("journal", {}).get(key) for key in (
                    "valid", "cycle_count", "closed_cycle_count", "open_cycle_count",
                    "pending_cycle_count", "order_observation_count", "unknown_evaluation_count")}})
            if fingerprint != previous_fingerprint:
                print(json.dumps(report, sort_keys=True, allow_nan=False), flush=True)
                previous_fingerprint = fingerprint
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
