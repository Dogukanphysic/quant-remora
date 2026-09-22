"""Conservative background trader for Binance Spot Testnet.

The worker is deliberately isolated from the paper engines.  It owns one
SQLite database, one process lock, and at most one BTCUSDT position acquired by
this worker.  Exchange/account BTC balances are never used to infer position
state because Spot Testnet accounts are prefunded.

Credentials are read by :class:`binance_execution.Client` from the inherited
process environment.  They are never accepted by this module's CLI, stored in
SQLite, or written to logs.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Callable, Iterator, Mapping, Sequence
import uuid

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised on non-Windows CI/hosts
    import fcntl

import binance_execution as execution
import testnet_learning_store as learning_store


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"

HOURLY_MODE = os.getenv("BINANCE_TESTNET_POLICY_MODE", "daily") == "hourly"
POLICY_CONFIG_PATH = ROOT / "config" / ("binance-testnet-hourly-policy.json" if HOURLY_MODE else "binance-testnet-active-policy.json")


def _load_policy_config(path: Path = POLICY_CONFIG_PATH) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Binance Testnet policy config cannot be loaded: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("Binance Testnet policy config has an invalid schema.")
    if value.get("kind") != "testnet_execution_policy_config":
        raise ValueError("Binance Testnet policy config has an invalid kind.")
    if value.get("environment") != "binance_spot_testnet":
        raise ValueError("Binance Testnet policy config targets another environment.")
    if value.get("market_data_source") != "binance_public_spot":
        raise ValueError("Binance Testnet policy config has an invalid data source.")
    if value.get("status") != "testnet_exploration_candidate":
        raise ValueError("Binance Testnet policy is not an exploration candidate.")
    if value.get("testnet_only") is not True or value.get(
        "testnet_execution_eligible"
    ) is not True:
        raise ValueError("Binance Testnet policy is not execution-eligible for Testnet.")
    for flag in (
        "paper_eligible", "real_money_eligible", "real_orders_enabled",
        "live_trading_enabled",
    ):
        if value.get(flag) is not False:
            raise ValueError(f"Binance Testnet policy must keep {flag}=false.")

    policy_id = value.get("policy_id")
    model_version = value.get("model_version")
    if not isinstance(policy_id, str) or not re.fullmatch(r"[a-z0-9_]{8,80}", policy_id):
        raise ValueError("Binance Testnet policy id is invalid.")
    expected_policy = "btc_hourly_momentum_24h_t002_testnet_v1" if HOURLY_MODE else "btc_daily_momentum_30d_t10_testnet_v1"
    if policy_id != expected_policy:
        raise ValueError("Binance Testnet policy id is not approved by this worker build.")
    if not isinstance(model_version, str) or not re.fullmatch(
        r"[0-9a-f]{64}", model_version
    ):
        raise ValueError("Binance Testnet policy model version is invalid.")

    rule = value.get("rule")
    order = value.get("order")
    if not isinstance(rule, dict) or rule.get("family") != "momentum":
        raise ValueError("Binance Testnet policy rule is invalid.")
    if int(rule.get("lookback_hours" if HOURLY_MODE else "lookback_days", 0)) != (24 if HOURLY_MODE else 30):
        raise ValueError("Binance Testnet policy must use the registered 30-day lookback.")
    try:
        threshold = Decimal(str(rule.get("threshold")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Binance Testnet policy threshold is invalid.") from exc
    if not threshold.is_finite() or threshold != Decimal("0.002" if HOURLY_MODE else "0.10"):
        raise ValueError("Binance Testnet policy threshold must match trained t10.")
    if not isinstance(order, dict) or order.get("symbol") != "BTCUSDT" or order.get(
        "mode"
    ) != "long_cash":
        raise ValueError("Binance Testnet order policy is invalid.")
    try:
        quote = Decimal(str(order.get("quote_usdt")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Binance Testnet quote size is invalid.") from exc
    if not quote.is_finite() or quote != Decimal("10"):
        raise ValueError("Binance Testnet quote size must match trained 10 USDT pilot.")
    override = value.get("testnet_sizing_override")
    if override is not None:
        if not isinstance(override, dict) or override != {
            "quote_usdt": "15",
            "scope": "next_entries_only",
            "authorization": "user_requested_testnet_risk_increase",
        }:
            raise ValueError("Only the explicit 15 USDT Testnet sizing override is supported.")
    return value


POLICY_CONFIG = _load_policy_config()
POLICY_ID = str(POLICY_CONFIG["policy_id"])
POLICY_MODEL_VERSION = str(POLICY_CONFIG["model_version"])
POLICY = POLICY_ID
POLICY_SPEC_HASH = POLICY_MODEL_VERSION
_LEDGER_STEM = f"binance-testnet-{POLICY_ID}-{POLICY_MODEL_VERSION[:12]}"
DB_PATH = STATE_DIR / f"{_LEDGER_STEM}.sqlite3"
LOCK_PATH = STATE_DIR / f"{_LEDGER_STEM}.lock"
# This lock deliberately stays stable across policy/model versions.  Otherwise an
# operator could start the old ledger while a retrain swaps the active config and
# the new ledger starts under a different policy-specific lock.
CONTROL_LOCK_PATH = STATE_DIR / "binance-testnet-worker-control.lock"
# A running worker holds this second, account-wide lease for its entire lifetime.
# Policy-specific locks remain useful for status, while this lease prevents two
# different policy ledgers from trading the same Testnet account concurrently.
ACCOUNT_LOCK_PATH = STATE_DIR / "binance-testnet-worker-account.lock"
LEARNING_DB_PATH = STATE_DIR / f"{_LEDGER_STEM}-online-learning.sqlite3"

SYMBOL = "BTCUSDT"
BASE_ASSET = "BTC"
QUOTE_ASSET = "USDT"
INTERVAL = os.getenv("BINANCE_TESTNET_DECISION_INTERVAL", "1h") if HOURLY_MODE else "1d"
if INTERVAL not in ({"1h", "15m"} if HOURLY_MODE else {"1d"}):
    raise ValueError("Unsupported decision interval")
LOOKBACK_DAYS = (96 if INTERVAL == "15m" else int(POLICY_CONFIG["rule"]["lookback_hours" if HOURLY_MODE else "lookback_days"]))
MOMENTUM_THRESHOLD = Decimal(str(POLICY_CONFIG["rule"]["threshold"]))
ENTRY_QUOTE_USDT = Decimal(str(
    POLICY_CONFIG.get("testnet_sizing_override", POLICY_CONFIG["order"])["quote_usdt"]
))
POLL_SECONDS = 5.0
API_POLL_SECONDS = 60.0
DAY_MS = 900_000 if INTERVAL == "15m" else 3_600_000 if HOURLY_MODE else 86_400_000
MAX_CANDLE_AGE_MS = (25*60*1000) if INTERVAL == "15m" else (90 * 60 * 1000) if HOURLY_MODE else (36 * 60 * 60 * 1000)
STARTUP_WAIT_SECONDS = 3.0
STOP_WAIT_SECONDS = 20.0
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_LEARNING_OUTBOX_SCHEMA = 1
_LEARNING_OUTBOX_OPERATIONS = frozenset(
    {
        "capture_daily_label",
        "open_round_trip",
        "close_round_trip",
        "epoch_reset_quarantine",
    }
)
_LEARNING_SOURCE_CALL_FAILED = object()


class WorkerHalt(RuntimeError):
    """A fail-closed condition requiring inspection or manual intervention."""


class WorkerStopped(RuntimeError):
    """The operator disabled new decisions before an intent could be submitted."""


class AccountBindingError(WorkerHalt):
    """The supplied API key cannot safely own this execution ledger."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decimal(value: object, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WorkerHalt(f"Invalid {field}: {value!r}") from exc
    if not number.is_finite():
        raise WorkerHalt(f"Invalid {field}: {value!r}")
    return number


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        _ensure_schema(db)
    except BaseException:
        db.close()
        raise
    return db


def _connect_read_only(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open an existing worker ledger without running schema or recovery writes."""

    db_path = Path(path)
    # ``mode=ro`` prevents an accidental create when an operator asks for
    # status before the worker has initialized its ledger.  ``query_only`` is
    # a second guard against writes through this connection.
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    db = sqlite3.connect(
        uri,
        uri=True,
        timeout=15,
        isolation_level=None,
    )
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
    except BaseException:
        db.close()
        raise
    return db


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS worker_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            active_policy TEXT NOT NULL DEFAULT '',
            policy_spec_hash TEXT NOT NULL DEFAULT '',
            policy_started_ms INTEGER,
            api_key_fingerprint_sha256 TEXT,
            desired_running INTEGER NOT NULL DEFAULT 0 CHECK (desired_running IN (0,1)),
            halted INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0,1)),
            halt_reason TEXT,
            position_qty TEXT NOT NULL DEFAULT '0',
            position_quote_cost TEXT NOT NULL DEFAULT '0',
            last_candle_close_ms INTEGER,
            pending_client_id TEXT,
            pending_side TEXT CHECK (pending_side IS NULL OR pending_side IN ('BUY','SELL')),
            pending_decision_ms INTEGER,
            pending_quote TEXT,
            pending_qty TEXT,
            last_error TEXT,
            realized_pnl_usdt TEXT NOT NULL DEFAULT '0',
            pnl_complete INTEGER NOT NULL DEFAULT 1 CHECK (pnl_complete IN (0,1)),
            pnl_incomplete_reason TEXT,
            completed_round_trips INTEGER NOT NULL DEFAULT 0,
            transient_failures INTEGER NOT NULL DEFAULT 0,
            last_transient_error_ms INTEGER,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_decisions (
            candle_close_ms INTEGER PRIMARY KEY,
            policy TEXT NOT NULL,
            close_latest TEXT NOT NULL,
            close_30d TEXT NOT NULL,
            momentum TEXT NOT NULL,
            feature_schema TEXT,
            feature_json TEXT,
            target_long INTEGER NOT NULL CHECK (target_long IN (0,1)),
            action TEXT NOT NULL,
            client_id TEXT,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_order_intents (
            client_id TEXT PRIMARY KEY,
            policy TEXT NOT NULL,
            decision_ms INTEGER NOT NULL,
            candle_close_ms INTEGER NOT NULL,
            symbol TEXT NOT NULL CHECK (symbol = 'BTCUSDT'),
            side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
            requested_quote TEXT,
            requested_qty TEXT,
            state TEXT NOT NULL,
            exchange_order_id TEXT,
            executed_qty TEXT,
            net_base_qty TEXT,
            cumulative_quote_qty TEXT,
            realized_pnl_usdt TEXT,
            commission_by_asset TEXT,
            response_json TEXT,
            error TEXT,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL,
            FOREIGN KEY (candle_close_ms) REFERENCES worker_decisions(candle_close_ms)
        );

        CREATE INDEX IF NOT EXISTS worker_intents_state_idx
            ON worker_order_intents(state, updated_ms);

        CREATE TABLE IF NOT EXISTS worker_epochs (
            epoch_id INTEGER PRIMARY KEY AUTOINCREMENT,
            archived_ms INTEGER NOT NULL,
            reason TEXT NOT NULL,
            state_json TEXT NOT NULL,
            decision_count INTEGER NOT NULL,
            order_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_epoch_decisions (
            epoch_id INTEGER NOT NULL,
            candle_close_ms INTEGER NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY (epoch_id, candle_close_ms),
            FOREIGN KEY (epoch_id) REFERENCES worker_epochs(epoch_id)
        );

        CREATE TABLE IF NOT EXISTS worker_epoch_order_intents (
            epoch_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            record_json TEXT NOT NULL,
            PRIMARY KEY (epoch_id, client_id),
            FOREIGN KEY (epoch_id) REFERENCES worker_epochs(epoch_id)
        );

        CREATE TABLE IF NOT EXISTS worker_learning_outbox (
            event_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL CHECK (
                operation IN (
                    'capture_daily_label',
                    'open_round_trip',
                    'close_round_trip',
                    'epoch_reset_quarantine'
                )
            ),
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_ms INTEGER NOT NULL,
            last_attempt_ms INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (attempt_count >= 0),
            resolved_ms INTEGER,
            last_error TEXT
        );

        CREATE INDEX IF NOT EXISTS worker_learning_outbox_pending_idx
            ON worker_learning_outbox(resolved_ms, created_ms);
        """
    )
    # Additive migration for databases created before realized-P&L accounting.
    state_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(worker_state)")
    }
    if "realized_pnl_usdt" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "realized_pnl_usdt TEXT NOT NULL DEFAULT '0'"
        )
    if "completed_round_trips" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "completed_round_trips INTEGER NOT NULL DEFAULT 0"
        )
    if "pnl_complete" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "pnl_complete INTEGER NOT NULL DEFAULT 1"
        )
    if "pnl_incomplete_reason" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN pnl_incomplete_reason TEXT"
        )
    if "transient_failures" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "transient_failures INTEGER NOT NULL DEFAULT 0"
        )
    if "last_transient_error_ms" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN last_transient_error_ms INTEGER"
        )
    if "active_policy" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN active_policy TEXT NOT NULL DEFAULT ''"
        )
    if "policy_spec_hash" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN "
            "policy_spec_hash TEXT NOT NULL DEFAULT ''"
        )
    if "policy_started_ms" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN policy_started_ms INTEGER"
        )
    if "api_key_fingerprint_sha256" not in state_columns:
        db.execute(
            "ALTER TABLE worker_state ADD COLUMN api_key_fingerprint_sha256 TEXT"
        )
    intent_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(worker_order_intents)")
    }
    if "realized_pnl_usdt" not in intent_columns:
        db.execute(
            "ALTER TABLE worker_order_intents ADD COLUMN realized_pnl_usdt TEXT"
        )
    if "commission_by_asset" not in intent_columns:
        db.execute(
            "ALTER TABLE worker_order_intents ADD COLUMN commission_by_asset TEXT"
        )
    decision_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(worker_decisions)")
    }
    if "feature_schema" not in decision_columns:
        db.execute("ALTER TABLE worker_decisions ADD COLUMN feature_schema TEXT")
    if "feature_json" not in decision_columns:
        db.execute("ALTER TABLE worker_decisions ADD COLUMN feature_json TEXT")
    now = _now_ms()
    db.execute(
        """INSERT OR IGNORE INTO worker_state
           (singleton, active_policy, policy_spec_hash, policy_started_ms,
            created_ms, updated_ms) VALUES (1, ?, ?, ?, ?, ?)""",
        (POLICY, POLICY_SPEC_HASH, now, now, now),
    )
    _bind_or_validate_policy(db, now)
    try:
        learning_store.ensure_source_schema(
            db, POLICY, POLICY_SPEC_HASH, now
        )
    except Exception:
        # The proposal-only learner is a sidecar. Its schema must never make
        # the execution ledger unavailable or halt an otherwise safe cycle.
        pass


def _bind_or_validate_policy(db: sqlite3.Connection, now_ms: int) -> None:
    """Bind a pristine ledger once and reject silent policy mixing thereafter."""

    state = db.execute(
        "SELECT * FROM worker_state WHERE singleton=1"
    ).fetchone()
    if state is None:  # pragma: no cover - schema insertion immediately precedes this
        raise WorkerHalt("Worker state is missing during policy binding.")
    active_policy = str(state["active_policy"] or "")
    spec_hash = str(state["policy_spec_hash"] or "")
    if active_policy == "" and spec_hash == "":
        decisions = int(db.execute("SELECT COUNT(*) FROM worker_decisions").fetchone()[0])
        intents = int(db.execute("SELECT COUNT(*) FROM worker_order_intents").fetchone()[0])
        dirty = bool(
            decisions or intents or state["desired_running"] or state["halted"]
            or state["last_candle_close_ms"] is not None
            or state["pending_client_id"] is not None
            or _decimal(state["position_qty"], "tracked position") != 0
            or _decimal(state["realized_pnl_usdt"], "realized P&L") != 0
        )
        if dirty:
            raise WorkerHalt(
                "An existing Binance Testnet ledger has no policy binding; "
                "explicit archival or migration is required."
            )
        db.execute(
            """UPDATE worker_state SET active_policy=?, policy_spec_hash=?,
               policy_started_ms=?, updated_ms=? WHERE singleton=1""",
            (POLICY, POLICY_SPEC_HASH, now_ms, now_ms),
        )
        return
    if active_policy != POLICY or spec_hash != POLICY_SPEC_HASH:
        raise WorkerHalt(
            "Binance Testnet ledger policy does not match the configured policy; "
            "refusing to mix policy epochs."
        )


@contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def _safe_learning_source_call(
    db: sqlite3.Connection,
    operation: str,
    function: Callable[..., object],
    *args: object,
    outbox_reference: Mapping[str, object] | None = None,
    **kwargs: object,
) -> object | None:
    """Isolate learning writes so they can never break execution accounting."""

    savepoint = "worker_learning_sidecar"
    db.execute(f"SAVEPOINT {savepoint}")
    try:
        result = function(*args, **kwargs)
    except Exception as exc:
        db.execute(f"ROLLBACK TO {savepoint}")
        db.execute(f"RELEASE {savepoint}")
        if outbox_reference is not None:
            _enqueue_learning_outbox(
                db,
                operation,
                outbox_reference,
                exc,
                _now_ms(),
            )
        if isinstance(exc, learning_store.LearningIntegrityError):
            try:
                learning_store.record_source_error(
                    db,
                    f"{operation}: {type(exc).__name__}: {exc}",
                    _now_ms(),
                )
            except Exception:
                pass
        return _LEARNING_SOURCE_CALL_FAILED
    db.execute(f"RELEASE {savepoint}")
    return result


def _learning_outbox_payload(
    operation: str, reference: Mapping[str, object]
) -> tuple[str, str, str]:
    if operation not in _LEARNING_OUTBOX_OPERATIONS:
        raise ValueError(f"Unsupported learning outbox operation: {operation}")
    payload = {
        "schema": _LEARNING_OUTBOX_SCHEMA,
        "operation": operation,
        "reference": dict(reference),
    }
    encoded = _json(payload)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest, f"learning-outbox:{digest}"


def _learning_outbox_error(exc: BaseException) -> str:
    """Return a bounded diagnostic that cannot accidentally persist a secret."""

    return f"{type(exc).__name__}: learning sidecar operation failed"


def _enqueue_learning_outbox(
    db: sqlite3.Connection,
    operation: str,
    reference: Mapping[str, object],
    exc: BaseException,
    now_ms: int,
) -> str:
    encoded, digest, event_id = _learning_outbox_payload(operation, reference)
    db.execute(
        """INSERT OR IGNORE INTO worker_learning_outbox
           (event_id, operation, payload_json, payload_sha256, created_ms,
            last_attempt_ms, attempt_count, resolved_ms, last_error)
           VALUES (?, ?, ?, ?, ?, ?, 1, NULL, ?)""",
        (
            event_id,
            operation,
            encoded,
            digest,
            int(now_ms),
            int(now_ms),
            _learning_outbox_error(exc),
        ),
    )
    return event_id


def _durable_learning_source_call(
    db: sqlite3.Connection,
    operation: str,
    function: Callable[..., object],
    *args: object,
    outbox_reference: Mapping[str, object],
    **kwargs: object,
) -> object | None:
    """Preserve source-event order and enqueue every uncommitted mutation."""

    if (
        not _learning_source_writes_healthy(db)
        or _unresolved_learning_outbox_count(db)
    ):
        _enqueue_learning_outbox(
            db,
            operation,
            outbox_reference,
            RuntimeError("prior learning evidence is unresolved"),
            _now_ms(),
        )
        return _LEARNING_SOURCE_CALL_FAILED
    return _safe_learning_source_call(
        db,
        operation,
        function,
        *args,
        outbox_reference=outbox_reference,
        **kwargs,
    )


def _learning_source_writes_healthy(db: sqlite3.Connection) -> bool:
    """Permit direct evidence writes only after affirmative refresh recovery."""

    try:
        health = db.execute(
            """SELECT last_attempt_failed, last_success_ms
               FROM worker_learning_refresh_health WHERE singleton=1"""
        ).fetchone()
        if (
            health is None
            or bool(health["last_attempt_failed"])
            or health["last_success_ms"] is None
        ):
            return False
        transition_table = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table'
                 AND name='worker_learning_pending_candidate_transition'"""
        ).fetchone()
        if transition_table is not None:
            pending = db.execute(
                """SELECT 1
                   FROM worker_learning_pending_candidate_transition
                   LIMIT 1"""
            ).fetchone()
            if pending is not None:
                return False
    except (KeyError, sqlite3.DatabaseError):
        return False
    return True


def _unresolved_learning_outbox_count(db: sqlite3.Connection) -> int:
    return int(
        db.execute(
            """SELECT COUNT(*) FROM worker_learning_outbox
               WHERE resolved_ms IS NULL"""
        ).fetchone()[0]
    )


def _parse_learning_outbox_row(row: sqlite3.Row) -> tuple[str, dict[str, object]]:
    operation = str(row["operation"])
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError) as exc:
        raise learning_store.LearningIntegrityError(
            "Learning outbox payload is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise learning_store.LearningIntegrityError(
            "Learning outbox payload is not an object."
        )
    canonical = _json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    expected_event_id = f"learning-outbox:{digest}"
    if (
        canonical != str(row["payload_json"])
        or digest != str(row["payload_sha256"])
        or expected_event_id != str(row["event_id"])
        or operation not in _LEARNING_OUTBOX_OPERATIONS
        or payload.get("schema") != _LEARNING_OUTBOX_SCHEMA
        or payload.get("operation") != operation
        or not isinstance(payload.get("reference"), dict)
    ):
        raise learning_store.LearningIntegrityError(
            "Learning outbox identity or canonical payload is invalid."
        )
    reference = dict(payload["reference"])
    if reference.get("policy") != POLICY or reference.get(
        "model_version"
    ) != POLICY_SPEC_HASH:
        raise learning_store.LearningIntegrityError(
            "Learning outbox policy identity does not match this worker."
        )
    return operation, reference


def _replay_learning_outbox(db: sqlite3.Connection) -> int:
    """Idempotently repair durable sidecar events from core ledger facts."""

    rows = db.execute(
        """SELECT * FROM worker_learning_outbox
           WHERE resolved_ms IS NULL
           ORDER BY rowid"""
    ).fetchall()
    resolved = 0
    for row in rows:
        operation = str(row["operation"])
        savepoint = "worker_learning_outbox_replay"
        db.execute(f"SAVEPOINT {savepoint}")
        try:
            operation, reference = _parse_learning_outbox_row(row)
            client_id = reference.get("client_id")
            if operation in {"open_round_trip", "close_round_trip"} and (
                not isinstance(client_id, str) or not client_id
            ):
                raise learning_store.LearningIntegrityError(
                    "Learning outbox client reference is invalid."
                )
            now = _now_ms()
            if operation == "capture_daily_label":
                if set(reference) != {
                    "candle_close_ms",
                    "policy",
                    "model_version",
                }:
                    raise learning_store.LearningIntegrityError(
                        "Learning daily-label reference is invalid."
                    )
                candle_ms = reference.get("candle_close_ms")
                if (
                    not isinstance(candle_ms, int)
                    or isinstance(candle_ms, bool)
                    or candle_ms < 0
                ):
                    raise learning_store.LearningIntegrityError(
                        "Learning daily-label candle reference is invalid."
                    )
                decision = db.execute(
                    """SELECT candle_close_ms, policy, close_latest
                       FROM worker_decisions WHERE candle_close_ms=?""",
                    (candle_ms,),
                ).fetchone()
                if decision is None or str(decision["policy"]) != POLICY:
                    raise learning_store.LearningIntegrityError(
                        "Learning daily-label decision reference is missing."
                    )
                learning_store.capture_daily_label(
                    db,
                    {
                        "candle_close_ms": int(decision["candle_close_ms"]),
                        "close_latest": str(decision["close_latest"]),
                    },
                    POLICY,
                    POLICY_SPEC_HASH,
                    now,
                )
            elif operation == "open_round_trip":
                if set(reference) != {"client_id", "policy", "model_version"}:
                    raise learning_store.LearningIntegrityError(
                        "Learning open-event reference is invalid."
                    )
                learning_store.open_round_trip(
                    db, client_id, POLICY, POLICY_SPEC_HASH, now
                )
            elif operation == "close_round_trip":
                if set(reference) != {
                    "client_id",
                    "policy",
                    "model_version",
                    "exact_pnl",
                } or not isinstance(reference.get("exact_pnl"), bool):
                    raise learning_store.LearningIntegrityError(
                        "Learning close-event reference is invalid."
                    )
                result = learning_store.close_round_trip(
                    db,
                    client_id,
                    POLICY,
                    POLICY_SPEC_HASH,
                    bool(reference["exact_pnl"]),
                    now,
                )
                if result is None:
                    raise learning_store.LearningIntegrityError(
                        "Learning close-event cannot yet be reconstructed."
                    )
            else:
                if (
                    set(reference)
                    != {
                        "reason",
                        "policy",
                        "model_version",
                        "archive_sequence",
                    }
                    or not isinstance(reference.get("reason"), str)
                    or not isinstance(reference.get("archive_sequence"), int)
                    or isinstance(reference.get("archive_sequence"), bool)
                    or int(reference["archive_sequence"]) <= 0
                ):
                    raise learning_store.LearningIntegrityError(
                        "Learning quarantine-event reference is invalid."
                    )
                learning_store.quarantine_open_round_trips(
                    db,
                    str(reference["reason"]),
                    now,
                    policy=POLICY,
                    model_version=POLICY_SPEC_HASH,
                )
        except Exception as exc:
            db.execute(f"ROLLBACK TO {savepoint}")
            db.execute(f"RELEASE {savepoint}")
            now = _now_ms()
            db.execute(
                """UPDATE worker_learning_outbox
                   SET last_attempt_ms=?, attempt_count=attempt_count+1,
                       last_error=? WHERE event_id=? AND resolved_ms IS NULL""",
                (
                    now,
                    _learning_outbox_error(exc),
                    str(row["event_id"]),
                ),
            )
            if isinstance(exc, learning_store.LearningIntegrityError):
                try:
                    learning_store.record_source_error(
                        db,
                        f"outbox_replay:{operation}: "
                        f"{type(exc).__name__}: {exc}",
                        now,
                    )
                except Exception:
                    pass
            # Later events may depend on this mutation (capture before fill,
            # open before close). Never manufacture a gap or binding from a
            # suffix whose causal predecessor is still unresolved.
            break
        db.execute(f"RELEASE {savepoint}")
        now = _now_ms()
        db.execute(
            """UPDATE worker_learning_outbox
               SET resolved_ms=?, last_attempt_ms=?, attempt_count=attempt_count+1,
                   last_error=NULL WHERE event_id=? AND resolved_ms IS NULL""",
            (now, now, str(row["event_id"])),
        )
        resolved += 1
    return resolved


def _learning_db_path(source_db_path: Path | str) -> Path:
    """Keep every aggregate physically scoped to exactly one source ledger."""

    source = Path(source_db_path).resolve()
    return source.with_name(f"{source.stem}-online-learning.sqlite3")


def _execution_gate() -> None:
    if os.getenv("BINANCE_TESTNET_WORKER_ENABLED", "").lower() != "true":
        raise WorkerHalt(
            "Worker is fail-closed; set BINANCE_TESTNET_WORKER_ENABLED=true."
        )
    if os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") != "testnet":
        raise WorkerHalt(
            "Order execution is fail-closed; set BINANCE_ORDER_EXECUTION_ENABLED=testnet."
        )
    if not os.getenv("BINANCE_TESTNET_API_KEY") or not os.getenv(
        "BINANCE_TESTNET_SECRET_KEY"
    ):
        raise WorkerHalt("Binance Spot Testnet credentials are missing.")


def _reset_gate() -> None:
    if os.getenv("BINANCE_TESTNET_RESET_ENABLED") != "reset":
        raise WorkerHalt(
            "Testnet epoch reset is fail-closed; set "
            "BINANCE_TESTNET_RESET_ENABLED=reset."
        )
    if not os.getenv("BINANCE_TESTNET_API_KEY") or not os.getenv(
        "BINANCE_TESTNET_SECRET_KEY"
    ):
        raise WorkerHalt("Binance Spot Testnet credentials are missing.")


def _client_order_id(decision_ms: int, side: str) -> str:
    material = (
        f"{POLICY}|{POLICY_SPEC_HASH}|{decision_ms}|{side.upper()}"
    ).encode("ascii")
    digest = hashlib.sha256(material).hexdigest()[:12]
    # Binance permits at most 36 characters for newClientOrderId.
    return f"qr-{side[0].lower()}-{decision_ms}-{digest}"[:36]


def _rule(rules: object, name: str) -> object:
    if isinstance(rules, Mapping):
        return rules[name]
    return getattr(rules, name)


def _closed_candles(
    klines: Sequence[Sequence[object]], *, now_ms: int | None = None
) -> list[tuple[int, Decimal]]:
    """Return sorted, unique ``(close_time_ms, close)`` pairs.

    ``Client.klines`` already excludes the in-progress UTC candle.  We still
    validate shape/order here so malformed data cannot create an order.
    """
    parsed: list[tuple[int, Decimal]] = []
    for candle in klines:
        if not isinstance(candle, Sequence) or isinstance(candle, (str, bytes)):
            raise WorkerHalt("Malformed Binance daily kline response.")
        if len(candle) < 7:
            raise WorkerHalt("Malformed Binance daily kline response.")
        open_time = int(candle[0])
        close_time = int(candle[6])
        if open_time % DAY_MS != 0 or close_time - open_time != DAY_MS - 1:
            raise WorkerHalt("Daily klines must be complete UTC calendar days.")
        close = _decimal(candle[4], "daily close")
        if close <= 0:
            raise WorkerHalt("Daily close must be positive.")
        if parsed and close_time <= parsed[-1][0]:
            raise WorkerHalt("Daily klines must be strictly chronological and unique.")
        parsed.append((close_time, close))
    if len(parsed) < LOOKBACK_DAYS + 1:
        raise WorkerHalt("At least 31 completed daily candles are required.")
    window = parsed[-(LOOKBACK_DAYS + 1) :]
    for previous, current in zip(window, window[1:]):
        if current[0] - previous[0] != DAY_MS:
            raise WorkerHalt("The 31 daily candles must be exactly contiguous.")
    current_ms = _now_ms() if now_ms is None else int(now_ms)
    age_ms = current_ms - window[-1][0]
    if age_ms < 0:
        raise WorkerHalt("Latest daily candle is not completed yet.")
    if age_ms > MAX_CANDLE_AGE_MS:
        raise WorkerHalt("Latest completed daily candle is stale (older than 36 hours).")
    return window


def _signal(market_data_client: object) -> dict[str, object]:
    with localcontext(_DECIMAL_CONTEXT):
        return _signal_in_context(market_data_client)


def _signal_in_context(market_data_client: object) -> dict[str, object]:
    window = _closed_candles(
        market_data_client.klines(
            interval=INTERVAL, limit=LOOKBACK_DAYS + 2, symbol=SYMBOL
        )
    )
    old_close = window[0][1]
    latest_close = window[-1][1]
    momentum = latest_close / old_close - Decimal("1")
    closes = [item[1] for item in window]
    daily_returns = [
        closes[index] / closes[index - 1] - Decimal("1")
        for index in range(1, len(closes))
    ]
    volatility_window = daily_returns[-(80 if INTERVAL == "15m" else 20):]
    mean_return = sum(volatility_window, Decimal("0")) / Decimal(
        len(volatility_window)
    )
    realized_volatility = (
        sum(
            ((value - mean_return) ** 2 for value in volatility_window),
            Decimal("0"),
        )
        / Decimal(len(volatility_window))
    ).sqrt()
    features = {
        "close": format(latest_close, "f"),
        "return_1d": format(latest_close / closes[-2] - Decimal("1"), "f"),
        "return_7d": format(latest_close / closes[-8] - Decimal("1"), "f"),
        "momentum_30d": format(momentum, "f"),
        "realized_volatility_20d": format(realized_volatility, "f"),
    }
    if HOURLY_MODE:
        features = {"close": features["close"], "return_1h": features["return_1d"],
                    "return_7h": features["return_7d"], "momentum_24h": features["momentum_30d"],
                    "realized_volatility_20h": features["realized_volatility_20d"]}
        features['decision_interval'] = INTERVAL
        features['cadence_contract'] = 'momentum24h_15m_v1' if INTERVAL == '15m' else 'momentum24h_1h_v1'
        if INTERVAL == '15m':
            features['return_15m'] = features.pop('return_1h')
            features['return_7h'] = format(latest_close/closes[-29]-Decimal('1'),'f')
    return {
        "candle_close_ms": window[-1][0],
        "close_latest": latest_close,
        "close_30d": old_close,
        "momentum": momentum,
        "feature_schema": "btc_15m_causal_v1" if INTERVAL == '15m' else "btc_hourly_causal_v1" if HOURLY_MODE else "btc_daily_causal_v1",
        "features": features,
        "target_long": momentum > MOMENTUM_THRESHOLD,
    }


def _free_balance(account: object, asset: str) -> Decimal:
    if not isinstance(account, Mapping):
        raise WorkerHalt("Binance account response is invalid.")
    balances = account.get("balances")
    if not isinstance(balances, list):
        raise WorkerHalt("Binance account balances are missing.")
    matches = [
        balance
        for balance in balances
        if isinstance(balance, Mapping) and balance.get("asset") == asset
    ]
    if len(matches) > 1:
        raise WorkerHalt(f"Binance returned duplicate {asset} balances.")
    if not matches:
        return Decimal("0")
    free = _decimal(matches[0].get("free"), f"free {asset} balance")
    if free < 0:
        raise WorkerHalt(f"Free {asset} balance cannot be negative.")
    return free


def _commissions_by_asset(order: Mapping[str, object]) -> dict[str, Decimal]:
    fills = order.get("fills")
    if not isinstance(fills, list) or not fills:
        raise WorkerHalt("A FULL Binance order response with fills is required.")
    commissions: dict[str, Decimal] = {}
    for fill in fills:
        if not isinstance(fill, Mapping):
            raise WorkerHalt("Binance fill response is invalid.")
        amount = _decimal(fill.get("commission", "0"), "fill commission")
        if amount < 0:
            raise WorkerHalt("Fill commission cannot be negative.")
        asset = fill.get("commissionAsset")
        if not isinstance(asset, str) or not asset:
            raise WorkerHalt("Fill commission asset is missing.")
        commissions[asset] = commissions.get(asset, Decimal("0")) + amount
    return commissions


def _state(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute("SELECT * FROM worker_state WHERE singleton=1").fetchone()
    if row is None:  # pragma: no cover - guarded by schema initialization
        raise WorkerHalt("Worker state is missing.")
    return row


def _halt(db: sqlite3.Connection, reason: str, *, client_id: str | None = None) -> None:
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_state SET halted=1, halt_reason=?, last_error=?,
               updated_ms=? WHERE singleton=1""",
            (reason, reason, now),
        )
        if client_id:
            db.execute(
                """UPDATE worker_order_intents SET state='halted', error=?, updated_ms=?
                   WHERE client_id=?""",
                (reason, now, client_id),
            )


def _transient_retry(db: sqlite3.Connection, reason: str) -> dict[str, object]:
    """Record a pre-intent read outage without converting it into a halt."""
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_state SET transient_failures=transient_failures+1,
               last_transient_error_ms=?, last_error=?, updated_ms=? WHERE singleton=1""",
            (now, reason, now),
        )
        failures = int(_state(db)["transient_failures"])
    backoff = min(900, int(API_POLL_SECONDS) * (2 ** min(failures - 1, 4)))
    return {"action": "retry", "reason": reason, "backoff_seconds": backoff}


def _clear_transient_error(db: sqlite3.Connection) -> None:
    state = _state(db)
    if not state["transient_failures"] and state["last_error"] is None:
        return
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_state SET transient_failures=0,
               last_transient_error_ms=NULL, last_error=NULL, updated_ms=?
               WHERE singleton=1""",
            (now,),
        )


def _order_identity(order: Mapping[str, object], client_id: str, side: str) -> None:
    returned_id = order.get("clientOrderId") or order.get("origClientOrderId")
    if returned_id != client_id:
        raise WorkerHalt(
            f"Managed order client-id mismatch: expected {client_id!r}, got {returned_id!r}."
        )
    if order.get("symbol") != SYMBOL:
        raise WorkerHalt("Managed order symbol mismatch.")
    if str(order.get("side", "")).upper() != side:
        raise WorkerHalt("Managed order side mismatch.")


def _client_api_key_fingerprint(client: object) -> str:
    """Return a one-way account binding without persisting credential material."""

    api_key = getattr(client, "api_key", None)
    if (
        not isinstance(api_key, str)
        or len(api_key) < 32
        or not api_key.isascii()
        or any(character.isspace() for character in api_key)
    ):
        raise AccountBindingError(
            "Binance Testnet API key is missing or unsuitable for account binding."
        )
    return hashlib.sha256(api_key.encode("ascii")).hexdigest()


def _assert_account_fingerprint(
    db: sqlite3.Connection,
    client: object,
    *,
    allow_unbound: bool,
) -> tuple[str, bool]:
    """Compare the caller with the bound account without exposing either value."""

    candidate = _client_api_key_fingerprint(client)
    bound = _state(db)["api_key_fingerprint_sha256"]
    if bound is None:
        if allow_unbound:
            return candidate, False
        raise AccountBindingError(
            "Binance Testnet ledger is not yet bound to a proven API key."
        )
    if not isinstance(bound, str) or not re.fullmatch(r"[0-9a-f]{64}", bound):
        raise AccountBindingError(
            "Binance Testnet ledger account binding is invalid."
        )
    if bound != candidate:
        raise AccountBindingError(
            "Binance Testnet API key does not match this execution ledger."
        )
    return candidate, True


def validate_bound_account(
    *,
    db_path: Path | str = DB_PATH,
    client: object | None = None,
) -> dict[str, object]:
    """Prove that the caller's key matches the ledger before any stop window.

    This check is deliberately read-only and refuses legacy unbound ledgers.
    The separate signed account command proves that the same credential is
    currently accepted by Binance Spot Testnet.
    """

    api = client if client is not None else execution.Client()
    with closing(_connect_read_only(db_path)) as db:
        _candidate, bound = _assert_account_fingerprint(
            db, api, allow_unbound=False
        )
        state = _state(db)
        return {
            "validated": True,
            "account_bound": bool(bound),
            "api_key_matches_ledger": True,
            "policy": str(state["active_policy"]),
            "symbol": SYMBOL,
            "execution_environment": "binance_spot_testnet",
        }


def _latest_filled_buy_origin(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute(
        """SELECT client_id, exchange_order_id, executed_qty, net_base_qty,
                  cumulative_quote_qty
           FROM worker_order_intents
           WHERE side='BUY' AND state='filled'
           ORDER BY decision_ms DESC LIMIT 1"""
    ).fetchone()


def _validate_position_origin_order(
    origin: Mapping[str, object],
    remote: Mapping[str, object],
    tracked_qty: Decimal,
) -> None:
    client_id = str(origin["client_id"])
    _order_identity(remote, client_id, "BUY")
    if str(remote.get("status", "")).upper() != "FILLED":
        raise WorkerHalt("Worker BUY origin is no longer FILLED.")
    stored_order_id = origin["exchange_order_id"]
    remote_order_id = remote.get("orderId")
    if (
        stored_order_id is None
        or isinstance(remote_order_id, bool)
        or not str(remote_order_id).isascii()
        or not str(remote_order_id).isdigit()
        or int(str(remote_order_id)) <= 0
        or str(remote_order_id) != str(stored_order_id)
    ):
        raise WorkerHalt("Worker BUY origin exchange order id no longer matches.")
    remote_executed = _decimal(remote.get("executedQty"), "origin executed quantity")
    stored_executed = _decimal(origin["executed_qty"], "stored origin quantity")
    remote_quote = _decimal(
        remote.get("cummulativeQuoteQty"), "origin cumulative quote"
    )
    stored_quote = _decimal(
        origin["cumulative_quote_qty"], "stored origin cumulative quote"
    )
    origin_net = _decimal(origin["net_base_qty"], "stored origin net quantity")
    if remote_executed != stored_executed or remote_quote != stored_quote:
        raise WorkerHalt("Worker BUY origin execution no longer matches local state.")
    if tracked_qty <= 0 or tracked_qty > origin_net:
        raise WorkerHalt("Tracked BTC quantity exceeds its worker BUY provenance.")


def _prove_legacy_pending_account(
    db: sqlite3.Connection, client: object, state: Mapping[str, object]
) -> None:
    client_id = str(state["pending_client_id"])
    side = str(state["pending_side"] or "")
    intent = db.execute(
        """SELECT client_id, side, exchange_order_id
           FROM worker_order_intents WHERE client_id=?""",
        (client_id,),
    ).fetchone()
    if intent is None or str(intent["side"]) != side or side not in {"BUY", "SELL"}:
        raise AccountBindingError(
            "Legacy pending order has inconsistent local provenance; account binding refused."
        )
    try:
        remote = client.order_by_client_id(client_id, symbol=SYMBOL)
    except Exception as exc:
        raise AccountBindingError(
            "Legacy pending order could not be proven on the supplied Testnet account."
        ) from exc
    if not isinstance(remote, Mapping):
        raise AccountBindingError(
            "Legacy pending order proof returned an invalid response."
        )
    try:
        _order_identity(remote, client_id, side)
        status = str(remote.get("status", "")).upper()
        if status not in {
            "NEW", "PARTIALLY_FILLED", "FILLED", "PENDING_CANCEL",
            "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH",
        }:
            raise WorkerHalt("Legacy pending order status is invalid.")
        order_id = remote.get("orderId")
        order_id_text = str(order_id)
        if (
            isinstance(order_id, bool)
            or not order_id_text.isascii()
            or not order_id_text.isdigit()
            or int(order_id_text) <= 0
        ):
            raise WorkerHalt("Legacy pending order id is invalid.")
        stored_order_id = intent["exchange_order_id"]
        if stored_order_id is not None and str(stored_order_id) != order_id_text:
            raise WorkerHalt("Legacy pending exchange order id does not match.")
    except WorkerHalt as exc:
        raise AccountBindingError(
            f"Legacy pending order proof failed: {exc}"
        ) from exc


def _legacy_ledger_is_pristine(
    db: sqlite3.Connection, state: Mapping[str, object]
) -> bool:
    counts = db.execute(
        """SELECT
           (SELECT COUNT(*) FROM worker_decisions),
           (SELECT COUNT(*) FROM worker_order_intents),
           (SELECT COUNT(*) FROM worker_epochs)"""
    ).fetchone()
    return bool(
        tuple(int(value) for value in counts) == (0, 0, 0)
        and state["pending_client_id"] is None
        and state["last_candle_close_ms"] is None
        and _decimal(state["position_qty"], "tracked position") == 0
        and _decimal(state["position_quote_cost"], "tracked position cost") == 0
        and _decimal(state["realized_pnl_usdt"], "realized P&L") == 0
        and int(state["completed_round_trips"]) == 0
    )


def _prove_legacy_flat_history(db: sqlite3.Connection, client: object) -> None:
    """Bind non-pristine flat history only through its latest terminal order."""

    intent = db.execute(
        """SELECT client_id, side, state, exchange_order_id, executed_qty,
                  cumulative_quote_qty, response_json
           FROM worker_order_intents
           WHERE state IN ('filled','terminal')
           ORDER BY decision_ms DESC, updated_ms DESC, client_id DESC LIMIT 1"""
    ).fetchone()
    if intent is None:
        raise AccountBindingError(
            "Legacy non-pristine flat ledger has no active terminal order proof; "
            "manual archival is required."
        )
    client_id = str(intent["client_id"])
    side = str(intent["side"])
    try:
        remote = client.order_by_client_id(client_id, symbol=SYMBOL)
    except Exception as exc:
        raise AccountBindingError(
            "Legacy flat ledger terminal order could not be proven on the supplied "
            "Testnet account."
        ) from exc
    if not isinstance(remote, Mapping):
        raise AccountBindingError(
            "Legacy flat ledger terminal-order proof returned an invalid response."
        )
    try:
        _order_identity(remote, client_id, side)
        remote_status = str(remote.get("status", "")).upper()
        if str(intent["state"]) == "filled":
            if remote_status != "FILLED":
                raise WorkerHalt("Latest filled order is no longer FILLED.")
            stored_order_id = intent["exchange_order_id"]
            if stored_order_id is None or str(remote.get("orderId")) != str(
                stored_order_id
            ):
                raise WorkerHalt("Latest filled exchange order id does not match.")
            remote_executed = _decimal(
                remote.get("executedQty"), "terminal executed quantity"
            )
            stored_executed = _decimal(
                intent["executed_qty"], "stored executed quantity"
            )
            if remote_executed != stored_executed:
                raise WorkerHalt("Latest filled executed quantity does not match.")
            if _decimal(
                remote.get("cummulativeQuoteQty"), "terminal cumulative quote"
            ) != _decimal(intent["cumulative_quote_qty"], "stored cumulative quote"):
                raise WorkerHalt("Latest filled cumulative quote does not match.")
        else:
            try:
                stored = json.loads(str(intent["response_json"]))
            except (TypeError, ValueError) as exc:
                raise WorkerHalt("Stored terminal order response is invalid.") from exc
            if not isinstance(stored, Mapping):
                raise WorkerHalt("Stored terminal order response is invalid.")
            _order_identity(stored, client_id, side)
            stored_status = str(stored.get("status", "")).upper()
            if stored_status not in {
                "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH",
            } or remote_status != stored_status:
                raise WorkerHalt("Latest terminal order status does not match.")
            if str(remote.get("orderId")) != str(stored.get("orderId")):
                raise WorkerHalt("Latest terminal exchange order id does not match.")
            remote_executed = _decimal(
                remote.get("executedQty", "0"), "terminal executed quantity"
            )
            if remote_executed != 0:
                raise WorkerHalt("Latest terminal order unexpectedly has an execution.")
            if _decimal(
                remote.get("cummulativeQuoteQty", "0"), "terminal cumulative quote"
            ) != _decimal(
                stored.get("cummulativeQuoteQty", "0"), "stored cumulative quote"
            ):
                raise WorkerHalt("Latest terminal cumulative quote does not match.")
    except WorkerHalt as exc:
        raise AccountBindingError(
            f"Legacy flat ledger terminal-order proof failed: {exc}"
        ) from exc


def _ensure_account_binding(db: sqlite3.Connection, client: object) -> None:
    """Bind a legacy ledger only after a signed, state-appropriate proof."""

    candidate, already_bound = _assert_account_fingerprint(
        db, client, allow_unbound=True
    )
    if already_bound:
        return

    state = _state(db)
    tracked_qty = _decimal(state["position_qty"], "tracked position")
    if tracked_qty < 0:
        raise AccountBindingError(
            "Negative tracked position prevents Testnet account binding."
        )
    if tracked_qty > 0:
        origin = _latest_filled_buy_origin(db)
        if origin is None:
            raise AccountBindingError(
                "Legacy open position has no local BUY provenance; account binding refused."
            )
        client_id = str(origin["client_id"])
        try:
            remote = client.order_by_client_id(client_id, symbol=SYMBOL)
        except Exception as exc:
            raise AccountBindingError(
                "Legacy open position BUY origin could not be proven on the supplied "
                "Testnet account."
            ) from exc
        if not isinstance(remote, Mapping):
            raise AccountBindingError(
                "Legacy open position proof returned an invalid response."
            )
        try:
            _validate_position_origin_order(origin, remote, tracked_qty)
        except WorkerHalt as exc:
            raise AccountBindingError(
                f"Legacy open position proof failed: {exc}"
            ) from exc
    elif state["pending_client_id"]:
        _prove_legacy_pending_account(db, client, state)
    elif _legacy_ledger_is_pristine(db, state):
        try:
            account = client.account()
        except Exception as exc:
            raise AccountBindingError(
                "Signed Binance Testnet account proof failed; ledger remains unbound."
            ) from exc
        if not isinstance(account, Mapping):
            raise AccountBindingError(
                "Signed Binance Testnet account proof returned an invalid response."
            )
    else:
        _prove_legacy_flat_history(db, client)

    now = _now_ms()
    db.execute(
        """UPDATE worker_state SET api_key_fingerprint_sha256=?, updated_ms=?
           WHERE singleton=1 AND api_key_fingerprint_sha256 IS NULL""",
        (candidate, now),
    )
    _, bound = _assert_account_fingerprint(db, client, allow_unbound=False)
    if not bound:  # pragma: no cover - guarded by the assertion above
        raise AccountBindingError("Binance Testnet account binding failed closed.")


def _is_recoverable_timestamp_halt(state: sqlite3.Row) -> bool:
    """Recognize only legacy pre-order signed-read ``-1021`` halts."""

    if not state["halted"] or state["pending_client_id"]:
        return False
    reason = state["halt_reason"]
    if not isinstance(reason, str):
        return False
    safe_read_prefixes = (
        "Worker BUY origin ",
        "Open-order safety check failed:",
        "USDT balance safety check failed:",
        "BTC balance safety check failed:",
    )
    return (
        reason.startswith(safe_read_prefixes)
        and "BinanceAPIError: Binance HTTP 400:" in reason
        and re.search(r'"code"\s*:\s*-1021(?:\D|$)', reason) is not None
        and "Timestamp for this request is outside of the recvWindow" in reason
    )


def _recover_timestamp_halt(
    db_path: Path | str,
    lock_path: Path | str,
    client: object,
) -> bool:
    """Clear a legacy clock-skew halt only after full remote read proof.

    Position, intent, decision, and learning records are immutable in this
    repair path.  Any remote outage or mismatch leaves the halt intact.
    """

    with _process_lock(_account_lock_path(lock_path)):
        with closing(_connect(db_path)) as db:
            state = _state(db)
            if not _is_recoverable_timestamp_halt(state):
                return False
            original_reason = str(state["halt_reason"])
            _assert_account_fingerprint(db, client, allow_unbound=False)

            try:
                account = client.account()
                open_orders = client.open_orders(symbol=SYMBOL)
            except Exception as exc:
                raise WorkerHalt(
                    "Clock-skew recovery could not complete signed account checks."
                ) from exc
            if not isinstance(account, Mapping):
                raise WorkerHalt(
                    "Clock-skew recovery received an invalid account response."
                )
            if not isinstance(open_orders, list):
                raise WorkerHalt(
                    "Clock-skew recovery received an invalid open-order response."
                )
            if open_orders:
                raise WorkerHalt(
                    "Clock-skew recovery refuses existing BTCUSDT open orders."
                )

            tracked_qty = _decimal(state["position_qty"], "tracked position")
            if tracked_qty < 0:
                raise WorkerHalt(
                    "Clock-skew recovery refuses a negative tracked position."
                )
            if tracked_qty > 0:
                free_base = _free_balance(account, BASE_ASSET)
                if free_base < tracked_qty:
                    raise WorkerHalt(
                        "Clock-skew recovery cannot prove enough free BTC for the "
                        "tracked position."
                    )
                origin = _latest_filled_buy_origin(db)
                if origin is None:
                    raise WorkerHalt(
                        "Clock-skew recovery cannot prove the tracked BUY origin."
                    )
                client_id = str(origin["client_id"])
                try:
                    remote = client.order_by_client_id(client_id, symbol=SYMBOL)
                except Exception as exc:
                    raise WorkerHalt(
                        "Clock-skew recovery could not read the tracked BUY origin."
                    ) from exc
                if not isinstance(remote, Mapping):
                    raise WorkerHalt(
                        "Clock-skew recovery received an invalid BUY-origin response."
                    )
                _validate_position_origin_order(origin, remote, tracked_qty)

            now = _now_ms()
            with _transaction(db):
                current = _state(db)
                if (
                    not _is_recoverable_timestamp_halt(current)
                    or current["halt_reason"] != original_reason
                ):
                    raise WorkerHalt(
                        "Clock-skew halt changed during recovery; no repair was applied."
                    )
                cursor = db.execute(
                    """UPDATE worker_state SET desired_running=0, halted=0,
                       halt_reason=NULL, last_error=NULL, transient_failures=0,
                       last_transient_error_ms=NULL, updated_ms=?
                       WHERE singleton=1 AND halted=1 AND halt_reason=?
                         AND pending_client_id IS NULL""",
                    (now, original_reason),
                )
                if cursor.rowcount != 1:
                    raise WorkerHalt(
                        "Clock-skew halt changed during recovery; no repair was applied."
                    )
            return True


def _apply_filled(
    db: sqlite3.Connection,
    order: Mapping[str, object],
    client_id: str,
    side: str,
) -> dict[str, object]:
    with localcontext(_DECIMAL_CONTEXT):
        return _apply_filled_in_context(db, order, client_id, side)


def _apply_filled_in_context(
    db: sqlite3.Connection,
    order: Mapping[str, object],
    client_id: str,
    side: str,
) -> dict[str, object]:
    _order_identity(order, client_id, side)
    if str(order.get("status", "")).upper() != "FILLED":
        raise WorkerHalt("Attempted to apply a non-filled managed order.")

    executed = _decimal(order.get("executedQty"), "executed quantity")
    quote = _decimal(order.get("cummulativeQuoteQty", "0"), "cumulative quote quantity")
    if executed <= 0:
        raise WorkerHalt("Filled managed order has no executed quantity.")

    state = _state(db)
    current = _decimal(state["position_qty"], "tracked position")
    current_cost = _decimal(state["position_quote_cost"], "position cost")
    cumulative_pnl = _decimal(state["realized_pnl_usdt"], "realized PnL")
    completed_round_trips = int(state["completed_round_trips"])
    commissions = _commissions_by_asset(order)
    quote_commission = commissions.get(QUOTE_ASSET, Decimal("0"))
    base_commission = commissions.get(BASE_ASSET, Decimal("0"))
    unsupported = {
        asset: amount
        for asset, amount in commissions.items()
        if asset not in {BASE_ASSET, QUOTE_ASSET} and amount != 0
    }
    incomplete_reason = state["pnl_incomplete_reason"]
    if unsupported:
        detail = ", ".join(
            f"{asset}={format(amount, 'f')}" for asset, amount in sorted(unsupported.items())
        )
        incomplete_reason = (
            "Exact USDT P&L is unavailable because fill commission used an "
            f"unsupported asset: {detail}."
        )
    pnl_complete = bool(state["pnl_complete"]) and not unsupported
    realized_pnl: Decimal | None = None
    if side == "BUY":
        if current != 0:
            raise WorkerHalt("A managed BUY cannot be applied over an existing position.")
        net = _decimal(
            execution.net_base_quantity(order, base_asset=BASE_ASSET),
            "net acquired quantity",
        )
        if net <= 0:
            raise WorkerHalt("Filled BUY has no net acquired BTC quantity.")
        new_qty = current + net
        new_cost = current_cost + quote + quote_commission
    else:
        net = Decimal("0")
        if current <= 0:
            raise WorkerHalt("A managed SELL cannot be applied without a tracked position.")
        base_depletion = executed + base_commission
        if base_depletion > current:
            raise WorkerHalt(
                "Filled SELL plus BTC commission exceeds the worker's tracked quantity."
            )
        new_qty = current - base_depletion
        if new_qty < 0:
            new_qty = Decimal("0")
        sold_cost = current_cost * base_depletion / current
        proceeds = quote - quote_commission
        if proceeds < 0:
            raise WorkerHalt("Quote-asset commission exceeds SELL proceeds.")
        calculated_pnl = proceeds - sold_cost
        if pnl_complete:
            realized_pnl = calculated_pnl
            cumulative_pnl += calculated_pnl
        new_cost = current_cost - sold_cost
        if new_qty == 0:
            new_cost = Decimal("0")
            completed_round_trips += 1

    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_order_intents SET state='filled', exchange_order_id=?,
               executed_qty=?, net_base_qty=?, cumulative_quote_qty=?, response_json=?,
               realized_pnl_usdt=?, commission_by_asset=?, error=NULL, updated_ms=?
               WHERE client_id=?""",
            (
                str(order.get("orderId", "")),
                format(executed, "f"),
                format(net, "f") if side == "BUY" else None,
                format(quote, "f"),
                _json(order),
                format(realized_pnl, "f") if realized_pnl is not None else None,
                _json({asset: format(amount, "f") for asset, amount in commissions.items()}),
                now,
                client_id,
            ),
        )
        db.execute(
            """UPDATE worker_state SET position_qty=?, position_quote_cost=?,
               pending_client_id=NULL, pending_side=NULL, pending_decision_ms=NULL,
               pending_quote=NULL, pending_qty=NULL, halted=?, halt_reason=?,
               last_error=NULL, realized_pnl_usdt=?, completed_round_trips=?,
               pnl_complete=?, pnl_incomplete_reason=?,
               updated_ms=? WHERE singleton=1""",
            (
                format(new_qty, "f"),
                format(new_cost, "f"),
                int(bool(unsupported)),
                incomplete_reason if unsupported else None,
                format(cumulative_pnl, "f"),
                completed_round_trips,
                int(pnl_complete),
                incomplete_reason,
                now,
            ),
        )
        if side == "BUY":
            _durable_learning_source_call(
                db,
                "open_round_trip",
                learning_store.open_round_trip,
                db,
                client_id,
                POLICY,
                POLICY_SPEC_HASH,
                now,
                outbox_reference={
                    "client_id": client_id,
                    "policy": POLICY,
                    "model_version": POLICY_SPEC_HASH,
                },
            )
        elif new_qty == 0:
            _durable_learning_source_call(
                db,
                "close_round_trip",
                learning_store.close_round_trip,
                db,
                client_id,
                POLICY,
                POLICY_SPEC_HASH,
                bool(pnl_complete),
                now,
                outbox_reference={
                    "client_id": client_id,
                    "policy": POLICY,
                    "model_version": POLICY_SPEC_HASH,
                    "exact_pnl": bool(pnl_complete),
                },
            )
    execution_action = "bought" if side == "BUY" else "sold"
    return {
        "action": "halted" if unsupported else execution_action,
        "execution_action": execution_action,
        "client_id": client_id,
        "executed_qty": format(executed, "f"),
        "position_qty": format(new_qty, "f"),
        "realized_pnl_usdt": (
            format(realized_pnl, "f") if realized_pnl is not None else None
        ),
        "pnl_complete": pnl_complete,
        "pnl_incomplete_reason": incomplete_reason,
    }


def _terminal_without_fill(
    db: sqlite3.Connection, order: Mapping[str, object], client_id: str, side: str
) -> dict[str, object]:
    _order_identity(order, client_id, side)
    status = str(order.get("status", "")).upper()
    if status not in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        raise WorkerHalt(f"Unexpected managed order status: {status or 'missing'}.")
    executed = _decimal(order.get("executedQty", "0"), "terminal executed quantity")
    if executed != 0:
        raise WorkerHalt(
            f"Managed order ended as {status} after a partial execution; "
            "automatic position changes are blocked pending manual reconciliation."
        )
    now = _now_ms()
    with _transaction(db):
        db.execute(
            """UPDATE worker_order_intents SET state='terminal', response_json=?,
               error=?, updated_ms=? WHERE client_id=?""",
            (_json(order), status, now, client_id),
        )
        db.execute(
            """UPDATE worker_state SET pending_client_id=NULL, pending_side=NULL,
               pending_decision_ms=NULL, pending_quote=NULL, pending_qty=NULL,
               halted=0, halt_reason=NULL, last_error=?, updated_ms=? WHERE singleton=1""",
            (f"Managed order ended as {status}; it will not be retried.", now),
        )
    return {"action": "terminal", "client_id": client_id, "status": status}


def _consume_order(
    db: sqlite3.Connection,
    order: Mapping[str, object],
    client_id: str,
    side: str,
    client: object | None = None,
) -> dict[str, object]:
    _order_identity(order, client_id, side)
    status = str(order.get("status", "")).upper()
    if status == "FILLED":
        filled_order = order
        if not isinstance(order.get("fills"), list) or not order.get("fills"):
            reconcile = getattr(client, "reconcile_filled_order", None)
            if not callable(reconcile):
                raise WorkerHalt(
                    "FILLED order lookup omitted fills and the client cannot reconstruct them."
                )
            filled_order = reconcile(client_id, symbol=SYMBOL)
            if not isinstance(filled_order, Mapping):
                raise WorkerHalt("Filled-order reconciliation returned an invalid response.")
            _order_identity(filled_order, client_id, side)
            if str(filled_order.get("status", "")).upper() != "FILLED":
                raise WorkerHalt("Filled-order reconciliation changed the order status.")
        return _apply_filled(db, filled_order, client_id, side)
    if status in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        return _terminal_without_fill(db, order, client_id, side)
    raise WorkerHalt(f"Managed market order has unsafe pending status {status or 'missing'}.")


def _reconcile_pending(db: sqlite3.Connection, client: object) -> dict[str, object] | None:
    state = _state(db)
    client_id = state["pending_client_id"]
    if not client_id:
        return None
    side = str(state["pending_side"])
    try:
        order = client.order_by_client_id(client_id, symbol=SYMBOL)
        if not isinstance(order, Mapping):
            raise WorkerHalt("Managed order lookup returned an invalid response.")
        return _consume_order(db, order, client_id, side, client)
    except WorkerHalt as exc:
        _halt(db, str(exc), client_id=client_id)
        return {"action": "halted", "reason": str(exc), "client_id": client_id}
    except Exception as exc:  # an absent or unreachable pending order is ambiguous
        reason = (
            f"Unable to reconcile managed pending order {client_id}; no order was retried: "
            f"{type(exc).__name__}: {exc}"
        )
        _halt(db, reason, client_id=client_id)
        return {"action": "halted", "reason": reason, "client_id": client_id}


def _verify_position_origin(
    db: sqlite3.Connection, client: object, tracked_qty: Decimal
) -> dict[str, object] | None:
    """Prove that local BTC came from a still-present worker BUY order."""
    origin = _latest_filled_buy_origin(db)
    if origin is None:
        reason = "Tracked BTC has no filled worker BUY provenance; worker halted."
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    client_id = str(origin["client_id"])
    try:
        remote = client.order_by_client_id(client_id, symbol=SYMBOL)
    except execution.BinanceTransportError as exc:
        return _transient_retry(db, f"Position-origin read outage: {exc}")
    except execution.BinanceOrderNotFoundError as exc:
        reason = (
            f"Worker BUY origin {client_id} is absent from Binance Spot Testnet; "
            f"a Testnet reset is suspected ({type(exc).__name__}: {exc})."
        )
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    except Exception as exc:
        reason = (
            f"Worker BUY origin {client_id} lookup failed without definitive "
            f"Testnet-reset evidence ({type(exc).__name__}: {exc})."
        )
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    if not isinstance(remote, Mapping):
        reason = "Worker BUY origin lookup returned an invalid response."
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    try:
        _validate_position_origin_order(origin, remote, tracked_qty)
    except WorkerHalt as exc:
        reason = f"Worker BUY provenance mismatch: {exc}"
        _halt(db, reason)
        return {"action": "halted", "reason": reason}
    return None


def _persist_decision(
    db: sqlite3.Connection,
    signal: Mapping[str, object],
    action: str,
    client_id: str | None,
    side: str | None = None,
    requested_quote: Decimal | None = None,
    requested_qty: Decimal | None = None,
    require_desired: bool = False,
    learning_refresh_healthy: bool = True,
) -> bool:
    """Atomically claim a candle and, when needed, prepare an order intent."""
    candle_ms = int(signal["candle_close_ms"])
    now = _now_ms()
    with _transaction(db):
        state = _state(db)
        if require_desired and not state["desired_running"]:
            raise WorkerStopped("Worker stop was requested before decision persistence.")
        previous = state["last_candle_close_ms"]
        if previous is not None and int(previous) >= candle_ms:
            return False
        capture_reference = {
            "candle_close_ms": candle_ms,
            "policy": POLICY,
            "model_version": POLICY_SPEC_HASH,
        }
        if HOURLY_MODE or signal.get("operator_exit"):
            # Hourly labels live in their own challenger database. Never feed
            # a 1h outcome into the daily store's immutable 24h contract.
            pass
        elif learning_refresh_healthy:
            _durable_learning_source_call(
                db,
                "capture_daily_label",
                learning_store.capture_daily_label,
                db,
                signal,
                POLICY,
                POLICY_SPEC_HASH,
                now,
                outbox_reference=capture_reference,
            )
        else:
            # A candidate transition may be durably staged on the aggregate
            # while the source registration still references its predecessor.
            # Never label against that predecessor; keep the exact decision
            # reference in the source-ledger outbox for ordered recovery.
            _enqueue_learning_outbox(
                db,
                "capture_daily_label",
                capture_reference,
                RuntimeError("learning refresh is not healthy"),
                now,
            )
        db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                feature_schema, feature_json, target_long, action, client_id,
                created_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candle_ms,
                POLICY,
                format(signal["close_latest"], "f"),
                format(signal["close_30d"], "f"),
                format(signal["momentum"], "f"),
                str(signal["feature_schema"]),
                _json(signal["features"]),
                int(bool(signal["target_long"])),
                action,
                client_id,
                now,
            ),
        )
        if client_id and side:
            db.execute(
                """INSERT INTO worker_order_intents
                   (client_id, policy, decision_ms, candle_close_ms, symbol, side,
                    requested_quote, requested_qty, state, created_ms, updated_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)""",
                (
                    client_id,
                    POLICY,
                    candle_ms,
                    candle_ms,
                    SYMBOL,
                    side,
                    format(requested_quote, "f") if requested_quote is not None else None,
                    format(requested_qty, "f") if requested_qty is not None else None,
                    now,
                    now,
                ),
            )
            db.execute(
                """UPDATE worker_state SET last_candle_close_ms=?, pending_client_id=?,
                   pending_side=?, pending_decision_ms=?, pending_quote=?, pending_qty=?,
                   updated_ms=? WHERE singleton=1""",
                (
                    candle_ms,
                    client_id,
                    side,
                    candle_ms,
                    format(requested_quote, "f") if requested_quote is not None else None,
                    format(requested_qty, "f") if requested_qty is not None else None,
                    now,
                ),
            )
        else:
            db.execute(
                """UPDATE worker_state SET last_candle_close_ms=?, updated_ms=?
                   WHERE singleton=1""",
                (candle_ms, now),
            )
    return True


def _submit_prepared(
    db: sqlite3.Connection,
    client: object,
    client_id: str,
    side: str,
    *,
    quote: Decimal | None = None,
    quantity: Decimal | None = None,
    require_desired: bool = False,
) -> dict[str, object]:
    """Submit exactly once, then reconcile; POST is never automatically retried."""
    if require_desired:
        with _transaction(db):
            state = _state(db)
            if not state["desired_running"]:
                intent = db.execute(
                    "SELECT state FROM worker_order_intents WHERE client_id=?",
                    (client_id,),
                ).fetchone()
                if intent is None or intent["state"] != "prepared":
                    raise WorkerHalt("Stopped worker has an inconsistent prepared intent.")
                now = _now_ms()
                db.execute(
                    """UPDATE worker_order_intents SET state='aborted_before_post',
                       error='operator stop before POST', updated_ms=? WHERE client_id=?""",
                    (now, client_id),
                )
                db.execute(
                    """UPDATE worker_state SET pending_client_id=NULL, pending_side=NULL,
                       pending_decision_ms=NULL, pending_quote=NULL, pending_qty=NULL,
                       updated_ms=? WHERE singleton=1""",
                    (now,),
                )
                return {"action": "stopped", "client_id": client_id}
    try:
        if side == "BUY":
            response = client.place_market_buy(
                quote, symbol=SYMBOL, client_order_id=client_id
            )
        else:
            response = client.place_market_sell(
                quantity, symbol=SYMBOL, client_order_id=client_id
            )
        if not isinstance(response, Mapping):
            raise WorkerHalt("Managed order submission returned an invalid response.")
        now = _now_ms()
        with _transaction(db):
            db.execute(
                """UPDATE worker_order_intents SET state='submitted', response_json=?,
                   updated_ms=? WHERE client_id=?""",
                (_json(response), now, client_id),
            )
        try:
            return _consume_order(db, response, client_id, side, client)
        except WorkerHalt as exc:
            _halt(db, str(exc), client_id=client_id)
            return {"action": "halted", "reason": str(exc), "client_id": client_id}
    except WorkerHalt as exc:
        _halt(db, str(exc), client_id=client_id)
        return {"action": "halted", "reason": str(exc), "client_id": client_id}
    except Exception as submit_exc:
        # A timeout can occur after Binance accepted the POST.  A single GET is
        # safe; issuing a second POST is forbidden even when GET says not found.
        now = _now_ms()
        with _transaction(db):
            db.execute(
                """UPDATE worker_order_intents SET state='unknown', error=?, updated_ms=?
                   WHERE client_id=?""",
                (f"{type(submit_exc).__name__}: {submit_exc}", now, client_id),
            )
        try:
            response = client.order_by_client_id(client_id, symbol=SYMBOL)
            if not isinstance(response, Mapping):
                raise WorkerHalt("Managed order lookup returned an invalid response.")
            return _consume_order(db, response, client_id, side, client)
        except Exception as lookup_exc:
            reason = (
                f"Ambiguous submission for {client_id}; POST was not retried and lookup "
                f"did not prove a terminal result ({type(lookup_exc).__name__}: {lookup_exc})."
            )
            _halt(db, reason, client_id=client_id)
            return {"action": "halted", "reason": reason, "client_id": client_id}


def run_once(
    *,
    db_path: Path | str = DB_PATH,
    client: object | None = None,
    market_data_client: object | None = None,
    enforce_desired: bool = False,
    exit_only: bool = False,
) -> dict[str, object]:
    """Reconcile an existing intent or evaluate one completed daily candle."""
    _execution_gate()
    api = client if client is not None else execution.Client()
    market = (
        market_data_client
        if market_data_client is not None
        else execution.PublicMarketDataClient()
    )
    with closing(_connect(db_path)) as db:
        _ensure_account_binding(db, api)
        learning = _prepare_learning_before_decision(db_path)
        learning_refresh_healthy = _learning_refresh_allows_source_mutation(
            learning
        )
        # Pending durable intent always takes precedence, including after a
        # prior halt.  This permits a restart to observe a delayed fill without
        # ever posting the order again. Learning preparation deliberately runs
        # first so a staged candidate transition cannot attribute this fill to
        # its predecessor; unhealthy source writes are queued by the outbox.
        reconciled = _reconcile_pending(db, api)
        if reconciled is not None:
            return reconciled

        state = _state(db)
        if state["halted"]:
            return {"action": "halted", "reason": state["halt_reason"]}
        if enforce_desired and not state["desired_running"]:
            return {"action": "stopped"}
        state = _state(db)
        tracked_qty = _decimal(state["position_qty"], "tracked position")
        unresolved_learning = _unresolved_learning_outbox_count(db)
        if tracked_qty == 0 and unresolved_learning:
            # Keep a flat ledger flat until prior trade evidence is durable.
            # An existing long is still allowed to reach its risk-reducing SELL.
            return {
                "action": "learning_evidence_pending",
                "unresolved_learning_outbox": unresolved_learning,
            }
        if tracked_qty > 0:
            origin_result = _verify_position_origin(db, api, tracked_qty)
            if origin_result is not None:
                return origin_result

        try:
            open_orders = api.open_orders(symbol=SYMBOL)
            if not isinstance(open_orders, list):
                raise WorkerHalt("Binance open-order response is invalid.")
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Open-order read outage: {exc}")
        except Exception as exc:
            reason = f"Open-order safety check failed: {type(exc).__name__}: {exc}"
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        if open_orders:
            identifiers = [
                str(order.get("clientOrderId", order.get("orderId", "unknown")))
                if isinstance(order, Mapping)
                else "invalid"
                for order in open_orders[:5]
            ]
            reason = (
                "Untracked BTCUSDT open order(s) detected; worker halted: "
                + ", ".join(identifiers)
            )
            _halt(db, reason)
            return {"action": "halted", "reason": reason}

        try:
            signal = _signal(market)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Daily-kline read outage: {exc}")
        last = state["last_candle_close_ms"]
        if HOURLY_MODE and os.getenv("BINANCE_TESTNET_AUTO_MODEL") == "true" and not exit_only:
            if last is None or int(last) < int(signal["candle_close_ms"]):
                import hourly_testnet_learning
                import hourly_model_authority
                try:
                    hourly_testnet_learning.refresh(market)
                    signal = hourly_model_authority.apply(signal)
                except Exception:
                    signal['features']['model_authority_error'] = 'fallback_to_momentum'
        if os.getenv('BINANCE_TESTNET_EXPLORATION') == 'true' and not exit_only:
            if not HOURLY_MODE or INTERVAL != '15m' or ENTRY_QUOTE_USDT > Decimal('15'):
                raise WorkerHalt('Exploration requires 15m Testnet and at most 15 USDT entries')
            if last is None or int(last) < int(signal['candle_close_ms']):
                import testnet_exploration
                signal = testnet_exploration.apply(signal,environment='binance_spot_testnet',
                                                   interval=INTERVAL,now_ms=_now_ms())
        if exit_only:
            signal = dict(signal, target_long=False, operator_exit=True,
                          candle_close_ms=max(_now_ms(), int(last or 0)+1),
                          feature_schema="operator_exit_for_hourly_switch_v1",
                          features={"reason": "user_requested_hourly_testnet_switch"})
        if last is not None and int(last) >= int(signal["candle_close_ms"]):
            _clear_transient_error(db)
            return {
                "action": "waiting",
                "candle_close_ms": int(signal["candle_close_ms"]),
            }

        target_long = bool(signal["target_long"])
        if tracked_qty == 0 and not target_long:
            _clear_transient_error(db)
            try:
                _persist_decision(
                    db, signal, "hold_cash", None,
                    require_desired=enforce_desired,
                    learning_refresh_healthy=learning_refresh_healthy,
                )
            except WorkerStopped:
                return {"action": "stopped"}
            return {
                "action": "hold_cash",
                "momentum": format(signal["momentum"], "f"),
                "candle_close_ms": int(signal["candle_close_ms"]),
            }
        if tracked_qty > 0 and target_long:
            _clear_transient_error(db)
            try:
                _persist_decision(
                    db, signal, "hold_long", None,
                    require_desired=enforce_desired,
                    learning_refresh_healthy=learning_refresh_healthy,
                )
            except WorkerStopped:
                return {"action": "stopped"}
            return {
                "action": "hold_long",
                "momentum": format(signal["momentum"], "f"),
                "position_qty": format(tracked_qty, "f"),
                "candle_close_ms": int(signal["candle_close_ms"]),
            }

        try:
            rules = api.symbol_rules(symbol=SYMBOL)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Symbol-rules read outage: {exc}")
        min_notional = _decimal(_rule(rules, "min_notional"), "minimum notional")
        step_size = _decimal(_rule(rules, "step_size"), "step size")
        min_qty = _decimal(_rule(rules, "min_qty"), "minimum quantity")

        candle_ms = int(signal["candle_close_ms"])
        if tracked_qty == 0:
            if not learning_refresh_healthy:
                _clear_transient_error(db)
                try:
                    _persist_decision(
                        db,
                        signal,
                        "hold_cash",
                        None,
                        require_desired=enforce_desired,
                        learning_refresh_healthy=False,
                    )
                except WorkerStopped:
                    return {"action": "stopped"}
                return {
                    "action": "learning_refresh_unhealthy",
                    "reason": "New entry deferred until learning recovery completes.",
                    "candle_close_ms": candle_ms,
                }
            if ENTRY_QUOTE_USDT < min_notional:
                reason = (
                    f"Fixed {ENTRY_QUOTE_USDT} USDT entry is below Binance minimum "
                    f"notional {min_notional}."
                )
                _halt(db, reason)
                return {"action": "halted", "reason": reason}
            try:
                free_quote = _free_balance(api.account(), QUOTE_ASSET)
            except execution.BinanceTransportError as exc:
                return _transient_retry(db, f"Account-balance read outage: {exc}")
            except Exception as exc:
                reason = f"USDT balance safety check failed: {type(exc).__name__}: {exc}"
                _halt(db, reason)
                return {"action": "halted", "reason": reason}
            if free_quote < ENTRY_QUOTE_USDT:
                reason = (
                    f"Insufficient free Testnet USDT: need {ENTRY_QUOTE_USDT}, "
                    f"available {free_quote}."
                )
                _halt(db, reason)
                return {"action": "halted", "reason": reason}
            _clear_transient_error(db)
            side = "BUY"
            client_id = _client_order_id(candle_ms, side)
            try:
                claimed = _persist_decision(
                    db,
                    signal,
                    "buy",
                    client_id,
                    side,
                    requested_quote=ENTRY_QUOTE_USDT,
                    require_desired=enforce_desired,
                    learning_refresh_healthy=learning_refresh_healthy,
                )
            except WorkerStopped:
                return {"action": "stopped"}
            if not claimed:
                return {"action": "waiting", "candle_close_ms": candle_ms}
            return _submit_prepared(
                db, api, client_id, side, quote=ENTRY_QUOTE_USDT,
                require_desired=enforce_desired,
            )

        side = "SELL"
        origin_result = _verify_position_origin(db, api, tracked_qty)
        if origin_result is not None:
            return origin_result
        quantity = execution.floor_step(tracked_qty, step_size)
        try:
            ticker = api.book_ticker(symbol=SYMBOL)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Book-ticker read outage: {exc}")
        if not isinstance(ticker, Mapping):
            reason = "Binance book ticker returned an invalid response."
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        bid = _decimal(ticker.get("bidPrice"), "best bid")
        if quantity < min_qty or quantity * bid < min_notional:
            reason = (
                f"Tracked worker position is dust: sellable={quantity} BTC, "
                f"notional={quantity * bid} USDT. Manual Testnet cleanup is required."
            )
            _halt(db, reason)
            return {"action": "dust", "reason": reason, "position_qty": str(tracked_qty)}
        try:
            free_base = _free_balance(api.account(), BASE_ASSET)
        except execution.BinanceTransportError as exc:
            return _transient_retry(db, f"Account-balance read outage: {exc}")
        except Exception as exc:
            reason = f"BTC balance safety check failed: {type(exc).__name__}: {exc}"
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        _clear_transient_error(db)
        if free_base < quantity:
            reason = (
                f"Insufficient free Testnet BTC for tracked SELL: need {quantity}, "
                f"available {free_base}."
            )
            _halt(db, reason)
            return {"action": "halted", "reason": reason}
        client_id = _client_order_id(candle_ms, side)
        try:
            claimed = _persist_decision(
                db, signal, "sell", client_id, side, requested_qty=quantity,
                require_desired=enforce_desired,
                learning_refresh_healthy=learning_refresh_healthy,
            )
        except WorkerStopped:
            return {"action": "stopped"}
        if not claimed:
            return {"action": "waiting", "candle_close_ms": candle_ms}
        return _submit_prepared(
            db, api, client_id, side, quantity=quantity,
            require_desired=enforce_desired,
        )


def _pid_alive(pid: int) -> bool:
    if pid <= 0 or pid > 0xFFFFFFFF:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not reliable for detached processes on
        # Windows/Python 3.12 and can surface WinError 87 as SystemError.
        # A zero-time wait on a synchronization handle is read-only and works
        # for both console and DETACHED_PROCESS workers.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            wait_for_single_object = kernel32.WaitForSingleObject
            wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            wait_for_single_object.restype = wintypes.DWORD
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL

            synchronize = 0x00100000
            wait_object_0 = 0x00000000
            wait_timeout = 0x00000102
            handle = open_process(synchronize, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER is Windows' normal answer for a PID
                # that no longer exists.  Access denied or an unknown probe
                # failure must keep the lock active (fail closed), otherwise a
                # second worker could start beside a live protected process.
                return ctypes.get_last_error() != 87
            try:
                wait_result = wait_for_single_object(handle, 0)
                if wait_result == wait_timeout:
                    return True
                if wait_result == wait_object_0:
                    return False
                return True
            finally:
                close_handle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock(path: Path | str = LOCK_PATH) -> dict[str, object] | None:
    lock_path = Path(path)
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _open_advisory_lock_file(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")
        os.fsync(fd)
    return fd


def _try_advisory_lock(fd: int) -> bool:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised on non-Windows CI/hosts
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_advisory_lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - exercised on non-Windows CI/hosts
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_lock_metadata_fd(fd: int) -> dict[str, object] | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8192).decode("utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _metadata_has_live_owner(info: Mapping[str, object] | None) -> bool:
    if not info:
        return False
    try:
        pid = int(info.get("pid", 0))
        protocol = int(info.get("lock_protocol", 1))
    except (TypeError, ValueError):
        return False
    if protocol >= 2 and info.get("released") is True:
        return False
    return _pid_alive(pid)


def _write_lock_metadata(fd: int, token: str, *, released: bool = False) -> None:
    now = _now_ms()
    payload = _json(
        {
            "lock_protocol": 2,
            "pid": 0 if released else os.getpid(),
            "token": token,
            "started_ms": now,
            "released": released,
            "released_ms": now if released else None,
        }
    )
    encoded = payload.encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, encoded)
    os.fsync(fd)


def _lock_active(path: Path | str = LOCK_PATH) -> bool:
    lock_path = Path(path)
    if not lock_path.exists():
        return False
    try:
        fd = _open_advisory_lock_file(lock_path)
    except OSError:
        return True
    acquired = False
    try:
        acquired = _try_advisory_lock(fd)
        if not acquired:
            return True
        # A worker from the previous lock protocol owns only live-PID metadata,
        # not an advisory byte lock. Honour it during this rolling upgrade.
        return _metadata_has_live_owner(_read_lock_metadata_fd(fd))
    finally:
        if acquired:
            try:
                _release_advisory_lock(fd)
            finally:
                os.close(fd)
        else:
            os.close(fd)


@contextmanager
def _process_lock(path: Path | str = LOCK_PATH) -> Iterator[None]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    try:
        fd = _open_advisory_lock_file(lock_path)
    except OSError as exc:
        raise WorkerHalt("Binance Testnet worker is already running.") from exc
    try:
        acquired = _try_advisory_lock(fd)
    except BaseException:
        os.close(fd)
        raise
    if not acquired:
        os.close(fd)
        raise WorkerHalt("Binance Testnet worker is already running.")
    try:
        legacy_owner = _metadata_has_live_owner(_read_lock_metadata_fd(fd))
    except BaseException:
        try:
            _release_advisory_lock(fd)
        finally:
            os.close(fd)
        raise
    if legacy_owner:
        try:
            _release_advisory_lock(fd)
        finally:
            os.close(fd)
        raise WorkerHalt("Binance Testnet worker is already running.")
    try:
        _write_lock_metadata(fd, token)
        yield
    finally:
        metadata_error: BaseException | None = None
        try:
            _write_lock_metadata(fd, token, released=True)
        except BaseException as exc:
            metadata_error = exc
        try:
            _release_advisory_lock(fd)
        finally:
            os.close(fd)
        if metadata_error is not None:
            raise metadata_error


def _control_lock_path(worker_lock_path: Path | str) -> Path:
    path = Path(worker_lock_path)
    if path.resolve().parent == STATE_DIR.resolve():
        return CONTROL_LOCK_PATH
    return path.with_name(f"{path.stem}-control{path.suffix or '.lock'}")


def _account_lock_path(worker_lock_path: Path | str) -> Path:
    path = Path(worker_lock_path)
    if path.resolve().parent == STATE_DIR.resolve():
        return ACCOUNT_LOCK_PATH
    return path.with_name(f"{path.stem}-account{path.suffix or '.lock'}")


@contextmanager
def _control_mutex(worker_lock_path: Path | str = LOCK_PATH) -> Iterator[None]:
    """Serialize start/stop/reset so only one control transition can win."""
    path = _control_lock_path(worker_lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    token = uuid.uuid4().hex
    fd: int | None = None
    acquired = False
    while True:
        try:
            fd = _open_advisory_lock_file(path)
        except OSError as exc:
            raise WorkerHalt(
                "Another Binance Testnet control action is in progress."
            ) from exc
        try:
            acquired = _try_advisory_lock(fd)
            legacy_owner = bool(
                acquired
                and _metadata_has_live_owner(_read_lock_metadata_fd(fd))
            )
        except BaseException:
            if acquired:
                try:
                    _release_advisory_lock(fd)
                finally:
                    os.close(fd)
            else:
                os.close(fd)
            raise
        if acquired and not legacy_owner:
            break
        if acquired:
            try:
                _release_advisory_lock(fd)
            finally:
                os.close(fd)
            acquired = False
        else:
            os.close(fd)
        fd = None
        if time.monotonic() >= deadline:
            raise WorkerHalt("Another Binance Testnet control action is in progress.")
        time.sleep(0.05)
    if fd is None:  # pragma: no cover - loop exits only with an open descriptor
        raise WorkerHalt("Unable to acquire Binance Testnet control lock.")
    try:
        _write_lock_metadata(fd, token)
        yield
    finally:
        metadata_error: BaseException | None = None
        if acquired:
            try:
                _write_lock_metadata(fd, token, released=True)
            except BaseException as exc:
                metadata_error = exc
            try:
                _release_advisory_lock(fd)
            finally:
                os.close(fd)
        else:
            os.close(fd)
        if metadata_error is not None:
            raise metadata_error


@contextmanager
def learning_maintenance_lease(
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
) -> Iterator[dict[str, object]]:
    """Exclude worker start/run while a stopped-ledger migration is applied."""

    with _control_mutex(lock_path):
        initial = status_snapshot(db_path, lock_path)
        if (
            initial.get("status_available") is not True
            or initial.get("execution_state_known") is not True
        ):
            raise WorkerHalt(
                "Binance Testnet worker state is unavailable for learning maintenance."
            )
        if initial.get("running") or initial.get("desired_running"):
            raise WorkerHalt(
                "Historical learning maintenance requires a fully stopped worker."
            )
        if initial.get("pending_client_id"):
            raise WorkerHalt(
                "Historical learning maintenance refuses a pending order intent."
            )
        # A directly launched run process bypasses the control command but must
        # still own this account-wide lock. Holding it closes that race too.
        with _process_lock(_account_lock_path(lock_path)):
            stable = status_snapshot(db_path, lock_path)
            if (
                stable.get("status_available") is not True
                or stable.get("execution_state_known") is not True
                or stable.get("running")
                or stable.get("desired_running")
                or stable.get("pending_client_id")
            ):
                raise WorkerHalt(
                    "Binance Testnet worker state changed during learning maintenance."
                )
            yield stable


@contextmanager
def learning_upgrade_lease(
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
    client: object | None = None,
) -> Iterator[dict[str, object]]:
    """Stop, exclusively maintain, and restore the worker as one control action.

    The control mutex stays owned across the complete transition.  Therefore a
    later operator stop can neither be overwritten by a stale restart decision
    nor race the failure-recovery path.  A directly launched run process is
    excluded by the account-wide execution lease during the maintenance body.
    """

    api = client if client is not None else execution.Client()
    with _control_mutex(lock_path):
        initial = status_snapshot(db_path, lock_path)
        if (
            initial.get("status_available") is not True
            or initial.get("execution_state_known") is not True
        ):
            raise WorkerHalt(
                "Binance Testnet worker state is unavailable for learning upgrade."
            )
        if bool(initial.get("running")) != bool(initial.get("desired_running")):
            raise WorkerHalt(
                "Binance Testnet worker is already in a start/stop transition."
            )
        if initial.get("pending_client_id"):
            raise WorkerHalt(
                "Historical learning upgrade refuses a pending order intent."
            )
        # Prove every prerequisite needed by the recovery path before changing
        # durable run intent.  A matching public API key alone does not prove
        # that its secret is valid for signed Testnet requests.
        _execution_gate()
        try:
            signed_account = api.account()
        except Exception as exc:
            raise WorkerHalt(
                f"Binance Testnet signed account proof failed: {exc}"
            ) from exc
        if not isinstance(signed_account, Mapping):
            raise WorkerHalt(
                "Binance Testnet signed account proof returned an invalid response."
            )
        with closing(_connect_read_only(db_path)) as db:
            _assert_account_fingerprint(db, api, allow_unbound=False)

        restart_required = bool(initial.get("desired_running"))
        intent_changed = False
        operation_error: BaseException | None = None
        try:
            if restart_required:
                intent_changed = True
                _set_desired(db_path, False)
                deadline = time.monotonic() + STOP_WAIT_SECONDS
                while _lock_active(lock_path) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if _lock_active(lock_path):
                    raise WorkerHalt(
                        "Learning upgrade requested a stop, but the worker did "
                        "not release its lock before the deadline."
                    )

            account_lock_path = _account_lock_path(lock_path)
            account_deadline = time.monotonic() + STARTUP_WAIT_SECONDS
            while (
                _lock_active(account_lock_path)
                and time.monotonic() < account_deadline
            ):
                time.sleep(0.05)
            if _lock_active(account_lock_path):
                raise WorkerHalt(
                    "Stopped worker did not release its account-wide execution lease."
                )

            with _process_lock(account_lock_path):
                try:
                    stable = status_snapshot(db_path, lock_path)
                    if (
                        stable.get("status_available") is not True
                        or stable.get("execution_state_known") is not True
                        or stable.get("running")
                        or stable.get("desired_running")
                        or stable.get("pending_client_id")
                    ):
                        raise WorkerHalt(
                            "Binance Testnet worker state changed during learning upgrade."
                        )
                    yield {
                        "initial": initial,
                        "stopped": stable,
                        "restart_required": restart_required,
                    }
                finally:
                    if restart_required and intent_changed:
                        # Arm durable intent before releasing the account lease.
                        # A directly launched run process that wins the handoff
                        # will then remain alive and safely becomes the worker.
                        _set_desired(db_path, True)
        except BaseException as exc:
            operation_error = exc
        finally:
            if restart_required and intent_changed:
                try:
                    _control_locked(
                        "recover",
                        db_path=db_path,
                        lock_path=lock_path,
                        client=api,
                    )
                except BaseException as recovery_error:
                    if operation_error is None:
                        raise
                    raise WorkerHalt(
                        "Learning upgrade failed and the original running state "
                        f"could not be restored. Upgrade error: {operation_error}; "
                        f"recovery error: {recovery_error}"
                    ) from recovery_error
        if operation_error is not None:
            raise operation_error


def _set_desired(db_path: Path | str, desired: bool) -> None:
    with closing(_connect(db_path)) as db, _transaction(db):
        db.execute(
            "UPDATE worker_state SET desired_running=?, updated_ms=? WHERE singleton=1",
            (int(desired), _now_ms()),
        )


def _is_definitive_order_not_found(exc: BaseException) -> bool:
    """Accept only Binance's structured ``-2013`` order-query response."""

    return bool(
        isinstance(exc, execution.BinanceOrderNotFoundError)
        and exc.method == "GET"
        and exc.path == "/v3/order"
        and exc.api_code == -2013
        and exc.signed is True
    )


def _prove_open_position_origin_was_reset(
    db: sqlite3.Connection, state: Mapping[str, object], client: object | None
) -> None:
    """Require definitive remote deletion before discarding a tracked position."""

    tracked_qty = _decimal(state["position_qty"], "tracked position")
    if tracked_qty == 0:
        return
    if tracked_qty < 0:
        raise WorkerHalt("Testnet epoch reset refuses a negative tracked position.")
    origin = db.execute(
        """SELECT client_id, net_base_qty FROM worker_order_intents
           WHERE side='BUY' AND state='filled'
           ORDER BY decision_ms DESC LIMIT 1"""
    ).fetchone()
    if origin is None:
        raise WorkerHalt(
            "Testnet epoch reset refuses an open position without local BUY provenance."
        )
    origin_qty = _decimal(origin["net_base_qty"], "origin net quantity")
    if origin_qty <= 0 or tracked_qty > origin_qty:
        raise WorkerHalt(
            "Testnet epoch reset refuses inconsistent local BUY provenance."
        )
    if client is None:
        raise WorkerHalt(
            "Testnet epoch reset needs a signed order lookup for the open position."
        )
    client_id = str(origin["client_id"])
    try:
        client.order_by_client_id(client_id, symbol=SYMBOL)
    except Exception as exc:
        if _is_definitive_order_not_found(exc):
            return
        raise WorkerHalt(
            f"Testnet epoch reset could not prove that BUY origin {client_id} was "
            f"deleted ({type(exc).__name__}: {exc})."
        ) from exc
    raise WorkerHalt(
        f"Testnet epoch reset refuses to discard open position; BUY origin "
        f"{client_id} still exists on Binance Spot Testnet."
    )


def _archive_epoch_and_reset(
    db_path: Path | str, client: object | None = None
) -> dict[str, int]:
    """Archive the active ledger in-place and atomically initialize a new epoch."""
    reset_block_reason: str | None = None
    epoch_id = 0
    decisions: list[dict[str, object]] = []
    intents: list[dict[str, object]] = []
    with closing(_connect(db_path)) as db:
        # A direct caller must establish or match the same account binding as
        # the public reset path before any destructive transaction can begin.
        _ensure_account_binding(db, client)
        learning = _prepare_learning_before_decision(db_path)
        if not _learning_refresh_allows_source_mutation(learning):
            raise WorkerHalt(
                "Testnet epoch reset requires a healthy learning-source refresh."
            )
        with _transaction(db):
            _assert_account_fingerprint(db, client, allow_unbound=False)
            state = dict(_state(db))
            if state["desired_running"]:
                raise WorkerHalt("Testnet epoch reset requires desired_running=false.")
            if state["pending_client_id"]:
                raise WorkerHalt("Testnet epoch reset refuses a pending managed intent.")
            if _unresolved_learning_outbox_count(db):
                raise WorkerHalt(
                    "Testnet epoch reset refuses unresolved learning evidence."
                )
            if not _learning_source_writes_healthy(db):
                raise WorkerHalt(
                    "Testnet epoch reset refuses a pending learning transition."
                )
            # Re-check from the durable origin client id inside the same transaction
            # that will clear the ledger. Direct callers cannot bypass this proof.
            _prove_open_position_origin_was_reset(db, state, client)
            decisions = [dict(row) for row in db.execute(
                "SELECT * FROM worker_decisions ORDER BY candle_close_ms"
            )]
            intents = [dict(row) for row in db.execute(
                "SELECT * FROM worker_order_intents ORDER BY decision_ms, client_id"
            )]
            archive_sequence = int(
                db.execute("SELECT COUNT(*) FROM worker_epochs").fetchone()[0]
            ) + 1
            preserved_last_candle = state["last_candle_close_ms"]
            if decisions:
                archived_max_candle = max(
                    int(row["candle_close_ms"]) for row in decisions
                )
                preserved_last_candle = max(
                    archived_max_candle,
                    (
                        archived_max_candle
                        if preserved_last_candle is None
                        else int(preserved_last_candle)
                    ),
                )
            now = _now_ms()
            quarantine_result = _safe_learning_source_call(
                db,
                "epoch_reset_quarantine",
                learning_store.quarantine_open_round_trips,
                db,
                "testnet_epoch_reset",
                now,
                policy=POLICY,
                model_version=POLICY_SPEC_HASH,
                outbox_reference={
                    "reason": "testnet_epoch_reset",
                    "policy": POLICY,
                    "model_version": POLICY_SPEC_HASH,
                    "archive_sequence": archive_sequence,
                },
            )
            if quarantine_result is _LEARNING_SOURCE_CALL_FAILED:
                # Commit the durable repair event while preserving every active
                # provenance row.  A later reset can proceed only after replay.
                reset_block_reason = (
                    "Testnet epoch reset deferred because learning evidence "
                    "quarantine is unresolved."
                )
            else:
                archived_state = dict(state)
                archived_state.pop("api_key_fingerprint_sha256", None)
                archived_state["account_bound"] = True
                cursor = db.execute(
                    """INSERT INTO worker_epochs
                       (archived_ms, reason, state_json, decision_count, order_count)
                       VALUES (?, 'binance_spot_testnet_epoch_reset', ?, ?, ?)""",
                    (now, _json(archived_state), len(decisions), len(intents)),
                )
                epoch_id = int(cursor.lastrowid)
                db.executemany(
                    """INSERT INTO worker_epoch_decisions
                       (epoch_id, candle_close_ms, record_json) VALUES (?, ?, ?)""",
                    [
                        (epoch_id, int(row["candle_close_ms"]), _json(row))
                        for row in decisions
                    ],
                )
                db.executemany(
                    """INSERT INTO worker_epoch_order_intents
                       (epoch_id, client_id, record_json) VALUES (?, ?, ?)""",
                    [
                        (epoch_id, str(row["client_id"]), _json(row))
                        for row in intents
                    ],
                )
                db.execute("DELETE FROM worker_order_intents")
                db.execute("DELETE FROM worker_decisions")
                db.execute(
                    """UPDATE worker_state SET desired_running=0, halted=0,
                       halt_reason=NULL, position_qty='0', position_quote_cost='0',
                       last_candle_close_ms=?, pending_client_id=NULL, pending_side=NULL,
                       pending_decision_ms=NULL, pending_quote=NULL, pending_qty=NULL,
                       last_error=NULL, realized_pnl_usdt='0', pnl_complete=1,
                       pnl_incomplete_reason=NULL, completed_round_trips=0,
                       transient_failures=0, last_transient_error_ms=NULL,
                       created_ms=?, updated_ms=? WHERE singleton=1""",
                    (preserved_last_candle, now, now),
                )
    if reset_block_reason is not None:
        raise WorkerHalt(reset_block_reason)
    return {
        "reset_epoch_id": epoch_id,
        "archived_decisions": len(decisions),
        "archived_orders": len(intents),
    }


def _learning_status_nonfatal(db_path: Path | str) -> dict[str, object]:
    try:
        return learning_store.status_snapshot(
            db_path, _learning_db_path(db_path)
        )
    except Exception as exc:
        return {
            "schema": learning_store.SCHEMA_VERSION,
            "mode": "proposal_only_manual_review_required",
            "status": "learning_status_unavailable",
            "last_error": f"{type(exc).__name__}: {exc}",
            "proposal_ready_for_review": False,
            "automatic_activation_enabled": False,
            "writes_active_config": False,
            "testnet_execution_eligible": False,
            "paper_eligible": False,
            "real_money_eligible": False,
            "real_orders_enabled": False,
            "live_trading_enabled": False,
        }


def _record_learning_refresh_error(
    db_path: Path | str, exc: Exception, attempted_ms: int
) -> None:
    """Atomically persist refresh health and any immutable integrity evidence."""

    message = f"refresh: {type(exc).__name__}: {exc}"
    try:
        with closing(_connect(db_path)) as db, _transaction(db):
            if isinstance(exc, learning_store.LearningIntegrityError):
                latest = db.execute(
                    """SELECT message, created_ms FROM worker_learning_source_errors
                       ORDER BY created_ms DESC LIMIT 1"""
                ).fetchone()
                duplicate_age = (
                    None
                    if latest is None
                    else attempted_ms - int(latest["created_ms"])
                )
                if not (
                    latest is not None
                    and str(latest["message"]) == message
                    and duplicate_age is not None
                    and 0 <= duplicate_age < 60 * 60 * 1000
                ):
                    learning_store.record_source_error(
                        db, message, attempted_ms
                    )
            learning_store.mark_refresh_failure(
                db,
                f"{type(exc).__name__}: {exc}",
                attempted_ms,
            )
    except Exception:
        pass


def _force_learning_refresh_unhealthy(
    snapshot: dict[str, object], message: str, now_ms: int
) -> dict[str, object]:
    snapshot["status"] = "learning_refresh_unhealthy"
    snapshot["proposal_ready_for_review"] = False
    snapshot["last_refresh_error"] = message
    snapshot["refresh_health"] = {
        "healthy": False,
        "last_attempt_failed": True,
        "sanitized_error": message,
        "attempted_ms": now_ms,
        "last_success_ms": (
            snapshot.get("refresh_health", {}).get("last_success_ms")
            if isinstance(snapshot.get("refresh_health"), Mapping)
            else None
        ),
    }
    latest = snapshot.get("latest")
    if isinstance(latest, dict):
        latest["status"] = "learning_refresh_unhealthy"
        latest["proposal_ready_for_review"] = False
        latest["last_error"] = message
    return snapshot


def _refresh_learning_nonfatal(db_path: Path | str) -> dict[str, object]:
    attempted_ms = _now_ms()
    try:
        learning_store.refresh(
            db_path, _learning_db_path(db_path), now_ms=attempted_ms
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        _record_learning_refresh_error(db_path, exc, attempted_ms)
        snapshot = _learning_status_nonfatal(db_path)
        return _force_learning_refresh_unhealthy(
            snapshot, message, attempted_ms
        )
    try:
        with closing(_connect(db_path)) as db:
            learning_store.mark_refresh_success(db, attempted_ms)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        snapshot = _learning_status_nonfatal(db_path)
        return _force_learning_refresh_unhealthy(
            snapshot, message, attempted_ms
        )
    # Re-read after clearing health; refresh() may have returned a snapshot
    # carrying the prior failure overlay.
    return _learning_status_nonfatal(db_path)


def _learning_refresh_allows_source_mutation(
    snapshot: Mapping[str, object],
) -> bool:
    """Require affirmative refresh health and no staged candidate transition."""

    health = snapshot.get("refresh_health")
    if not isinstance(health, Mapping):
        return False
    if health.get("healthy") is not True or health.get("last_attempt_failed") is True:
        return False
    status = str(snapshot.get("status", ""))
    latest = snapshot.get("latest")
    latest_status = (
        str(latest.get("status", "")) if isinstance(latest, Mapping) else ""
    )
    blocked_statuses = {
        "candidate_transition_pending",
        "learning_refresh_unhealthy",
        "learning_status_unavailable",
    }
    return status not in blocked_statuses and latest_status not in blocked_statuses


def _prepare_learning_before_decision(
    db_path: Path | str,
) -> dict[str, object]:
    """Recover aggregate/source transitions before replaying causal evidence."""

    first = _refresh_learning_nonfatal(db_path)
    if not _learning_refresh_allows_source_mutation(first):
        return first

    adoption_failed = False
    with closing(_connect(db_path)) as db:
        revision_before = int(db.execute(
            """SELECT learning_revision FROM worker_learning_meta
               WHERE singleton=1"""
        ).fetchone()[0])
        _replay_learning_outbox(db)
        if _unresolved_learning_outbox_count(db) == 0:
            adopted = _safe_learning_source_call(
                db,
                "adopt_open_round_trip",
                learning_store.adopt_open_round_trip,
                db,
                POLICY,
                POLICY_SPEC_HASH,
                _now_ms(),
            )
            adoption_failed = adopted is _LEARNING_SOURCE_CALL_FAILED
        revision_after = int(db.execute(
            """SELECT learning_revision FROM worker_learning_meta
               WHERE singleton=1"""
        ).fetchone()[0])
    if adoption_failed:
        attempted_ms = _now_ms()
        exc = learning_store.LearningStoreError(
            "Legacy learning-source adoption is temporarily unavailable."
        )
        _record_learning_refresh_error(db_path, exc, attempted_ms)
        return _force_learning_refresh_unhealthy(
            _learning_status_nonfatal(db_path),
            f"{type(exc).__name__}: {exc}",
            attempted_ms,
        )

    if revision_after == revision_before:
        return _learning_status_nonfatal(db_path)

    # Recompute the aggregate after ordered replay/adoption so the readiness
    # watermark and any transition staged by this refresh are current before
    # the execution decision starts.
    return _refresh_learning_nonfatal(db_path)


def learning_snapshot(
    db_path: Path | str = DB_PATH, *, refresh: bool = False
) -> dict[str, object]:
    """Read proposal-only learning progress without requiring API credentials."""

    if not refresh:
        return _learning_status_nonfatal(db_path)
    # Ensure the source schema exists and adopt any legacy managed open position.
    with closing(_connect(db_path)):
        pass
    return _refresh_learning_nonfatal(db_path)


def _caller_environment_status() -> dict[str, bool]:
    return {
        "caller_env_credentials_present": bool(
            os.getenv("BINANCE_TESTNET_API_KEY")
            and os.getenv("BINANCE_TESTNET_SECRET_KEY")
        ),
        "caller_env_worker_enabled": (
            os.getenv("BINANCE_TESTNET_WORKER_ENABLED", "").lower() == "true"
        ),
        "caller_env_execution_enabled": (
            os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") == "testnet"
        ),
    }


def _unavailable_status_snapshot(
    db_path: Path | str,
    lock_path: Path | str,
    exc: BaseException,
) -> dict[str, object]:
    detail = " ".join(str(exc).split())[:500]
    error = type(exc).__name__ if not detail else f"{type(exc).__name__}: {detail}"
    return {
        "status": "worker_status_unavailable",
        "status_available": False,
        "status_error": error,
        "execution_state_known": False,
        "configured_policy": POLICY,
        "policy": None,
        "policy_model_version": None,
        "policy_match": False,
        "momentum_threshold": format(MOMENTUM_THRESHOLD, "f"),
        "entry_quote_usdt": format(ENTRY_QUOTE_USDT, "f"),
        "symbol": SYMBOL,
        "market_data_source": "binance_public_spot",
        "execution_environment": "binance_spot_testnet",
        "account_bound": None,
        "running": _lock_active(lock_path),
        "desired_running": None,
        "halted": None,
        "halt_reason": None,
        "position": "unknown",
        "tracked_position_qty": None,
        "tracked_position_cost_usdt": None,
        "realized_pnl_usdt": None,
        "pnl_complete": None,
        "pnl_incomplete_reason": None,
        "completed_round_trips": None,
        "archived_epochs": None,
        "unresolved_learning_outbox": None,
        "transient_failures": None,
        "last_error": None,
        "pending_client_id": None,
        "last_candle_close_ms": None,
        "intents": None,
        "filled_orders": None,
        "latest_decision": None,
        "learning": _learning_status_nonfatal(db_path),
        **_caller_environment_status(),
    }


def status_snapshot(
    db_path: Path | str = DB_PATH, lock_path: Path | str = LOCK_PATH
) -> dict[str, object]:
    try:
        with closing(_connect_read_only(db_path)) as db:
            # One deferred read transaction pins all fields to the same SQLite
            # snapshot while the worker may concurrently commit a fill/reset.
            db.execute("BEGIN")
            try:
                state = dict(_state(db))
                counts = db.execute(
                    """SELECT COUNT(*) AS intents,
                       COALESCE(SUM(state='filled'),0) AS filled
                       FROM worker_order_intents"""
                ).fetchone()
                latest = db.execute(
                    """SELECT candle_close_ms, momentum, action, client_id
                       FROM worker_decisions ORDER BY candle_close_ms DESC LIMIT 1"""
                ).fetchone()
                archived_epochs = int(
                    db.execute("SELECT COUNT(*) FROM worker_epochs").fetchone()[0]
                )
                unresolved_learning_outbox = _unresolved_learning_outbox_count(db)
            finally:
                if db.in_transaction:
                    db.execute("ROLLBACK")
        qty = _decimal(state["position_qty"], "tracked position")
        return {
            "status": "available",
            "status_available": True,
            "status_error": None,
            "execution_state_known": True,
            "policy": state["active_policy"],
            "policy_model_version": state["policy_spec_hash"],
            "configured_policy": POLICY,
            "decision_interval": INTERVAL,
            "caller_env_automatic_model_authority": HOURLY_MODE and os.getenv("BINANCE_TESTNET_AUTO_MODEL") == "true",
            "policy_match": (
                state["active_policy"] == POLICY
                and state["policy_spec_hash"] == POLICY_SPEC_HASH
            ),
            "momentum_threshold": format(MOMENTUM_THRESHOLD, "f"),
            "entry_quote_usdt": format(ENTRY_QUOTE_USDT, "f"),
            "symbol": SYMBOL,
            "market_data_source": "binance_public_spot",
            "execution_environment": "binance_spot_testnet",
            "account_bound": state["api_key_fingerprint_sha256"] is not None,
            "running": _lock_active(lock_path),
            "desired_running": bool(state["desired_running"]),
            "halted": bool(state["halted"]),
            "halt_reason": state["halt_reason"],
            "position": "cash" if qty == 0 else "long",
            "tracked_position_qty": format(qty, "f"),
            "tracked_position_cost_usdt": state["position_quote_cost"],
            "realized_pnl_usdt": state["realized_pnl_usdt"],
            "pnl_complete": bool(state["pnl_complete"]),
            "pnl_incomplete_reason": state["pnl_incomplete_reason"],
            "completed_round_trips": int(state["completed_round_trips"]),
            "archived_epochs": archived_epochs,
            "unresolved_learning_outbox": unresolved_learning_outbox,
            "transient_failures": int(state["transient_failures"]),
            "last_error": state["last_error"],
            "pending_client_id": state["pending_client_id"],
            "last_candle_close_ms": state["last_candle_close_ms"],
            "intents": int(counts["intents"]),
            "filled_orders": int(counts["filled"]),
            "latest_decision": dict(latest) if latest else None,
            "learning": _learning_status_nonfatal(db_path),
            **_caller_environment_status(),
        }
    except Exception as exc:
        return _unavailable_status_snapshot(db_path, lock_path, exc)


def run_forever(
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
    poll_seconds: float = POLL_SECONDS,
    api_poll_seconds: float = API_POLL_SECONDS,
    arm: bool = False,
    client: object | None = None,
    market_data_client: object | None = None,
) -> dict[str, object]:
    try:
        _execution_gate()
    except Exception:
        try:
            _set_desired(db_path, False)
        except Exception:
            pass
        raise
    if arm:
        _set_desired(db_path, True)
    api = client if client is not None else execution.Client()
    market = (
        market_data_client
        if market_data_client is not None
        else execution.PublicMarketDataClient()
    )
    stop_event = threading.Event()
    control_poll = max(0.1, min(float(poll_seconds), 5.0))
    api_poll = max(API_POLL_SECONDS, float(api_poll_seconds))
    next_api_poll = 0.0
    # The account-wide lease is always acquired first. Therefore an active
    # policy worker lock implies that the same correctly launched process has
    # already passed the account exclusion gate.
    with _process_lock(_account_lock_path(lock_path)), _process_lock(lock_path):
        with closing(_connect(db_path)) as db:
            _ensure_account_binding(db, api)
        # Recover or bind any aggregate learner candidate before the next daily
        # decision can seal a label. This sidecar call is deliberately nonfatal.
        _prepare_learning_before_decision(db_path)
        try:
            while True:
                with closing(_connect(db_path)) as db:
                    state = _state(db)
                    pending = bool(state["pending_client_id"])
                    if not state["desired_running"] and not pending:
                        break
                    if not state["desired_running"] and pending:
                        # Stop still permits only reconciliation of an intent
                        # that may already have reached Binance.
                        next_api_poll = 0.0
                    # A halted pending intent gets exactly one reconciliation pass
                    # after an explicit restart. Other halted states are terminal.
                    if state["halted"] and not pending:
                        break
                now_mono = time.monotonic()
                if now_mono < next_api_poll:
                    stop_event.wait(min(control_poll, next_api_poll - now_mono))
                    continue
                result = run_once(
                    db_path=db_path,
                    client=api,
                    market_data_client=market,
                    enforce_desired=True,
                )
                _refresh_learning_nonfatal(db_path)
                if HOURLY_MODE:
                    try:
                        import hourly_testnet_learning
                        hourly_testnet_learning.refresh(market)
                    except Exception:
                        # Execution reconciliation and exits must remain available.
                        pass
                delay = max(api_poll, float(result.get("backoff_seconds", 0)))
                next_api_poll = time.monotonic() + delay
                if result.get("action") == "halted":
                    break
                # desired_running remains responsive without repeating API calls.
                stop_event.wait(control_poll)
        except Exception as exc:
            try:
                with closing(_connect(db_path)) as db:
                    _halt(
                        db,
                        f"Worker loop failed closed: {type(exc).__name__}: {exc}",
                    )
            except Exception:
                try:
                    _set_desired(db_path, False)
                except Exception:
                    pass
            raise
    return status_snapshot(db_path, lock_path)


def _control_locked(
    action: str,
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
    client: object | None = None,
) -> dict[str, object]:
    """Start, stop, or inspect the detached Testnet worker."""
    command = action.lower().strip()
    if command == "status":
        return status_snapshot(db_path, lock_path)
    if command == "stop":
        _set_desired(db_path, False)
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        while _lock_active(lock_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _lock_active(lock_path):
            raise WorkerHalt(
                "Stop was requested, but the worker has not released its lock; "
                "no stopped state is being reported yet."
            )
        return status_snapshot(db_path, lock_path)
    if command == "reset":
        _reset_gate()
        if _lock_active(lock_path):
            raise WorkerHalt("Testnet epoch reset requires the worker to be stopped.")
        account_lock_path = _account_lock_path(lock_path)
        if _lock_active(account_lock_path):
            raise WorkerHalt(
                "Testnet epoch reset refuses an active account-wide execution lease."
            )
        # Hold the same account-wide lease as the execution loop throughout the
        # remote checks and local archive, closing the check/use race with a
        # directly launched worker.
        with _process_lock(account_lock_path):
            api = client if client is not None else execution.Client()
            with closing(_connect(db_path)) as db:
                state = _state(db)
                if state["desired_running"]:
                    raise WorkerHalt(
                        "Testnet epoch reset requires desired_running=false."
                    )
                if state["pending_client_id"]:
                    raise WorkerHalt(
                        "Testnet epoch reset refuses a pending managed intent."
                    )
                _ensure_account_binding(db, api)
            try:
                open_orders = api.open_orders(symbol=SYMBOL)
            except Exception as exc:
                raise WorkerHalt(
                    f"Testnet reset open-order check failed: {exc}"
                ) from exc
            if not isinstance(open_orders, list):
                raise WorkerHalt("Testnet reset open-order response is invalid.")
            if open_orders:
                raise WorkerHalt(
                    "Testnet epoch reset refuses existing BTCUSDT open orders."
                )
            archive = _archive_epoch_and_reset(db_path, api)
        return {**status_snapshot(db_path, lock_path), **archive}
    if command == "recover":
        _execution_gate()
        api = client if client is not None else execution.Client()
        with closing(_connect(db_path)) as db:
            _assert_account_fingerprint(db, api, allow_unbound=False)
            state = _state(db)
            if state["halted"] and not state["pending_client_id"]:
                raise WorkerHalt(
                    "A halted worker without a pending intent cannot be auto-recovered."
                )
        # Publish durable run intent before inspecting the two process locks.
        # This makes the account-lock → worker-lock handoff safe even when a
        # directly launched run process races this recovery command.
        _set_desired(db_path, True)
        account_lock_path = _account_lock_path(lock_path)
        deadline = time.monotonic() + STARTUP_WAIT_SECONDS
        stable_running = False
        while time.monotonic() < deadline:
            worker_active = _lock_active(lock_path)
            account_active = _lock_active(account_lock_path)
            if worker_active and account_active:
                if stable_running:
                    snapshot = status_snapshot(db_path, lock_path)
                    if snapshot.get("running") and snapshot.get("desired_running"):
                        return snapshot
                stable_running = True
                time.sleep(0.05)
                continue
            stable_running = False
            if worker_active or account_active:
                # A correct run process acquires the account lease first and
                # releases it last. Account-only is therefore a valid startup
                # or shutdown handoff. A worker-only observation can be a
                # non-atomic sample, but it must not remain that way.
                time.sleep(0.05)
                continue
            break
        worker_active = _lock_active(lock_path)
        account_active = _lock_active(account_lock_path)
        if worker_active or account_active:
            _set_desired(db_path, False)
            if worker_active and not account_active:
                raise WorkerHalt(
                    "Running worker is missing its account-wide execution lease."
                )
            raise WorkerHalt(
                "Binance Testnet execution lease did not complete its startup "
                "or shutdown handoff in time."
            )
        try:
            return _control_locked(
                "start", db_path=db_path, lock_path=lock_path, client=api
            )
        except WorkerHalt as initial_start_error:
            # A direct run may acquire the account lease immediately after the
            # check above and then acquire the worker lock. Since desired=true
            # was published first, join that safe handoff instead of treating
            # the competing process as a failed recovery.
            # A nested start can clear the intent after its own child exits;
            # recovery owns the control mutex and must re-arm it while joining
            # a correct account-first raw-run handoff.
            _set_desired(db_path, True)
            account_lock_path = _account_lock_path(lock_path)
            if (
                not _lock_active(account_lock_path)
                and not _lock_active(lock_path)
            ):
                _set_desired(db_path, False)
                raise
            join_deadline = time.monotonic() + STARTUP_WAIT_SECONDS
            stable_running = False
            last_start_error = initial_start_error
            while time.monotonic() < join_deadline:
                worker_active = _lock_active(lock_path)
                account_active = _lock_active(account_lock_path)
                if worker_active and account_active:
                    snapshot = status_snapshot(db_path, lock_path)
                    if (
                        snapshot.get("running")
                        and snapshot.get("desired_running")
                    ):
                        if stable_running:
                            return snapshot
                        stable_running = True
                        time.sleep(0.05)
                        continue
                else:
                    stable_running = False
                if worker_active or account_active:
                    time.sleep(0.05)
                    continue
                try:
                    return _control_locked(
                        "start",
                        db_path=db_path,
                        lock_path=lock_path,
                        client=api,
                    )
                except WorkerHalt as retry_error:
                    last_start_error = retry_error
                    if (
                        not _lock_active(account_lock_path)
                        and not _lock_active(lock_path)
                    ):
                        _set_desired(db_path, False)
                        raise
                time.sleep(0.05)
            _set_desired(db_path, False)
            raise last_start_error
    if command != "start":
        raise ValueError(
            "Worker control action must be start, stop, status, reset, or recover."
        )

    # The process may have imported this module before a trainer waiting on the
    # same stable control mutex published a new config.  Never start with stale
    # in-memory policy constants after that handoff.
    if _load_policy_config(POLICY_CONFIG_PATH) != POLICY_CONFIG:
        raise WorkerHalt(
            "Active Binance Testnet policy changed after this process loaded it; "
            "run the start command again so it loads the new policy."
        )
    _execution_gate()
    if _lock_active(lock_path):
        raise WorkerHalt(
            "Binance Testnet worker is already running; a new caller environment "
            "cannot replace the detached worker's inherited credentials."
        )
    if _lock_active(_account_lock_path(lock_path)):
        raise WorkerHalt(
            "Another Binance Testnet policy worker already owns the account-wide "
            "execution lease. Stop that worker before starting this policy."
        )
    api = client if client is not None else execution.Client()
    recover_timestamp_halt = False
    with closing(_connect(db_path)) as db:
        _assert_account_fingerprint(db, api, allow_unbound=True)
        state = _state(db)
        if state["halted"] and not state["pending_client_id"]:
            if _is_recoverable_timestamp_halt(state):
                recover_timestamp_halt = True
            else:
                raise WorkerHalt(
                    "Worker is halted; inspect status and reconcile/repair state before restart."
                )
    if recover_timestamp_halt:
        _recover_timestamp_halt(db_path, lock_path, api)
    _set_desired(db_path, True)
    args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--db",
        str(Path(db_path).resolve()),
        "--lock",
        str(Path(lock_path).resolve()),
    ]
    kwargs: dict[str, object] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        child = subprocess.Popen(args, **kwargs)
    except Exception as exc:
        _set_desired(db_path, False)
        raise WorkerHalt(f"Unable to start Binance Testnet worker: {exc}") from exc

    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    account_lock_path = _account_lock_path(lock_path)
    stable_running = False
    child_exit_without_locks_observed = False
    while time.monotonic() < deadline:
        worker_active = _lock_active(lock_path)
        account_active = _lock_active(account_lock_path)
        if worker_active and account_active:
            child_exit_without_locks_observed = False
            if stable_running:
                snapshot = status_snapshot(db_path, lock_path)
                if snapshot.get("running") and snapshot.get("desired_running"):
                    return snapshot
            stable_running = True
            time.sleep(0.05)
            continue
        stable_running = False
        if worker_active or account_active:
            # Account-only is the expected account -> worker acquisition
            # handoff. Worker-only can only be a transient non-atomic sample
            # for this protocol, so wait for the pair to settle.
            child_exit_without_locks_observed = False
            time.sleep(0.05)
            continue
        return_code = child.poll()
        if return_code is not None:
            if not child_exit_without_locks_observed:
                # Give a raw runner that won the race one additional sample to
                # publish its account-first lock handoff before clearing the
                # shared desired-running intent.
                child_exit_without_locks_observed = True
                time.sleep(0.05)
                continue
            _set_desired(db_path, False)
            snapshot = status_snapshot(db_path, lock_path)
            detail = snapshot.get("halt_reason") or f"child exit code {return_code}"
            raise WorkerHalt(f"Binance Testnet worker did not remain running: {detail}")
        child_exit_without_locks_observed = False
        time.sleep(0.05)

    _set_desired(db_path, False)
    try:
        child.terminate()
    except (AttributeError, OSError, subprocess.SubprocessError):
        pass
    raise WorkerHalt("Binance Testnet worker did not acquire its startup lock in time.")


def control(
    action: str,
    *,
    db_path: Path | str = DB_PATH,
    lock_path: Path | str = LOCK_PATH,
    client: object | None = None,
) -> dict[str, object]:
    command = action.lower().strip()
    if command == "status":
        return status_snapshot(db_path, lock_path)
    if command not in {"start", "stop", "reset", "recover"}:
        raise ValueError(
            "Worker control action must be start, stop, status, reset, or recover."
        )
    with _control_mutex(lock_path):
        return _control_locked(
            command, db_path=db_path, lock_path=lock_path, client=client
        )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("start", "stop", "status", "run", "reset", "recover")
    )
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--lock", default=str(LOCK_PATH))
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            result = run_forever(db_path=args.db, lock_path=args.lock)
        else:
            result = control(args.action, db_path=args.db, lock_path=args.lock)
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    except Exception as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "AccountBindingError",
    "ACCOUNT_LOCK_PATH",
    "CONTROL_LOCK_PATH",
    "DB_PATH",
    "LEARNING_DB_PATH",
    "LOCK_PATH",
    "POLICY",
    "POLICY_CONFIG_PATH",
    "POLICY_ID",
    "POLICY_MODEL_VERSION",
    "SYMBOL",
    "WorkerHalt",
    "control",
    "learning_maintenance_lease",
    "learning_upgrade_lease",
    "learning_snapshot",
    "run_forever",
    "run_once",
    "status_snapshot",
    "validate_bound_account",
]
