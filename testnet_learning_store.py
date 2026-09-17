"""Append-only evidence store for the Binance Spot Testnet learner.

This module deliberately has no exchange, credential, order-execution, active
configuration, or policy-activation capability.  The execution worker writes
causal facts to its own ledger with the helpers below.  ``refresh`` copies and
verifies those immutable facts into a stable, source-bound SQLite database and
occasionally invokes the pure, proposal-only ``testnet_online_learner``.

Daily outcomes use an exact one-day horizon.  A missing day is recorded as a
gap and is never stretched into a synthetic label.  Round-trip review evidence
is derived only from hashed, exact, closed Testnet records; callers cannot
supply a promotional count.
"""

from __future__ import annotations

from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
from typing import Mapping, Sequence

import testnet_online_learner as learner


SCHEMA_VERSION = 2
DAY_MS = 86_400_000
RETRAIN_STRIDE = 30
MIN_TRAINING_SAMPLES = learner.MIN_TRAINING_SAMPLES
LEARNING_DB_PATH = (
    Path(__file__).resolve().parent
    / "state"
    / "binance-testnet-online-learning.sqlite3"
)

_LEGACY_FEATURE_SCHEMA = "legacy_schema_v0_missing"
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_SAFE_FLAGS = {
    "testnet_execution_eligible": False,
    "paper_eligible": False,
    "real_money_eligible": False,
    "real_orders_enabled": False,
    "live_trading_enabled": False,
    "automatic_activation_enabled": False,
    "writes_active_config": False,
}
_SEED_MIGRATABLE_LEARNER_VERSIONS = frozenset({
    "8b77b5de5887c30f062536f9651ffd4be4943442612438a5c927deca9b8f32e0",
})


class LearningStoreError(RuntimeError):
    """Base class for isolated learning-store failures."""


class LearningIntegrityError(LearningStoreError):
    """Raised when immutable source or aggregate evidence does not verify."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_canonical_json(raw: object, field: str) -> object:
    if not isinstance(raw, str):
        raise LearningIntegrityError(f"{field} is not JSON text.")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise LearningIntegrityError(f"{field} is invalid JSON.") from exc
    if _canonical_json(value) != raw:
        raise LearningIntegrityError(f"{field} is not canonical JSON.")
    return value


def _text(value: object, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{field} must be short printable text.")
    return normalized


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer.") from exc
    if result < 0:
        raise ValueError(f"{field} must be non-negative.")
    return result


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal.")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite decimal.") from exc
    if not result.is_finite() or (positive and result <= 0):
        qualifier = "positive " if positive else ""
        raise ValueError(f"{field} must be a {qualifier}finite decimal.")
    return result


def _decimal_text(value: object, field: str, *, positive: bool = False) -> str:
    number = _decimal(value, field, positive=positive)
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _decimal_add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CONTEXT):
        return left + right


def _decimal_subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CONTEXT):
        return left - right


def _decimal_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext(_DECIMAL_CONTEXT):
        return numerator / denominator


def _model_prefix(policy: str, model_version: str) -> str:
    return _sha256({"policy": policy, "model_version": model_version})[:20]


def _registration(
    db: sqlite3.Connection, policy: str, model_version: str
) -> sqlite3.Row:
    row = db.execute(
        """SELECT * FROM worker_learning_registrations
           WHERE policy=? AND model_version=?""",
        (policy, model_version),
    ).fetchone()
    if row is None:
        raise LearningStoreError(
            "Learning source is not registered for this policy and model version."
        )
    return row


def _source_registration_identity(
    db: sqlite3.Connection,
) -> tuple[str, str]:
    rows = db.execute(
        """SELECT policy, model_version, identity_prefix
           FROM worker_learning_registrations
           ORDER BY policy, model_version"""
    ).fetchall()
    if len(rows) != 1:
        raise LearningIntegrityError(
            "Worker learning source must contain exactly one policy/model registration."
        )
    policy = str(rows[0]["policy"])
    model_version = str(rows[0]["model_version"])
    if str(rows[0]["identity_prefix"]) != _model_prefix(policy, model_version):
        raise LearningIntegrityError(
            "Worker learning registration identity seal does not verify."
        )
    return policy, model_version


def _require_source_row_identity(
    row: Mapping[str, object],
    policy: str,
    model_version: str,
    kind: str,
) -> None:
    if str(row["policy"]) != policy or str(row["model_version"]) != model_version:
        raise LearningIntegrityError(
            f"Source {kind} belongs to another policy/model identity."
        )


def ensure_source_schema(
    db: sqlite3.Connection,
    policy: str,
    model_version: str,
    now_ms: int,
) -> None:
    """Create append-only source tables and persist eligibility cutoffs.

    The first registration time is the frozen cutoff.  Consequently, an old
    decision that predates installation can be diagnosed or labelled, but it
    cannot be counted as true forward evidence merely because a caller says so.
    One worker ledger is bound to exactly one policy/model identity.
    """

    policy = _text(policy, "policy", maximum=160)
    model_version = _text(model_version, "model_version", maximum=160)
    now_ms = _integer(now_ms, "now_ms")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS worker_learning_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            schema_version INTEGER NOT NULL,
            source_ledger_id TEXT NOT NULL UNIQUE,
            learning_revision INTEGER NOT NULL DEFAULT 0,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_learning_registrations (
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            identity_prefix TEXT NOT NULL,
            oos_decision_created_cutoff_ms INTEGER NOT NULL,
            true_forward_decision_created_cutoff_ms INTEGER NOT NULL,
            freeze_cutoff_candle_ms INTEGER,
            frozen_candidate_json TEXT,
            frozen_candidate_sha256 TEXT,
            created_ms INTEGER NOT NULL,
            PRIMARY KEY (policy, model_version),
            UNIQUE (identity_prefix)
        );

        CREATE TABLE IF NOT EXISTS worker_learning_daily_labels (
            record_id TEXT PRIMARY KEY,
            sample_id TEXT NOT NULL UNIQUE,
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            decision_candle_close_ms INTEGER NOT NULL,
            label_candle_close_ms INTEGER NOT NULL,
            out_of_sample INTEGER NOT NULL CHECK (out_of_sample IN (0,1)),
            true_forward_after_freeze INTEGER NOT NULL
                CHECK (true_forward_after_freeze IN (0,1)),
            sample_json TEXT NOT NULL,
            sample_sha256 TEXT NOT NULL,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL,
            CHECK (label_candle_close_ms-decision_candle_close_ms=86400000),
            FOREIGN KEY (policy, model_version)
                REFERENCES worker_learning_registrations(policy, model_version)
        );

        CREATE INDEX IF NOT EXISTS worker_learning_labels_time_idx
            ON worker_learning_daily_labels(decision_candle_close_ms,
                                            label_candle_close_ms);

        CREATE TABLE IF NOT EXISTS worker_learning_daily_gaps (
            gap_id TEXT PRIMARY KEY,
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            previous_candle_close_ms INTEGER,
            current_candle_close_ms INTEGER NOT NULL,
            reason TEXT NOT NULL,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL,
            FOREIGN KEY (policy, model_version)
                REFERENCES worker_learning_registrations(policy, model_version)
        );

        CREATE TABLE IF NOT EXISTS worker_learning_candidate_retirements (
            retirement_id TEXT PRIMARY KEY,
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            frozen_candidate_sha256 TEXT NOT NULL,
            frozen_candidate_json TEXT NOT NULL,
            freeze_cutoff_candle_ms INTEGER NOT NULL,
            break_gap_id TEXT,
            boundary_sample_sha256 TEXT,
            boundary_decision_ts INTEGER,
            reason TEXT NOT NULL,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL,
            UNIQUE (policy, model_version, frozen_candidate_sha256),
            FOREIGN KEY (policy, model_version)
                REFERENCES worker_learning_registrations(policy, model_version),
            FOREIGN KEY (break_gap_id)
                REFERENCES worker_learning_daily_gaps(gap_id),
            CHECK (break_gap_id IS NOT NULL
                   OR (boundary_sample_sha256 IS NOT NULL
                       AND boundary_decision_ts IS NOT NULL))
        );

        CREATE TABLE IF NOT EXISTS worker_learning_round_trips (
            record_id TEXT PRIMARY KEY,
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            entry_client_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open','closed','quarantined')),
            entry_candle_close_ms INTEGER NOT NULL,
            exit_client_id TEXT,
            exit_candle_close_ms INTEGER,
            entry_feature_schema TEXT NOT NULL,
            entry_feature_json TEXT,
            entry_feature_status TEXT NOT NULL,
            entry_freeze_id TEXT,
            entry_freeze_bound_ms INTEGER,
            entry_decision_created_ms INTEGER,
            entry_candidate_bound INTEGER NOT NULL DEFAULT 0
                CHECK (entry_candidate_bound IN (0,1)),
            entry_momentum TEXT NOT NULL,
            entry_cost_usdt TEXT NOT NULL,
            entry_quote_usdt TEXT NOT NULL,
            entry_fee_usdt TEXT NOT NULL,
            entry_qty TEXT NOT NULL,
            exit_momentum TEXT,
            exit_quote_usdt TEXT,
            exit_fee_usdt TEXT,
            exit_proceeds_usdt TEXT,
            realized_pnl_usdt TEXT,
            net_return TEXT,
            exact_pnl INTEGER NOT NULL CHECK (exact_pnl IN (0,1)),
            quarantine_reason TEXT,
            source_kind TEXT NOT NULL,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL,
            UNIQUE (policy, model_version, entry_client_id, status),
            FOREIGN KEY (policy, model_version)
                REFERENCES worker_learning_registrations(policy, model_version),
            CHECK ((status='closed' AND exact_pnl=1 AND net_return IS NOT NULL
                    AND realized_pnl_usdt IS NOT NULL)
                   OR (status!='closed' AND exact_pnl=0)),
            CHECK ((status='quarantined' AND quarantine_reason IS NOT NULL)
                   OR (status!='quarantined' AND quarantine_reason IS NULL))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS worker_learning_one_terminal_idx
            ON worker_learning_round_trips(policy, model_version, entry_client_id)
            WHERE status IN ('closed','quarantined');

        CREATE INDEX IF NOT EXISTS worker_learning_round_trip_status_idx
            ON worker_learning_round_trips(status, entry_candle_close_ms);

        CREATE TABLE IF NOT EXISTS worker_learning_source_errors (
            error_id TEXT PRIMARY KEY,
            policy TEXT,
            model_version TEXT,
            message TEXT NOT NULL,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_learning_refresh_health (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            last_attempt_failed INTEGER NOT NULL
                CHECK (last_attempt_failed IN (0,1)),
            sanitized_error TEXT,
            attempted_ms INTEGER,
            last_success_ms INTEGER,
            CHECK ((last_attempt_failed=1 AND sanitized_error IS NOT NULL
                    AND attempted_ms IS NOT NULL)
                    OR (last_attempt_failed=0 AND sanitized_error IS NULL))
        );

        CREATE TABLE IF NOT EXISTS worker_learning_pending_candidate_transition (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            transition_json TEXT NOT NULL,
            transition_sha256 TEXT NOT NULL UNIQUE,
            requested_ms INTEGER NOT NULL
        );
        """
    )
    meta_columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(worker_learning_meta)")
    }
    revision_migrated = "learning_revision" not in meta_columns
    if revision_migrated:
        db.execute(
            """ALTER TABLE worker_learning_meta
               ADD COLUMN learning_revision INTEGER NOT NULL DEFAULT 0"""
        )
    registration_columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(worker_learning_registrations)")
    }
    for name, declaration in (
        ("freeze_cutoff_candle_ms", "INTEGER"),
        ("frozen_candidate_json", "TEXT"),
        ("frozen_candidate_sha256", "TEXT"),
    ):
        if name not in registration_columns:
            db.execute(
                f"ALTER TABLE worker_learning_registrations ADD COLUMN {name} {declaration}"
            )
    retirement_columns = {
        str(row[1])
        for row in db.execute(
            "PRAGMA table_info(worker_learning_candidate_retirements)"
        )
    }
    for name, declaration in (
        ("boundary_sample_sha256", "TEXT"),
        ("boundary_decision_ts", "INTEGER"),
    ):
        if name not in retirement_columns:
            db.execute(
                f"ALTER TABLE worker_learning_candidate_retirements ADD COLUMN {name} {declaration}"
            )
    round_trip_columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(worker_learning_round_trips)")
    }
    # These migrations only support development databases created by an early
    # build of this module.  Production source rows are always written with all
    # exact accounting fields present.
    for name, declaration in (
        ("entry_momentum", "TEXT"),
        ("entry_quote_usdt", "TEXT"),
        ("entry_fee_usdt", "TEXT"),
        ("exit_momentum", "TEXT"),
        ("exit_quote_usdt", "TEXT"),
        ("exit_fee_usdt", "TEXT"),
        ("entry_freeze_id", "TEXT"),
        ("entry_freeze_bound_ms", "INTEGER"),
        ("entry_decision_created_ms", "INTEGER"),
        ("entry_candidate_bound", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in round_trip_columns:
            db.execute(
                f"ALTER TABLE worker_learning_round_trips ADD COLUMN {name} {declaration}"
            )
    db.execute(
        """INSERT OR IGNORE INTO worker_learning_meta
           (singleton, schema_version, source_ledger_id, learning_revision,
            created_ms)
           VALUES (1, ?, lower(hex(randomblob(32))), 0, ?)""",
        (SCHEMA_VERSION, now_ms),
    )
    meta = db.execute(
        "SELECT schema_version FROM worker_learning_meta WHERE singleton=1"
    ).fetchone()
    if meta is None or int(meta[0]) != SCHEMA_VERSION:
        raise LearningStoreError("Unsupported worker learning source schema.")
    if revision_migrated:
        legacy_revision = sum(
            int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "worker_learning_daily_labels",
                "worker_learning_daily_gaps",
                "worker_learning_round_trips",
                "worker_learning_source_errors",
            )
        )
        outbox_exists = db.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='worker_learning_outbox'"""
        ).fetchone()
        if outbox_exists is not None:
            legacy_revision += int(
                db.execute(
                    """SELECT COUNT(*) FROM worker_learning_outbox
                       WHERE resolved_ms IS NULL"""
                ).fetchone()[0]
            )
        db.execute(
            """UPDATE worker_learning_meta SET learning_revision=?
               WHERE singleton=1""",
            (legacy_revision,),
        )
    db.execute(
        """INSERT OR IGNORE INTO worker_learning_refresh_health
           (singleton, last_attempt_failed, sanitized_error, attempted_ms,
            last_success_ms) VALUES (1, 0, NULL, NULL, NULL)"""
    )
    outbox_exists = db.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='worker_learning_outbox'"""
    ).fetchone()
    if outbox_exists is not None:
        db.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS worker_learning_outbox_revision_insert
            AFTER INSERT ON worker_learning_outbox
            WHEN NEW.resolved_ms IS NULL
            BEGIN
                UPDATE worker_learning_meta
                SET learning_revision=learning_revision+1
                WHERE singleton=1;
            END;

            CREATE TRIGGER IF NOT EXISTS worker_learning_outbox_revision_resolve
            AFTER UPDATE OF resolved_ms ON worker_learning_outbox
            WHEN (OLD.resolved_ms IS NULL) != (NEW.resolved_ms IS NULL)
            BEGIN
                UPDATE worker_learning_meta
                SET learning_revision=learning_revision+1
                WHERE singleton=1;
            END;
            """
        )
    prefix = _model_prefix(policy, model_version)
    existing_registrations = db.execute(
        """SELECT policy, model_version FROM worker_learning_registrations
           ORDER BY policy, model_version"""
    ).fetchall()
    if existing_registrations and (
        len(existing_registrations) != 1
        or str(existing_registrations[0]["policy"]) != policy
        or str(existing_registrations[0]["model_version"]) != model_version
    ):
        raise LearningIntegrityError(
            "Worker learning source is already bound to another policy/model identity."
        )
    db.execute(
        """INSERT OR IGNORE INTO worker_learning_registrations
           (policy, model_version, identity_prefix,
            oos_decision_created_cutoff_ms,
            true_forward_decision_created_cutoff_ms, created_ms)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (policy, model_version, prefix, now_ms, now_ms, now_ms),
    )
    row = _registration(db, policy, model_version)
    if str(row["identity_prefix"]) != prefix:
        raise LearningIntegrityError("Policy/model learning identity changed.")
    if _source_registration_identity(db) != (policy, model_version):
        raise LearningIntegrityError("Worker learning registration identity changed.")


def _bump_source_revision(db: sqlite3.Connection) -> None:
    changed = db.execute(
        """UPDATE worker_learning_meta
           SET learning_revision=learning_revision+1 WHERE singleton=1"""
    ).rowcount
    if changed != 1:
        raise LearningStoreError("Worker learning source metadata is missing.")


def _candidate_transition_payload(
    *,
    retiring_candidate: Mapping[str, object] | None,
    replacement_candidate: Mapping[str, object] | None,
    break_gap: Mapping[str, object] | None,
    boundary_sample: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Return the canonical intent for one crash-recoverable source transition."""

    if retiring_candidate is None and replacement_candidate is None:
        return None
    if break_gap is not None and boundary_sample is not None:
        raise LearningIntegrityError(
            "Candidate transition has two incompatible retirement boundaries."
        )
    retiring_hash = None
    if retiring_candidate is not None:
        _candidate_json, retiring_hash, _cutoff = _candidate_binding_details(
            retiring_candidate
        )
        if break_gap is None and boundary_sample is None:
            raise LearningIntegrityError(
                "Candidate retirement is missing boundary evidence."
            )
    elif break_gap is not None or boundary_sample is not None:
        raise LearningIntegrityError(
            "Candidate transition has retirement evidence without a candidate."
        )
    replacement_hash = None
    if replacement_candidate is not None:
        _candidate_json, replacement_hash, _cutoff = _candidate_binding_details(
            replacement_candidate
        )
    return {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_candidate_source_transition",
        "retiring_candidate_sha256": retiring_hash,
        "replacement_candidate_sha256": replacement_hash,
        "break_gap_id": (
            None if break_gap is None else str(break_gap["source_gap_id"])
        ),
        "break_gap_sha256": (
            None
            if break_gap is None
            else str(break_gap["source_record_sha256"])
        ),
        "boundary_sample_sha256": (
            None
            if boundary_sample is None
            else str(boundary_sample["immutable_sha256"])
        ),
        "boundary_decision_ts": (
            None
            if boundary_sample is None
            else _integer(boundary_sample["decision_ts"], "boundary decision")
        ),
    }


def _validate_candidate_transition_seal(
    encoded: object, supplied_digest: object, label: str
) -> dict[str, object]:
    payload = _parse_canonical_json(encoded, label)
    required = {
        "schema",
        "kind",
        "retiring_candidate_sha256",
        "replacement_candidate_sha256",
        "break_gap_id",
        "break_gap_sha256",
        "boundary_sample_sha256",
        "boundary_decision_ts",
    }
    digest = _sha256(payload)
    if (
        not isinstance(payload, Mapping)
        or set(payload) != required
        or payload.get("schema") != SCHEMA_VERSION
        or payload.get("kind") != "testnet_candidate_source_transition"
        or not hmac.compare_digest(digest, str(supplied_digest))
    ):
        raise LearningIntegrityError(
            "Pending candidate transition seal does not verify."
        )
    retiring_hash = payload.get("retiring_candidate_sha256")
    replacement_hash = payload.get("replacement_candidate_sha256")
    for name, value in (
        ("retiring candidate", retiring_hash),
        ("replacement candidate", replacement_hash),
    ):
        if value is not None and (
            not isinstance(value, str) or len(value) != 64
        ):
            raise LearningIntegrityError(f"Pending {name} hash is invalid.")
    gap_values = (
        payload.get("break_gap_id"),
        payload.get("break_gap_sha256"),
    )
    boundary_values = (
        payload.get("boundary_sample_sha256"),
        payload.get("boundary_decision_ts"),
    )
    gap_present = all(value is not None for value in gap_values)
    boundary_present = all(value is not None for value in boundary_values)
    if any(value is not None for value in gap_values) != gap_present:
        raise LearningIntegrityError("Pending candidate gap boundary is partial.")
    if any(value is not None for value in boundary_values) != boundary_present:
        raise LearningIntegrityError("Pending candidate sample boundary is partial.")
    if retiring_hash is None:
        if gap_present or boundary_present:
            raise LearningIntegrityError(
                "Pending transition has boundary evidence without retirement."
            )
    elif gap_present == boundary_present:
        raise LearningIntegrityError(
            "Pending candidate retirement must have exactly one boundary."
        )
    if retiring_hash is None and replacement_hash is None:
        raise LearningIntegrityError("Pending candidate transition is empty.")
    return dict(payload)


def _verified_pending_candidate_transition(
    db: sqlite3.Connection,
) -> dict[str, object] | None:
    table = db.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table'
             AND name='worker_learning_pending_candidate_transition'"""
    ).fetchone()
    if table is None:
        raise LearningStoreError(
            "Worker candidate-transition coordination schema is missing."
        )
    row = db.execute(
        """SELECT transition_json, transition_sha256
           FROM worker_learning_pending_candidate_transition WHERE singleton=1"""
    ).fetchone()
    if row is None:
        return None
    return _validate_candidate_transition_seal(
        row["transition_json"],
        row["transition_sha256"],
        "pending candidate transition",
    )


def _aggregate_pending_candidate_transition(
    state: Mapping[str, object],
) -> dict[str, object] | None:
    encoded = state.get("pending_candidate_transition_json")
    digest = state.get("pending_candidate_transition_sha256")
    if encoded is None and digest is None:
        return None
    if encoded is None or digest is None:
        raise LearningIntegrityError(
            "Aggregate candidate transition is only partially persisted."
        )
    return _validate_candidate_transition_seal(
        encoded, digest, "aggregate pending candidate transition"
    )


def _stage_source_candidate_transition(
    source_db_path: Path | str,
    transition: Mapping[str, object] | None,
    requested_ms: int,
) -> None:
    """Durably block new source bindings before the aggregate phase commits."""

    db = sqlite3.connect(Path(source_db_path), timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("BEGIN IMMEDIATE")
        # Validate an earlier interrupted phase before replacing it with the
        # transition recomputed from the current aggregate/source snapshots.
        _verified_pending_candidate_transition(db)
        if transition is None:
            db.execute(
                "DELETE FROM worker_learning_pending_candidate_transition WHERE singleton=1"
            )
        else:
            payload = dict(transition)
            encoded = _canonical_json(payload)
            digest = _sha256(payload)
            db.execute(
                """INSERT INTO worker_learning_pending_candidate_transition
                   (singleton, transition_json, transition_sha256, requested_ms)
                   VALUES (1, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     transition_json=excluded.transition_json,
                     transition_sha256=excluded.transition_sha256,
                     requested_ms=excluded.requested_ms""",
                (encoded, digest, _integer(requested_ms, "transition requested_ms")),
            )
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def _assert_no_pending_candidate_transition(db: sqlite3.Connection) -> None:
    if _verified_pending_candidate_transition(db) is not None:
        raise LearningStoreError(
            "Candidate source transition is pending; defer learning capture."
        )


def _record_gap(
    db: sqlite3.Connection,
    *,
    registration: sqlite3.Row,
    policy: str,
    model_version: str,
    previous_ms: int | None,
    current_ms: int,
    reason: str,
    now_ms: int,
) -> dict[str, object]:
    payload = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_daily_label_gap",
        "policy": policy,
        "model_version": model_version,
        "identity_prefix": str(registration["identity_prefix"]),
        "previous_candle_close_ms": previous_ms,
        "current_candle_close_ms": current_ms,
        "reason": reason,
    }
    digest = _sha256(payload)
    gap_id = f"gap:{str(registration['identity_prefix'])}:{digest[:32]}"
    encoded = _canonical_json(payload)
    inserted = db.execute(
        """INSERT OR IGNORE INTO worker_learning_daily_gaps
           (gap_id, policy, model_version, previous_candle_close_ms,
            current_candle_close_ms, reason, record_json, record_sha256,
            created_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            gap_id,
            policy,
            model_version,
            previous_ms,
            current_ms,
            reason,
            encoded,
            digest,
            now_ms,
        ),
    )
    if inserted.rowcount:
        _bump_source_revision(db)
    stored = db.execute(
        "SELECT record_json, record_sha256 FROM worker_learning_daily_gaps WHERE gap_id=?",
        (gap_id,),
    ).fetchone()
    if stored is None or stored[0] != encoded or stored[1] != digest:
        raise LearningIntegrityError("Daily gap identity collision.")
    candidate_retired = _retire_registration_for_gap(
        db,
        registration=registration,
        gap_id=gap_id,
        gap_digest=digest,
        current_ms=current_ms,
        gap_reason=reason,
        now_ms=now_ms,
    )
    return {
        "status": "gap_recorded",
        "gap_id": gap_id,
        "reason": reason,
        "candidate_retired": candidate_retired,
    }


def _retire_registration_for_gap(
    db: sqlite3.Connection,
    *,
    registration: sqlite3.Row,
    gap_id: str,
    gap_digest: str,
    current_ms: int,
    gap_reason: str,
    now_ms: int,
) -> bool:
    """Archive and clear a source freeze whose forward cohort was broken."""

    candidate_hash = registration["frozen_candidate_sha256"]
    candidate_json = registration["frozen_candidate_json"]
    cutoff = registration["freeze_cutoff_candle_ms"]
    if candidate_hash is None or candidate_json is None or cutoff is None:
        return False
    if current_ms <= int(cutoff):
        return False
    parsed = _parse_canonical_json(candidate_json, "source frozen candidate")
    if not isinstance(parsed, Mapping):
        raise LearningIntegrityError("Source frozen candidate is not an object.")
    supplied = str(candidate_hash)
    expected = _sha256(
        {key: value for key, value in parsed.items() if key != "immutable_sha256"}
    )
    if parsed.get("immutable_sha256") != supplied or expected != supplied:
        raise LearningIntegrityError("Source frozen candidate seal does not verify.")
    retirement = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_source_candidate_retirement",
        "policy": str(registration["policy"]),
        "model_version": str(registration["model_version"]),
        "frozen_candidate_sha256": supplied,
        "frozen_candidate": dict(parsed),
        "freeze_cutoff_candle_ms": int(cutoff),
        "break_gap_id": gap_id,
        "break_gap_sha256": gap_digest,
        "reason": "post_freeze_daily_gap",
        "gap_reason": gap_reason,
    }
    retirement_digest = _sha256(retirement)
    retirement_id = f"retire:{supplied[:24]}:{gap_digest[:24]}"
    encoded = _canonical_json(retirement)
    db.execute(
        """INSERT OR IGNORE INTO worker_learning_candidate_retirements
           (retirement_id, policy, model_version, frozen_candidate_sha256,
            frozen_candidate_json, freeze_cutoff_candle_ms, break_gap_id,
            reason, record_json, record_sha256, created_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            retirement_id,
            registration["policy"],
            registration["model_version"],
            supplied,
            candidate_json,
            int(cutoff),
            gap_id,
            "post_freeze_daily_gap",
            encoded,
            retirement_digest,
            now_ms,
        ),
    )
    changed = db.execute(
        """UPDATE worker_learning_registrations SET
           freeze_cutoff_candle_ms=NULL, frozen_candidate_json=NULL,
           frozen_candidate_sha256=NULL,
           true_forward_decision_created_cutoff_ms=?
           WHERE policy=? AND model_version=?
             AND frozen_candidate_sha256=?""",
        (
            now_ms,
            registration["policy"],
            registration["model_version"],
            supplied,
        ),
    ).rowcount
    return bool(changed)


def _retire_registration_for_boundary(
    db: sqlite3.Connection,
    *,
    registration: sqlite3.Row,
    boundary_sample: Mapping[str, object],
    now_ms: int,
) -> bool:
    """Archive a freeze broken by an unbound post-freeze source sample."""

    candidate_hash = registration["frozen_candidate_sha256"]
    candidate_json = registration["frozen_candidate_json"]
    cutoff = registration["freeze_cutoff_candle_ms"]
    if candidate_hash is None or candidate_json is None or cutoff is None:
        return False
    parsed = _parse_canonical_json(candidate_json, "source frozen candidate")
    if not isinstance(parsed, Mapping):
        raise LearningIntegrityError("Source frozen candidate is not an object.")
    supplied = str(candidate_hash)
    expected = _sha256(
        {key: value for key, value in parsed.items() if key != "immutable_sha256"}
    )
    if parsed.get("immutable_sha256") != supplied or expected != supplied:
        raise LearningIntegrityError("Source frozen candidate seal does not verify.")
    boundary_hash = str(boundary_sample.get("immutable_sha256", ""))
    boundary_decision_ts = _integer(
        boundary_sample.get("decision_ts"), "retirement boundary decision"
    )
    if len(boundary_hash) != 64 or boundary_decision_ts < int(cutoff):
        raise LearningIntegrityError("Candidate retirement boundary is invalid.")
    retirement = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_source_candidate_retirement",
        "policy": str(registration["policy"]),
        "model_version": str(registration["model_version"]),
        "frozen_candidate_sha256": supplied,
        "frozen_candidate": dict(parsed),
        "freeze_cutoff_candle_ms": int(cutoff),
        "break_gap_id": None,
        "break_gap_sha256": None,
        "boundary_sample_sha256": boundary_hash,
        "boundary_decision_ts": boundary_decision_ts,
        "reason": "post_freeze_source_binding_missing",
    }
    retirement_digest = _sha256(retirement)
    retirement_id = f"retire:{supplied[:24]}:{boundary_hash[:24]}"
    db.execute(
        """INSERT OR IGNORE INTO worker_learning_candidate_retirements
           (retirement_id, policy, model_version, frozen_candidate_sha256,
            frozen_candidate_json, freeze_cutoff_candle_ms, break_gap_id,
            boundary_sample_sha256, boundary_decision_ts, reason,
            record_json, record_sha256, created_ms)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
        (
            retirement_id,
            registration["policy"],
            registration["model_version"],
            supplied,
            candidate_json,
            int(cutoff),
            boundary_hash,
            boundary_decision_ts,
            "post_freeze_source_binding_missing",
            _canonical_json(retirement),
            retirement_digest,
            now_ms,
        ),
    )
    changed = db.execute(
        """UPDATE worker_learning_registrations SET
           freeze_cutoff_candle_ms=NULL, frozen_candidate_json=NULL,
           frozen_candidate_sha256=NULL,
           true_forward_decision_created_cutoff_ms=?
           WHERE policy=? AND model_version=?
             AND frozen_candidate_sha256=?""",
        (
            now_ms,
            registration["policy"],
            registration["model_version"],
            supplied,
        ),
    ).rowcount
    return bool(changed)


def capture_daily_label(
    db: sqlite3.Connection,
    signal: Mapping[str, object],
    policy: str,
    model_version: str,
    now_ms: int,
) -> dict[str, object]:
    """Seal the previous decision's exact next-day result, if continuous.

    Call this inside the same transaction and immediately before inserting the
    current decision.  Repeating the call is idempotent.
    """

    if not isinstance(signal, Mapping):
        raise ValueError("signal must be a mapping.")
    policy = _text(policy, "policy", maximum=160)
    model_version = _text(model_version, "model_version", maximum=160)
    now_ms = _integer(now_ms, "now_ms")
    current_ms = _integer(signal.get("candle_close_ms"), "candle_close_ms")
    current_close = _decimal(signal.get("close_latest"), "close_latest", positive=True)
    _assert_no_pending_candidate_transition(db)
    registration = _registration(db, policy, model_version)

    latest = db.execute(
        "SELECT candle_close_ms FROM worker_decisions ORDER BY candle_close_ms DESC LIMIT 1"
    ).fetchone()
    if latest is not None and int(latest[0]) > current_ms:
        current_decision = db.execute(
            """SELECT policy, close_latest FROM worker_decisions
               WHERE candle_close_ms=?""",
            (current_ms,),
        ).fetchone()
        if current_decision is None or str(current_decision["policy"]) != policy:
            return _record_gap(
                db,
                registration=registration,
                policy=policy,
                model_version=model_version,
                previous_ms=int(latest[0]),
                current_ms=current_ms,
                reason="out_of_order_decision",
                now_ms=now_ms,
            )
        if _decimal(
            current_decision["close_latest"],
            "backfill decision close",
            positive=True,
        ) != current_close:
            raise LearningIntegrityError(
                "Backfilled learning signal does not match its core decision."
            )
    previous = db.execute(
        """SELECT candle_close_ms, policy, close_latest, momentum, target_long,
                  action, created_ms
           FROM worker_decisions
           WHERE candle_close_ms < ?
           ORDER BY candle_close_ms DESC LIMIT 1""",
        (current_ms,),
    ).fetchone()
    if previous is None:
        last_sealed = db.execute(
            """SELECT label_candle_close_ms
               FROM worker_learning_daily_labels
               WHERE policy=? AND model_version=?
               ORDER BY label_candle_close_ms DESC LIMIT 1""",
            (policy, model_version),
        ).fetchone()
        if last_sealed is not None and current_ms > int(last_sealed[0]):
            return _record_gap(
                db,
                registration=registration,
                policy=policy,
                model_version=model_version,
                previous_ms=int(last_sealed[0]),
                current_ms=current_ms,
                reason="missing_decision_epoch_boundary",
                now_ms=now_ms,
            )
        return {"status": "awaiting_previous_decision"}

    previous_ms = int(previous["candle_close_ms"])
    if str(previous["policy"]) != policy:
        return _record_gap(
            db,
            registration=registration,
            policy=policy,
            model_version=model_version,
            previous_ms=previous_ms,
            current_ms=current_ms,
            reason="policy_boundary",
            now_ms=now_ms,
        )
    if current_ms - previous_ms != DAY_MS:
        return _record_gap(
            db,
            registration=registration,
            policy=policy,
            model_version=model_version,
            previous_ms=previous_ms,
            current_ms=current_ms,
            reason="non_contiguous_daily_horizon",
            now_ms=now_ms,
        )

    # A failed sidecar capture must not disappear merely because the core
    # execution decision still committed.  Compare the decision that would be
    # labelled now with the last successfully sealed label boundary.  When it
    # has advanced past that boundary, record the missing horizon, retire any
    # affected frozen cohort, and seal the current exact horizon under the new
    # lifecycle in the same transaction.
    capture_gap: dict[str, object] | None = None
    last_sealed = db.execute(
        """SELECT label_candle_close_ms
           FROM worker_learning_daily_labels
           WHERE policy=? AND model_version=?
           ORDER BY label_candle_close_ms DESC LIMIT 1""",
        (policy, model_version),
    ).fetchone()
    last_gap = db.execute(
        """SELECT current_candle_close_ms
           FROM worker_learning_daily_gaps
           WHERE policy=? AND model_version=?
           ORDER BY current_candle_close_ms DESC LIMIT 1""",
        (policy, model_version),
    ).fetchone()
    sealed_boundary = None if last_sealed is None else int(last_sealed[0])
    gap_boundary = None if last_gap is None else int(last_gap[0])
    known_boundary = max(
        boundary
        for boundary in (sealed_boundary, gap_boundary)
        if boundary is not None
    ) if sealed_boundary is not None or gap_boundary is not None else None
    if known_boundary is not None and previous_ms > known_boundary:
        capture_gap = _record_gap(
            db,
            registration=registration,
            policy=policy,
            model_version=model_version,
            previous_ms=known_boundary,
            current_ms=previous_ms,
            reason="missing_learning_label_boundary",
            now_ms=now_ms,
        )
        registration = _registration(db, policy, model_version)

    previous_close = _decimal(
        previous["close_latest"], "previous close", positive=True
    )
    decision_created_ms = _integer(previous["created_ms"], "decision created_ms")
    out_of_sample = (
        decision_created_ms
        >= int(registration["oos_decision_created_cutoff_ms"])
    )
    freeze_cutoff = registration["freeze_cutoff_candle_ms"]
    freeze_id = registration["frozen_candidate_sha256"]
    true_forward = bool(
        out_of_sample
        and freeze_cutoff is not None
        and freeze_id is not None
        and previous_ms >= int(freeze_cutoff)
        and decision_created_ms
        > int(registration["true_forward_decision_created_cutoff_ms"])
    )
    sample_id = (
        f"d1:{registration['identity_prefix']}:{previous_ms}:{current_ms}"
    )
    sample_payload: dict[str, object] = {
        "sample_id": sample_id,
        "decision_ts": previous_ms,
        "label_available_ts": current_ms,
        "momentum": _decimal_text(previous["momentum"], "previous momentum"),
        "forward_return": _decimal_text(
            _decimal_subtract(
                _decimal_ratio(current_close, previous_close), Decimal("1")
            ),
            "forward return",
        ),
        "closed": True,
        "out_of_sample": out_of_sample,
        "true_forward_after_freeze": true_forward,
        "active_target_long": bool(previous["target_long"]),
        "action": _text(str(previous["action"]), "previous action", maximum=64),
    }
    if true_forward:
        sample_payload["freeze_id"] = str(freeze_id)
    sealed = learner.seal_sample(sample_payload)
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_closed_daily_label",
        "policy": policy,
        "model_version": model_version,
        "identity_prefix": str(registration["identity_prefix"]),
        "sample": sealed,
    }
    record_digest = _sha256(record)
    record_id = f"label:{registration['identity_prefix']}:{record_digest[:32]}"
    sample_json = _canonical_json(sealed)
    record_json = _canonical_json(record)
    inserted = db.execute(
        """INSERT OR IGNORE INTO worker_learning_daily_labels
           (record_id, sample_id, policy, model_version,
            decision_candle_close_ms, label_candle_close_ms, out_of_sample,
            true_forward_after_freeze, sample_json, sample_sha256,
            record_json, record_sha256, created_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record_id,
            sample_id,
            policy,
            model_version,
            previous_ms,
            current_ms,
            int(out_of_sample),
            int(true_forward),
            sample_json,
            str(sealed["immutable_sha256"]),
            record_json,
            record_digest,
            now_ms,
        ),
    )
    if inserted.rowcount:
        _bump_source_revision(db)
    stored = db.execute(
        """SELECT sample_json, record_json, record_sha256
           FROM worker_learning_daily_labels WHERE record_id=?""",
        (record_id,),
    ).fetchone()
    if (
        stored is None
        or stored["sample_json"] != sample_json
        or stored["record_json"] != record_json
        or stored["record_sha256"] != record_digest
    ):
        raise LearningIntegrityError("Daily label identity collision.")
    result: dict[str, object] = {
        "status": "label_sealed",
        "record_id": record_id,
        "sample_id": sample_id,
        "out_of_sample": out_of_sample,
        "true_forward_after_freeze": true_forward,
    }
    if capture_gap is not None:
        result["recovered_gap"] = capture_gap
    return result


def _feature_copy(decision: sqlite3.Row) -> tuple[str, str | None, str]:
    raw_schema = decision["feature_schema"]
    raw_json = decision["feature_json"]
    if raw_json is None:
        return _LEGACY_FEATURE_SCHEMA, None, "diagnostic_missing_legacy_feature"
    if not isinstance(raw_json, str):
        return _LEGACY_FEATURE_SCHEMA, None, "diagnostic_invalid_legacy_feature"
    try:
        parsed = json.loads(raw_json)
        canonical = _canonical_json(parsed)
    except (TypeError, ValueError):
        return _LEGACY_FEATURE_SCHEMA, None, "diagnostic_invalid_legacy_feature"
    schema = (
        str(raw_schema).strip()
        if raw_schema is not None and str(raw_schema).strip()
        else "legacy_schema_v0_unspecified"
    )
    status = "complete" if raw_schema else "diagnostic_unversioned_feature"
    return schema, canonical, status


def _round_trip_record(
    *,
    policy: str,
    model_version: str,
    entry_client_id: str,
    status: str,
    entry_candle_close_ms: int,
    exit_client_id: str | None,
    exit_candle_close_ms: int | None,
    entry_feature_schema: str,
    entry_feature_json: str | None,
    entry_feature_status: str,
    entry_freeze_id: str | None,
    entry_freeze_bound_ms: int | None,
    entry_decision_created_ms: int | None,
    entry_candidate_bound: bool,
    entry_momentum: str,
    entry_cost_usdt: str,
    entry_quote_usdt: str,
    entry_fee_usdt: str,
    entry_qty: str,
    exit_momentum: str | None,
    exit_quote_usdt: str | None,
    exit_fee_usdt: str | None,
    exit_proceeds_usdt: str | None,
    realized_pnl_usdt: str | None,
    net_return: str | None,
    exact_pnl: bool,
    quarantine_reason: str | None,
    source_kind: str,
) -> dict[str, object]:
    features = (
        None
        if entry_feature_json is None
        else _parse_canonical_json(entry_feature_json, "entry feature JSON")
    )
    return {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_round_trip_evidence",
        "policy": policy,
        "model_version": model_version,
        "entry_client_id": entry_client_id,
        "status": status,
        "entry_candle_close_ms": entry_candle_close_ms,
        "exit_client_id": exit_client_id,
        "exit_candle_close_ms": exit_candle_close_ms,
        "entry_feature_schema": entry_feature_schema,
        "entry_features": features,
        "entry_feature_status": entry_feature_status,
        "entry_freeze_id": entry_freeze_id,
        "entry_freeze_bound_ms": entry_freeze_bound_ms,
        "entry_decision_created_ms": entry_decision_created_ms,
        "entry_candidate_bound": entry_candidate_bound,
        "entry_momentum": entry_momentum,
        "entry_cost_usdt": entry_cost_usdt,
        "entry_quote_usdt": entry_quote_usdt,
        "entry_fee_usdt": entry_fee_usdt,
        "entry_qty": entry_qty,
        "exit_momentum": exit_momentum,
        "exit_quote_usdt": exit_quote_usdt,
        "exit_fee_usdt": exit_fee_usdt,
        "exit_proceeds_usdt": exit_proceeds_usdt,
        "realized_pnl_usdt": realized_pnl_usdt,
        "net_return": net_return,
        "exact_pnl": exact_pnl,
        "quarantine_reason": quarantine_reason,
        "source_kind": source_kind,
    }


def _insert_round_trip(
    db: sqlite3.Connection,
    record: Mapping[str, object],
    entry_feature_json: str | None,
    now_ms: int,
) -> dict[str, object]:
    digest = _sha256(record)
    identity_prefix = _model_prefix(
        str(record["policy"]), str(record["model_version"])
    )
    record_id = (
        f"rt:{identity_prefix}:{record['status']}:{digest[:32]}"
    )
    encoded = _canonical_json(record)
    values = (
        record_id,
        record["policy"],
        record["model_version"],
        record["entry_client_id"],
        record["status"],
        record["entry_candle_close_ms"],
        record["exit_client_id"],
        record["exit_candle_close_ms"],
        record["entry_feature_schema"],
        entry_feature_json,
        record["entry_feature_status"],
        record["entry_freeze_id"],
        record["entry_freeze_bound_ms"],
        record["entry_decision_created_ms"],
        int(bool(record["entry_candidate_bound"])),
        record["entry_momentum"],
        record["entry_cost_usdt"],
        record["entry_quote_usdt"],
        record["entry_fee_usdt"],
        record["entry_qty"],
        record["exit_momentum"],
        record["exit_quote_usdt"],
        record["exit_fee_usdt"],
        record["exit_proceeds_usdt"],
        record["realized_pnl_usdt"],
        record["net_return"],
        int(bool(record["exact_pnl"])),
        record["quarantine_reason"],
        record["source_kind"],
        encoded,
        digest,
        now_ms,
    )
    try:
        inserted = db.execute(
            """INSERT OR IGNORE INTO worker_learning_round_trips
               (record_id, policy, model_version, entry_client_id, status,
                 entry_candle_close_ms, exit_client_id, exit_candle_close_ms,
                 entry_feature_schema, entry_feature_json, entry_feature_status,
                 entry_freeze_id, entry_freeze_bound_ms,
                 entry_decision_created_ms, entry_candidate_bound,
                 entry_momentum, entry_cost_usdt, entry_quote_usdt,
                entry_fee_usdt, entry_qty, exit_momentum, exit_quote_usdt,
                exit_fee_usdt, exit_proceeds_usdt,
                realized_pnl_usdt, net_return, exact_pnl, quarantine_reason,
               source_kind, record_json, record_sha256, created_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise LearningIntegrityError(
            "Round-trip terminal evidence conflicts with an existing record."
        ) from exc
    if inserted.rowcount:
        _bump_source_revision(db)
    stored = db.execute(
        """SELECT record_id, record_json, record_sha256
           FROM worker_learning_round_trips
           WHERE policy=? AND model_version=? AND entry_client_id=? AND status=?""",
        (
            record["policy"],
            record["model_version"],
            record["entry_client_id"],
            record["status"],
        ),
    ).fetchone()
    if (
        stored is None
        or stored["record_json"] != encoded
        or not hmac.compare_digest(str(stored["record_sha256"]), digest)
    ):
        raise LearningIntegrityError("Round-trip identity collision.")
    return {
        "status": str(record["status"]),
        "record_id": str(stored["record_id"]),
        "entry_client_id": str(record["entry_client_id"]),
        "exact_pnl": bool(record["exact_pnl"]),
    }


def _open_from_intent(
    db: sqlite3.Connection,
    client_id: str,
    policy: str,
    model_version: str,
    now_ms: int,
    *,
    source_kind: str,
) -> dict[str, object]:
    _assert_no_pending_candidate_transition(db)
    registration = _registration(db, policy, model_version)
    client_id = _text(client_id, "client_id", maximum=160)
    existing = db.execute(
        """SELECT record_id, status, exact_pnl
           FROM worker_learning_round_trips
           WHERE policy=? AND model_version=? AND entry_client_id=?
           ORDER BY CASE status WHEN 'closed' THEN 0 WHEN 'quarantined' THEN 1 ELSE 2 END
           LIMIT 1""",
        (policy, model_version, client_id),
    ).fetchone()
    if existing is not None:
        return {
            "status": str(existing["status"]),
            "record_id": str(existing["record_id"]),
            "entry_client_id": client_id,
            "exact_pnl": bool(existing["exact_pnl"]),
        }
    intent = db.execute(
        """SELECT i.client_id, i.policy AS intent_policy, i.side, i.state,
                  i.candle_close_ms, i.net_base_qty, i.cumulative_quote_qty,
                   i.commission_by_asset,
                   d.policy AS decision_policy, d.feature_schema, d.feature_json,
                   d.momentum AS entry_momentum,
                   d.created_ms AS entry_decision_created_ms
           FROM worker_order_intents AS i
           JOIN worker_decisions AS d ON d.candle_close_ms=i.candle_close_ms
           WHERE i.client_id=?""",
        (client_id,),
    ).fetchone()
    if intent is None:
        raise LearningStoreError("BUY intent is missing for round-trip evidence.")
    if (
        str(intent["side"]).upper() != "BUY"
        or str(intent["state"]).lower() != "filled"
        or str(intent["intent_policy"]) != policy
        or str(intent["decision_policy"]) != policy
    ):
        raise LearningIntegrityError("BUY intent does not match the learning identity.")
    state = db.execute(
        """SELECT position_qty, position_quote_cost
           FROM worker_state WHERE singleton=1"""
    ).fetchone()
    if state is None:
        raise LearningStoreError("Worker state is missing for BUY evidence.")
    quantity = _decimal_text(state["position_qty"], "position quantity", positive=True)
    cost = _decimal_text(
        state["position_quote_cost"], "position quote cost", positive=True
    )
    entry_quote = _decimal(
        intent["cumulative_quote_qty"], "BUY cumulative quote", positive=True
    )
    entry_cost = _decimal(cost, "position quote cost", positive=True)
    if intent["commission_by_asset"] is None:
        # Old managed fills may predate commission serialization.  Preserve the
        # worker's exact quote-cost accounting and expose the difference as the
        # fee; no feature values or market prices are fabricated.
        entry_fee = _decimal_subtract(entry_cost, entry_quote)
        if entry_fee < 0:
            raise LearningIntegrityError("Legacy BUY cost is below its quote amount.")
    else:
        commissions = _commission_map(intent["commission_by_asset"])
        unsupported = {
            asset: value
            for asset, value in commissions.items()
            if asset not in {"BTC", "USDT"} and value != 0
        }
        if unsupported:
            raise LearningIntegrityError("BUY uses an unsupported commission asset.")
        entry_fee = commissions.get("USDT", Decimal("0"))
        if _decimal_add(entry_quote, entry_fee) != entry_cost:
            raise LearningIntegrityError("BUY cost does not reconcile to quote and fee.")
    feature_schema, feature_json, feature_status = _feature_copy(intent)
    entry_decision_created_ms = _integer(
        intent["entry_decision_created_ms"], "entry decision created_ms"
    )
    entry_freeze_id = registration["frozen_candidate_sha256"]
    entry_freeze_bound_ms = (
        None
        if entry_freeze_id is None
        else int(registration["true_forward_decision_created_cutoff_ms"])
    )
    entry_candidate_bound = bool(
        source_kind == "managed_buy_fill"
        and entry_freeze_id is not None
        and entry_freeze_bound_ms is not None
        and entry_decision_created_ms > entry_freeze_bound_ms
    )
    record = _round_trip_record(
        policy=policy,
        model_version=model_version,
        entry_client_id=client_id,
        status="open",
        entry_candle_close_ms=_integer(intent["candle_close_ms"], "entry candle"),
        exit_client_id=None,
        exit_candle_close_ms=None,
        entry_feature_schema=feature_schema,
        entry_feature_json=feature_json,
        entry_feature_status=feature_status,
        entry_freeze_id=(
            None if entry_freeze_id is None else str(entry_freeze_id)
        ),
        entry_freeze_bound_ms=entry_freeze_bound_ms,
        entry_decision_created_ms=entry_decision_created_ms,
        entry_candidate_bound=entry_candidate_bound,
        entry_momentum=_decimal_text(intent["entry_momentum"], "entry momentum"),
        entry_cost_usdt=cost,
        entry_quote_usdt=_decimal_text(entry_quote, "entry quote"),
        entry_fee_usdt=_decimal_text(entry_fee, "entry fee"),
        entry_qty=quantity,
        exit_momentum=None,
        exit_quote_usdt=None,
        exit_fee_usdt=None,
        exit_proceeds_usdt=None,
        realized_pnl_usdt=None,
        net_return=None,
        exact_pnl=False,
        quarantine_reason=None,
        source_kind=source_kind,
    )
    return _insert_round_trip(db, record, feature_json, now_ms)


def open_round_trip(
    db: sqlite3.Connection,
    client_id: str,
    policy: str,
    model_version: str,
    now_ms: int,
) -> dict[str, object]:
    """Append an open round-trip fact after a managed BUY is fully accounted."""

    return _open_from_intent(
        db,
        client_id,
        _text(policy, "policy", maximum=160),
        _text(model_version, "model_version", maximum=160),
        _integer(now_ms, "now_ms"),
        source_kind="managed_buy_fill",
    )


def adopt_open_round_trip(
    db: sqlite3.Connection,
    policy: str,
    model_version: str,
    now_ms: int,
) -> dict[str, object] | None:
    """Adopt a pre-existing managed Testnet position without inventing features."""

    policy = _text(policy, "policy", maximum=160)
    model_version = _text(model_version, "model_version", maximum=160)
    now_ms = _integer(now_ms, "now_ms")
    _registration(db, policy, model_version)
    state = db.execute(
        "SELECT position_qty FROM worker_state WHERE singleton=1"
    ).fetchone()
    if state is None or _decimal(state[0], "position quantity") <= 0:
        return None
    unmatched = db.execute(
        """SELECT o.record_id, o.entry_client_id
           FROM worker_learning_round_trips AS o
           WHERE o.policy=? AND o.model_version=? AND o.status='open'
             AND NOT EXISTS (
                 SELECT 1 FROM worker_learning_round_trips AS t
                 WHERE t.policy=o.policy AND t.model_version=o.model_version
                   AND t.entry_client_id=o.entry_client_id
                   AND t.status IN ('closed','quarantined'))
           ORDER BY o.entry_candle_close_ms DESC LIMIT 1""",
        (policy, model_version),
    ).fetchone()
    if unmatched is not None:
        return {
            "status": "open",
            "record_id": str(unmatched["record_id"]),
            "entry_client_id": str(unmatched["entry_client_id"]),
            "exact_pnl": False,
        }
    origin = db.execute(
        """SELECT client_id FROM worker_order_intents
           WHERE policy=? AND side='BUY' AND state='filled'
           ORDER BY decision_ms DESC LIMIT 1""",
        (policy,),
    ).fetchone()
    if origin is None:
        raise LearningIntegrityError(
            "Tracked position has no managed BUY to adopt into learning evidence."
        )
    return _open_from_intent(
        db,
        str(origin["client_id"]),
        policy,
        model_version,
        now_ms,
        source_kind="legacy_open_position_adopted",
    )


def _open_record_for_close(
    db: sqlite3.Connection, policy: str, model_version: str
) -> sqlite3.Row | None:
    return db.execute(
        """SELECT o.* FROM worker_learning_round_trips AS o
           WHERE o.policy=? AND o.model_version=? AND o.status='open'
             AND NOT EXISTS (
                 SELECT 1 FROM worker_learning_round_trips AS t
                 WHERE t.policy=o.policy AND t.model_version=o.model_version
                   AND t.entry_client_id=o.entry_client_id
                   AND t.status IN ('closed','quarantined'))
           ORDER BY o.entry_candle_close_ms DESC LIMIT 1""",
        (policy, model_version),
    ).fetchone()


def _commission_map(raw: object) -> dict[str, Decimal]:
    if raw is None:
        raise LearningIntegrityError("SELL commission evidence is missing.")
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise LearningIntegrityError("SELL commission evidence is invalid.") from exc
    if not isinstance(value, Mapping):
        raise LearningIntegrityError("SELL commission evidence is not an object.")
    result: dict[str, Decimal] = {}
    for asset, amount in value.items():
        asset_name = _text(str(asset), "commission asset", maximum=32).upper()
        result[asset_name] = _decimal(amount, "commission amount")
    return result


def close_round_trip(
    db: sqlite3.Connection,
    client_id: str,
    policy: str,
    model_version: str,
    pnl_complete: bool,
    now_ms: int,
) -> dict[str, object] | None:
    """Append exact closed evidence, or quarantine evidence that is incomplete.

    This reads the already-accounted SELL intent and worker state.  A partial
    exit leaves the open record alone.  Only a flat, internally reconciled exit
    can become exact evidence for the learner's round-trip review gate.
    """

    client_id = _text(client_id, "client_id", maximum=160)
    policy = _text(policy, "policy", maximum=160)
    model_version = _text(model_version, "model_version", maximum=160)
    now_ms = _integer(now_ms, "now_ms")
    if not isinstance(pnl_complete, bool):
        raise ValueError("pnl_complete must be boolean.")
    _registration(db, policy, model_version)
    intent = db.execute(
        """SELECT i.client_id, i.policy, i.side, i.state, i.candle_close_ms,
                  i.cumulative_quote_qty, i.realized_pnl_usdt,
                  i.commission_by_asset, d.momentum AS exit_momentum
           FROM worker_order_intents AS i
           JOIN worker_decisions AS d ON d.candle_close_ms=i.candle_close_ms
           WHERE i.client_id=?""",
        (client_id,),
    ).fetchone()
    if intent is None:
        raise LearningStoreError("SELL intent is missing for round-trip evidence.")
    if (
        str(intent["policy"]) != policy
        or str(intent["side"]).upper() != "SELL"
        or str(intent["state"]).lower() != "filled"
    ):
        raise LearningIntegrityError("SELL intent does not match learning identity.")
    state = db.execute(
        """SELECT position_qty, pnl_complete, pnl_incomplete_reason
           FROM worker_state WHERE singleton=1"""
    ).fetchone()
    if state is None:
        raise LearningStoreError("Worker state is missing for SELL evidence.")
    if _decimal(state["position_qty"], "position quantity") != 0:
        return None
    open_row = _open_record_for_close(db, policy, model_version)
    if open_row is None:
        terminal = db.execute(
            """SELECT record_id, status, entry_client_id, exact_pnl, exit_client_id
               FROM worker_learning_round_trips
               WHERE policy=? AND model_version=? AND exit_client_id=?
                 AND status IN ('closed','quarantined') LIMIT 1""",
            (policy, model_version, client_id),
        ).fetchone()
        if terminal is not None:
            return {
                "status": str(terminal["status"]),
                "record_id": str(terminal["record_id"]),
                "entry_client_id": str(terminal["entry_client_id"]),
                "exact_pnl": bool(terminal["exact_pnl"]),
            }
        raise LearningIntegrityError("Flat SELL has no open round-trip evidence.")

    reason: str | None = None
    entry_cost = _decimal(
        open_row["entry_cost_usdt"], "entry cost", positive=True
    )
    worker_exact = pnl_complete and bool(state["pnl_complete"])
    realized: Decimal | None = None
    proceeds: Decimal | None = None
    exit_quote: Decimal | None = None
    exit_fee: Decimal | None = None
    if not worker_exact:
        reason = str(state["pnl_incomplete_reason"] or "worker_pnl_incomplete")
    elif intent["realized_pnl_usdt"] is None:
        reason = "sell_intent_realized_pnl_missing"
    else:
        try:
            realized = _decimal(intent["realized_pnl_usdt"], "realized P&L")
            gross = _decimal(
                intent["cumulative_quote_qty"], "SELL cumulative quote", positive=True
            )
            exit_quote = gross
            commissions = _commission_map(intent["commission_by_asset"])
            unsupported = {
                asset: value
                for asset, value in commissions.items()
                if asset not in {"BTC", "USDT"} and value != 0
            }
            if unsupported:
                reason = "unsupported_sell_commission_asset"
            else:
                exit_fee = commissions.get("USDT", Decimal("0"))
                proceeds = _decimal_subtract(gross, exit_fee)
                if proceeds < 0:
                    reason = "sell_commission_exceeds_proceeds"
                elif _decimal_subtract(proceeds, entry_cost) != realized:
                    reason = "round_trip_pnl_reconciliation_mismatch"
        except (ValueError, LearningIntegrityError) as exc:
            reason = f"incomplete_sell_evidence:{type(exc).__name__}"

    status = "closed" if reason is None else "quarantined"
    realized_text = (
        _decimal_text(realized, "realized P&L")
        if reason is None and realized is not None
        else None
    )
    proceeds_text = (
        _decimal_text(proceeds, "exit proceeds")
        if reason is None and proceeds is not None
        else None
    )
    net_return = (
        _decimal_text(_decimal_ratio(realized, entry_cost), "net return")
        if reason is None and realized is not None
        else None
    )
    record = _round_trip_record(
        policy=policy,
        model_version=model_version,
        entry_client_id=str(open_row["entry_client_id"]),
        status=status,
        entry_candle_close_ms=int(open_row["entry_candle_close_ms"]),
        exit_client_id=client_id,
        exit_candle_close_ms=_integer(intent["candle_close_ms"], "exit candle"),
        entry_feature_schema=str(open_row["entry_feature_schema"]),
        entry_feature_json=open_row["entry_feature_json"],
        entry_feature_status=str(open_row["entry_feature_status"]),
        entry_freeze_id=open_row["entry_freeze_id"],
        entry_freeze_bound_ms=open_row["entry_freeze_bound_ms"],
        entry_decision_created_ms=open_row["entry_decision_created_ms"],
        entry_candidate_bound=bool(open_row["entry_candidate_bound"]),
        entry_momentum=str(open_row["entry_momentum"]),
        entry_cost_usdt=str(open_row["entry_cost_usdt"]),
        entry_quote_usdt=str(open_row["entry_quote_usdt"]),
        entry_fee_usdt=str(open_row["entry_fee_usdt"]),
        entry_qty=str(open_row["entry_qty"]),
        exit_momentum=_decimal_text(intent["exit_momentum"], "exit momentum"),
        exit_quote_usdt=(
            _decimal_text(exit_quote, "exit quote")
            if reason is None and exit_quote is not None else None
        ),
        exit_fee_usdt=(
            _decimal_text(exit_fee, "exit fee")
            if reason is None and exit_fee is not None else None
        ),
        exit_proceeds_usdt=proceeds_text,
        realized_pnl_usdt=realized_text,
        net_return=net_return,
        exact_pnl=reason is None,
        quarantine_reason=reason,
        source_kind="managed_sell_fill",
    )
    return _insert_round_trip(
        db, record, open_row["entry_feature_json"], now_ms
    )


def quarantine_open_round_trips(
    db: sqlite3.Connection,
    reason: str,
    now_ms: int,
    *,
    policy: str | None = None,
    model_version: str | None = None,
) -> list[dict[str, object]]:
    """Append terminal quarantine facts before an epoch reset discards intents."""

    reason = _text(reason, "quarantine reason", maximum=512)
    now_ms = _integer(now_ms, "now_ms")
    clauses = [
        "o.status='open'",
        "NOT EXISTS (SELECT 1 FROM worker_learning_round_trips AS t "
        "WHERE t.policy=o.policy AND t.model_version=o.model_version "
        "AND t.entry_client_id=o.entry_client_id "
        "AND t.status IN ('closed','quarantined'))",
    ]
    parameters: list[object] = []
    if policy is not None:
        clauses.append("o.policy=?")
        parameters.append(_text(policy, "policy", maximum=160))
    if model_version is not None:
        clauses.append("o.model_version=?")
        parameters.append(_text(model_version, "model_version", maximum=160))
    rows = db.execute(
        "SELECT o.* FROM worker_learning_round_trips AS o WHERE "
        + " AND ".join(clauses)
        + " ORDER BY o.entry_candle_close_ms",
        parameters,
    ).fetchall()
    results: list[dict[str, object]] = []
    for row in rows:
        record = _round_trip_record(
            policy=str(row["policy"]),
            model_version=str(row["model_version"]),
            entry_client_id=str(row["entry_client_id"]),
            status="quarantined",
            entry_candle_close_ms=int(row["entry_candle_close_ms"]),
            exit_client_id=None,
            exit_candle_close_ms=None,
            entry_feature_schema=str(row["entry_feature_schema"]),
            entry_feature_json=row["entry_feature_json"],
            entry_feature_status=str(row["entry_feature_status"]),
            entry_freeze_id=row["entry_freeze_id"],
            entry_freeze_bound_ms=row["entry_freeze_bound_ms"],
            entry_decision_created_ms=row["entry_decision_created_ms"],
            entry_candidate_bound=bool(row["entry_candidate_bound"]),
            entry_momentum=str(row["entry_momentum"]),
            entry_cost_usdt=str(row["entry_cost_usdt"]),
            entry_quote_usdt=str(row["entry_quote_usdt"]),
            entry_fee_usdt=str(row["entry_fee_usdt"]),
            entry_qty=str(row["entry_qty"]),
            exit_momentum=None,
            exit_quote_usdt=None,
            exit_fee_usdt=None,
            exit_proceeds_usdt=None,
            realized_pnl_usdt=None,
            net_return=None,
            exact_pnl=False,
            quarantine_reason=reason,
            source_kind="epoch_reset_quarantine",
        )
        results.append(_insert_round_trip(db, record, row["entry_feature_json"], now_ms))
    return results


def record_source_error(
    db: sqlite3.Connection, message: str, now_ms: int
) -> dict[str, object]:
    """Append a sanitized learning-only error without changing worker state."""

    message = _text(message, "learning source error", maximum=1000)
    now_ms = _integer(now_ms, "now_ms")
    policy, model_version = _source_registration_identity(db)
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_learning_source_error",
        "policy": policy,
        "model_version": model_version,
        "message": message,
        "observed_ms": now_ms,
    }
    digest = _sha256(record)
    error_id = f"source-error:{digest}"
    encoded = _canonical_json(record)
    inserted = db.execute(
        """INSERT OR IGNORE INTO worker_learning_source_errors
           (error_id, policy, model_version, message, record_json,
            record_sha256, created_ms) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (error_id, policy, model_version, message, encoded, digest, now_ms),
    )
    if inserted.rowcount:
        _bump_source_revision(db)
    stored = db.execute(
        """SELECT message, record_json, record_sha256
           FROM worker_learning_source_errors WHERE error_id=?""",
        (error_id,),
    ).fetchone()
    if (
        stored is None
        or stored["message"] != message
        or not hmac.compare_digest(str(stored["record_json"]), encoded)
        or not hmac.compare_digest(str(stored["record_sha256"]), digest)
    ):
        raise LearningIntegrityError("Learning source error identity collision.")
    return {"status": "recorded", "error_id": error_id}


def _sanitize_refresh_error(message: str) -> str:
    if not isinstance(message, str):
        raise ValueError("refresh error must be text.")
    printable = "".join(
        character if character.isprintable() else " " for character in message
    )
    sanitized = " ".join(printable.split())[:1000].strip()
    if not sanitized:
        raise ValueError("refresh error must contain printable text.")
    return sanitized


def _refresh_health_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "healthy": not bool(row["last_attempt_failed"]),
        "last_attempt_failed": bool(row["last_attempt_failed"]),
        "sanitized_error": row["sanitized_error"],
        "attempted_ms": row["attempted_ms"],
        "last_success_ms": row["last_success_ms"],
    }


def _refresh_health(db: sqlite3.Connection) -> dict[str, object]:
    row = db.execute(
        "SELECT * FROM worker_learning_refresh_health WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise LearningStoreError("Learning refresh health is missing.")
    return _refresh_health_from_row(row)


def mark_refresh_failure(
    db: sqlite3.Connection, message: str, now_ms: int
) -> dict[str, object]:
    """Persist the latest failed aggregate refresh on the source ledger."""

    sanitized = _sanitize_refresh_error(message)
    now_ms = _integer(now_ms, "now_ms")
    cursor = db.execute(
        """UPDATE worker_learning_refresh_health SET
           last_attempt_failed=1, sanitized_error=?, attempted_ms=?
           WHERE singleton=1
             AND (attempted_ms IS NULL OR attempted_ms<=?)""",
        (sanitized, now_ms, now_ms),
    )
    if cursor.rowcount == 0:
        row = db.execute(
            "SELECT 1 FROM worker_learning_refresh_health WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise LearningStoreError("Learning refresh health is missing.")
    return _refresh_health(db)


def mark_refresh_success(
    db: sqlite3.Connection, now_ms: int
) -> dict[str, object]:
    """Clear a prior transient refresh failure after a completed refresh."""

    now_ms = _integer(now_ms, "now_ms")
    cursor = db.execute(
        """UPDATE worker_learning_refresh_health SET
           last_attempt_failed=0, sanitized_error=NULL, attempted_ms=?,
           last_success_ms=?
           WHERE singleton=1
             AND (attempted_ms IS NULL OR attempted_ms<=?)""",
        (now_ms, now_ms, now_ms),
    )
    if cursor.rowcount == 0:
        row = db.execute(
            "SELECT 1 FROM worker_learning_refresh_health WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise LearningStoreError("Learning refresh health is missing.")
    return _refresh_health(db)


def _connect_learning(path: Path | str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        _ensure_learning_schema(db)
        return db
    except BaseException:
        db.close()
        raise


def _ensure_learning_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS online_sources (
            source_ledger_id TEXT PRIMARY KEY,
            source_schema_version INTEGER NOT NULL,
            first_seen_ms INTEGER NOT NULL,
            last_seen_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS online_aggregate_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            source_ledger_id TEXT NOT NULL UNIQUE,
            policy TEXT,
            model_version TEXT,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS online_samples (
            source_ledger_id TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            decision_ts INTEGER NOT NULL,
            label_available_ts INTEGER NOT NULL,
            out_of_sample INTEGER NOT NULL CHECK (out_of_sample IN (0,1)),
            true_forward_after_freeze INTEGER NOT NULL
                CHECK (true_forward_after_freeze IN (0,1)),
            sample_id TEXT NOT NULL,
            sample_json TEXT NOT NULL,
            sample_sha256 TEXT NOT NULL,
            source_record_sha256 TEXT NOT NULL,
            ingested_ms INTEGER NOT NULL,
            PRIMARY KEY (source_ledger_id, source_record_id),
            UNIQUE (decision_ts, label_available_ts),
            UNIQUE (sample_id),
            FOREIGN KEY (source_ledger_id) REFERENCES online_sources(source_ledger_id),
            CHECK (label_available_ts-decision_ts=86400000)
        );

        CREATE INDEX IF NOT EXISTS online_samples_time_idx
            ON online_samples(decision_ts, label_available_ts);

        CREATE TABLE IF NOT EXISTS historical_development_seeds (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            seed_id TEXT NOT NULL UNIQUE,
            source_ledger_id TEXT NOT NULL,
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL UNIQUE,
            seed_record_json TEXT NOT NULL,
            seed_record_sha256 TEXT NOT NULL UNIQUE,
            sample_count INTEGER NOT NULL CHECK (sample_count>0),
            first_decision_ts INTEGER NOT NULL,
            last_decision_ts INTEGER NOT NULL,
            last_label_available_ts INTEGER NOT NULL,
            last_close TEXT NOT NULL,
            created_ms INTEGER NOT NULL,
            FOREIGN KEY (source_ledger_id) REFERENCES online_sources(source_ledger_id),
            CHECK (last_label_available_ts-last_decision_ts=86400000)
        );

        CREATE TABLE IF NOT EXISTS historical_development_samples (
            seed_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal>=0),
            decision_ts INTEGER NOT NULL,
            label_available_ts INTEGER NOT NULL,
            sample_id TEXT NOT NULL UNIQUE,
            sample_json TEXT NOT NULL,
            sample_sha256 TEXT NOT NULL UNIQUE,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            PRIMARY KEY (seed_id, ordinal),
            UNIQUE (decision_ts, label_available_ts),
            FOREIGN KEY (seed_id) REFERENCES historical_development_seeds(seed_id),
            CHECK (label_available_ts-decision_ts=86400000)
        );

        CREATE INDEX IF NOT EXISTS historical_development_samples_time_idx
            ON historical_development_samples(decision_ts, label_available_ts);

        CREATE TABLE IF NOT EXISTS online_daily_gaps (
            source_ledger_id TEXT NOT NULL,
            source_gap_id TEXT NOT NULL,
            record_json TEXT NOT NULL,
            source_record_sha256 TEXT NOT NULL,
            ingested_ms INTEGER NOT NULL,
            PRIMARY KEY (source_ledger_id, source_gap_id),
            FOREIGN KEY (source_ledger_id) REFERENCES online_sources(source_ledger_id)
        );

        CREATE TABLE IF NOT EXISTS online_round_trips (
            source_ledger_id TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            policy TEXT NOT NULL,
            model_version TEXT NOT NULL,
            entry_client_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open','closed','quarantined')),
            exact_pnl INTEGER NOT NULL CHECK (exact_pnl IN (0,1)),
            net_return TEXT,
            record_json TEXT NOT NULL,
            source_record_sha256 TEXT NOT NULL,
            ingested_ms INTEGER NOT NULL,
            PRIMARY KEY (source_ledger_id, source_record_id),
            UNIQUE (source_ledger_id, policy, model_version,
                    entry_client_id, status),
            FOREIGN KEY (source_ledger_id) REFERENCES online_sources(source_ledger_id),
            CHECK ((status='closed' AND exact_pnl=1 AND net_return IS NOT NULL)
                   OR (status!='closed' AND exact_pnl=0))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS online_one_terminal_idx
            ON online_round_trips(source_ledger_id, policy, model_version,
                                  entry_client_id)
            WHERE status IN ('closed','quarantined');

        CREATE TABLE IF NOT EXISTS online_source_errors (
            source_ledger_id TEXT NOT NULL,
            source_error_id TEXT NOT NULL,
            record_json TEXT NOT NULL,
            source_record_sha256 TEXT NOT NULL,
            ingested_ms INTEGER NOT NULL,
            PRIMARY KEY (source_ledger_id, source_error_id),
            FOREIGN KEY (source_ledger_id) REFERENCES online_sources(source_ledger_id)
        );

        CREATE TABLE IF NOT EXISTS online_ingest_conflicts (
            conflict_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            detail_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS online_learning_runs (
            run_id TEXT PRIMARY KEY,
            run_kind TEXT NOT NULL
                CHECK (run_kind IN ('progress','training','evaluation')),
            learner_version TEXT NOT NULL,
            active_segment_id TEXT NOT NULL,
            dataset_version TEXT NOT NULL,
            eligible_sample_count INTEGER NOT NULL,
            exact_round_trip_count INTEGER NOT NULL,
            exact_round_trip_evidence_sha256 TEXT NOT NULL,
            report_version TEXT NOT NULL,
            proposal_ready_for_review INTEGER NOT NULL
                CHECK (proposal_ready_for_review IN (0,1)),
            artifact_json TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS online_candidate_retirements (
            retirement_id TEXT PRIMARY KEY,
            frozen_candidate_sha256 TEXT NOT NULL UNIQUE,
            frozen_candidate_json TEXT NOT NULL,
            freeze_cutoff_ts INTEGER NOT NULL,
            reason TEXT NOT NULL,
            break_gap_id TEXT,
            break_gap_sha256 TEXT,
            boundary_sample_sha256 TEXT,
            boundary_decision_ts INTEGER,
            previous_segment_id TEXT NOT NULL,
            next_segment_id TEXT NOT NULL,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS online_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            schema_version INTEGER NOT NULL,
            learner_version TEXT NOT NULL,
            finalized_daily_label_count INTEGER NOT NULL,
            eligible_oos_daily_label_count INTEGER NOT NULL,
            true_forward_daily_label_count INTEGER NOT NULL,
            total_true_forward_daily_label_count INTEGER NOT NULL,
            exact_closed_round_trip_count INTEGER NOT NULL,
            candidate_matched_round_trip_count INTEGER NOT NULL,
            quarantined_round_trip_count INTEGER NOT NULL,
            open_round_trip_count INTEGER NOT NULL,
            ingest_conflict_count INTEGER NOT NULL,
            daily_gap_count INTEGER NOT NULL,
            source_error_count INTEGER NOT NULL,
            unresolved_learning_outbox_count INTEGER NOT NULL,
            ingested_source_revision INTEGER NOT NULL,
            aggregate_identity_sha256 TEXT,
            safety_violation_count INTEGER NOT NULL,
            safety_violation_digest TEXT NOT NULL,
            active_segment_id TEXT,
            active_segment_start_ts INTEGER,
            retired_candidate_count INTEGER NOT NULL,
            last_trained_sample_count INTEGER NOT NULL,
            next_training_sample_count INTEGER NOT NULL,
            last_training_ms INTEGER,
            latest_run_id TEXT,
            latest_report_version TEXT,
            latest_status TEXT NOT NULL,
            frozen_candidate_json TEXT,
            frozen_candidate_sha256 TEXT,
            freeze_cutoff_ts INTEGER,
            pending_candidate_transition_json TEXT,
            pending_candidate_transition_sha256 TEXT,
            proposal_ready_for_review INTEGER NOT NULL
                CHECK (proposal_ready_for_review IN (0,1)),
            exact_round_trip_evidence_sha256 TEXT NOT NULL,
            learner_migration_sha256 TEXT,
            last_error TEXT,
            updated_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS online_state_events (
            state_version TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            state_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS online_learner_migrations (
            migration_id TEXT PRIMARY KEY,
            from_learner_version TEXT NOT NULL,
            to_learner_version TEXT NOT NULL,
            source_ledger_id TEXT NOT NULL,
            source_learning_revision INTEGER NOT NULL,
            prior_state_version TEXT NOT NULL,
            prior_latest_run_id TEXT,
            record_json TEXT NOT NULL,
            record_sha256 TEXT NOT NULL UNIQUE,
            created_ms INTEGER NOT NULL
        );
        """
    )
    state_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(online_state)")
    }
    for name, declaration in (
        ("frozen_candidate_json", "TEXT"),
        ("frozen_candidate_sha256", "TEXT"),
        ("freeze_cutoff_ts", "INTEGER"),
        ("pending_candidate_transition_json", "TEXT"),
        ("pending_candidate_transition_sha256", "TEXT"),
        ("candidate_matched_round_trip_count", "INTEGER NOT NULL DEFAULT 0"),
        ("daily_gap_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_true_forward_daily_label_count", "INTEGER NOT NULL DEFAULT 0"),
        ("active_segment_id", "TEXT"),
        ("active_segment_start_ts", "INTEGER"),
        ("retired_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
        ("unresolved_learning_outbox_count", "INTEGER NOT NULL DEFAULT 0"),
        ("ingested_source_revision", "INTEGER NOT NULL DEFAULT -1"),
        ("aggregate_identity_sha256", "TEXT"),
        ("safety_violation_count", "INTEGER NOT NULL DEFAULT 0"),
        ("safety_violation_digest", "TEXT NOT NULL DEFAULT ''"),
        ("learner_migration_sha256", "TEXT"),
    ):
        if name not in state_columns:
            db.execute(f"ALTER TABLE online_state ADD COLUMN {name} {declaration}")
    aggregate_identity_columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(online_aggregate_identity)")
    }
    for name in ("policy", "model_version"):
        if name not in aggregate_identity_columns:
            db.execute(
                f"ALTER TABLE online_aggregate_identity ADD COLUMN {name} TEXT"
            )
    run_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(online_learning_runs)")
    }
    if "active_segment_id" not in run_columns:
        db.execute(
            "ALTER TABLE online_learning_runs ADD COLUMN active_segment_id TEXT"
        )
    retirement_columns = {
        str(row[1])
        for row in db.execute("PRAGMA table_info(online_candidate_retirements)")
    }
    for name, declaration in (
        ("boundary_sample_sha256", "TEXT"),
        ("boundary_decision_ts", "INTEGER"),
    ):
        if name not in retirement_columns:
            db.execute(
                f"ALTER TABLE online_candidate_retirements ADD COLUMN {name} {declaration}"
            )
    empty_digest = _sha256([])
    existing_sources = [
        str(row[0])
        for row in db.execute(
            "SELECT source_ledger_id FROM online_sources ORDER BY source_ledger_id"
        )
    ]
    if len(existing_sources) > 1:
        raise LearningIntegrityError(
            "Online learning database contains multiple source ledgers."
        )
    if existing_sources:
        db.execute(
            """INSERT OR IGNORE INTO online_aggregate_identity
               (singleton, source_ledger_id, created_ms) VALUES (1, ?, 0)""",
            (existing_sources[0],),
        )
    aggregate_identity = db.execute(
        """SELECT source_ledger_id FROM online_aggregate_identity
           WHERE singleton=1"""
    ).fetchone()
    if (
        aggregate_identity is not None
        and existing_sources
        and str(aggregate_identity[0]) != existing_sources[0]
    ):
        raise LearningIntegrityError(
            "Online learning source identity does not match ingested evidence."
        )
    db.execute(
        """UPDATE online_state SET safety_violation_digest=?
           WHERE safety_violation_digest=''""",
        (empty_digest,),
    )
    db.execute(
        """INSERT OR IGNORE INTO online_state
           (singleton, schema_version, learner_version,
            finalized_daily_label_count, eligible_oos_daily_label_count,
            true_forward_daily_label_count,
            total_true_forward_daily_label_count,
            exact_closed_round_trip_count,
            candidate_matched_round_trip_count,
            quarantined_round_trip_count, open_round_trip_count,
            ingest_conflict_count, daily_gap_count, source_error_count,
            unresolved_learning_outbox_count,
            ingested_source_revision,
            safety_violation_count, safety_violation_digest,
            active_segment_id, active_segment_start_ts,
            retired_candidate_count,
            last_trained_sample_count, next_training_sample_count,
            latest_status, proposal_ready_for_review,
            exact_round_trip_evidence_sha256, updated_ms)
           VALUES (1, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                   0, 0, -1, ?, NULL, NULL, 0, 0, ?,
                   'collecting_daily_labels', 0, ?, 0)""",
        (
            SCHEMA_VERSION,
            learner.LEARNER_VERSION,
            empty_digest,
            MIN_TRAINING_SAMPLES,
            empty_digest,
        ),
    )
    state = db.execute("SELECT * FROM online_state WHERE singleton=1").fetchone()
    if (
        state is None
        or int(state["schema_version"]) != SCHEMA_VERSION
        or str(state["learner_version"]) != learner.LEARNER_VERSION
    ):
        raise LearningStoreError("Online learning database version does not match code.")


def _source_read_connection(path: Path | str) -> sqlite3.Connection:
    source_path = Path(path).resolve()
    uri = source_path.as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, timeout=15, isolation_level=None, uri=True)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        return db
    except BaseException:
        db.close()
        raise


def _source_maintenance_connection(path: Path | str) -> sqlite3.Connection:
    """Open an existing source ledger so a no-write reservation can be held."""

    source_path = Path(path).resolve()
    uri = source_path.as_uri() + "?mode=rw"
    db = sqlite3.connect(uri, timeout=15, isolation_level=None, uri=True)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA foreign_keys=ON")
        return db
    except BaseException:
        db.close()
        raise


def _verify_source_sample(row: sqlite3.Row) -> tuple[dict[str, object], dict[str, object]]:
    record = _parse_canonical_json(row["record_json"], "source label record")
    if not isinstance(record, Mapping) or record.get("kind") != "testnet_closed_daily_label":
        raise LearningIntegrityError("Source label record has the wrong kind.")
    digest = _sha256(record)
    if not hmac.compare_digest(digest, str(row["record_sha256"])):
        raise LearningIntegrityError("Source label record hash does not match.")
    if record.get("policy") != row["policy"] or record.get("model_version") != row["model_version"]:
        raise LearningIntegrityError("Source label identity columns do not match record.")
    sample = record.get("sample")
    if not isinstance(sample, Mapping):
        raise LearningIntegrityError("Source label sample is missing.")
    sealed = dict(sample)
    supplied = sealed.pop("immutable_sha256", None)
    expected = learner.seal_sample(sealed)
    if supplied != expected.get("immutable_sha256") or dict(sample) != expected:
        raise LearningIntegrityError("Source daily sample seal does not verify.")
    if _canonical_json(sample) != row["sample_json"]:
        raise LearningIntegrityError("Source sample JSON does not match its record.")
    if sample.get("sample_id") != row["sample_id"]:
        raise LearningIntegrityError("Source sample identity does not match its columns.")
    return dict(record), dict(sample)


def _verify_source_round_trip(row: sqlite3.Row) -> dict[str, object]:
    record = _parse_canonical_json(row["record_json"], "source round-trip record")
    if not isinstance(record, Mapping) or record.get("kind") != "testnet_round_trip_evidence":
        raise LearningIntegrityError("Source round-trip record has the wrong kind.")
    digest = _sha256(record)
    if not hmac.compare_digest(digest, str(row["record_sha256"])):
        raise LearningIntegrityError("Source round-trip record hash does not match.")
    comparisons = {
        "policy": row["policy"],
        "model_version": row["model_version"],
        "entry_client_id": row["entry_client_id"],
        "status": row["status"],
        "exact_pnl": bool(row["exact_pnl"]),
        "net_return": row["net_return"],
    }
    if any(record.get(key) != value for key, value in comparisons.items()):
        raise LearningIntegrityError("Source round-trip columns do not match record.")
    binding_fields = (
        "entry_freeze_id",
        "entry_freeze_bound_ms",
        "entry_decision_created_ms",
        "entry_candidate_bound",
    )
    present = tuple(key in record for key in binding_fields)
    if any(present) and not all(present):
        raise LearningIntegrityError(
            "Source round-trip candidate binding is only partially sealed."
        )
    if all(present):
        binding_comparisons = {
            "entry_freeze_id": row["entry_freeze_id"],
            "entry_freeze_bound_ms": row["entry_freeze_bound_ms"],
            "entry_decision_created_ms": row["entry_decision_created_ms"],
            "entry_candidate_bound": bool(row["entry_candidate_bound"]),
        }
        if any(
            record.get(key) != value
            for key, value in binding_comparisons.items()
        ):
            raise LearningIntegrityError(
                "Source round-trip binding columns do not match record."
            )
    elif (
        row["entry_freeze_id"] is not None
        or row["entry_freeze_bound_ms"] is not None
        or row["entry_decision_created_ms"] is not None
        or bool(row["entry_candidate_bound"])
    ):
        raise LearningIntegrityError(
            "Legacy round-trip has non-neutral candidate binding columns."
        )
    if row["status"] == "closed" and not bool(row["exact_pnl"]):
        raise LearningIntegrityError("Closed round trip is not exact.")
    return dict(record)


def _record_conflict(
    db: sqlite3.Connection, kind: str, detail: Mapping[str, object], now_ms: int
) -> None:
    payload = {"schema": SCHEMA_VERSION, "kind": kind, "detail": dict(detail)}
    digest = _sha256(payload)
    db.execute(
        """INSERT OR IGNORE INTO online_ingest_conflicts
           (conflict_id, kind, detail_json, detail_sha256, created_ms)
           VALUES (?, ?, ?, ?, ?)""",
        (f"conflict:{digest}", kind, _canonical_json(payload), digest, now_ms),
    )


def _ingest_source(
    source: sqlite3.Connection, aggregate: sqlite3.Connection, now_ms: int
) -> str:
    meta = source.execute(
        "SELECT * FROM worker_learning_meta WHERE singleton=1"
    ).fetchone()
    if meta is None or int(meta["schema_version"]) != SCHEMA_VERSION:
        raise LearningStoreError("Worker learning source schema is missing or unsupported.")
    source_id = str(meta["source_ledger_id"])
    source_policy, source_model_version = _source_registration_identity(source)
    identity = aggregate.execute(
        """SELECT source_ledger_id, policy, model_version
           FROM online_aggregate_identity
           WHERE singleton=1"""
    ).fetchone()
    if identity is None:
        aggregate.execute(
            """INSERT INTO online_aggregate_identity
               (singleton, source_ledger_id, policy, model_version, created_ms)
               VALUES (1, ?, ?, ?, ?)""",
            (source_id, source_policy, source_model_version, now_ms),
        )
    elif not hmac.compare_digest(str(identity[0]), source_id):
        raise LearningIntegrityError(
            "Online learning database is bound to another source ledger."
        )
    elif identity["policy"] is None and identity["model_version"] is None:
        aggregate.execute(
            """UPDATE online_aggregate_identity SET policy=?, model_version=?
               WHERE singleton=1 AND policy IS NULL AND model_version IS NULL""",
            (source_policy, source_model_version),
        )
    elif (
        str(identity["policy"]) != source_policy
        or str(identity["model_version"]) != source_model_version
    ):
        raise LearningIntegrityError(
            "Online learning aggregate is bound to another policy/model identity."
        )
    identity_digest = _sha256(
        {
            "source_ledger_id": source_id,
            "policy": source_policy,
            "model_version": source_model_version,
        }
    )
    aggregate.execute(
        """UPDATE online_state SET aggregate_identity_sha256=?
           WHERE singleton=1""",
        (identity_digest,),
    )
    aggregate.execute(
        """INSERT OR IGNORE INTO online_sources
           (source_ledger_id, source_schema_version, first_seen_ms, last_seen_ms)
           VALUES (?, ?, ?, ?)""",
        (source_id, int(meta["schema_version"]), now_ms, now_ms),
    )
    aggregate.execute(
        "UPDATE online_sources SET last_seen_ms=? WHERE source_ledger_id=?",
        (now_ms, source_id),
    )
    for row in source.execute(
        "SELECT * FROM worker_learning_daily_labels ORDER BY decision_candle_close_ms"
    ):
        _require_source_row_identity(
            row, source_policy, source_model_version, "daily label"
        )
        _record, sample = _verify_source_sample(row)
        existing = aggregate.execute(
            """SELECT source_record_sha256 FROM online_samples
               WHERE source_ledger_id=? AND source_record_id=?""",
            (source_id, row["record_id"]),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(
                str(existing[0]), str(row["record_sha256"])
            ):
                raise LearningIntegrityError("Previously ingested daily label changed.")
            continue
        overlap = aggregate.execute(
            """SELECT source_ledger_id, source_record_id, source_record_sha256
               FROM online_samples WHERE decision_ts=? AND label_available_ts=?""",
            (sample["decision_ts"], sample["label_available_ts"]),
        ).fetchone()
        if overlap is not None:
            _record_conflict(
                aggregate,
                "overlapping_daily_label",
                {
                    "incoming_source_ledger_id": source_id,
                    "incoming_record_id": row["record_id"],
                    "existing_source_ledger_id": overlap["source_ledger_id"],
                    "existing_record_id": overlap["source_record_id"],
                    "decision_ts": sample["decision_ts"],
                    "label_available_ts": sample["label_available_ts"],
                },
                now_ms,
            )
            continue
        aggregate.execute(
            """INSERT INTO online_samples
               (source_ledger_id, source_record_id, policy, model_version,
                decision_ts, label_available_ts, out_of_sample,
                true_forward_after_freeze, sample_id, sample_json,
                sample_sha256, source_record_sha256, ingested_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                row["record_id"],
                row["policy"],
                row["model_version"],
                sample["decision_ts"],
                sample["label_available_ts"],
                int(bool(sample["out_of_sample"])),
                int(bool(sample["true_forward_after_freeze"])),
                sample["sample_id"],
                row["sample_json"],
                row["sample_sha256"],
                row["record_sha256"],
                now_ms,
            ),
        )

    for row in source.execute(
        "SELECT * FROM worker_learning_daily_gaps ORDER BY created_ms, gap_id"
    ):
        _require_source_row_identity(
            row, source_policy, source_model_version, "daily gap"
        )
        record = _parse_canonical_json(row["record_json"], "source daily gap")
        digest = _sha256(record)
        if (
            not isinstance(record, Mapping)
            or record.get("kind") != "testnet_daily_label_gap"
            or record.get("policy") != row["policy"]
            or record.get("model_version") != row["model_version"]
            or record.get("previous_candle_close_ms")
            != row["previous_candle_close_ms"]
            or record.get("current_candle_close_ms")
            != row["current_candle_close_ms"]
            or record.get("reason") != row["reason"]
            or not hmac.compare_digest(digest, str(row["record_sha256"]))
        ):
            raise LearningIntegrityError("Source daily gap hash does not match.")
        existing = aggregate.execute(
            """SELECT record_json, source_record_sha256
               FROM online_daily_gaps
               WHERE source_ledger_id=? AND source_gap_id=?""",
            (source_id, row["gap_id"]),
        ).fetchone()
        if existing is not None:
            if (
                not hmac.compare_digest(
                    str(existing["source_record_sha256"]),
                    str(row["record_sha256"]),
                )
                or not hmac.compare_digest(
                    str(existing["record_json"]), str(row["record_json"])
                )
            ):
                raise LearningIntegrityError(
                    "Previously ingested daily gap evidence changed."
                )
            continue
        aggregate.execute(
            """INSERT INTO online_daily_gaps
               (source_ledger_id, source_gap_id, record_json,
                source_record_sha256, ingested_ms) VALUES (?, ?, ?, ?, ?)""",
            (
                source_id,
                row["gap_id"],
                row["record_json"],
                row["record_sha256"],
                now_ms,
            ),
        )

    for row in source.execute(
        "SELECT * FROM worker_learning_round_trips ORDER BY created_ms, record_id"
    ):
        _require_source_row_identity(
            row, source_policy, source_model_version, "round trip"
        )
        record = _verify_source_round_trip(row)
        existing = aggregate.execute(
            """SELECT source_record_sha256 FROM online_round_trips
               WHERE source_ledger_id=? AND source_record_id=?""",
            (source_id, row["record_id"]),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(
                str(existing[0]), str(row["record_sha256"])
            ):
                raise LearningIntegrityError(
                    "Previously ingested round-trip evidence changed."
                )
            continue
        try:
            aggregate.execute(
                """INSERT INTO online_round_trips
                   (source_ledger_id, source_record_id, policy, model_version,
                    entry_client_id, status, exact_pnl, net_return, record_json,
                    source_record_sha256, ingested_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id,
                    row["record_id"],
                    row["policy"],
                    row["model_version"],
                    row["entry_client_id"],
                    row["status"],
                    int(bool(row["exact_pnl"])),
                    row["net_return"],
                    row["record_json"],
                    row["record_sha256"],
                    now_ms,
                ),
            )
        except sqlite3.IntegrityError:
            _record_conflict(
                aggregate,
                "round_trip_terminal_conflict",
                {
                    "source_ledger_id": source_id,
                    "source_record_id": row["record_id"],
                    "entry_client_id": record["entry_client_id"],
                    "status": record["status"],
                },
                now_ms,
            )

    for row in source.execute(
        "SELECT * FROM worker_learning_source_errors ORDER BY created_ms, error_id"
    ):
        _require_source_row_identity(
            row, source_policy, source_model_version, "source error"
        )
        record = _parse_canonical_json(row["record_json"], "source error record")
        digest = _sha256(record)
        if (
            not isinstance(record, Mapping)
            or record.get("kind") != "testnet_learning_source_error"
            or record.get("policy") != row["policy"]
            or record.get("model_version") != row["model_version"]
            or record.get("message") != row["message"]
            or not hmac.compare_digest(digest, str(row["record_sha256"]))
        ):
            raise LearningIntegrityError("Source error record hash does not match.")
        existing = aggregate.execute(
            """SELECT record_json, source_record_sha256
               FROM online_source_errors
               WHERE source_ledger_id=? AND source_error_id=?""",
            (source_id, row["error_id"]),
        ).fetchone()
        if existing is not None:
            if (
                not hmac.compare_digest(
                    str(existing["source_record_sha256"]),
                    str(row["record_sha256"]),
                )
                or not hmac.compare_digest(
                    str(existing["record_json"]), str(row["record_json"])
                )
            ):
                raise LearningIntegrityError(
                    "Previously ingested source error evidence changed."
                )
            continue
        aggregate.execute(
            """INSERT INTO online_source_errors
               (source_ledger_id, source_error_id, record_json,
                source_record_sha256, ingested_ms) VALUES (?, ?, ?, ?, ?)""",
            (
                source_id,
                row["error_id"],
                row["record_json"],
                row["record_sha256"],
                now_ms,
            ),
        )
    return source_id


def _aggregate_source_identity(db: sqlite3.Connection) -> tuple[str, str, str]:
    row = db.execute(
        """SELECT source_ledger_id, policy, model_version
           FROM online_aggregate_identity
           WHERE singleton=1"""
    ).fetchone()
    if row is None or row["policy"] is None or row["model_version"] is None:
        raise LearningIntegrityError("Online aggregate source identity is missing.")
    return str(row["source_ledger_id"]), str(row["policy"]), str(
        row["model_version"]
    )


def _aggregate_source_id(db: sqlite3.Connection) -> str:
    return _aggregate_source_identity(db)[0]


def _validated_seed_manifest(
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Validate a builder-owned manifest without trusting caller fields."""

    if not isinstance(manifest, Mapping):
        raise LearningIntegrityError("Historical development seed is not an object.")
    try:
        import binance_testnet_learning_seed as historical_bootstrap

        verified = historical_bootstrap.verify_seed_manifest(manifest)
    except (ImportError, AttributeError) as exc:
        raise LearningStoreError(
            "Historical development seed verifier is unavailable."
        ) from exc
    except (TypeError, ValueError) as exc:
        raise LearningIntegrityError(
            f"Historical development seed does not verify: {exc}"
        ) from exc
    if not isinstance(verified, Mapping):
        raise LearningIntegrityError(
            "Historical development seed verifier returned malformed data."
        )
    try:
        supplied_json = _canonical_json(dict(manifest))
        verified_json = _canonical_json(dict(verified))
    except (TypeError, ValueError) as exc:
        raise LearningIntegrityError(
            "Historical development seed is not canonical JSON data."
        ) from exc
    if supplied_json != verified_json:
        raise LearningIntegrityError(
            "Historical development seed is not in canonical verified form."
        )
    return dict(verified)


def _historical_seed_record(
    *,
    seed_id: str | None,
    source_ledger_id: str,
    policy: str,
    model_version: str,
    manifest: Mapping[str, object],
) -> tuple[str, dict[str, object], str]:
    manifest_sha256 = str(manifest["immutable_sha256"])
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_historical_development_seed",
        "source_ledger_id": source_ledger_id,
        "policy": policy,
        "model_version": model_version,
        "manifest_sha256": manifest_sha256,
        "sample_count": int(manifest["sample_count"]),
        "first_decision_ts": int(manifest["first_decision_ts"]),
        "last_decision_ts": int(manifest["last_decision_ts"]),
        "last_label_available_ts": int(manifest["last_label_available_ts"]),
        "last_close": str(manifest["last_close"]),
        "evidence_role": "development_only_not_forward_or_execution_evidence",
    }
    digest = _sha256(record)
    expected_id = f"history:{_model_prefix(policy, model_version)}:{digest[:32]}"
    if seed_id is not None and not hmac.compare_digest(seed_id, expected_id):
        raise LearningIntegrityError("Historical development seed id does not verify.")
    return expected_id, record, digest


def _historical_sample_record(
    seed_id: str,
    ordinal: int,
    sample: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_historical_development_sample",
        "seed_id": seed_id,
        "ordinal": ordinal,
        "sample": dict(sample),
    }
    return record, _sha256(record)


def _verified_historical_seed(
    db: sqlite3.Connection,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    """Return the single immutable development seed after full verification."""

    historical_tables = {
        str(row[0])
        for row in db.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name IN
                     ('historical_development_seeds',
                      'historical_development_samples')"""
        )
    }
    if not historical_tables:
        # Read-only status must remain available for a schema-2 aggregate that
        # predates this additive feature.  The next mutating refresh installs
        # both tables before a seed can be accepted.
        return None, []
    if historical_tables != {
        "historical_development_seeds",
        "historical_development_samples",
    }:
        raise LearningIntegrityError(
            "Historical development schema is only partially installed."
        )
    row = db.execute(
        "SELECT * FROM historical_development_seeds WHERE singleton=1"
    ).fetchone()
    sample_row_count = int(
        db.execute(
            "SELECT COUNT(*) FROM historical_development_samples"
        ).fetchone()[0]
    )
    if row is None:
        if sample_row_count:
            raise LearningIntegrityError(
                "Historical development samples exist without their seed."
            )
        return None, []

    source_id, source_policy, source_model_version = _aggregate_source_identity(db)
    manifest_value = _parse_canonical_json(
        row["manifest_json"], "historical development seed manifest"
    )
    if not isinstance(manifest_value, Mapping):
        raise LearningIntegrityError(
            "Historical development seed manifest is not an object."
        )
    manifest = _validated_seed_manifest(manifest_value)
    manifest_sha256 = str(manifest["immutable_sha256"])
    seed_id, expected_record, expected_record_digest = _historical_seed_record(
        seed_id=str(row["seed_id"]),
        source_ledger_id=source_id,
        policy=source_policy,
        model_version=source_model_version,
        manifest=manifest,
    )
    record_value = _parse_canonical_json(
        row["seed_record_json"], "historical development seed record"
    )
    if (
        not isinstance(record_value, Mapping)
        or dict(record_value) != expected_record
        or str(row["source_ledger_id"]) != source_id
        or str(row["policy"]) != source_policy
        or str(row["model_version"]) != source_model_version
        or not hmac.compare_digest(str(row["manifest_sha256"]), manifest_sha256)
        or not hmac.compare_digest(
            str(row["seed_record_sha256"]), expected_record_digest
        )
        or int(row["sample_count"]) != int(manifest["sample_count"])
        or int(row["first_decision_ts"]) != int(manifest["first_decision_ts"])
        or int(row["last_decision_ts"]) != int(manifest["last_decision_ts"])
        or int(row["last_label_available_ts"])
        != int(manifest["last_label_available_ts"])
        or str(row["last_close"]) != str(manifest["last_close"])
    ):
        raise LearningIntegrityError(
            "Historical development seed provenance does not verify."
        )

    manifest_samples = manifest.get("samples")
    if not isinstance(manifest_samples, list):
        raise LearningIntegrityError(
            "Historical development seed samples are malformed."
        )
    rows = db.execute(
        """SELECT * FROM historical_development_samples
           WHERE seed_id=? ORDER BY ordinal""",
        (seed_id,),
    ).fetchall()
    if len(rows) != len(manifest_samples) or len(rows) != sample_row_count:
        raise LearningIntegrityError(
            "Historical development sample count does not verify."
        )
    verified_samples: list[dict[str, object]] = []
    for ordinal, (sample_row, manifest_sample) in enumerate(
        zip(rows, manifest_samples)
    ):
        if not isinstance(manifest_sample, Mapping):
            raise LearningIntegrityError(
                "Historical development sample is not an object."
            )
        sample_value = _parse_canonical_json(
            sample_row["sample_json"], "historical development sample"
        )
        if not isinstance(sample_value, Mapping):
            raise LearningIntegrityError(
                "Historical development sample is not an object."
            )
        try:
            normalized = learner.seal_sample(
                {
                    key: value
                    for key, value in sample_value.items()
                    if key != "immutable_sha256"
                }
            )
        except (TypeError, ValueError) as exc:
            raise LearningIntegrityError(
                f"Historical development sample does not verify: {exc}"
            ) from exc
        record, record_digest = _historical_sample_record(
            seed_id, ordinal, normalized
        )
        stored_record = _parse_canonical_json(
            sample_row["record_json"], "historical development sample record"
        )
        if (
            int(sample_row["ordinal"]) != ordinal
            or dict(sample_value) != dict(manifest_sample)
            or dict(sample_value) != normalized
            or not isinstance(stored_record, Mapping)
            or dict(stored_record) != record
            or int(sample_row["decision_ts"]) != int(normalized["decision_ts"])
            or int(sample_row["label_available_ts"])
            != int(normalized["label_available_ts"])
            or str(sample_row["sample_id"]) != str(normalized["sample_id"])
            or not hmac.compare_digest(
                str(sample_row["sample_sha256"]),
                str(normalized["immutable_sha256"]),
            )
            or not hmac.compare_digest(
                str(sample_row["record_sha256"]), record_digest
            )
        ):
            raise LearningIntegrityError(
                "Historical development sample provenance does not verify."
            )
        if (
            bool(normalized["out_of_sample"])
            or bool(normalized["true_forward_after_freeze"])
            or normalized.get("freeze_id") is not None
        ):
            raise LearningIntegrityError(
                "Historical seed attempted to claim forward evidence."
            )
        verified_samples.append(normalized)

    try:
        # Reuse the learner's public validation path.  A progress report cannot
        # promote or activate anything and proves ordering/contiguity here.
        learner.learn(verified_samples, incumbent_threshold="0.10")
    except ValueError as exc:
        raise LearningIntegrityError(
            f"Historical development sample sequence is invalid: {exc}"
        ) from exc
    return {
        "seed_id": seed_id,
        "manifest_sha256": manifest_sha256,
        "sample_count": len(verified_samples),
        "first_decision_ts": int(manifest["first_decision_ts"]),
        "last_decision_ts": int(manifest["last_decision_ts"]),
        "last_label_available_ts": int(manifest["last_label_available_ts"]),
        "last_close": str(manifest["last_close"]),
        "evidence_role": str(manifest["evidence_role"]),
    }, verified_samples


def _verified_online_samples(
    db: sqlite3.Connection,
) -> list[dict[str, object]]:
    source_id, source_policy, source_model_version = _aggregate_source_identity(db)
    verified: list[dict[str, object]] = []
    for row in db.execute("SELECT * FROM online_samples"):
        sample = _parse_canonical_json(row["sample_json"], "online sample")
        if not isinstance(sample, Mapping):
            raise LearningIntegrityError("Online sample is not an object.")
        supplied = sample.get("immutable_sha256")
        payload = {key: value for key, value in sample.items() if key != "immutable_sha256"}
        expected = learner.seal_sample(payload)
        identity_prefix = _model_prefix(
            str(row["policy"]), str(row["model_version"])
        )
        source_record = {
            "schema": SCHEMA_VERSION,
            "kind": "testnet_closed_daily_label",
            "policy": row["policy"],
            "model_version": row["model_version"],
            "identity_prefix": identity_prefix,
            "sample": dict(sample),
        }
        source_digest = _sha256(source_record)
        expected_record_id = f"label:{identity_prefix}:{source_digest[:32]}"
        if (
            supplied != expected["immutable_sha256"]
            or supplied != row["sample_sha256"]
            or str(row["source_ledger_id"]) != source_id
            or str(row["policy"]) != source_policy
            or str(row["model_version"]) != source_model_version
            or str(row["source_record_id"]) != expected_record_id
            or str(row["source_record_sha256"]) != source_digest
            or int(row["decision_ts"]) != int(sample["decision_ts"])
            or int(row["label_available_ts"])
            != int(sample["label_available_ts"])
            or bool(row["out_of_sample"]) != bool(sample["out_of_sample"])
            or bool(row["true_forward_after_freeze"])
            != bool(sample["true_forward_after_freeze"])
            or str(row["sample_id"]) != str(sample["sample_id"])
        ):
            raise LearningIntegrityError("Online sample seal does not verify.")
        verified.append(dict(sample))
    verified.sort(
        key=lambda sample: (
            int(sample["decision_ts"]),
            int(sample["label_available_ts"]),
        )
    )
    return verified


def _verified_learning_segments(
    db: sqlite3.Connection,
) -> list[list[dict[str, object]]]:
    verified_live_samples = _verified_online_samples(db)
    seed, historical_samples = _verified_historical_seed(db)
    if historical_samples:
        non_oos_samples = [
            sample
            for sample in verified_live_samples
            if not bool(sample["out_of_sample"])
        ]
        if non_oos_samples and (
            len(non_oos_samples) != 1
            or non_oos_samples[0] is not verified_live_samples[0]
            or not _is_historical_pre_oos_bridge(seed, non_oos_samples[0])
        ):
            raise LearningIntegrityError(
                "Historical learning suffix contains an unexpected non-OOS sample."
            )
        live_samples = verified_live_samples
    else:
        # Pre-registration labels are not development evidence unless a sealed
        # historical prefix gives the single boundary label exact provenance.
        live_samples = [
            sample
            for sample in verified_live_samples
            if bool(sample["out_of_sample"])
        ]
    if not live_samples:
        return [historical_samples] if historical_samples else []
    live_segments: list[list[dict[str, object]]] = [[]]
    previous_label: int | None = None
    for sample in live_samples:
        if (
            previous_label is not None
            and int(sample["decision_ts"]) != previous_label
        ):
            live_segments.append([])
        live_segments[-1].append(sample)
        previous_label = int(sample["label_available_ts"])
    if not historical_samples:
        return live_segments

    historical_last = int(historical_samples[-1]["label_available_ts"])
    first_live = int(live_segments[0][0]["decision_ts"])
    if first_live < historical_last:
        raise LearningIntegrityError(
            "Live daily evidence overlaps the historical development seed."
        )
    if first_live == historical_last:
        live_segments[0] = historical_samples + live_segments[0]
        return live_segments
    # A non-contiguous live suffix remains a separate lifecycle.  Gap handling
    # below chooses the latest segment and never reuses this historical prefix.
    return [historical_samples, *live_segments]


def _is_historical_pre_oos_bridge(
    seed: Mapping[str, object] | None,
    sample: Mapping[str, object],
) -> bool:
    """Accept only the one causal label spanning a pre-registration anchor."""

    if seed is None:
        return False
    boundary = int(seed["last_label_available_ts"])
    return bool(
        int(sample["decision_ts"]) == boundary
        and int(sample["label_available_ts"]) == boundary + DAY_MS
        and not bool(sample["out_of_sample"])
        and not bool(sample["true_forward_after_freeze"])
        and "freeze_id" not in sample
    )


def _candidate_segment_index(
    segments: Sequence[Sequence[Mapping[str, object]]],
    frozen_candidate: Mapping[str, object],
) -> int:
    expected_count = int(frozen_candidate["development_sample_count"])
    first_hash = str(frozen_candidate["development_first_hash"])
    last_hash = str(frozen_candidate["development_last_hash"])
    for index, segment in enumerate(segments):
        if (
            len(segment) >= expected_count
            and segment[0].get("immutable_sha256") == first_hash
            and segment[expected_count - 1].get("immutable_sha256") == last_hash
        ):
            return index
    raise LearningIntegrityError(
        "Frozen candidate development segment is no longer available contiguously."
    )


def _verified_learning_samples(
    db: sqlite3.Connection,
    frozen_candidate: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    segments = _verified_learning_segments(db)
    if not segments:
        return []
    if frozen_candidate is None:
        # A gap never becomes a stretched label.  New training uses only the
        # latest strict daily segment, while all older facts remain immutable.
        return segments[-1]
    return list(segments[_candidate_segment_index(segments, frozen_candidate)])


def _active_state_learning_samples(
    db: sqlite3.Connection,
    state: Mapping[str, object],
) -> list[dict[str, object]]:
    """Reconstruct the active sealed segment named by persisted state."""

    active_segment_id = state.get("active_segment_id")
    segments = _verified_learning_segments(db)
    if active_segment_id is None:
        return []
    for segment in segments:
        for start in range(len(segment)):
            suffix = segment[start:]
            if _segment_id(suffix) == str(active_segment_id):
                return [dict(sample) for sample in suffix]
    if str(active_segment_id) == _segment_id([]):
        return []
    # A gap can name an empty future segment.  It carries no development
    # labels until the first exact daily sample arrives.
    for gap in _verified_daily_gaps(db):
        if _segment_id([], gap) == str(active_segment_id):
            return []
    raise LearningIntegrityError(
        "Active learning segment does not match sealed aggregate evidence."
    )


def _verified_daily_gaps(db: sqlite3.Connection) -> list[dict[str, object]]:
    gaps: list[dict[str, object]] = []
    source_id, source_policy, source_model_version = _aggregate_source_identity(db)
    for row in db.execute(
        """SELECT source_ledger_id, source_gap_id, record_json,
                  source_record_sha256
           FROM online_daily_gaps"""
    ):
        record = _parse_canonical_json(row["record_json"], "online daily gap")
        if not isinstance(record, Mapping) or record.get("kind") != "testnet_daily_label_gap":
            raise LearningIntegrityError("Online daily gap semantics are invalid.")
        digest = _sha256(record)
        expected_gap_id = (
            f"gap:{record.get('identity_prefix')}:{digest[:32]}"
        )
        if (
            str(row["source_ledger_id"]) != source_id
            or record.get("policy") != source_policy
            or record.get("model_version") != source_model_version
            or str(row["source_gap_id"]) != expected_gap_id
            or not hmac.compare_digest(
                digest, str(row["source_record_sha256"])
            )
        ):
            raise LearningIntegrityError("Online daily gap seal does not verify.")
        current_ms = _integer(
            record.get("current_candle_close_ms"), "gap current candle"
        )
        previous_raw = record.get("previous_candle_close_ms")
        previous_ms = (
            None
            if previous_raw is None
            else _integer(previous_raw, "gap previous candle")
        )
        gaps.append(
            {
                "source_gap_id": str(row["source_gap_id"]),
                "source_record_sha256": digest,
                "current_candle_close_ms": current_ms,
                "previous_candle_close_ms": previous_ms,
                "reason": str(record.get("reason", "unknown")),
            }
        )
    gaps.sort(
        key=lambda item: (
            int(item["current_candle_close_ms"]),
            str(item["source_gap_id"]),
        )
    )
    return gaps


def _verified_online_source_errors(
    db: sqlite3.Connection,
) -> list[dict[str, object]]:
    source_id, source_policy, source_model_version = _aggregate_source_identity(db)
    records: list[dict[str, object]] = []
    for row in db.execute("SELECT * FROM online_source_errors"):
        record = _parse_canonical_json(
            row["record_json"], "online source error"
        )
        digest = _sha256(record)
        if (
            not isinstance(record, Mapping)
            or record.get("kind") != "testnet_learning_source_error"
            or str(row["source_ledger_id"]) != source_id
            or record.get("policy") != source_policy
            or record.get("model_version") != source_model_version
            or str(row["source_error_id"]) != f"source-error:{digest}"
            or not hmac.compare_digest(
                digest, str(row["source_record_sha256"])
            )
        ):
            raise LearningIntegrityError(
                "Online source error seal does not verify."
            )
        records.append(dict(record))
    records.sort(
        key=lambda record: (
            int(record["observed_ms"]),
            _sha256(record),
        )
    )
    return records


def _verified_ingest_conflicts(
    db: sqlite3.Connection,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for row in db.execute("SELECT * FROM online_ingest_conflicts"):
        record = _parse_canonical_json(
            row["detail_json"], "online ingest conflict"
        )
        digest = _sha256(record)
        if (
            not isinstance(record, Mapping)
            or record.get("schema") != SCHEMA_VERSION
            or record.get("kind") != row["kind"]
            or not isinstance(record.get("detail"), Mapping)
            or str(row["conflict_id"]) != f"conflict:{digest}"
            or not hmac.compare_digest(digest, str(row["detail_sha256"]))
        ):
            raise LearningIntegrityError(
                "Online ingest conflict seal does not verify."
            )
        records.append(dict(record))
    records.sort(key=_sha256)
    return records


def _segment_id(
    samples: Sequence[Mapping[str, object]],
    pending_gap: Mapping[str, object] | None = None,
) -> str:
    if samples:
        basis = {
            "kind": "strict_daily_segment",
            "first_decision_ts": int(samples[0]["decision_ts"]),
            "first_sample_sha256": str(samples[0]["immutable_sha256"]),
        }
    elif pending_gap is not None:
        basis = {
            "kind": "strict_daily_segment_after_gap",
            "break_gap_sha256": str(pending_gap["source_record_sha256"]),
            "first_decision_ts": int(pending_gap["current_candle_close_ms"]),
        }
    else:
        basis = {"kind": "strict_daily_segment_initial_empty"}
    return f"segment:{_sha256(basis)}"


def _latest_development_segment(
    segments: Sequence[Sequence[Mapping[str, object]]],
    gaps: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], Mapping[str, object] | None]:
    """Return only the newest segment after the newest observed gap boundary."""

    if not segments:
        return [], (gaps[-1] if gaps else None)
    if not gaps:
        return [dict(sample) for sample in segments[-1]], None
    latest_gap = gaps[-1]
    boundary = int(latest_gap["current_candle_close_ms"])
    eligible = [
        segment
        for segment in segments
        if segment and int(segment[0]["decision_ts"]) >= boundary
    ]
    if not eligible:
        return [], latest_gap
    return [dict(sample) for sample in eligible[-1]], latest_gap


def _verified_online_round_trip_records(
    db: sqlite3.Connection,
) -> list[tuple[dict[str, object], str]]:
    source_id, source_policy, source_model_version = _aggregate_source_identity(db)
    records: list[tuple[dict[str, object], str]] = []
    for row in db.execute("SELECT * FROM online_round_trips"):
        record = _parse_canonical_json(row["record_json"], "online round trip")
        digest = _sha256(record)
        if not isinstance(record, Mapping):
            raise LearningIntegrityError("Online round trip is not an object.")
        identity_prefix = _model_prefix(
            str(record.get("policy")), str(record.get("model_version"))
        )
        expected_record_id = (
            f"rt:{identity_prefix}:{record.get('status')}:{digest[:32]}"
        )
        legacy_record_id = f"legacy:{record.get('status')}:{digest[:32]}"
        has_candidate_binding = all(
            field in record
            for field in (
                "entry_freeze_id",
                "entry_freeze_bound_ms",
                "entry_decision_created_ms",
                "entry_candidate_bound",
            )
        )
        allowed_record_ids = (
            {expected_record_id}
            if has_candidate_binding
            else {expected_record_id, legacy_record_id}
        )
        if (
            str(row["source_ledger_id"]) != source_id
            or record.get("policy") != source_policy
            or record.get("model_version") != source_model_version
            or str(row["source_record_id"])
            not in allowed_record_ids
            or record.get("policy") != row["policy"]
            or record.get("model_version") != row["model_version"]
            or record.get("entry_client_id") != row["entry_client_id"]
            or record.get("status") != row["status"]
            or bool(record.get("exact_pnl")) != bool(row["exact_pnl"])
            or record.get("net_return") != row["net_return"]
            or not hmac.compare_digest(
                digest, str(row["source_record_sha256"])
            )
        ):
            raise LearningIntegrityError("Online round-trip seal does not verify.")
        records.append((dict(record), digest))
    records.sort(
        key=lambda item: (
            str(item[0]["policy"]),
            str(item[0]["model_version"]),
            str(item[0]["entry_client_id"]),
            str(item[0]["status"]),
        )
    )
    return records


def _exact_round_trip_records(
    db: sqlite3.Connection,
) -> list[tuple[dict[str, object], str]]:
    return [
        item
        for item in _verified_online_round_trip_records(db)
        if item[0].get("status") == "closed"
        and item[0].get("exact_pnl") is True
    ]


def _learner_metric(value: Decimal) -> str:
    # Mirrors the public learner contract's 12-decimal reconciled return.
    if not value.is_finite():
        raise LearningIntegrityError("Round-trip metric is not finite.")
    with localcontext(_DECIMAL_CONTEXT):
        rounded = value.quantize(Decimal("0.000000000001"))
    return _decimal_text(rounded, "round-trip metric")


def _candidate_round_trips(
    records: Sequence[tuple[Mapping[str, object], str]],
    frozen_candidate: Mapping[str, object] | None,
    latest_label_ts: int | None,
    samples: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Bind only observed trades that implement the frozen threshold edges."""

    if frozen_candidate is None or latest_label_ts is None:
        return []
    threshold = _decimal(
        frozen_candidate["challenger_threshold"], "challenger threshold"
    )
    freeze_id = str(frozen_candidate["immutable_sha256"])
    freeze_cutoff = _integer(
        frozen_candidate["freeze_cutoff_ts"], "freeze cutoff"
    )
    momentum_path = {
        int(sample["decision_ts"]): _decimal(
            sample["momentum"], "daily path momentum"
        )
        for sample in samples
    }
    result: list[dict[str, object]] = []
    for record, source_hash in records:
        entry_ts = _integer(record["entry_candle_close_ms"], "entry candle")
        exit_ts = _integer(record["exit_candle_close_ms"], "exit candle")
        entry_freeze_id = record.get("entry_freeze_id")
        entry_freeze_bound_ms = record.get("entry_freeze_bound_ms")
        entry_decision_created_ms = record.get("entry_decision_created_ms")
        if (
            entry_ts <= freeze_cutoff
            or exit_ts <= entry_ts
            or exit_ts > latest_label_ts
            or record.get("entry_candidate_bound") is not True
            or entry_freeze_id != freeze_id
            or entry_freeze_bound_ms is None
            or entry_decision_created_ms is None
            or _integer(
                entry_decision_created_ms, "entry decision created_ms"
            )
            <= _integer(entry_freeze_bound_ms, "entry freeze bound_ms")
        ):
            continue
        entry_momentum = _decimal(record.get("entry_momentum"), "entry momentum")
        exit_momentum = _decimal(record.get("exit_momentum"), "exit momentum")
        previous_momentum = momentum_path.get(entry_ts - DAY_MS)
        path_entry = momentum_path.get(entry_ts)
        path_exit = momentum_path.get(exit_ts)
        if (
            previous_momentum is None
            or path_entry is None
            or path_exit is None
            or path_entry != entry_momentum
            or path_exit != exit_momentum
            or previous_momentum > threshold
            or entry_momentum <= threshold
            or exit_momentum > threshold
        ):
            continue
        cursor = entry_ts + DAY_MS
        path_matches = True
        while cursor < exit_ts:
            momentum = momentum_path.get(cursor)
            if momentum is None or momentum <= threshold:
                path_matches = False
                break
            cursor += DAY_MS
        if not path_matches or cursor != exit_ts:
            continue
        entry_cost = _decimal(
            record["entry_cost_usdt"], "entry cost", positive=True
        )
        proceeds = _decimal(record["exit_proceeds_usdt"], "exit proceeds")
        pnl = _decimal(record["realized_pnl_usdt"], "realized P&L")
        if _decimal_subtract(proceeds, entry_cost) != pnl:
            raise LearningIntegrityError(
                "Candidate-bound round trip no longer reconciles."
            )
        payload = {
            "round_trip_id": f"rtf:{source_hash[:48]}",
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "entry_client_id": str(record["entry_client_id"]),
            "exit_client_id": str(record["exit_client_id"]),
            "entry_cost_usdt": _decimal_text(entry_cost, "entry cost"),
            "exit_proceeds_usdt": _decimal_text(proceeds, "exit proceeds"),
            "realized_pnl_usdt": _decimal_text(pnl, "realized P&L"),
            "net_return": _learner_metric(_decimal_ratio(pnl, entry_cost)),
            "environment": "binance_spot_testnet",
            "symbol": "BTCUSDT",
            "freeze_id": freeze_id,
            "closed": True,
        }
        result.append(learner.seal_round_trip(payload))
    result.sort(key=lambda item: (int(item["entry_ts"]), int(item["exit_ts"])))
    return result


def _candidate_binding_details(
    candidate: Mapping[str, object],
) -> tuple[str, str, int]:
    candidate_json = _canonical_json(candidate)
    candidate_hash = str(candidate.get("immutable_sha256", ""))
    if len(candidate_hash) != 64 or candidate_hash != _sha256(
        {key: value for key, value in candidate.items() if key != "immutable_sha256"}
    ):
        raise LearningIntegrityError("Frozen candidate seal does not verify.")
    cutoff = _integer(candidate["freeze_cutoff_ts"], "freeze cutoff")
    return candidate_json, candidate_hash, cutoff


def _transition_candidate_on_source(
    source_db_path: Path | str,
    *,
    retiring_candidate: Mapping[str, object] | None,
    replacement_candidate: Mapping[str, object] | None,
    break_gap: Mapping[str, object] | None,
    boundary_sample: Mapping[str, object] | None,
    bound_ms: int,
) -> None:
    """Atomically retire an old source freeze and optionally bind its successor."""

    bound_ms = _integer(bound_ms, "frozen candidate append cutoff")
    expected_transition = _candidate_transition_payload(
        retiring_candidate=retiring_candidate,
        replacement_candidate=replacement_candidate,
        break_gap=break_gap,
        boundary_sample=boundary_sample,
    )
    if expected_transition is None:
        raise LearningIntegrityError("Candidate source transition is empty.")
    retiring_details = (
        None
        if retiring_candidate is None
        else _candidate_binding_details(retiring_candidate)
    )
    replacement_details = (
        None
        if replacement_candidate is None
        else _candidate_binding_details(replacement_candidate)
    )
    path = Path(source_db_path)
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        pending_transition = _verified_pending_candidate_transition(db)
        source_policy, source_model_version = _source_registration_identity(db)
        rows = db.execute(
            """SELECT policy, model_version, identity_prefix,
                      freeze_cutoff_candle_ms,
                       frozen_candidate_json, frozen_candidate_sha256
               FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (source_policy, source_model_version),
        ).fetchall()
        if len(rows) != 1:
            raise LearningIntegrityError(
                "Candidate transition source identity is not unique."
            )
        if pending_transition is None:
            target_hash = (
                None if replacement_details is None else replacement_details[1]
            )
            if any(row["frozen_candidate_sha256"] != target_hash for row in rows):
                raise LearningIntegrityError(
                    "Candidate source transition is missing its pending intent."
                )
        elif pending_transition != expected_transition:
            raise LearningIntegrityError(
                "Candidate source transition does not match its pending intent."
            )
        if retiring_details is not None:
            _retiring_json, retiring_hash, _retiring_cutoff = retiring_details
            for row in rows:
                current_hash = row["frozen_candidate_sha256"]
                if current_hash is None:
                    continue
                if str(current_hash) != retiring_hash:
                    if (
                        replacement_details is not None
                        and str(current_hash) == replacement_details[1]
                    ):
                        continue
                    raise LearningIntegrityError(
                        "Source registration changed during candidate retirement."
                    )
                if break_gap is None and boundary_sample is None:
                    raise LearningIntegrityError(
                        "Candidate retirement is missing boundary evidence."
                    )
                if break_gap is not None:
                    retired = _retire_registration_for_gap(
                        db,
                        registration=row,
                        gap_id=str(break_gap["source_gap_id"]),
                        gap_digest=str(break_gap["source_record_sha256"]),
                        current_ms=int(break_gap["current_candle_close_ms"]),
                        gap_reason=str(break_gap["reason"]),
                        now_ms=bound_ms,
                    )
                else:
                    retired = _retire_registration_for_boundary(
                        db,
                        registration=row,
                        boundary_sample=boundary_sample,
                        now_ms=bound_ms,
                    )
                if not retired:
                    raise LearningIntegrityError(
                        "Candidate source retirement did not update its registration."
                    )
            rows = db.execute(
                """SELECT policy, model_version, identity_prefix,
                          freeze_cutoff_candle_ms, frozen_candidate_json,
                          frozen_candidate_sha256
                   FROM worker_learning_registrations
                   WHERE policy=? AND model_version=?""",
                (source_policy, source_model_version),
            ).fetchall()
            if len(rows) != 1:
                raise LearningIntegrityError(
                    "Candidate transition source identity changed during retirement."
                )
        if replacement_details is None:
            db.execute(
                "DELETE FROM worker_learning_pending_candidate_transition WHERE singleton=1"
            )
            db.execute("COMMIT")
            return
        candidate_json, candidate_hash, cutoff = replacement_details
        for row in rows:
            if row["frozen_candidate_sha256"] is None:
                changed = db.execute(
                    """UPDATE worker_learning_registrations SET
                       freeze_cutoff_candle_ms=?, frozen_candidate_json=?,
                       frozen_candidate_sha256=?,
                       true_forward_decision_created_cutoff_ms=?
                       WHERE policy=? AND model_version=?
                         AND frozen_candidate_sha256 IS NULL""",
                    (
                        cutoff,
                        candidate_json,
                        candidate_hash,
                        bound_ms,
                        row["policy"],
                        row["model_version"],
                    ),
                ).rowcount
                if changed != 1:
                    raise LearningIntegrityError(
                        "Candidate source binding did not update its registration."
                    )
            elif (
                row["frozen_candidate_sha256"] != candidate_hash
                or row["frozen_candidate_json"] != candidate_json
                or int(row["freeze_cutoff_candle_ms"]) != cutoff
            ):
                raise LearningIntegrityError(
                    "Source registration is bound to another frozen candidate."
                )
        db.execute(
            "DELETE FROM worker_learning_pending_candidate_transition WHERE singleton=1"
        )
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def _bind_candidate_to_source(
    source_db_path: Path | str,
    candidate: Mapping[str, object],
    bound_ms: int,
) -> None:
    """Idempotently bind one active candidate to future source labels."""

    transition = _candidate_transition_payload(
        retiring_candidate=None,
        replacement_candidate=candidate,
        break_gap=None,
        boundary_sample=None,
    )
    _stage_source_candidate_transition(source_db_path, transition, bound_ms)
    _transition_candidate_on_source(
        source_db_path,
        retiring_candidate=None,
        replacement_candidate=candidate,
        break_gap=None,
        boundary_sample=None,
        bound_ms=bound_ms,
    )


def _store_candidate_retirement(
    db: sqlite3.Connection,
    *,
    candidate: Mapping[str, object],
    reason: str,
    break_gap: Mapping[str, object] | None,
    boundary_sample: Mapping[str, object] | None,
    previous_segment_id: str,
    next_segment_id: str,
    now_ms: int,
) -> str:
    candidate_json, candidate_hash, cutoff = _candidate_binding_details(candidate)
    reason = _text(reason, "candidate retirement reason", maximum=160)
    record = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_online_candidate_retirement",
        "frozen_candidate_sha256": candidate_hash,
        "frozen_candidate": dict(candidate),
        "freeze_cutoff_ts": cutoff,
        "reason": reason,
        "break_gap_id": (
            None if break_gap is None else str(break_gap["source_gap_id"])
        ),
        "break_gap_sha256": (
            None
            if break_gap is None
            else str(break_gap["source_record_sha256"])
        ),
        "boundary_sample_sha256": (
            None
            if boundary_sample is None
            else str(boundary_sample["immutable_sha256"])
        ),
        "boundary_decision_ts": (
            None
            if boundary_sample is None
            else int(boundary_sample["decision_ts"])
        ),
        "previous_segment_id": previous_segment_id,
        "next_segment_id": next_segment_id,
    }
    digest = _sha256(record)
    retirement_id = f"retirement:{candidate_hash[:32]}:{digest[:24]}"
    db.execute(
        """INSERT OR IGNORE INTO online_candidate_retirements
           (retirement_id, frozen_candidate_sha256, frozen_candidate_json,
            freeze_cutoff_ts, reason, break_gap_id, break_gap_sha256,
            boundary_sample_sha256, boundary_decision_ts,
            previous_segment_id, next_segment_id, record_json,
            record_sha256, created_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            retirement_id,
            candidate_hash,
            candidate_json,
            cutoff,
            reason,
            record["break_gap_id"],
            record["break_gap_sha256"],
            record["boundary_sample_sha256"],
            record["boundary_decision_ts"],
            previous_segment_id,
            next_segment_id,
            _canonical_json(record),
            digest,
            now_ms,
        ),
    )
    return retirement_id


def _safety_violations(db: sqlite3.Connection) -> tuple[str, ...]:
    violations: list[str] = []
    if _verified_ingest_conflicts(db):
        violations.append("online_ingest_conflict_present")
    if _verified_online_source_errors(db):
        violations.append("source_learning_error_present")
    return tuple(violations)


def _unresolved_learning_outbox_count(db: sqlite3.Connection) -> int:
    """Return pending core-ledger learning events; legacy ledgers have none."""

    table = db.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name='worker_learning_outbox'"""
    ).fetchone()
    if table is None:
        return 0
    return int(
        db.execute(
            """SELECT COUNT(*) FROM worker_learning_outbox
               WHERE resolved_ms IS NULL"""
        ).fetchone()[0]
    )


def _store_run(
    db: sqlite3.Connection,
    *,
    report: Mapping[str, object],
    run_kind: str,
    active_segment_id: str,
    sample_hashes: Sequence[str],
    round_trip_hashes: Sequence[str],
    round_trip_digest: str,
    now_ms: int,
) -> str:
    artifact = {
        "schema": SCHEMA_VERSION,
        "kind": "testnet_online_learning_frozen_artifact",
        "run_kind": run_kind,
        "learner_version": learner.LEARNER_VERSION,
        "active_segment_id": active_segment_id,
        "eligible_sample_hashes": list(sample_hashes),
        "exact_closed_round_trip_hashes": list(round_trip_hashes),
        "exact_round_trip_evidence_sha256": round_trip_digest,
        "report": dict(report),
        **_SAFE_FLAGS,
    }
    artifact_digest = _sha256(artifact)
    run_basis = {
        "artifact_sha256": artifact_digest,
        "run_kind": run_kind,
        "learner_version": learner.LEARNER_VERSION,
    }
    run_id = _sha256(run_basis)
    db.execute(
        """INSERT OR IGNORE INTO online_learning_runs
           (run_id, run_kind, learner_version, active_segment_id,
            dataset_version,
            eligible_sample_count, exact_round_trip_count,
            exact_round_trip_evidence_sha256, report_version,
            proposal_ready_for_review, artifact_json, artifact_sha256,
           created_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            run_kind,
            learner.LEARNER_VERSION,
            active_segment_id,
            report["dataset_version"],
            len(sample_hashes),
            len(round_trip_hashes),
            round_trip_digest,
            report["report_version"],
            int(bool(report["proposal_ready_for_review"])),
            _canonical_json(artifact),
            artifact_digest,
            now_ms,
        ),
    )
    return run_id


def _state_payload(db: sqlite3.Connection) -> dict[str, object]:
    row = db.execute("SELECT * FROM online_state WHERE singleton=1").fetchone()
    if row is None:
        raise LearningStoreError("Online learning state is missing.")
    return {key: row[key] for key in row.keys() if key not in {"singleton", "updated_ms"}}


def _freeze_state(db: sqlite3.Connection, now_ms: int) -> None:
    payload = _state_payload(db)
    digest = _sha256(payload)
    db.execute(
        """INSERT OR IGNORE INTO online_state_events
           (state_version, state_json, state_sha256, created_ms)
           VALUES (?, ?, ?, ?)""",
        (digest, _canonical_json(payload), digest, now_ms),
    )


def _acknowledge_candidate_transition(
    db: sqlite3.Connection,
    transition: Mapping[str, object],
    now_ms: int,
) -> None:
    """Seal phase two only after the source registration transaction commits."""

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM online_state WHERE singleton=1").fetchone()
        if row is None:
            raise LearningStoreError("Online learning state is missing.")
        state = {key: row[key] for key in row.keys()}
        pending = _aggregate_pending_candidate_transition(state)
        if pending is None:
            if state.get("frozen_candidate_sha256") != transition.get(
                "replacement_candidate_sha256"
            ):
                raise LearningIntegrityError(
                    "Aggregate candidate transition was acknowledged to another target."
                )
            db.execute("COMMIT")
            return
        if pending != dict(transition):
            raise LearningIntegrityError(
                "Aggregate candidate transition changed before acknowledgement."
            )
        db.execute(
            """UPDATE online_state SET
               pending_candidate_transition_json=NULL,
               pending_candidate_transition_sha256=NULL,
               updated_ms=? WHERE singleton=1""",
            (_integer(now_ms, "transition acknowledgement time"),),
        )
        _freeze_state(db, now_ms)
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def refresh(
    source_db_path: Path | str,
    learning_db_path: Path | str = LEARNING_DB_PATH,
    now_ms: int | None = None,
) -> dict[str, object]:
    """Ingest facts and advance one gap-bounded proposal-only lifecycle."""

    observed_ms = _now_ms() if now_ms is None else _integer(now_ms, "now_ms")
    aggregate = _connect_learning(learning_db_path)
    source: sqlite3.Connection | None = None
    retiring_candidate: dict[str, object] | None = None
    retirement_gap: Mapping[str, object] | None = None
    retirement_boundary_sample: Mapping[str, object] | None = None
    source_retiring_candidate: dict[str, object] | None = None
    source_retirement_gap: Mapping[str, object] | None = None
    source_retirement_boundary_sample: Mapping[str, object] | None = None
    candidate_to_bind: dict[str, object] | None = None
    pending_transition: dict[str, object] | None = None
    try:
        source = _source_read_connection(source_db_path)
        try:
            source.execute("BEGIN")
            aggregate.execute("BEGIN IMMEDIATE")
            try:
                initial_state = aggregate.execute(
                    "SELECT * FROM online_state WHERE singleton=1"
                ).fetchone()
                if initial_state is None:
                    raise LearningStoreError("Online learning state is missing.")
                frozen_candidate: dict[str, object] | None = None
                if initial_state["frozen_candidate_json"] is not None:
                    parsed = _parse_canonical_json(
                        initial_state["frozen_candidate_json"],
                        "frozen candidate",
                    )
                    if not isinstance(parsed, Mapping):
                        raise LearningIntegrityError(
                            "Frozen candidate is not an object."
                        )
                    frozen_candidate = dict(parsed)
                _ingest_source(source, aggregate, observed_ms)
                source_revision_row = source.execute(
                    """SELECT learning_revision FROM worker_learning_meta
                       WHERE singleton=1"""
                ).fetchone()
                if source_revision_row is None:
                    raise LearningStoreError(
                        "Worker learning source revision is missing."
                    )
                source_revision = _integer(
                    source_revision_row[0], "source learning revision"
                )
                segments = _verified_learning_segments(aggregate)
                gaps = _verified_daily_gaps(aggregate)
                prior = aggregate.execute(
                    "SELECT * FROM online_state WHERE singleton=1"
                ).fetchone()
                if prior is None:
                    raise LearningStoreError("Online learning state is missing.")
                pending_gap: Mapping[str, object] | None = None
                if frozen_candidate is None:
                    samples, pending_gap = _latest_development_segment(
                        segments, gaps
                    )
                    persisted_start = prior["active_segment_start_ts"]
                    latest_gap_boundary = (
                        None
                        if not gaps
                        else int(gaps[-1]["current_candle_close_ms"])
                    )
                    if (
                        persisted_start is not None
                        and (
                            latest_gap_boundary is None
                            or latest_gap_boundary <= int(persisted_start)
                        )
                    ):
                        lifecycle_samples = [
                            sample
                            for sample in samples
                            if int(sample["decision_ts"])
                            >= int(persisted_start)
                        ]
                        if (
                            lifecycle_samples
                            and int(lifecycle_samples[0]["decision_ts"])
                            == int(persisted_start)
                        ):
                            samples = lifecycle_samples
                            pending_gap = None
                    active_segment_id = _segment_id(samples, pending_gap)
                    active_segment_start_ts = (
                        int(samples[0]["decision_ts"])
                        if samples
                        else (
                            None
                            if pending_gap is None
                            else int(pending_gap["current_candle_close_ms"])
                        )
                    )
                else:
                    candidate_index = _candidate_segment_index(
                        segments, frozen_candidate
                    )
                    candidate_samples = [
                        dict(sample) for sample in segments[candidate_index]
                    ]
                    freeze_cutoff = _integer(
                        frozen_candidate["freeze_cutoff_ts"], "freeze cutoff"
                    )
                    post_freeze_gaps = [
                        gap
                        for gap in gaps
                        if int(gap["current_candle_close_ms"]) > freeze_cutoff
                    ]
                    development_count = int(
                        frozen_candidate["development_sample_count"]
                    )
                    forward_samples = candidate_samples[development_count:]
                    historical_seed, _historical_samples = (
                        _verified_historical_seed(aggregate)
                    )
                    historical_pre_oos_bridge = bool(
                        historical_seed is not None
                        and forward_samples
                        and freeze_cutoff
                        == int(historical_seed["last_label_available_ts"])
                        and _is_historical_pre_oos_bridge(
                            historical_seed, forward_samples[0]
                        )
                    )
                    bridge_is_exact = bool(
                        forward_samples
                        and int(forward_samples[0]["decision_ts"])
                        == freeze_cutoff
                        and int(forward_samples[0]["label_available_ts"])
                        == freeze_cutoff + DAY_MS
                        and (
                            bool(forward_samples[0]["out_of_sample"])
                            or historical_pre_oos_bridge
                        )
                        and not bool(
                            forward_samples[0]["true_forward_after_freeze"]
                        )
                        and "freeze_id" not in forward_samples[0]
                    )
                    wrong_freeze = [
                        sample
                        for sample in forward_samples
                        if bool(sample["true_forward_after_freeze"])
                        and sample.get("freeze_id")
                        != frozen_candidate["immutable_sha256"]
                    ]
                    if wrong_freeze:
                        raise LearningIntegrityError(
                            "Forward sample references another frozen candidate."
                        )
                    first_unbound_index = next(
                        (
                            index
                            for index, sample in enumerate(forward_samples)
                            if not (bridge_is_exact and index == 0)
                            if not bool(sample["true_forward_after_freeze"])
                        ),
                        None,
                    )
                    if post_freeze_gaps:
                        retiring_candidate = dict(frozen_candidate)
                        retirement_gap = post_freeze_gaps[0]
                        samples, pending_gap = _latest_development_segment(
                            segments, gaps
                        )
                        previous_segment_id = _segment_id(candidate_samples)
                        active_segment_id = _segment_id(samples, pending_gap)
                        active_segment_start_ts = (
                            int(samples[0]["decision_ts"])
                            if samples
                            else int(pending_gap["current_candle_close_ms"])
                        )
                        _store_candidate_retirement(
                            aggregate,
                            candidate=retiring_candidate,
                            reason="post_freeze_daily_gap",
                            break_gap=retirement_gap,
                            boundary_sample=None,
                            previous_segment_id=previous_segment_id,
                            next_segment_id=active_segment_id,
                            now_ms=observed_ms,
                        )
                        frozen_candidate = None
                    elif first_unbound_index is not None:
                        unbound_suffix = forward_samples[first_unbound_index:]
                        if any(
                            bool(sample["true_forward_after_freeze"])
                            for sample in unbound_suffix
                        ):
                            raise LearningIntegrityError(
                                "Frozen and unbound forward evidence is interleaved."
                            )
                        retiring_candidate = dict(frozen_candidate)
                        boundary_sample = unbound_suffix[0]
                        retirement_boundary_sample = boundary_sample
                        samples = [dict(sample) for sample in unbound_suffix]
                        previous_segment_id = _segment_id(candidate_samples)
                        active_segment_id = _segment_id(samples)
                        active_segment_start_ts = int(
                            boundary_sample["decision_ts"]
                        )
                        _store_candidate_retirement(
                            aggregate,
                            candidate=retiring_candidate,
                            reason="post_freeze_source_binding_missing",
                            break_gap=None,
                            boundary_sample=boundary_sample,
                            previous_segment_id=previous_segment_id,
                            next_segment_id=active_segment_id,
                            now_ms=observed_ms,
                        )
                        frozen_candidate = None
                    else:
                        if candidate_index != len(segments) - 1:
                            raise LearningIntegrityError(
                                "A later sample segment lacks daily-gap evidence."
                            )
                        samples = candidate_samples
                        active_segment_id = _segment_id(samples)
                        active_segment_start_ts = int(
                            samples[0]["decision_ts"]
                        )

                all_round_trip_records = (
                    _verified_online_round_trip_records(aggregate)
                )
                exact_records = [
                    item
                    for item in all_round_trip_records
                    if item[0].get("status") == "closed"
                    and item[0].get("exact_pnl") is True
                ]
                latest_label_ts = (
                    int(samples[-1]["label_available_ts"]) if samples else None
                )
                matched_trips = _candidate_round_trips(
                    exact_records, frozen_candidate, latest_label_ts, samples
                )
                round_trip_hashes = [
                    str(item["immutable_sha256"]) for item in matched_trips
                ]
                round_trip_digest = _sha256(round_trip_hashes)
                sample_hashes = [
                    str(sample["immutable_sha256"]) for sample in samples
                ]
                verified_samples = _verified_online_samples(aggregate)
                finalized_count = len(verified_samples)
                active_oos_count = sum(
                    bool(sample["out_of_sample"]) for sample in samples
                )
                total_true_forward_count = sum(
                    bool(sample["out_of_sample"])
                    and bool(sample["true_forward_after_freeze"])
                    for sample in verified_samples
                )
                active_true_forward_count = sum(
                    bool(sample["true_forward_after_freeze"])
                    for sample in samples
                )
                quarantined_count = sum(
                    record.get("status") == "quarantined"
                    for record, _digest in all_round_trip_records
                )
                terminal_keys = {
                    (
                        record["policy"],
                        record["model_version"],
                        record["entry_client_id"],
                    )
                    for record, _digest in all_round_trip_records
                    if record.get("status") in {"closed", "quarantined"}
                }
                open_count = sum(
                    record.get("status") == "open"
                    and (
                        record["policy"],
                        record["model_version"],
                        record["entry_client_id"],
                    )
                    not in terminal_keys
                    for record, _digest in all_round_trip_records
                )
                conflict_count = len(_verified_ingest_conflicts(aggregate))
                daily_gap_count = len(gaps)
                source_error_count = len(
                    _verified_online_source_errors(aggregate)
                )
                retired_candidate_count = int(
                    aggregate.execute(
                        "SELECT COUNT(*) FROM online_candidate_retirements"
                    ).fetchone()[0]
                )
                unresolved_outbox_count = _unresolved_learning_outbox_count(
                    source
                )
                safety_violations = _safety_violations(aggregate)
                evaluation_safety_violations = safety_violations + (
                    ("unresolved_learning_outbox",)
                    if unresolved_outbox_count
                    else ()
                )
                safety_digest = _sha256(list(safety_violations))
                safety_changed = (
                    int(prior["safety_violation_count"])
                    != len(safety_violations)
                    or str(prior["safety_violation_digest"]) != safety_digest
                )
                safety_requires_block = bool(safety_violations) and (
                    bool(prior["proposal_ready_for_review"])
                    or str(prior["latest_status"])
                    != "blocked_by_safety_violation"
                )
                outbox_changed = (
                    int(prior["unresolved_learning_outbox_count"])
                    != unresolved_outbox_count
                )
                outbox_requires_block = bool(unresolved_outbox_count) and (
                    bool(prior["proposal_ready_for_review"])
                    or str(prior["latest_status"])
                    != "blocked_by_safety_violation"
                )
                source_revision_changed = (
                    int(prior["ingested_source_revision"])
                    != source_revision
                )
                segment_changed = (
                    prior["active_segment_id"] is None
                    or str(prior["active_segment_id"]) != active_segment_id
                )
                last_trained = int(prior["last_trained_sample_count"])
                last_training_ms = prior["last_training_ms"]
                if segment_changed and frozen_candidate is None:
                    last_trained = 0
                    last_training_ms = None

                training_due = (
                    frozen_candidate is None
                    and len(samples) >= MIN_TRAINING_SAMPLES
                    and (
                        last_trained < MIN_TRAINING_SAMPLES
                        or len(samples) >= last_trained + RETRAIN_STRIDE
                    )
                )
                should_train = training_due and not unresolved_outbox_count
                should_progress = (
                    frozen_candidate is None
                    and not should_train
                    and (
                        segment_changed
                        or safety_changed
                        or safety_requires_block
                        or outbox_changed
                        or outbox_requires_block
                        or source_revision_changed
                        or (training_due and bool(unresolved_outbox_count))
                        or prior["latest_run_id"] is None
                        or (
                            len(samples) < MIN_TRAINING_SAMPLES
                            and int(prior["eligible_oos_daily_label_count"])
                            != active_oos_count
                        )
                    )
                )
                should_evaluate = frozen_candidate is not None and (
                    safety_changed
                    or safety_requires_block
                    or outbox_changed
                    or outbox_requires_block
                    or source_revision_changed
                    or prior["latest_run_id"] is None
                    or int(prior["eligible_oos_daily_label_count"])
                    != active_oos_count
                    or int(prior["candidate_matched_round_trip_count"])
                    != len(matched_trips)
                    or str(prior["exact_round_trip_evidence_sha256"])
                    != round_trip_digest
                )

                latest_run_id = prior["latest_run_id"]
                latest_report_version = prior["latest_report_version"]
                latest_status = str(prior["latest_status"])
                proposal_ready = bool(prior["proposal_ready_for_review"])
                frozen_json = prior["frozen_candidate_json"]
                frozen_hash = prior["frozen_candidate_sha256"]
                freeze_cutoff = prior["freeze_cutoff_ts"]
                if retiring_candidate is not None:
                    frozen_json = None
                    frozen_hash = None
                    freeze_cutoff = None
                    proposal_ready = False
                if evaluation_safety_violations:
                    proposal_ready = False

                if should_train or should_progress or should_evaluate:
                    if frozen_candidate is None:
                        report = learner.learn(
                            samples,
                            incumbent_threshold="0.10",
                            safety_violations=evaluation_safety_violations,
                        )
                        run_kind = "training" if should_train else "progress"
                    else:
                        report = learner.learn(
                            samples,
                            incumbent_threshold="0.10",
                            frozen_candidate=frozen_candidate,
                            actual_round_trips=matched_trips,
                            safety_violations=evaluation_safety_violations,
                            verified_pre_registration_embargo=(
                                historical_pre_oos_bridge
                            ),
                        )
                        run_kind = "evaluation"
                    latest_run_id = _store_run(
                        aggregate,
                        report=report,
                        run_kind=run_kind,
                        active_segment_id=active_segment_id,
                        sample_hashes=sample_hashes,
                        round_trip_hashes=round_trip_hashes,
                        round_trip_digest=round_trip_digest,
                        now_ms=observed_ms,
                    )
                    latest_report_version = report["report_version"]
                    latest_status = str(report["status"])
                    proposal_ready = bool(
                        report["proposal_ready_for_review"]
                    ) and not evaluation_safety_violations
                    if should_train:
                        last_trained = len(samples)
                        last_training_ms = observed_ms
                        candidate = report.get("frozen_candidate")
                        if isinstance(candidate, Mapping):
                            frozen_candidate = dict(candidate)
                            frozen_json = _canonical_json(frozen_candidate)
                            frozen_hash = str(
                                frozen_candidate["immutable_sha256"]
                            )
                            freeze_cutoff = int(
                                frozen_candidate["freeze_cutoff_ts"]
                            )

                next_training = (
                    MIN_TRAINING_SAMPLES
                    if last_trained < MIN_TRAINING_SAMPLES
                    else last_trained + RETRAIN_STRIDE
                )
                aggregate.execute(
                    """UPDATE online_state SET
                       finalized_daily_label_count=?,
                       eligible_oos_daily_label_count=?,
                       true_forward_daily_label_count=?,
                       total_true_forward_daily_label_count=?,
                       exact_closed_round_trip_count=?,
                       candidate_matched_round_trip_count=?,
                       quarantined_round_trip_count=?, open_round_trip_count=?,
                       ingest_conflict_count=?, daily_gap_count=?,
                       source_error_count=?, unresolved_learning_outbox_count=?,
                       ingested_source_revision=?,
                       safety_violation_count=?,
                       safety_violation_digest=?, active_segment_id=?,
                       active_segment_start_ts=?, retired_candidate_count=?,
                       last_trained_sample_count=?,
                       next_training_sample_count=?, last_training_ms=?,
                       latest_run_id=?, latest_report_version=?, latest_status=?,
                       frozen_candidate_json=?, frozen_candidate_sha256=?,
                       freeze_cutoff_ts=?, proposal_ready_for_review=?,
                       exact_round_trip_evidence_sha256=?, last_error=NULL,
                       updated_ms=? WHERE singleton=1""",
                    (
                        finalized_count,
                        active_oos_count,
                        active_true_forward_count,
                        total_true_forward_count,
                        len(exact_records),
                        len(matched_trips),
                        quarantined_count,
                        open_count,
                        conflict_count,
                        daily_gap_count,
                        source_error_count,
                        unresolved_outbox_count,
                        source_revision,
                        len(safety_violations),
                        safety_digest,
                        active_segment_id,
                        active_segment_start_ts,
                        retired_candidate_count,
                        last_trained,
                        next_training,
                        last_training_ms,
                        latest_run_id,
                        latest_report_version,
                        latest_status,
                        frozen_json,
                        frozen_hash,
                        freeze_cutoff,
                        int(proposal_ready),
                        round_trip_digest,
                        observed_ms,
                    ),
                )
                candidate_to_bind = (
                    None
                    if frozen_candidate is None
                    else dict(frozen_candidate)
                )
                source_registration_hashes = [
                    None if row[0] is None else str(row[0])
                    for row in source.execute(
                        """SELECT frozen_candidate_sha256
                           FROM worker_learning_registrations
                           ORDER BY policy, model_version"""
                    )
                ]
                source_hashes = {
                    value
                    for value in source_registration_hashes
                    if value is not None
                }
                target_hash = (
                    None
                    if candidate_to_bind is None
                    else str(candidate_to_bind["immutable_sha256"])
                )
                unexpected_hashes = {
                    value for value in source_hashes if value != target_hash
                }
                if len(unexpected_hashes) > 1:
                    raise LearningIntegrityError(
                        "Source registrations reference multiple retired candidates."
                    )
                if unexpected_hashes:
                    unexpected_hash = next(iter(unexpected_hashes))
                    if (
                        retiring_candidate is not None
                        and str(retiring_candidate["immutable_sha256"])
                        == unexpected_hash
                    ):
                        source_retiring_candidate = dict(retiring_candidate)
                        source_retirement_gap = retirement_gap
                        source_retirement_boundary_sample = (
                            retirement_boundary_sample
                        )
                    else:
                        retirement = aggregate.execute(
                            """SELECT frozen_candidate_json, break_gap_id,
                                      break_gap_sha256,
                                      boundary_sample_sha256,
                                      boundary_decision_ts
                               FROM online_candidate_retirements
                               WHERE frozen_candidate_sha256=?""",
                            (unexpected_hash,),
                        ).fetchone()
                        if retirement is None:
                            raise LearningIntegrityError(
                                "Source candidate has no aggregate retirement evidence."
                            )
                        parsed_retirement = _parse_canonical_json(
                            retirement["frozen_candidate_json"],
                            "retired source candidate",
                        )
                        if not isinstance(parsed_retirement, Mapping):
                            raise LearningIntegrityError(
                                "Retired source candidate is not an object."
                        )
                        source_retiring_candidate = dict(parsed_retirement)
                        if retirement["break_gap_id"] is not None:
                            source_retirement_gap = next(
                                (
                                    gap
                                    for gap in gaps
                                    if str(gap["source_gap_id"])
                                    == str(retirement["break_gap_id"])
                                    and str(gap["source_record_sha256"])
                                    == str(retirement["break_gap_sha256"])
                                ),
                                None,
                            )
                            if source_retirement_gap is None:
                                raise LearningIntegrityError(
                                    "Retired source candidate has no matching gap evidence."
                                )
                        else:
                            source_retirement_boundary_sample = next(
                                (
                                    sample
                                    for segment in segments
                                    for sample in segment
                                    if str(sample["immutable_sha256"])
                                    == str(retirement["boundary_sample_sha256"])
                                    and int(sample["decision_ts"])
                                    == int(retirement["boundary_decision_ts"])
                                ),
                                None,
                            )
                            if source_retirement_boundary_sample is None:
                                raise LearningIntegrityError(
                                    "Retired source candidate has no matching boundary sample."
                                )
                transition_required = bool(
                    source_retiring_candidate is not None
                    or (
                        candidate_to_bind is not None
                        and any(
                            value != target_hash
                            for value in source_registration_hashes
                        )
                    )
                )
                pending_transition = (
                    _candidate_transition_payload(
                        retiring_candidate=source_retiring_candidate,
                        replacement_candidate=candidate_to_bind,
                        break_gap=source_retirement_gap,
                        boundary_sample=source_retirement_boundary_sample,
                    )
                    if transition_required
                    else None
                )
                aggregate.execute(
                    """UPDATE online_state SET
                       pending_candidate_transition_json=?,
                       pending_candidate_transition_sha256=?
                       WHERE singleton=1""",
                    (
                        None
                        if pending_transition is None
                        else _canonical_json(pending_transition),
                        None
                        if pending_transition is None
                        else _sha256(pending_transition),
                    ),
                )
                _freeze_state(aggregate, observed_ms)
                # The source-side blocker must be durable before the aggregate
                # phase can expose a new candidate.  Closing the coherent source
                # read snapshot first also avoids a reader/writer lock upgrade.
                if source.in_transaction:
                    source.execute("ROLLBACK")
                source.close()
                source = None
                _stage_source_candidate_transition(
                    source_db_path, pending_transition, observed_ms
                )
                aggregate.execute("COMMIT")
            except BaseException:
                if aggregate.in_transaction:
                    aggregate.execute("ROLLBACK")
                raise
        finally:
            if source is not None:
                if source.in_transaction:
                    source.execute("ROLLBACK")
                source.close()
                source = None

        if pending_transition is not None:
            _transition_candidate_on_source(
                source_db_path,
                retiring_candidate=source_retiring_candidate,
                replacement_candidate=candidate_to_bind,
                break_gap=source_retirement_gap,
                boundary_sample=source_retirement_boundary_sample,
                bound_ms=observed_ms,
            )
            _acknowledge_candidate_transition(
                aggregate, pending_transition, observed_ms
            )
    finally:
        if source is not None:
            source.close()
        aggregate.close()
    return status_snapshot(source_db_path, learning_db_path)


def _verified_learner_migration(
    db: sqlite3.Connection,
) -> dict[str, object] | None:
    """Verify the optional v4→v5 seed migration and its state binding."""

    state = db.execute(
        "SELECT * FROM online_state WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise LearningStoreError("Online learning state is missing.")
    state_keys = set(state.keys())
    state_digest = (
        state["learner_migration_sha256"]
        if "learner_migration_sha256" in state_keys
        else None
    )
    table_exists = db.execute(
        """SELECT 1 FROM sqlite_master WHERE type='table'
           AND name='online_learner_migrations'"""
    ).fetchone() is not None
    rows = (
        list(db.execute("SELECT * FROM online_learner_migrations"))
        if table_exists
        else []
    )
    if state_digest is None:
        if rows:
            raise LearningIntegrityError(
                "Learner migration records are not bound to online state."
            )
        return None
    if not isinstance(state_digest, str) or len(rows) != 1:
        raise LearningIntegrityError(
            "Online state learner migration provenance is incomplete."
        )
    row = rows[0]
    record = _parse_canonical_json(
        row["record_json"], "learner migration record"
    )
    if not isinstance(record, Mapping):
        raise LearningIntegrityError("Learner migration record is not an object.")
    expected_keys = {
        "schema",
        "kind",
        "from_learner_version",
        "to_learner_version",
        "source_ledger_id",
        "source_learning_revision",
        "prior_state_version",
        "prior_latest_run_id",
        "historical_manifest_sha256",
        "open_pre_candidate_round_trips_preserved",
        "created_ms",
    }
    record_digest = _sha256(record)
    expected_id = f"learner-migration:{record_digest[:48]}"
    source_id, _policy, _model_version = _aggregate_source_identity(db)
    if (
        set(record) != expected_keys
        or record.get("schema") != SCHEMA_VERSION
        or record.get("kind")
        != "historical_seed_learner_version_migration"
        or record.get("from_learner_version")
        not in _SEED_MIGRATABLE_LEARNER_VERSIONS
        or record.get("to_learner_version") != learner.LEARNER_VERSION
        or record.get("source_ledger_id") != source_id
        or int(record.get("source_learning_revision", -1)) < 0
        or int(record.get("open_pre_candidate_round_trips_preserved", -1)) < 0
        or int(record.get("created_ms", -1)) < 0
        or row["migration_id"] != expected_id
        or row["from_learner_version"] != record["from_learner_version"]
        or row["to_learner_version"] != record["to_learner_version"]
        or row["source_ledger_id"] != source_id
        or int(row["source_learning_revision"])
        != int(record["source_learning_revision"])
        or row["prior_state_version"] != record["prior_state_version"]
        or row["prior_latest_run_id"] != record["prior_latest_run_id"]
        or int(row["created_ms"]) != int(record["created_ms"])
        or not hmac.compare_digest(str(row["record_sha256"]), record_digest)
        or not hmac.compare_digest(state_digest, record_digest)
    ):
        raise LearningIntegrityError(
            "Learner migration record seal does not verify."
        )

    prior_event = db.execute(
        """SELECT state_json, state_sha256 FROM online_state_events
           WHERE state_version=?""",
        (record["prior_state_version"],),
    ).fetchone()
    if prior_event is None:
        raise LearningIntegrityError(
            "Learner migration prior state event is missing."
        )
    prior_state = _parse_canonical_json(
        prior_event["state_json"], "learner migration prior state"
    )
    if (
        not isinstance(prior_state, Mapping)
        or _sha256(prior_state) != record["prior_state_version"]
        or not hmac.compare_digest(
            str(prior_event["state_sha256"]), str(record["prior_state_version"])
        )
        or prior_state.get("learner_version")
        != record["from_learner_version"]
        or prior_state.get("latest_run_id") != record["prior_latest_run_id"]
    ):
        raise LearningIntegrityError(
            "Learner migration prior state event does not verify."
        )
    prior_run_id = record["prior_latest_run_id"]
    if prior_run_id is not None:
        prior_run = db.execute(
            "SELECT learner_version FROM online_learning_runs WHERE run_id=?",
            (prior_run_id,),
        ).fetchone()
        if (
            prior_run is None
            or prior_run["learner_version"] != record["from_learner_version"]
        ):
            raise LearningIntegrityError(
                "Learner migration prior run provenance does not verify."
            )

    seed_table = db.execute(
        """SELECT 1 FROM sqlite_master WHERE type='table'
           AND name='historical_development_seeds'"""
    ).fetchone()
    if seed_table is not None:
        seed_rows = list(db.execute(
            "SELECT manifest_sha256 FROM historical_development_seeds"
        ))
        if len(seed_rows) > 1 or (
            seed_rows
            and seed_rows[0]["manifest_sha256"]
            != record["historical_manifest_sha256"]
        ):
            raise LearningIntegrityError(
                "Historical seed does not match its learner migration."
            )
    return dict(record)


def _migrate_learner_version_for_historical_seed(
    source_db_path: Path | str,
    learning_db_path: Path | str,
    manifest: Mapping[str, object],
    now_ms: int,
) -> bool:
    """Version one empty pre-seed lifecycle without rewriting old artifacts.

    Revision 5 only widens the already documented embargo contract for one
    store-verified historical boundary.  Migration is consequently restricted
    to the prior revision's first lifecycle before any label, gap, candidate,
    closed trip, error, retirement, or training evidence exists.  An open,
    pre-candidate execution trip may remain: it is immutable operational state
    and cannot satisfy a candidate review gate.
    """

    learning_path = Path(learning_db_path)
    if not learning_path.is_file():
        return False
    aggregate = sqlite3.connect(learning_path, timeout=15, isolation_level=None)
    source: sqlite3.Connection | None = None
    try:
        aggregate.row_factory = sqlite3.Row
        aggregate.execute("PRAGMA busy_timeout=15000")
        aggregate.execute("PRAGMA foreign_keys=ON")
        source = _source_maintenance_connection(source_db_path)
        aggregate.execute("BEGIN IMMEDIATE")
        source.execute("BEGIN IMMEDIATE")
        try:
            state_row = aggregate.execute(
                "SELECT * FROM online_state WHERE singleton=1"
            ).fetchone()
            if state_row is None:
                raise LearningStoreError("Online learning state is missing.")
            state = {key: state_row[key] for key in state_row.keys()}
            persisted_version = str(state["learner_version"])
            if persisted_version == learner.LEARNER_VERSION:
                migration = _verified_learner_migration(aggregate)
                if (
                    migration is not None
                    and migration["historical_manifest_sha256"]
                    != manifest["immutable_sha256"]
                ):
                    raise LearningIntegrityError(
                        "Historical seed manifest does not match the completed "
                        "learner migration."
                    )
                aggregate.execute("ROLLBACK")
                source.execute("ROLLBACK")
                return False
            if persisted_version not in _SEED_MIGRATABLE_LEARNER_VERSIONS:
                raise LearningStoreError(
                    "Online learning database version cannot be migrated by "
                    "the historical seed workflow."
                )
            if int(state["schema_version"]) != SCHEMA_VERSION:
                raise LearningIntegrityError(
                    "Historical seed learner migration found a schema mismatch."
                )

            meta = source.execute(
                """SELECT source_ledger_id, schema_version, learning_revision
                   FROM worker_learning_meta WHERE singleton=1"""
            ).fetchone()
            worker_state = source.execute(
                """SELECT desired_running, pending_client_id
                   FROM worker_state WHERE singleton=1"""
            ).fetchone()
            if (
                meta is None
                or int(meta["schema_version"]) != SCHEMA_VERSION
                or worker_state is None
                or bool(worker_state["desired_running"])
                or worker_state["pending_client_id"] is not None
            ):
                raise LearningStoreError(
                    "Historical seed learner migration requires a fully stopped, "
                    "coherent source ledger."
                )
            source_id = str(meta["source_ledger_id"])
            source_revision = int(meta["learning_revision"])
            policy, model_version = _source_registration_identity(source)
            aggregate_id, aggregate_policy, aggregate_model = (
                _aggregate_source_identity(aggregate)
            )
            if (
                not hmac.compare_digest(source_id, aggregate_id)
                or policy != aggregate_policy
                or model_version != aggregate_model
                or int(state["ingested_source_revision"]) != source_revision
            ):
                raise LearningIntegrityError(
                    "Historical seed learner migration source identity/revision "
                    "does not match the aggregate."
                )

            registration = _registration(source, policy, model_version)
            if (
                registration["frozen_candidate_sha256"] is not None
                or registration["frozen_candidate_json"] is not None
                or registration["freeze_cutoff_candle_ms"] is not None
                or _verified_pending_candidate_transition(source) is not None
            ):
                raise LearningStoreError(
                    "Historical seed learner migration refuses a candidate lifecycle."
                )

            source_zero_tables = (
                "worker_learning_daily_labels",
                "worker_learning_daily_gaps",
                "worker_learning_source_errors",
                "worker_learning_outbox",
                "worker_learning_candidate_retirements",
            )
            if any(
                int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in source_zero_tables
            ):
                raise LearningStoreError(
                    "Historical seed learner migration requires an unused label lifecycle."
                )

            aggregate_zero_tables = (
                "online_samples",
                "online_daily_gaps",
                "online_source_errors",
                "online_ingest_conflicts",
                "online_candidate_retirements",
            )
            for table in aggregate_zero_tables:
                if int(
                    aggregate.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                ):
                    raise LearningStoreError(
                        "Historical seed learner migration found aggregate evidence."
                    )
            historical_table = aggregate.execute(
                """SELECT 1 FROM sqlite_master WHERE type='table'
                   AND name='historical_development_seeds'"""
            ).fetchone()
            if historical_table is not None and int(
                aggregate.execute(
                    "SELECT COUNT(*) FROM historical_development_seeds"
                ).fetchone()[0]
            ):
                raise LearningStoreError(
                    "Historical seed learner migration cannot replace an existing seed."
                )

            forbidden_state_values = (
                int(state["finalized_daily_label_count"]),
                int(state["eligible_oos_daily_label_count"]),
                int(state["true_forward_daily_label_count"]),
                int(state["total_true_forward_daily_label_count"]),
                int(state["exact_closed_round_trip_count"]),
                int(state["candidate_matched_round_trip_count"]),
                int(state["quarantined_round_trip_count"]),
                int(state["ingest_conflict_count"]),
                int(state["daily_gap_count"]),
                int(state["source_error_count"]),
                int(state["unresolved_learning_outbox_count"]),
                int(state["safety_violation_count"]),
                int(state["retired_candidate_count"]),
                int(state["last_trained_sample_count"]),
                int(state["proposal_ready_for_review"]),
            )
            if (
                any(forbidden_state_values)
                or state["frozen_candidate_json"] is not None
                or state["frozen_candidate_sha256"] is not None
                or state["freeze_cutoff_ts"] is not None
                or state["pending_candidate_transition_json"] is not None
                or state["pending_candidate_transition_sha256"] is not None
            ):
                raise LearningStoreError(
                    "Historical seed learner migration found trained or review evidence."
                )

            source_trip_rows = list(source.execute(
                "SELECT * FROM worker_learning_round_trips"
            ))
            source_trip_records = [
                _verify_source_round_trip(row) for row in source_trip_rows
            ]
            aggregate_trip_records = _verified_online_round_trip_records(aggregate)
            if (
                len(source_trip_records) != len(aggregate_trip_records)
                or len(aggregate_trip_records) != int(state["open_round_trip_count"])
                or {
                    _sha256(record) for record in source_trip_records
                } != {digest for _record, digest in aggregate_trip_records}
                or any(
                    record.get("status") != "open"
                    or bool(record.get("exact_pnl"))
                    or bool(record.get("entry_candidate_bound"))
                    or record.get("entry_freeze_id") is not None
                    for record in source_trip_records
                )
            ):
                raise LearningStoreError(
                    "Historical seed learner migration found non-neutral trip evidence."
                )

            latest_decision = source.execute(
                """SELECT candle_close_ms, policy, close_latest
                   FROM worker_decisions ORDER BY candle_close_ms DESC LIMIT 1"""
            ).fetchone()
            if latest_decision is None:
                raise LearningStoreError(
                    "Historical seed learner migration requires a current decision."
                )
            if (
                str(latest_decision["policy"]) != policy
                or int(latest_decision["candle_close_ms"])
                != int(manifest["last_label_available_ts"])
                or _decimal_text(
                    latest_decision["close_latest"],
                    "latest worker decision close",
                    positive=True,
                )
                != _decimal_text(
                    manifest["last_close"], "historical seed last close", positive=True
                )
            ):
                raise LearningIntegrityError(
                    "Historical seed learner migration tail does not match the worker."
                )

            state_payload = {
                key: value
                for key, value in state.items()
                if key not in {"singleton", "updated_ms"}
            }
            # A v5 status probe may have added this nullable column before
            # failing closed on the v4 learner version.  It was not part of the
            # immutable v4 state payload, so exclude only its neutral NULL form
            # when verifying that prior event.
            if state_payload.get("learner_migration_sha256") is None:
                state_payload.pop("learner_migration_sha256", None)
            prior_state_version = _sha256(state_payload)
            prior_event = aggregate.execute(
                """SELECT state_json, state_sha256 FROM online_state_events
                   WHERE state_version=?""",
                (prior_state_version,),
            ).fetchone()
            if (
                prior_event is None
                or str(prior_event["state_json"]) != _canonical_json(state_payload)
                or not hmac.compare_digest(
                    str(prior_event["state_sha256"]), prior_state_version
                )
            ):
                raise LearningIntegrityError(
                    "Historical seed learner migration prior state does not verify."
                )

            prior_run_id = state["latest_run_id"]
            if (prior_run_id is None) != (state["latest_report_version"] is None):
                raise LearningIntegrityError(
                    "Historical seed learner migration run pointer is incomplete."
                )
            if prior_run_id is not None:
                run = aggregate.execute(
                    "SELECT * FROM online_learning_runs WHERE run_id=?",
                    (prior_run_id,),
                ).fetchone()
                if run is None:
                    raise LearningIntegrityError(
                        "Historical seed learner migration prior run is missing."
                    )
                artifact = _parse_canonical_json(
                    run["artifact_json"], "prior learning artifact"
                )
                if not isinstance(artifact, Mapping):
                    raise LearningIntegrityError(
                        "Historical seed learner migration prior artifact is invalid."
                    )
                artifact_digest = _sha256(artifact)
                report = artifact.get("report")
                report_content = dict(report) if isinstance(report, Mapping) else {}
                report_version = report_content.pop("report_version", None)
                if (
                    str(run["learner_version"]) != persisted_version
                    or artifact.get("learner_version") != persisted_version
                    or not hmac.compare_digest(
                        str(run["artifact_sha256"]), artifact_digest
                    )
                    or not hmac.compare_digest(
                        str(run["run_id"]),
                        _sha256({
                            "artifact_sha256": artifact_digest,
                            "run_kind": run["run_kind"],
                            "learner_version": run["learner_version"],
                        }),
                    )
                    or int(run["eligible_sample_count"]) != 0
                    or int(run["exact_round_trip_count"]) != 0
                    or artifact.get("eligible_sample_hashes") != []
                    or artifact.get("exact_closed_round_trip_hashes") != []
                    or report_version != _sha256(report_content)
                    or report_version != state["latest_report_version"]
                ):
                    raise LearningIntegrityError(
                        "Historical seed learner migration prior run does not verify."
                    )

            aggregate.execute(
                """CREATE TABLE IF NOT EXISTS online_learner_migrations (
                       migration_id TEXT PRIMARY KEY,
                       from_learner_version TEXT NOT NULL,
                       to_learner_version TEXT NOT NULL,
                       source_ledger_id TEXT NOT NULL,
                       source_learning_revision INTEGER NOT NULL,
                       prior_state_version TEXT NOT NULL,
                       prior_latest_run_id TEXT,
                       record_json TEXT NOT NULL,
                       record_sha256 TEXT NOT NULL UNIQUE,
                       created_ms INTEGER NOT NULL
                   )"""
            )
            record = {
                "schema": SCHEMA_VERSION,
                "kind": "historical_seed_learner_version_migration",
                "from_learner_version": persisted_version,
                "to_learner_version": learner.LEARNER_VERSION,
                "source_ledger_id": source_id,
                "source_learning_revision": source_revision,
                "prior_state_version": prior_state_version,
                "prior_latest_run_id": prior_run_id,
                "historical_manifest_sha256": str(manifest["immutable_sha256"]),
                "open_pre_candidate_round_trips_preserved": len(
                    source_trip_records
                ),
                "created_ms": now_ms,
            }
            record_digest = _sha256(record)
            migration_id = f"learner-migration:{record_digest[:48]}"
            aggregate.execute(
                """INSERT INTO online_learner_migrations
                   (migration_id, from_learner_version, to_learner_version,
                    source_ledger_id, source_learning_revision,
                    prior_state_version, prior_latest_run_id, record_json,
                    record_sha256, created_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    migration_id,
                    persisted_version,
                    learner.LEARNER_VERSION,
                    source_id,
                    source_revision,
                    prior_state_version,
                    prior_run_id,
                    _canonical_json(record),
                    record_digest,
                    now_ms,
                ),
            )
            if "learner_migration_sha256" not in {
                str(column[1])
                for column in aggregate.execute("PRAGMA table_info(online_state)")
            }:
                aggregate.execute(
                    "ALTER TABLE online_state ADD COLUMN "
                    "learner_migration_sha256 TEXT"
                )
            aggregate.execute(
                """UPDATE online_state SET learner_version=?, latest_run_id=NULL,
                   latest_report_version=NULL,
                   latest_status='learner_version_migrated_awaiting_refresh',
                   proposal_ready_for_review=0, learner_migration_sha256=?,
                   last_error=NULL, updated_ms=?
                   WHERE singleton=1""",
                (learner.LEARNER_VERSION, record_digest, now_ms),
            )
            _freeze_state(aggregate, now_ms)
            aggregate.execute("COMMIT")
            source.execute("ROLLBACK")
            return True
        except BaseException:
            if aggregate.in_transaction:
                aggregate.execute("ROLLBACK")
            raise
    finally:
        if source is not None:
            if source.in_transaction:
                source.execute("ROLLBACK")
            source.close()
        aggregate.close()


def seed_historical_development(
    source_db_path: Path | str,
    seed_manifest: Mapping[str, object],
    learning_db_path: Path | str = LEARNING_DB_PATH,
    now_ms: int | None = None,
) -> dict[str, object]:
    """Append one source-bound historical development seed.

    The seed can influence threshold selection only.  Its samples are sealed
    with ``out_of_sample=false`` and can never satisfy forward-validation or
    Testnet execution gates.  A source/aggregate pair accepts at most one seed;
    replaying the exact manifest is idempotent while replacement fails closed.
    """

    observed_ms = _now_ms() if now_ms is None else _integer(now_ms, "now_ms")
    manifest = _validated_seed_manifest(seed_manifest)

    _migrate_learner_version_for_historical_seed(
        source_db_path,
        learning_db_path,
        manifest,
        observed_ms,
    )

    # First ingest and seal every source fact using the ordinary lifecycle.
    # Seeding is then permitted only against that coherent source revision.
    synchronized = refresh(source_db_path, learning_db_path, now_ms=observed_ms)
    if synchronized.get("provenance_valid") is not True:
        raise LearningIntegrityError(
            "Historical development seed requires a verified synchronized aggregate."
        )

    aggregate = _connect_learning(learning_db_path)
    source: sqlite3.Connection | None = None
    try:
        source = _source_maintenance_connection(source_db_path)
        # Match refresh's cross-database lock order: aggregate first, source
        # second. The source reservation then closes the desired_running
        # check/use race without deadlocking a concurrent refresh transition.
        aggregate.execute("BEGIN IMMEDIATE")
        source.execute("BEGIN IMMEDIATE")
        try:
            meta = source.execute(
                """SELECT source_ledger_id, schema_version, learning_revision
                   FROM worker_learning_meta WHERE singleton=1"""
            ).fetchone()
            if meta is None or int(meta["schema_version"]) != SCHEMA_VERSION:
                raise LearningStoreError(
                    "Worker learning source schema is missing or unsupported."
                )
            source_id = str(meta["source_ledger_id"])
            policy, model_version = _source_registration_identity(source)
            aggregate_source_id, aggregate_policy, aggregate_model = (
                _aggregate_source_identity(aggregate)
            )
            if (
                not hmac.compare_digest(source_id, aggregate_source_id)
                or policy != aggregate_policy
                or model_version != aggregate_model
            ):
                raise LearningIntegrityError(
                    "Historical development seed source identity does not match the aggregate."
                )

            existing_seed, _existing_samples = _verified_historical_seed(aggregate)
            if existing_seed is not None:
                if not hmac.compare_digest(
                    str(existing_seed["manifest_sha256"]),
                    str(manifest["immutable_sha256"]),
                ):
                    raise LearningIntegrityError(
                        "Historical development seed is immutable and cannot be replaced."
                    )
                aggregate.execute("COMMIT")
                if source.in_transaction:
                    source.execute("ROLLBACK")
                return {**existing_seed, "idempotent": True}

            state_row = aggregate.execute(
                "SELECT * FROM online_state WHERE singleton=1"
            ).fetchone()
            if state_row is None:
                raise LearningStoreError("Online learning state is missing.")
            state = {key: state_row[key] for key in state_row.keys()}
            if int(state["ingested_source_revision"]) != int(
                meta["learning_revision"]
            ):
                raise LearningIntegrityError(
                    "Historical development seed requires the latest source revision."
                )
            if (
                state["frozen_candidate_sha256"] is not None
                or _aggregate_pending_candidate_transition(state) is not None
                or _verified_pending_candidate_transition(source) is not None
            ):
                raise LearningStoreError(
                    "Historical development seed is unavailable during a candidate lifecycle transition."
                )
            live_label_count = int(
                aggregate.execute("SELECT COUNT(*) FROM online_samples").fetchone()[0]
            )
            source_live_label_count = int(
                source.execute(
                    "SELECT COUNT(*) FROM worker_learning_daily_labels"
                ).fetchone()[0]
            )
            gap_count = int(
                aggregate.execute(
                    "SELECT COUNT(*) FROM online_daily_gaps"
                ).fetchone()[0]
            )
            source_gap_count = int(
                source.execute(
                    "SELECT COUNT(*) FROM worker_learning_daily_gaps"
                ).fetchone()[0]
            )
            retirement_count = int(
                aggregate.execute(
                    "SELECT COUNT(*) FROM online_candidate_retirements"
                ).fetchone()[0]
            )
            if live_label_count or source_live_label_count:
                raise LearningStoreError(
                    "Historical development seed must be installed before live daily labels."
                )
            if gap_count or source_gap_count or retirement_count:
                raise LearningStoreError(
                    "Historical development seed is allowed only in the first gap-free lifecycle."
                )
            if int(state["last_trained_sample_count"]) != 0:
                raise LearningStoreError(
                    "Historical development seed cannot replace prior training evidence."
                )
            if _unresolved_learning_outbox_count(source):
                raise LearningStoreError(
                    "Historical development seed requires an empty learning outbox."
                )
            worker_state = source.execute(
                """SELECT desired_running, pending_client_id
                   FROM worker_state WHERE singleton=1"""
            ).fetchone()
            if worker_state is None:
                raise LearningStoreError("Worker state is missing.")
            if bool(worker_state["desired_running"]) or worker_state[
                "pending_client_id"
            ] is not None:
                raise LearningStoreError(
                    "Historical development seed requires a fully stopped worker "
                    "with no pending order intent."
                )

            latest_decision = source.execute(
                """SELECT candle_close_ms, policy, close_latest
                   FROM worker_decisions
                   ORDER BY candle_close_ms DESC LIMIT 1"""
            ).fetchone()
            if latest_decision is None:
                raise LearningStoreError(
                    "Historical development seed requires a current worker decision."
                )
            latest_close = _decimal_text(
                latest_decision["close_latest"],
                "latest worker decision close",
                positive=True,
            )
            manifest_close = _decimal_text(
                manifest["last_close"], "historical seed last close", positive=True
            )
            if (
                str(latest_decision["policy"]) != policy
                or int(latest_decision["candle_close_ms"])
                != int(manifest["last_label_available_ts"])
                or latest_close != manifest_close
                or int(manifest["last_candle_close_ms"])
                != int(manifest["last_label_available_ts"])
            ):
                raise LearningIntegrityError(
                    "Historical seed tail does not exactly bridge to the latest worker decision."
                )

            samples_value = manifest.get("samples")
            if not isinstance(samples_value, list) or not samples_value:
                raise LearningIntegrityError(
                    "Historical development seed has no samples."
                )
            samples = [dict(sample) for sample in samples_value]
            if any(
                bool(sample["out_of_sample"])
                or bool(sample["true_forward_after_freeze"])
                or sample.get("freeze_id") is not None
                for sample in samples
            ):
                raise LearningIntegrityError(
                    "Historical development seed attempted to claim forward evidence."
                )

            seed_id, seed_record, seed_record_digest = _historical_seed_record(
                seed_id=None,
                source_ledger_id=source_id,
                policy=policy,
                model_version=model_version,
                manifest=manifest,
            )
            aggregate.execute(
                """INSERT INTO historical_development_seeds
                   (singleton, seed_id, source_ledger_id, policy, model_version,
                    manifest_json, manifest_sha256, seed_record_json,
                    seed_record_sha256, sample_count, first_decision_ts,
                    last_decision_ts, last_label_available_ts, last_close,
                    created_ms)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    seed_id,
                    source_id,
                    policy,
                    model_version,
                    _canonical_json(manifest),
                    manifest["immutable_sha256"],
                    _canonical_json(seed_record),
                    seed_record_digest,
                    len(samples),
                    manifest["first_decision_ts"],
                    manifest["last_decision_ts"],
                    manifest["last_label_available_ts"],
                    manifest_close,
                    observed_ms,
                ),
            )
            for ordinal, sample in enumerate(samples):
                record, record_digest = _historical_sample_record(
                    seed_id, ordinal, sample
                )
                aggregate.execute(
                    """INSERT INTO historical_development_samples
                       (seed_id, ordinal, decision_ts, label_available_ts,
                        sample_id, sample_json, sample_sha256, record_json,
                        record_sha256)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        seed_id,
                        ordinal,
                        sample["decision_ts"],
                        sample["label_available_ts"],
                        sample["sample_id"],
                        _canonical_json(sample),
                        sample["immutable_sha256"],
                        _canonical_json(record),
                        record_digest,
                    ),
                )
            stored_seed, _stored_samples = _verified_historical_seed(aggregate)
            if stored_seed is None:
                raise LearningIntegrityError(
                    "Historical development seed was not persisted."
                )
            aggregate.execute(
                """UPDATE online_state SET active_segment_id=?,
                   active_segment_start_ts=?, updated_ms=? WHERE singleton=1""",
                (
                    _segment_id(samples),
                    int(samples[0]["decision_ts"]),
                    observed_ms,
                ),
            )
            _freeze_state(aggregate, observed_ms)
            aggregate.execute("COMMIT")
            if source.in_transaction:
                source.execute("ROLLBACK")
            return {**stored_seed, "idempotent": False}
        except BaseException:
            if aggregate.in_transaction:
                aggregate.execute("ROLLBACK")
            raise
    finally:
        if source is not None:
            if source.in_transaction:
                source.execute("ROLLBACK")
            source.close()
        aggregate.close()

def _source_counts(path: Path | str) -> dict[str, object]:
    source = _source_read_connection(path)
    try:
        source.execute("BEGIN")
        meta = source.execute(
            """SELECT source_ledger_id, schema_version, learning_revision
               FROM worker_learning_meta WHERE singleton=1"""
        ).fetchone()
        if meta is None:
            raise LearningStoreError("Worker learning source is not initialized.")
        if int(meta["schema_version"]) != SCHEMA_VERSION:
            raise LearningIntegrityError(
                "Worker learning source schema does not match this code."
            )
        source_policy, source_model_version = _source_registration_identity(source)
        health_table = source.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='worker_learning_refresh_health'"""
        ).fetchone()
        health_row = (
            None
            if health_table is None
            else source.execute(
                """SELECT * FROM worker_learning_refresh_health
                   WHERE singleton=1"""
            ).fetchone()
        )
        refresh_health = (
            {
                "healthy": False,
                "last_attempt_failed": True,
                "sanitized_error": "learning refresh health is unavailable",
                "attempted_ms": None,
                "last_success_ms": None,
            }
            if health_row is None
            else _refresh_health_from_row(health_row)
        )
        pending_transition = _verified_pending_candidate_transition(source)
        return {
            "source_ledger_id": str(meta["source_ledger_id"]),
            "source_schema_version": int(meta["schema_version"]),
            "policy": source_policy,
            "model_version": source_model_version,
            "learning_revision": int(meta["learning_revision"]),
            "sealed_daily_labels": int(
                source.execute(
                    "SELECT COUNT(*) FROM worker_learning_daily_labels"
                ).fetchone()[0]
            ),
            "daily_gaps": int(
                source.execute(
                    "SELECT COUNT(*) FROM worker_learning_daily_gaps"
                ).fetchone()[0]
            ),
            "open_round_trip_records": int(
                source.execute(
                    "SELECT COUNT(*) FROM worker_learning_round_trips WHERE status='open'"
                ).fetchone()[0]
            ),
            "closed_exact_round_trip_records": int(
                source.execute(
                    """SELECT COUNT(*) FROM worker_learning_round_trips
                       WHERE status='closed' AND exact_pnl=1"""
                ).fetchone()[0]
            ),
            "quarantined_round_trip_records": int(
                source.execute(
                    """SELECT COUNT(*) FROM worker_learning_round_trips
                       WHERE status='quarantined'"""
                ).fetchone()[0]
            ),
            "source_errors": int(
                source.execute(
                    "SELECT COUNT(*) FROM worker_learning_source_errors"
                ).fetchone()[0]
            ),
            "candidate_retirements": int(
                source.execute(
                    """SELECT COUNT(*)
                       FROM worker_learning_candidate_retirements"""
                ).fetchone()[0]
            ),
            "unresolved_learning_outbox": (
                _unresolved_learning_outbox_count(source)
            ),
            "pending_candidate_transition": pending_transition,
            "refresh_health": refresh_health,
        }
    finally:
        if source.in_transaction:
            source.execute("ROLLBACK")
        source.close()


def _status_unavailable(
    error: BaseException | str,
    *,
    source: Mapping[str, object] | None = None,
    persisted_learner_version: object = None,
) -> dict[str, object]:
    message = _sanitize_refresh_error(
        error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    )
    source_snapshot = {} if source is None else dict(source)
    refresh_health = source_snapshot.get(
        "refresh_health",
        {
            "healthy": False,
            "last_attempt_failed": True,
            "sanitized_error": message,
            "attempted_ms": None,
            "last_success_ms": None,
        },
    )
    return {
        "schema": SCHEMA_VERSION,
        "mode": "proposal_only_manual_review_required",
        "status": "learning_status_unavailable",
        "proposal_ready_for_review": False,
        "provenance_valid": False,
        "learner_id": learner.LEARNER_ID,
        "learner_version": (
            None
            if persisted_learner_version is None
            else str(persisted_learner_version)
        ),
        "expected_learner_version": learner.LEARNER_VERSION,
        "source": source_snapshot,
        "historical_seed": None,
        "refresh_health": refresh_health,
        "evidence": {
            "historical_development_labels": 0,
            "development_labels": 0,
            "finalized_daily_labels": 0,
            "eligible_oos_daily_labels": 0,
            "true_forward_after_freeze_labels": 0,
            "total_true_forward_after_freeze_labels": 0,
            "exact_closed_testnet_round_trips": 0,
            "candidate_matched_exact_round_trips": 0,
            "quarantined_round_trips": 0,
            "currently_open_round_trips": 0,
            "ingest_conflicts": 0,
            "daily_gaps": 0,
            "source_errors": 0,
            "unresolved_learning_outbox": int(
                source_snapshot.get("unresolved_learning_outbox", 0)
            ),
            "pending_candidate_transition": bool(
                source_snapshot.get("pending_candidate_transition")
            ),
            "safety_violations": 0,
            "retired_candidates": 0,
        },
        "cadence": {
            "phase": "unavailable",
            "development_labels": 0,
            "first_training_at_daily_labels": MIN_TRAINING_SAMPLES,
            "retrain_every_new_daily_labels": RETRAIN_STRIDE,
            "last_trained_sample_count": 0,
            "next_training_sample_count": None,
            "labels_until_next_training": None,
            "true_forward_labels_until_review_minimum": (
                learner.MIN_TRUE_FORWARD_AFTER_FREEZE
            ),
            "last_training_ms": None,
            "candidate_frozen": False,
            "active_segment_id": None,
            "active_segment_start_ts": None,
        },
        "review_gates": {
            "minimum_oos_daily_labels": learner.MIN_PROMOTION_OOS_SAMPLES,
            "minimum_true_forward_after_freeze": (
                learner.MIN_TRUE_FORWARD_AFTER_FREEZE
            ),
            "minimum_exact_closed_testnet_round_trips": (
                learner.MIN_ACTUAL_ROUND_TRIPS
            ),
        },
        "latest": {
            "run_id": None,
            "report_version": None,
            "status": "learning_status_unavailable",
            "aggregate_status": None,
            "frozen_candidate_sha256": None,
            "freeze_cutoff_ts": None,
            "proposal_ready_for_review": False,
            "frozen_run_count": 0,
            "retired_candidate_count": 0,
            "safety_violation_digest": None,
            "last_error": message,
        },
        "last_error": message,
        **_SAFE_FLAGS,
    }


def _validate_status_provenance(
    db: sqlite3.Connection, state: Mapping[str, object]
) -> None:
    if int(state["schema_version"]) != SCHEMA_VERSION:
        raise LearningIntegrityError(
            "Online learning state schema does not match this code."
        )
    if str(state["learner_version"]) != learner.LEARNER_VERSION:
        raise LearningIntegrityError(
            "Online learning state learner version does not match this code."
        )
    _verified_learner_migration(db)
    _aggregate_pending_candidate_transition(state)

    state_payload = {
        key: value
        for key, value in state.items()
        if key not in {"singleton", "updated_ms"}
    }
    state_digest = _sha256(state_payload)
    state_event = db.execute(
        """SELECT state_json, state_sha256 FROM online_state_events
           WHERE state_version=?""",
        (state_digest,),
    ).fetchone()
    if (
        state_event is None
        or not hmac.compare_digest(str(state_event["state_sha256"]), state_digest)
        or str(state_event["state_json"]) != _canonical_json(state_payload)
    ):
        raise LearningIntegrityError(
            "Online learning state has no matching immutable state event."
        )

    source_id, source_policy, source_model_version = _aggregate_source_identity(db)
    identity_digest = _sha256(
        {
            "source_ledger_id": source_id,
            "policy": source_policy,
            "model_version": source_model_version,
        }
    )
    if not hmac.compare_digest(
        str(state.get("aggregate_identity_sha256")), identity_digest
    ):
        raise LearningIntegrityError(
            "Online aggregate identity is not sealed by the current state."
        )

    verified_samples = _verified_online_samples(db)
    _historical_seed, _historical_samples = _verified_historical_seed(db)
    verified_gaps = _verified_daily_gaps(db)
    verified_round_trips = _verified_online_round_trip_records(db)
    verified_conflicts = _verified_ingest_conflicts(db)
    verified_source_errors = _verified_online_source_errors(db)
    terminal_keys = {
        (
            record["policy"],
            record["model_version"],
            record["entry_client_id"],
        )
        for record, _digest in verified_round_trips
        if record.get("status") in {"closed", "quarantined"}
    }
    verified_open_count = sum(
        record.get("status") == "open"
        and (
            record["policy"],
            record["model_version"],
            record["entry_client_id"],
        )
        not in terminal_keys
        for record, _digest in verified_round_trips
    )
    verified_safety = tuple(
        violation
        for present, violation in (
            (bool(verified_conflicts), "online_ingest_conflict_present"),
            (bool(verified_source_errors), "source_learning_error_present"),
        )
        if present
    )
    if (
        int(state["finalized_daily_label_count"]) != len(verified_samples)
        or int(state["total_true_forward_daily_label_count"])
        != sum(
            bool(sample["out_of_sample"])
            and bool(sample["true_forward_after_freeze"])
            for sample in verified_samples
        )
        or int(state["exact_closed_round_trip_count"])
        != sum(
            record.get("status") == "closed"
            and record.get("exact_pnl") is True
            for record, _digest in verified_round_trips
        )
        or int(state["quarantined_round_trip_count"])
        != sum(
            record.get("status") == "quarantined"
            for record, _digest in verified_round_trips
        )
        or int(state["open_round_trip_count"]) != verified_open_count
        or int(state["ingest_conflict_count"]) != len(verified_conflicts)
        or int(state["daily_gap_count"]) != len(verified_gaps)
        or int(state["source_error_count"]) != len(verified_source_errors)
        or int(state["safety_violation_count"]) != len(verified_safety)
        or str(state["safety_violation_digest"])
        != _sha256(list(verified_safety))
    ):
        raise LearningIntegrityError(
            "Online learning state evidence counts do not verify."
        )

    frozen_candidate: dict[str, object] | None = None
    frozen_values = (
        state["frozen_candidate_json"],
        state["frozen_candidate_sha256"],
        state["freeze_cutoff_ts"],
    )
    if any(value is None for value in frozen_values):
        if not all(value is None for value in frozen_values):
            raise LearningIntegrityError(
                "Online learning frozen candidate provenance is incomplete."
            )
    else:
        parsed = _parse_canonical_json(
            state["frozen_candidate_json"], "status frozen candidate"
        )
        if not isinstance(parsed, Mapping):
            raise LearningIntegrityError("Status frozen candidate is not an object.")
        payload = {
            key: value
            for key, value in parsed.items()
            if key != "immutable_sha256"
        }
        supplied = str(state["frozen_candidate_sha256"])
        if (
            parsed.get("immutable_sha256") != supplied
            or _sha256(payload) != supplied
            or parsed.get("learner_id") != learner.LEARNER_ID
            or parsed.get("learner_version") != learner.LEARNER_VERSION
            or int(parsed.get("freeze_cutoff_ts", -1))
            != int(state["freeze_cutoff_ts"])
        ):
            raise LearningIntegrityError(
                "Online learning frozen candidate provenance does not verify."
            )
        frozen_candidate = dict(parsed)

    active_samples = _active_state_learning_samples(db, state)
    if (
        int(state["eligible_oos_daily_label_count"])
        != sum(bool(sample["out_of_sample"]) for sample in active_samples)
        or int(state["true_forward_daily_label_count"])
        != sum(
            bool(sample["true_forward_after_freeze"])
            for sample in active_samples
        )
    ):
        raise LearningIntegrityError(
            "Online learning active evidence counts do not verify."
        )

    latest_run_id = state["latest_run_id"]
    latest_report_version = state["latest_report_version"]
    if (latest_run_id is None) != (latest_report_version is None):
        raise LearningIntegrityError(
            "Online learning latest run provenance is incomplete."
        )
    if latest_run_id is not None:
        run = db.execute(
            "SELECT * FROM online_learning_runs WHERE run_id=?",
            (latest_run_id,),
        ).fetchone()
        if run is None:
            raise LearningIntegrityError(
                "Online learning latest run is missing."
            )
        artifact = _parse_canonical_json(
            run["artifact_json"], "latest learning run artifact"
        )
        if not isinstance(artifact, Mapping):
            raise LearningIntegrityError(
                "Latest learning run artifact is not an object."
            )
        expected_artifact_keys = {
            "schema",
            "kind",
            "run_kind",
            "learner_version",
            "active_segment_id",
            "eligible_sample_hashes",
            "exact_closed_round_trip_hashes",
            "exact_round_trip_evidence_sha256",
            "report",
            *_SAFE_FLAGS,
        }
        artifact_digest = _sha256(artifact)
        run_basis = {
            "artifact_sha256": artifact_digest,
            "run_kind": run["run_kind"],
            "learner_version": run["learner_version"],
        }
        if (
            set(artifact) != expected_artifact_keys
            or artifact.get("schema") != SCHEMA_VERSION
            or artifact.get("kind")
            != "testnet_online_learning_frozen_artifact"
            or artifact.get("run_kind") != run["run_kind"]
            or artifact.get("learner_version") != learner.LEARNER_VERSION
            or artifact.get("active_segment_id") != run["active_segment_id"]
            or str(run["learner_version"]) != learner.LEARNER_VERSION
            or not hmac.compare_digest(
                str(run["artifact_sha256"]), artifact_digest
            )
            or not hmac.compare_digest(str(run["run_id"]), _sha256(run_basis))
            or str(run["report_version"]) != str(latest_report_version)
            or any(artifact.get(key) is not False for key in _SAFE_FLAGS)
        ):
            raise LearningIntegrityError(
                "Online learning latest run artifact provenance does not verify."
            )

        sample_hashes = artifact.get("eligible_sample_hashes")
        round_trip_hashes = artifact.get("exact_closed_round_trip_hashes")
        report = artifact.get("report")
        if (
            not isinstance(sample_hashes, list)
            or not all(isinstance(value, str) for value in sample_hashes)
            or not isinstance(round_trip_hashes, list)
            or not all(isinstance(value, str) for value in round_trip_hashes)
            or not isinstance(report, Mapping)
        ):
            raise LearningIntegrityError(
                "Online learning latest run artifact content is malformed."
            )
        report_content = dict(report)
        supplied_report_version = report_content.pop("report_version", None)
        if (
            supplied_report_version != _sha256(report_content)
            or supplied_report_version != run["report_version"]
            or report.get("schema") != learner.SCHEMA
            or report.get("learner_id") != learner.LEARNER_ID
            or report.get("learner_version") != learner.LEARNER_VERSION
            or report.get("dataset_version") != run["dataset_version"]
            or bool(report.get("proposal_ready_for_review"))
            != bool(run["proposal_ready_for_review"])
            or any(report.get(key) is not False for key in _SAFE_FLAGS)
            or len(sample_hashes) != int(run["eligible_sample_count"])
            or _sha256(sample_hashes) != str(run["dataset_version"])
            or len(round_trip_hashes) != int(run["exact_round_trip_count"])
            or _sha256(round_trip_hashes)
            != str(run["exact_round_trip_evidence_sha256"])
            or artifact.get("exact_round_trip_evidence_sha256")
            != run["exact_round_trip_evidence_sha256"]
            or bool(state["proposal_ready_for_review"])
            != bool(run["proposal_ready_for_review"])
            or str(state["latest_status"]) != str(report.get("status"))
        ):
            raise LearningIntegrityError(
                "Online learning latest report provenance does not verify."
            )

        segments = _verified_learning_segments(db)
        matching_segment: list[dict[str, object]] | None = None
        for segment in segments:
            for start in range(len(segment)):
                lifecycle_segment = segment[start:]
                hashes = [
                    str(item["immutable_sha256"])
                    for item in lifecycle_segment
                ]
                if (
                    _segment_id(lifecycle_segment)
                    == str(run["active_segment_id"])
                    and hashes[: len(sample_hashes)] == sample_hashes
                ):
                    matching_segment = lifecycle_segment
                    break
            if matching_segment is not None:
                break
        if sample_hashes and matching_segment is None:
            raise LearningIntegrityError(
                "Latest run sample hashes do not match verified aggregate evidence."
            )
        if round_trip_hashes:
            if matching_segment is None or frozen_candidate is None:
                raise LearningIntegrityError(
                    "Latest run round-trip evidence has no frozen sample segment."
                )
            latest_label_ts = int(matching_segment[-1]["label_available_ts"])
            verified_trips = _candidate_round_trips(
                _exact_round_trip_records(db),
                frozen_candidate,
                latest_label_ts,
                matching_segment,
            )
            if [
                str(item["immutable_sha256"]) for item in verified_trips
            ] != round_trip_hashes:
                raise LearningIntegrityError(
                    "Latest run round-trip hashes do not match verified evidence."
                )

        report_candidate = report.get("frozen_candidate")
        if frozen_candidate is None:
            if report_candidate is not None:
                raise LearningIntegrityError(
                    "Latest report candidate does not match aggregate state."
                )
        elif report_candidate != frozen_candidate:
            raise LearningIntegrityError(
                "Latest report candidate does not match aggregate state."
            )


def status_snapshot(
    source_db_path: Path | str,
    learning_db_path: Path | str = LEARNING_DB_PATH,
) -> dict[str, object]:
    """Return a read-only progress and safety snapshot."""

    try:
        source = _source_counts(source_db_path)
    except Exception as exc:
        return _status_unavailable(exc)
    learning_path = Path(learning_db_path)
    if learning_path.exists():
        persisted_learner_version: object = None
        try:
            aggregate = _source_read_connection(learning_path)
            try:
                aggregate.execute("BEGIN")
                row = aggregate.execute(
                    "SELECT * FROM online_state WHERE singleton=1"
                ).fetchone()
                if row is None:
                    raise LearningStoreError("Online learning state is missing.")
                state = {key: row[key] for key in row.keys()}
                persisted_learner_version = state.get("learner_version")
                _validate_status_provenance(aggregate, state)
                historical_seed, historical_samples = (
                    _verified_historical_seed(aggregate)
                )
                active_development_samples = _active_state_learning_samples(
                    aggregate, state
                )
                run_count = int(
                    aggregate.execute(
                        "SELECT COUNT(*) FROM online_learning_runs"
                    ).fetchone()[0]
                )
            finally:
                if aggregate.in_transaction:
                    aggregate.execute("ROLLBACK")
                aggregate.close()
        except Exception as exc:
            return _status_unavailable(
                exc,
                source=source,
                persisted_learner_version=persisted_learner_version,
            )
    else:
        state = {
            "finalized_daily_label_count": 0,
            "eligible_oos_daily_label_count": 0,
            "true_forward_daily_label_count": 0,
            "total_true_forward_daily_label_count": 0,
            "exact_closed_round_trip_count": 0,
            "candidate_matched_round_trip_count": 0,
            "quarantined_round_trip_count": 0,
            "open_round_trip_count": 0,
            "ingest_conflict_count": 0,
            "daily_gap_count": 0,
            "source_error_count": 0,
            "unresolved_learning_outbox_count": 0,
            "pending_candidate_transition_json": None,
            "pending_candidate_transition_sha256": None,
            "ingested_source_revision": -1,
            "aggregate_identity_sha256": None,
            "safety_violation_count": 0,
            "safety_violation_digest": _sha256([]),
            "active_segment_id": None,
            "active_segment_start_ts": None,
            "retired_candidate_count": 0,
            "last_trained_sample_count": 0,
            "next_training_sample_count": MIN_TRAINING_SAMPLES,
            "last_training_ms": None,
            "latest_run_id": None,
            "latest_report_version": None,
            "latest_status": "collecting_daily_labels",
            "frozen_candidate_sha256": None,
            "freeze_cutoff_ts": None,
            "proposal_ready_for_review": 0,
            "last_error": None,
        }
        run_count = 0
        historical_seed = None
        historical_samples = []
        active_development_samples = []
    try:
        # Re-read the source after the aggregate so a source commit that raced
        # the first read cannot inherit an older ready aggregate snapshot.
        source = _source_counts(source_db_path)
    except Exception as exc:
        return _status_unavailable(
            exc,
            source=source,
            persisted_learner_version=state.get("learner_version"),
        )
    sealed_identity = state.get("aggregate_identity_sha256")
    current_source_identity = _sha256(
        {
            "source_ledger_id": source["source_ledger_id"],
            "policy": source["policy"],
            "model_version": source["model_version"],
        }
    )
    if sealed_identity is not None and not hmac.compare_digest(
        str(sealed_identity), current_source_identity
    ):
        return _status_unavailable(
            "Worker source policy/model identity changed after aggregate binding.",
            source=source,
            persisted_learner_version=state.get("learner_version"),
        )
    eligible = int(state["eligible_oos_daily_label_count"])
    next_training = int(state["next_training_sample_count"])
    candidate_frozen = state["frozen_candidate_sha256"] is not None
    refresh_health = source["refresh_health"]
    if not isinstance(refresh_health, Mapping):
        raise LearningIntegrityError("Learning refresh health is malformed.")
    refresh_unhealthy = bool(refresh_health["last_attempt_failed"])
    unresolved_outbox = int(source["unresolved_learning_outbox"])
    aggregate_pending_transition = _aggregate_pending_candidate_transition(state)
    source_pending_transition = source.get("pending_candidate_transition")
    pending_transition = bool(
        aggregate_pending_transition is not None
        or source_pending_transition is not None
    )
    source_revision = int(source["learning_revision"])
    ingested_source_revision = int(state["ingested_source_revision"])
    source_revision_stale = source_revision != ingested_source_revision
    effective_status = (
        "learning_refresh_unhealthy"
        if refresh_unhealthy
        else (
            "blocked_by_unresolved_learning_outbox"
            if unresolved_outbox
            else (
                "candidate_transition_pending"
                if pending_transition
                else (
                    "stale_source_evidence"
                    if source_revision_stale
                    else str(state["latest_status"])
                )
            )
        )
    )
    effective_ready = (
        False
        if (
            refresh_unhealthy
            or source_revision_stale
            or unresolved_outbox
            or pending_transition
        )
        else bool(state["proposal_ready_for_review"])
    )
    return {
        "schema": SCHEMA_VERSION,
        "mode": "proposal_only_manual_review_required",
        "status": effective_status,
        "proposal_ready_for_review": effective_ready,
        "provenance_valid": True,
        "learner_id": learner.LEARNER_ID,
        "learner_version": learner.LEARNER_VERSION,
        "expected_learner_version": learner.LEARNER_VERSION,
        "source": source,
        "historical_seed": historical_seed,
        "refresh_health": dict(refresh_health),
        "evidence": {
            "historical_development_labels": len(historical_samples),
            "development_labels": len(active_development_samples),
            "finalized_daily_labels": int(state["finalized_daily_label_count"]),
            "eligible_oos_daily_labels": eligible,
            "true_forward_after_freeze_labels": int(
                state["true_forward_daily_label_count"]
            ),
            "total_true_forward_after_freeze_labels": int(
                state["total_true_forward_daily_label_count"]
            ),
            "exact_closed_testnet_round_trips": int(
                state["exact_closed_round_trip_count"]
            ),
            "candidate_matched_exact_round_trips": int(
                state["candidate_matched_round_trip_count"]
            ),
            "quarantined_round_trips": int(
                state["quarantined_round_trip_count"]
            ),
            "currently_open_round_trips": int(state["open_round_trip_count"]),
            "ingest_conflicts": int(state["ingest_conflict_count"]),
            "daily_gaps": int(state["daily_gap_count"]),
            "source_errors": int(state["source_error_count"]),
            "unresolved_learning_outbox": unresolved_outbox,
            "pending_candidate_transition": pending_transition,
            "source_learning_revision": source_revision,
            "ingested_source_revision": ingested_source_revision,
            "safety_violations": int(state["safety_violation_count"]),
            "retired_candidates": int(state["retired_candidate_count"]),
        },
        "cadence": {
            "phase": (
                "frozen_candidate_evaluation"
                if candidate_frozen else "development_collection"
            ),
            "development_labels": len(active_development_samples),
            "first_training_at_daily_labels": MIN_TRAINING_SAMPLES,
            "retrain_every_new_daily_labels": RETRAIN_STRIDE,
            "last_trained_sample_count": int(state["last_trained_sample_count"]),
            "next_training_sample_count": (
                None if candidate_frozen else next_training
            ),
            "labels_until_next_training": (
                None
                if candidate_frozen
                else max(0, next_training - len(active_development_samples))
            ),
            "true_forward_labels_until_review_minimum": max(
                0,
                learner.MIN_TRUE_FORWARD_AFTER_FREEZE
                - int(state["true_forward_daily_label_count"]),
            ),
            "last_training_ms": state["last_training_ms"],
            "candidate_frozen": candidate_frozen,
            "active_segment_id": state["active_segment_id"],
            "active_segment_start_ts": state["active_segment_start_ts"],
        },
        "review_gates": {
            "minimum_oos_daily_labels": learner.MIN_PROMOTION_OOS_SAMPLES,
            "minimum_true_forward_after_freeze": (
                learner.MIN_TRUE_FORWARD_AFTER_FREEZE
            ),
            "minimum_exact_closed_testnet_round_trips": (
                learner.MIN_ACTUAL_ROUND_TRIPS
            ),
        },
        "latest": {
            "run_id": state["latest_run_id"],
            "report_version": state["latest_report_version"],
            "status": effective_status,
            "aggregate_status": state["latest_status"],
            "frozen_candidate_sha256": state["frozen_candidate_sha256"],
            "freeze_cutoff_ts": state["freeze_cutoff_ts"],
            "proposal_ready_for_review": effective_ready,
            "frozen_run_count": run_count,
            "retired_candidate_count": int(
                state["retired_candidate_count"]
            ),
            "safety_violation_digest": state["safety_violation_digest"],
            "source_learning_revision": source_revision,
            "ingested_source_revision": ingested_source_revision,
            "last_error": (
                refresh_health["sanitized_error"]
                if refresh_unhealthy
                else (
                    "source learning evidence is newer than the aggregate"
                    if source_revision_stale
                    else state["last_error"]
                )
            ),
        },
        **_SAFE_FLAGS,
    }


__all__ = [
    "SCHEMA_VERSION",
    "DAY_MS",
    "RETRAIN_STRIDE",
    "MIN_TRAINING_SAMPLES",
    "LEARNING_DB_PATH",
    "LearningStoreError",
    "LearningIntegrityError",
    "ensure_source_schema",
    "capture_daily_label",
    "open_round_trip",
    "close_round_trip",
    "adopt_open_round_trip",
    "quarantine_open_round_trips",
    "record_source_error",
    "mark_refresh_failure",
    "mark_refresh_success",
    "refresh",
    "seed_historical_development",
    "status_snapshot",
]
