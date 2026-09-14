"""Frozen model challengers for the Bollinger 15M v2 stream.

The production prediction remains the only object that may reach paper capital.
This module trains one deliberately small challenger family, freezes its JSON
artifact, and records paired scores against the same immutable forward events.
Each challenger also owns an isolated micro paper account.  Its executions never
change the main $1,000 portfolio, ``eligible``, or production ``v2_executions``.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from statistics import fmean, median
from typing import Mapping, Sequence

import v2_model
import v2_store
from v2_engine import bootstrap_mean_lower_bound


MODEL_SCHEMA = 1
MODEL_KIND = "bollinger_15m_v2_challenger"
FAMILY = "weighted_interaction_logistic"
SCORE_KIND = "probability"
STATUS = "frozen_shadow"
TRANSFORM_VERSION = "strategy-interactions-v1"
TRAINING_PROTOCOL_VERSION = "frozen-prequential-challenger-v1"
DEFAULT_C = 0.05
DEFAULT_THRESHOLD = 0.50
MIN_MATCHED_FUTURE_EVENTS = 200
MIN_WOULD_ACCEPT = 30
MIN_EXECUTION_GATED_ACCEPTS = 30
MIN_PROFIT_FACTOR = 1.20
BOOTSTRAP_ITERATIONS = 2_000
PAPER_INITIAL_USD = 100.0
PAPER_RISK_FRACTION = 0.002
PAPER_ALLOCATION_CAP = 0.10
PAPER_DAILY_LOSS_LIMIT = 0.02
PAPER_DRAWDOWN_LIMIT = 0.08

INTERACTION_FEATURE_NAMES = tuple(
    [*v2_model.FEATURE_NAMES]
    + [f"{name}_x_is_breakout" for name in v2_model.FEATURE_NAMES[:-1]]
)


def _is_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _finite(value: object, name: str) -> float:
    if not _is_number(value):
        raise ValueError(f"{name} must be finite.")
    return float(value)


def _timestamp(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative integer millisecond timestamp.")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
    return value.strip()


def _assert_finite_json(value: object, path: str = "root") -> None:
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError(f"Non-finite JSON number at {path}.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_finite_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError(f"Non-string JSON key at {path}.")
        for key, item in value.items():
            _assert_finite_json(item, f"{path}.{key}")
        return
    raise ValueError(f"Unsupported JSON value at {path}.")


def _canonical_json(payload: Mapping[str, object]) -> str:
    plain = dict(payload)
    _assert_finite_json(plain)
    return json.dumps(
        plain, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def interaction_vector(vector: Sequence[float]) -> list[float]:
    """Apply the exact strategy interaction used by the family benchmark."""

    if (
        not isinstance(vector, (list, tuple))
        or len(vector) != len(v2_model.FEATURE_NAMES)
        or not all(_is_number(value) for value in vector)
    ):
        raise ValueError("A challenger input must be one finite v2 feature vector.")
    base = [float(value) for value in vector]
    strategy = base[-1]
    return [*base, *(value * strategy for value in base[:-1])]


def _canonical_training_samples(
    samples: Sequence[Mapping[str, object]],
    training_cutoff_label_ts: int,
) -> list[dict[str, object]]:
    cutoff = _timestamp(training_cutoff_label_ts, "training_cutoff_label_ts")
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise ValueError("Training samples must be a sequence.")
    prepared: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw in enumerate(samples):
        if not isinstance(raw, Mapping):
            raise ValueError(f"Training sample {index} must be a mapping.")
        source = raw.get("source")
        if source == v2_model.FORWARD_SAMPLE_SOURCE:
            sample = v2_model._canonical_forward_sample(raw, index)
        elif source == "historical_v2_h8":
            sample = dict(raw)
        else:
            raise ValueError(f"Training sample {index} has an unsupported source.")
        event_id = sample.get("id")
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            raise ValueError(f"Training sample {index} has an invalid or duplicate id.")
        seen.add(event_id)
        prepared.append(sample)

    v2_model._validate_samples(prepared)
    for index, sample in enumerate(prepared):
        if int(sample["label_available_ts"]) > cutoff:
            raise ValueError(
                f"Training sample {index} was not available at the frozen cutoff."
            )
    return sorted(
        prepared,
        key=lambda sample: (int(sample["fill_ts"]), str(sample["id"])),
    )


def _training_corpus_hash(samples: Sequence[Mapping[str, object]]) -> str:
    payload = {
        "schema": "bollinger-v2-challenger-training-corpus-v1",
        "events": [
            {
                "id": sample["id"],
                "strategy": sample["strategy"],
                "decision_ts": int(sample["decision_ts"]),
                "fill_ts": int(sample["fill_ts"]),
                "exit_ts": int(sample["exit_ts"]),
                "label_available_ts": int(sample["label_available_ts"]),
                "entry_reference": float(sample["entry_reference"]),
                "exit_reference": float(sample["exit_reference"]),
                "target": float(sample["target"]),
                "net_return": float(sample["net_return"]),
                "x": [float(value) for value in sample["x"]],
                "source": sample["source"],
            }
            for sample in samples
        ],
    }
    return _sha256_text(_canonical_json(payload))


def _artifact_version_payload(artifact: Mapping[str, object]) -> dict[str, object]:
    return {
        key: artifact[key]
        for key in (
            "schema",
            "kind",
            "family",
            "score_kind",
            "status",
            "cohort_id",
            "control_model_version",
            "policy",
            "spec_id",
            "signal_version",
            "cost_version",
            "execution_policy_version",
            "feature_version",
            "base_feature_names",
            "transform_version",
            "feature_names",
            "training_protocol_version",
            "training_corpus_hash",
            "training_cutoff_label_ts",
            "threshold",
            "created_ts",
            "fitted",
        )
    }


def _model_version(artifact: Mapping[str, object]) -> str:
    return _sha256_text(_canonical_json(_artifact_version_payload(artifact)))


def train_frozen_cohort(
    samples: Sequence[Mapping[str, object]],
    *,
    cohort_id: str,
    control_model_version: str,
    training_cutoff_label_ts: int,
    c_value: float = DEFAULT_C,
    threshold: float = DEFAULT_THRESHOLD,
    created_ts: int | None = None,
) -> dict[str, object]:
    """Fit one immutable weighted-interaction logistic challenger artifact.

    Every supplied label must already exist at ``training_cutoff_label_ts``.
    scikit-learn is imported only here; :func:`predict_probability` uses the
    standard library and the frozen JSON coefficients.
    """

    cohort = _required_text(cohort_id, "cohort_id")
    frozen_control = _required_text(
        control_model_version, "control_model_version"
    )
    cutoff = _timestamp(training_cutoff_label_ts, "training_cutoff_label_ts")
    created = cutoff if created_ts is None else _timestamp(created_ts, "created_ts")
    if created < cutoff:
        raise ValueError("created_ts cannot precede the frozen training cutoff.")
    c_value = _finite(c_value, "c_value")
    threshold = _finite(threshold, "threshold")
    if c_value <= 0:
        raise ValueError("c_value must be positive.")
    if not 0 < threshold < 1:
        raise ValueError("threshold must be inside (0, 1).")

    prepared = _canonical_training_samples(samples, cutoff)
    outcomes = [int(float(sample["net_return"]) > 0) for sample in prepared]
    if len(prepared) < 20 or len(set(outcomes)) < 2:
        raise ValueError("Challenger training needs 20 events and both outcome classes.")
    matrix = [interaction_vector(sample["x"]) for sample in prepared]
    returns = [float(sample["net_return"]) for sample in prepared]
    nonzero = [abs(value) for value in returns if abs(value) > 0]
    weight_scale = median(nonzero) if nonzero else 1.0
    weights = [min(3.0, max(0.5, abs(value) / weight_scale)) for value in returns]

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:  # pragma: no cover - depends on offline runtime
        raise RuntimeError("Challenger training requires scikit-learn.") from exc

    scaler = StandardScaler()
    standardized = scaler.fit_transform(matrix)
    estimator = LogisticRegression(
        C=c_value,
        solver="liblinear",
        random_state=v2_model.TRAINING_SEED,
        max_iter=2_000,
    )
    estimator.fit(standardized, outcomes, sample_weight=weights)
    fitted = {
        "algorithm": "sklearn.StandardScaler+weighted LogisticRegression(L2,liblinear)",
        "c": c_value,
        "mean": [float(value) for value in scaler.mean_],
        "scale": [float(value) if float(value) > 0 else 1.0 for value in scaler.scale_],
        "coefficients": [float(value) for value in estimator.coef_[0]],
        "intercept": float(estimator.intercept_[0]),
        "training_events": len(prepared),
        "training_base_rate": fmean(outcomes),
        "training_last_label_available_ts": max(
            int(sample["label_available_ts"]) for sample in prepared
        ),
        "magnitude_weight_floor": 0.5,
        "magnitude_weight_cap": 3.0,
        "magnitude_weight_median_abs_return": float(weight_scale),
    }
    artifact: dict[str, object] = {
        "schema": MODEL_SCHEMA,
        "kind": MODEL_KIND,
        "family": FAMILY,
        "score_kind": SCORE_KIND,
        "status": STATUS,
        "cohort_id": cohort,
        "control_model_version": frozen_control,
        "policy": v2_model.POLICY,
        "spec_id": v2_model.MAIN_SPEC.spec_id,
        "signal_version": v2_model.MAIN_SPEC.signal_version,
        "cost_version": v2_model.COST_SCENARIOS["30bp"].version,
        "execution_policy_version": v2_model.EXECUTION_POLICY_VERSION,
        "feature_version": v2_model.FEATURE_VERSION,
        "base_feature_names": list(v2_model.FEATURE_NAMES),
        "transform_version": TRANSFORM_VERSION,
        "feature_names": list(INTERACTION_FEATURE_NAMES),
        "training_protocol_version": TRAINING_PROTOCOL_VERSION,
        "training_corpus_hash": _training_corpus_hash(prepared),
        "training_cutoff_label_ts": cutoff,
        "threshold": threshold,
        "created_ts": created,
        "fitted": fitted,
    }
    artifact["model_version"] = _model_version(artifact)
    validate_artifact(artifact)
    return artifact


# A descriptive alias used by the paper/CLI integration.
build_frozen_cohort = train_frozen_cohort


def validate_artifact(artifact: Mapping[str, object]) -> None:
    if not isinstance(artifact, Mapping):
        raise ValueError("Challenger artifact must be a mapping.")
    plain = dict(artifact)
    _assert_finite_json(plain)
    required = {
        "schema": MODEL_SCHEMA,
        "kind": MODEL_KIND,
        "family": FAMILY,
        "score_kind": SCORE_KIND,
        "status": STATUS,
        "policy": v2_model.POLICY,
        "spec_id": v2_model.MAIN_SPEC.spec_id,
        "signal_version": v2_model.MAIN_SPEC.signal_version,
        "cost_version": v2_model.COST_SCENARIOS["30bp"].version,
        "execution_policy_version": v2_model.EXECUTION_POLICY_VERSION,
        "feature_version": v2_model.FEATURE_VERSION,
        "base_feature_names": list(v2_model.FEATURE_NAMES),
        "transform_version": TRANSFORM_VERSION,
        "feature_names": list(INTERACTION_FEATURE_NAMES),
        "training_protocol_version": TRAINING_PROTOCOL_VERSION,
    }
    for field, expected in required.items():
        if plain.get(field) != expected:
            raise ValueError(f"Unsupported challenger artifact field: {field}.")
    _required_text(plain.get("cohort_id"), "cohort_id")
    _required_text(
        plain.get("control_model_version"), "control_model_version"
    )
    if not _is_sha256(plain.get("training_corpus_hash")):
        raise ValueError("training_corpus_hash must be lowercase SHA-256.")
    cutoff = _timestamp(
        plain.get("training_cutoff_label_ts"), "training_cutoff_label_ts"
    )
    created = _timestamp(plain.get("created_ts"), "created_ts")
    if created < cutoff:
        raise ValueError("Challenger artifact was created before its training cutoff.")
    threshold = _finite(plain.get("threshold"), "threshold")
    if not 0 < threshold < 1:
        raise ValueError("Challenger threshold must be inside (0, 1).")
    fitted = plain.get("fitted")
    if not isinstance(fitted, Mapping):
        raise ValueError("Challenger fitted payload is required.")
    dimension = len(INTERACTION_FEATURE_NAMES)
    for field in ("mean", "scale", "coefficients"):
        values = fitted.get(field)
        if (
            not isinstance(values, list)
            or len(values) != dimension
            or not all(_is_number(value) for value in values)
        ):
            raise ValueError(f"Invalid challenger fitted {field}.")
    if not all(float(value) > 0 for value in fitted["scale"]):
        raise ValueError("Challenger fitted scales must be positive.")
    for field in (
        "intercept",
        "c",
        "training_base_rate",
        "magnitude_weight_floor",
        "magnitude_weight_cap",
        "magnitude_weight_median_abs_return",
    ):
        if not _is_number(fitted.get(field)):
            raise ValueError(f"Invalid challenger fitted {field}.")
    if float(fitted["c"]) <= 0:
        raise ValueError("Challenger fitted regularization must be positive.")
    if not 0 <= float(fitted["training_base_rate"]) <= 1:
        raise ValueError("Challenger training base rate must be in [0, 1].")
    events = fitted.get("training_events")
    last_label = fitted.get("training_last_label_available_ts")
    if isinstance(events, bool) or not isinstance(events, int) or events < 20:
        raise ValueError("Challenger fitted event count is invalid.")
    if _timestamp(last_label, "training_last_label_available_ts") > cutoff:
        raise ValueError("Challenger fit contains a label beyond its cutoff.")
    if plain.get("model_version") != _model_version(plain):
        raise ValueError("Challenger model_version does not match its frozen payload.")


def predict_probability(
    artifact: Mapping[str, object], vector: Sequence[float]
) -> float:
    """Run deterministic stdlib inference from one validated JSON artifact."""

    validate_artifact(artifact)
    transformed = interaction_vector(vector)
    fitted = artifact["fitted"]
    mean = fitted["mean"]
    scale = fitted["scale"]
    coefficients = fitted["coefficients"]
    logit = float(fitted["intercept"])
    for value, center, width, coefficient in zip(
        transformed, mean, scale, coefficients
    ):
        logit += (
            (float(value) - float(center))
            / float(width)
            * float(coefficient)
        )
    if logit >= 0:
        inverse = math.exp(-logit)
        probability = 1.0 / (1.0 + inverse)
    else:
        exponent = math.exp(logit)
        probability = exponent / (1.0 + exponent)
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("Challenger inference produced an invalid probability.")
    return probability


def _model_row(artifact: Mapping[str, object]) -> tuple[object, ...]:
    validate_artifact(artifact)
    payload = _canonical_json(dict(artifact))
    return (
        artifact["model_version"],
        artifact["cohort_id"],
        artifact["family"],
        artifact["score_kind"],
        payload,
        _sha256_text(payload),
        artifact["training_corpus_hash"],
        artifact["training_cutoff_label_ts"],
        artifact["threshold"],
        artifact["status"],
        artifact["created_ts"],
    )


def register_model(
    db: sqlite3.Connection,
    artifact: Mapping[str, object],
    *,
    manage_transaction: bool = True,
) -> bool:
    """Persist one immutable artifact; exact retries are idempotent."""

    if not isinstance(manage_transaction, bool):
        raise ValueError("manage_transaction must be boolean.")
    v2_store.ensure_tables(db)
    frozen = _model_row(artifact)

    def persist() -> bool:
        existing = db.execute(
            "SELECT model_version,cohort_id,family,score_kind,artifact_payload,"
            "artifact_sha256,training_corpus_hash,training_cutoff_label_ts,"
            "threshold,status,created_ts FROM v2_challenger_models "
            "WHERE model_version=?",
            (frozen[0],),
        ).fetchone()
        if existing is not None:
            if tuple(existing) == frozen:
                return False
            raise ValueError("Registered challenger artifact is immutable.")
        db.execute(
            "INSERT INTO v2_challenger_models "
            "(model_version,cohort_id,family,score_kind,artifact_payload,"
            "artifact_sha256,training_corpus_hash,training_cutoff_label_ts,"
            "threshold,status,created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            frozen,
        )
        return True

    if manage_transaction:
        with db:
            return persist()
    return persist()


def load_model(db: sqlite3.Connection, model_version: str) -> dict[str, object]:
    """Load and revalidate every persisted artifact field and digest."""

    v2_store.ensure_tables(db)
    version = _required_text(model_version, "model_version")
    row = db.execute(
        "SELECT model_version,cohort_id,family,score_kind,artifact_payload,"
        "artifact_sha256,training_corpus_hash,training_cutoff_label_ts,"
        "threshold,status,created_ts FROM v2_challenger_models "
        "WHERE model_version=?",
        (version,),
    ).fetchone()
    if row is None:
        raise ValueError("Challenger model is not registered.")
    try:
        artifact = json.loads(row[4])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Registered challenger artifact JSON is corrupt.") from exc
    if not isinstance(artifact, dict):
        raise ValueError("Registered challenger artifact JSON is not an object.")
    validate_artifact(artifact)
    canonical = _canonical_json(artifact)
    if canonical != row[4] or _sha256_text(canonical) != row[5]:
        raise ValueError("Registered challenger artifact digest does not match.")
    expected = _model_row(artifact)
    if tuple(row) != expected:
        raise ValueError("Registered challenger metadata does not match its artifact.")
    return artifact


def registered_model_versions(
    db: sqlite3.Connection, cohort_id: str | None = None
) -> list[str]:
    """List frozen versions without running evaluation or bootstrap work."""

    v2_store.ensure_tables(db)
    if cohort_id is None:
        rows = db.execute(
            "SELECT model_version FROM v2_challenger_models "
            "WHERE status=? ORDER BY created_ts,model_version",
            (STATUS,),
        ).fetchall()
    else:
        cohort = _required_text(cohort_id, "cohort_id")
        rows = db.execute(
            "SELECT model_version FROM v2_challenger_models "
            "WHERE status=? AND cohort_id=? ORDER BY created_ts,model_version",
            (STATUS, cohort),
        ).fetchall()
    return [str(row[0]) for row in rows]


# Synonym for integrations that treat the result as the active frozen registry.
frozen_models = registered_model_versions


def _feature_digest(vector: Sequence[float]) -> str:
    return _sha256_text(
        json.dumps(
            [float(value) for value in vector],
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def record_event_score(
    db: sqlite3.Connection,
    event_id: str,
    model_version: str,
    *,
    execution_gate: bool,
    created_ts: int,
    manage_transaction: bool = True,
) -> bool:
    """Score one newly inserted production event without touching capital.

    New scores must use the production prediction's exact creation timestamp.
    This is intended for ``v2_store.record_decisions(after_insert=...)`` with
    ``manage_transaction=False`` so a failure rolls back the whole decision.
    """

    if not isinstance(execution_gate, bool):
        raise ValueError("execution_gate must be boolean.")
    if not isinstance(manage_transaction, bool):
        raise ValueError("manage_transaction must be boolean.")
    event = _required_text(event_id, "event_id")
    created = _timestamp(created_ts, "created_ts")
    artifact = load_model(db, model_version)

    def persist() -> bool:
        prediction = db.execute(
            "SELECT stream_id,spec_id,policy,cost_version,decision_ts,fill_ts,"
            "model_version,feature_version,features,created_ts,resolved "
            "FROM v2_predictions WHERE event_id=?",
            (event,),
        ).fetchone()
        if prediction is None:
            raise ValueError("Challenger score requires a matching v2 prediction.")
        (
            stream_id, spec_id, policy, cost_version, decision_ts, _fill_ts,
            control_model_version, feature_version, encoded, prediction_ts,
            resolved,
        ) = prediction
        expected_contract = (
            v2_store.STREAM_ID,
            artifact["spec_id"],
            artifact["policy"],
            artifact["cost_version"],
            artifact["control_model_version"],
        )
        observed_contract = (
            stream_id, spec_id, policy, cost_version, control_model_version,
        )
        if observed_contract != expected_contract:
            raise ValueError(
                "Production prediction does not match the frozen challenger contract."
            )
        if feature_version != v2_model.FEATURE_VERSION or encoded is None:
            raise ValueError("Production prediction has no compatible frozen features.")
        try:
            vector = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Production prediction features are corrupt.") from exc
        # interaction_vector performs full numeric and dimensional validation.
        interaction_vector(vector)
        if created != int(prediction_ts):
            raise ValueError("Challenger score must share the prediction creation timestamp.")
        if int(decision_ts) <= int(artifact["training_cutoff_label_ts"]):
            raise ValueError("Challenger cannot score an event at or before its training cutoff.")
        probability = predict_probability(artifact, vector)
        threshold = float(artifact["threshold"])
        would_accept = probability >= threshold
        accepted = would_accept and execution_gate
        frozen = (
            event,
            artifact["model_version"],
            probability,
            threshold,
            int(would_accept),
            int(execution_gate),
            int(accepted),
            v2_model.FEATURE_VERSION,
            _feature_digest(vector),
            created,
        )
        existing = db.execute(
            "SELECT event_id,model_version,score,threshold,would_accept,"
            "execution_gate,accepted,feature_version,feature_digest,created_ts "
            "FROM v2_challenger_scores WHERE event_id=? AND model_version=?",
            (event, artifact["model_version"]),
        ).fetchone()
        if existing is not None:
            if tuple(existing) == frozen:
                return False
            raise ValueError("Frozen challenger score cannot be overwritten.")
        if bool(resolved) or db.execute(
            "SELECT 1 FROM v2_samples WHERE event_id=?", (event,)
        ).fetchone():
            raise ValueError("A new challenger score cannot be recorded after its label.")
        db.execute(
            "INSERT INTO v2_challenger_scores "
            "(event_id,model_version,score,threshold,would_accept,execution_gate,"
            "accepted,feature_version,feature_digest,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            frozen,
        )
        return True

    if manage_transaction:
        with db:
            return persist()
    return persist()


def _utc_day(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).date().isoformat()


def _decode_position(encoded: object) -> dict[str, object] | None:
    if encoded is None:
        return None
    try:
        position = json.loads(str(encoded))
    except json.JSONDecodeError as exc:
        raise ValueError("Challenger paper position is corrupt.") from exc
    if not isinstance(position, dict):
        raise ValueError("Challenger paper position must be an object.")
    required_numbers = (
        "quantity", "entry_reference", "entry", "cost", "stop", "target",
        "fee", "slippage", "ts_ms", "expires_ts_ms", "score", "threshold",
    )
    for name in required_numbers:
        _finite(position.get(name), f"position.{name}")
    for name in ("event_id", "strategy"):
        _required_text(position.get(name), f"position.{name}")
    return position


def _portfolio_row(db: sqlite3.Connection, model_version: str):
    return db.execute(
        "SELECT initial_usd,cash_usd,position,realized_pnl_usd,completed_trades,"
        "equity_usd,peak_equity_usd,drawdown_pct,day,day_start_equity,"
        "daily_halt,drawdown_halt,created_ts,updated_ts "
        "FROM v2_challenger_portfolios WHERE model_version=?",
        (model_version,),
    ).fetchone()


def _portfolio_state(model_version: str, row) -> dict[str, object]:
    if row is None:
        raise ValueError("Challenger paper portfolio is not initialized.")
    (
        initial, cash, encoded_position, realized, completed, equity, peak,
        drawdown, day, day_start, daily_halt, drawdown_halt, created, updated,
    ) = row
    state = {
        "model_version": model_version,
        "initial_usd": _finite(initial, "initial_usd"),
        "cash_usd": _finite(cash, "cash_usd"),
        "position": _decode_position(encoded_position),
        "realized_pnl_usd": _finite(realized, "realized_pnl_usd"),
        "completed_trades": int(completed),
        "equity_usd": _finite(equity, "equity_usd"),
        "peak_equity_usd": _finite(peak, "peak_equity_usd"),
        "drawdown_pct": _finite(drawdown, "drawdown_pct"),
        "day": _required_text(day, "day"),
        "day_start_equity": _finite(day_start, "day_start_equity"),
        "daily_halt": bool(daily_halt),
        "drawdown_halt": bool(drawdown_halt),
        "created_ts": int(created),
        "updated_ts": int(updated),
    }
    if min(state["initial_usd"], state["cash_usd"], state["equity_usd"],
           state["peak_equity_usd"], state["day_start_equity"]) < 0:
        raise ValueError("Challenger paper portfolio contains negative capital.")
    if state["completed_trades"] < 0:
        raise ValueError("Challenger paper trade count is invalid.")
    return state


def _write_portfolio(db: sqlite3.Connection, state: Mapping[str, object]) -> None:
    position = state.get("position")
    encoded = (
        json.dumps(position, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if position is not None else None
    )
    db.execute(
        "UPDATE v2_challenger_portfolios SET cash_usd=?,position=?,"
        "realized_pnl_usd=?,completed_trades=?,equity_usd=?,peak_equity_usd=?,"
        "drawdown_pct=?,day=?,day_start_equity=?,daily_halt=?,drawdown_halt=?,"
        "updated_ts=? WHERE model_version=?",
        (
            state["cash_usd"], encoded, state["realized_pnl_usd"],
            state["completed_trades"], state["equity_usd"],
            state["peak_equity_usd"], state["drawdown_pct"], state["day"],
            state["day_start_equity"], int(bool(state["daily_halt"])),
            int(bool(state["drawdown_halt"])), state["updated_ts"],
            state["model_version"],
        ),
    )


def ensure_paper_portfolio(
    db: sqlite3.Connection,
    model_version: str,
    now_ms: int,
    *,
    manage_transaction: bool = True,
) -> bool:
    """Create one isolated $100 paper account for a registered challenger."""

    v2_store.ensure_tables(db)
    artifact = load_model(db, model_version)
    created = _timestamp(now_ms, "now_ms")
    day = _utc_day(created)

    def persist() -> bool:
        cursor = db.execute(
            "INSERT OR IGNORE INTO v2_challenger_portfolios "
            "(model_version,initial_usd,cash_usd,position,realized_pnl_usd,"
            "completed_trades,equity_usd,peak_equity_usd,drawdown_pct,day,"
            "day_start_equity,daily_halt,drawdown_halt,created_ts,updated_ts) "
            "VALUES (?,?,?,NULL,0,0,?,?,0,?,?,0,0,?,?)",
            (
                artifact["model_version"], PAPER_INITIAL_USD, PAPER_INITIAL_USD,
                PAPER_INITIAL_USD, PAPER_INITIAL_USD, day, PAPER_INITIAL_USD,
                created, created,
            ),
        )
        return bool(cursor.rowcount)

    if manage_transaction:
        with db:
            return persist()
    return persist()


def paper_portfolio_snapshot(
    db: sqlite3.Connection, model_version: str
) -> dict[str, object] | None:
    """Return the isolated account and its immutable entry/exit counts."""

    version = _required_text(model_version, "model_version")
    row = _portfolio_row(db, version)
    if row is None:
        return None
    state = _portfolio_state(version, row)
    counts = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(CASE WHEN status='open' THEN 1 ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' THEN pnl_usd ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' AND pnl_usd>0 THEN pnl_usd ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' AND pnl_usd<0 THEN -pnl_usd ELSE 0 END),0) "
        "FROM v2_challenger_executions WHERE model_version=?",
        (version,),
    ).fetchone()
    gross_profit = float(counts[4])
    gross_loss = float(counts[5])
    state.update(
        {
            "pnl_usd": float(state["equity_usd"]) - float(state["initial_usd"]),
            "return_pct": (
                (float(state["equity_usd"]) / float(state["initial_usd"]) - 1) * 100
            ),
            "execution_records": int(counts[0]),
            "open_trades": int(counts[1]),
            "closed_trades": int(counts[2]),
            "closed_execution_pnl_usd": float(counts[3]),
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
            "included_in_main_1000_usd": False,
        }
    )
    return state


def open_paper_position(
    db: sqlite3.Connection,
    event_id: str,
    model_version: str,
    *,
    strategy: str,
    plan: Mapping[str, object],
    bid: float,
    quote_ts_ms: int,
    decision_close_ts_ms: int,
    now_ms: int,
    manage_transaction: bool = True,
) -> bool:
    """Open one persisted micro paper trade from an accepted frozen score."""

    event = _required_text(event_id, "event_id")
    version = _required_text(model_version, "model_version")
    strategy = _required_text(strategy, "strategy")
    quote_ts = _timestamp(quote_ts_ms, "quote_ts_ms")
    decision_close = _timestamp(decision_close_ts_ms, "decision_close_ts_ms")
    created = _timestamp(now_ms, "now_ms")
    if quote_ts < decision_close:
        raise ValueError("Challenger paper quote precedes the decision close.")
    bid_value = _finite(bid, "bid")
    if bid_value <= 0:
        raise ValueError("bid must be positive.")
    required = ("quantity", "entry_reference", "entry", "cost", "stop", "target",
                "fee", "slippage")
    values = {name: _finite(plan.get(name), f"plan.{name}") for name in required}
    if (
        min(values["quantity"], values["entry_reference"], values["entry"],
            values["cost"], values["stop"], values["target"]) <= 0
        or not values["stop"] < values["entry_reference"] < values["target"]
        or not 0 <= values["fee"] < 1
        or not 0 <= values["slippage"] < 1
    ):
        raise ValueError("Challenger paper position plan is invalid.")

    def persist() -> bool:
        score = db.execute(
            "SELECT score,threshold,accepted FROM v2_challenger_scores "
            "WHERE event_id=? AND model_version=?",
            (event, version),
        ).fetchone()
        if score is None or not bool(score[2]):
            return False
        ensure_paper_portfolio(
            db, version, created, manage_transaction=False
        )
        state = _portfolio_state(version, _portfolio_row(db, version))
        existing = db.execute(
            "SELECT 1 FROM v2_challenger_executions "
            "WHERE event_id=? AND model_version=?",
            (event, version),
        ).fetchone()
        if existing or state["position"] is not None:
            return False
        if state["daily_halt"] or state["drawdown_halt"]:
            return False
        if values["cost"] > float(state["cash_usd"]):
            return False
        position = {
            **values,
            "event_id": event,
            "strategy": strategy,
            "model_version": version,
            "score": float(score[0]),
            "threshold": float(score[1]),
            "ts_ms": created,
            "quote_ts_ms": quote_ts,
            "decision_close_ts_ms": decision_close,
            "expires_ts_ms": created + v2_model.MAIN_SPEC.horizon_bars * v2_store.BAR_MS,
        }
        state["cash_usd"] = float(state["cash_usd"]) - values["cost"]
        liquidation = (
            values["quantity"] * bid_value
            * (1 - values["slippage"]) * (1 - values["fee"])
        )
        state["position"] = position
        state["equity_usd"] = float(state["cash_usd"]) + liquidation
        state["peak_equity_usd"] = max(
            float(state["peak_equity_usd"]), float(state["equity_usd"])
        )
        state["drawdown_pct"] = (
            (1 - float(state["equity_usd"]) / float(state["peak_equity_usd"])) * 100
            if float(state["peak_equity_usd"]) > 0 else 0.0
        )
        state["daily_halt"] = (
            float(state["equity_usd"])
            <= float(state["day_start_equity"]) * (1 - PAPER_DAILY_LOSS_LIMIT)
        )
        state["drawdown_halt"] = (
            float(state["drawdown_pct"]) >= PAPER_DRAWDOWN_LIMIT * 100
        )
        state["updated_ts"] = created
        db.execute(
            "INSERT INTO v2_challenger_executions "
            "(event_id,model_version,entry_ts_ms,exit_ts_ms,entry_reference,"
            "entry_price,exit_price,quantity,cost_usd,proceeds_usd,pnl_usd,"
            "net_return,score,threshold,exit_reason,status,created_ts,updated_ts) "
            "VALUES (?,?,?,NULL,?,?,NULL,?,?,NULL,NULL,NULL,?,?,NULL,'open',?,?)",
            (
                event, version, created, values["entry_reference"],
                values["entry"], values["quantity"], values["cost"],
                float(score[0]), float(score[1]), created, created,
            ),
        )
        _write_portfolio(db, state)
        return True

    if manage_transaction:
        with db:
            return persist()
    return persist()


def paper_tick(
    db: sqlite3.Connection,
    quote: Mapping[str, object],
    now_ms: int,
) -> list[dict[str, object]]:
    """Mark isolated challenger accounts and close stop/target/H8 trades."""

    v2_store.ensure_tables(db)
    now = _timestamp(now_ms, "now_ms")
    bid = _finite(quote.get("bid"), "quote.bid")
    if bid <= 0:
        raise ValueError("quote.bid must be positive.")
    day = _utc_day(now)
    closed: list[dict[str, object]] = []
    with db:
        for version in registered_model_versions(db):
            ensure_paper_portfolio(db, version, now, manage_transaction=False)
            state = _portfolio_state(version, _portfolio_row(db, version))
            position = state["position"]
            if position:
                liquidation = (
                    float(position["quantity"]) * bid
                    * (1 - float(position["slippage"]))
                    * (1 - float(position["fee"]))
                )
            else:
                liquidation = 0.0
            equity = float(state["cash_usd"]) + liquidation
            if state["day"] != day:
                state["day"] = day
                state["day_start_equity"] = equity
                state["daily_halt"] = False
            state["peak_equity_usd"] = max(float(state["peak_equity_usd"]), equity)
            state["drawdown_pct"] = (
                (1 - equity / float(state["peak_equity_usd"])) * 100
                if float(state["peak_equity_usd"]) > 0 else 0.0
            )
            state["daily_halt"] = bool(
                state["daily_halt"]
                or equity <= float(state["day_start_equity"]) * (1 - PAPER_DAILY_LOSS_LIMIT)
            )
            state["drawdown_halt"] = bool(
                state["drawdown_halt"]
                or float(state["drawdown_pct"]) >= PAPER_DRAWDOWN_LIMIT * 100
            )
            reason = None
            if position:
                if state["drawdown_halt"]:
                    reason = "challenger_drawdown_halt"
                elif state["daily_halt"]:
                    reason = "challenger_daily_loss_halt"
                elif bid <= float(position["stop"]):
                    reason = "stop"
                elif bid >= float(position["target"]):
                    reason = "target"
                elif now >= int(position["expires_ts_ms"]):
                    reason = "challenger_h8_timeout"
            if reason:
                exit_price = bid * (1 - float(position["slippage"]))
                proceeds = (
                    float(position["quantity"]) * exit_price
                    * (1 - float(position["fee"]))
                )
                pnl = proceeds - float(position["cost"])
                net_return = pnl / float(position["cost"])
                state["cash_usd"] = float(state["cash_usd"]) + proceeds
                state["realized_pnl_usd"] = float(state["realized_pnl_usd"]) + pnl
                state["completed_trades"] = int(state["completed_trades"]) + 1
                state["position"] = None
                cursor = db.execute(
                    "UPDATE v2_challenger_executions SET exit_ts_ms=?,exit_price=?,"
                    "proceeds_usd=?,pnl_usd=?,net_return=?,exit_reason=?,status='closed',"
                    "updated_ts=? WHERE event_id=? AND model_version=? AND status='open'",
                    (
                        now, exit_price, proceeds, pnl, net_return, reason, now,
                        position["event_id"], version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Open challenger execution record is missing.")
                closed.append(
                    {
                        "event_id": position["event_id"],
                        "model_version": version,
                        "pnl_usd": pnl,
                        "net_return": net_return,
                        "exit_reason": reason,
                    }
                )
                equity = float(state["cash_usd"])
                state["peak_equity_usd"] = max(float(state["peak_equity_usd"]), equity)
                state["drawdown_pct"] = (
                    (1 - equity / float(state["peak_equity_usd"])) * 100
                    if float(state["peak_equity_usd"]) > 0 else 0.0
                )
            state["equity_usd"] = equity
            state["updated_ts"] = now
            _write_portfolio(db, state)
    return closed


def _return_40bp(detail_json: str) -> float:
    try:
        sample = json.loads(detail_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Forward sample detail is corrupt.") from exc
    if not isinstance(sample, Mapping):
        raise ValueError("Forward sample detail must be an object.")
    entry = _finite(sample.get("entry_reference"), "entry_reference")
    exit_reference = _finite(sample.get("exit_reference"), "exit_reference")
    if entry <= 0 or exit_reference <= 0:
        raise ValueError("Forward sample reference prices must be positive.")
    return v2_model._sample_return(sample, v2_model.COST_SCENARIOS["40bp"])


def evaluate_model(db: sqlite3.Connection, model_version: str) -> dict[str, object]:
    """Evaluate only post-cutoff paired labels and return a nomination status."""

    artifact = load_model(db, model_version)
    cutoff = int(artifact["training_cutoff_label_ts"])
    rows = db.execute(
        "SELECT p.event_id,p.decision_ts,p.model_version,p.accepted,"
        "p.created_ts,p.features,s.label_available_ts,s.detail,s.net_return,"
        "c.score,c.threshold,c.would_accept,c.execution_gate,c.accepted,"
        "c.feature_version,c.feature_digest,c.created_ts "
        "FROM v2_challenger_scores c "
        "JOIN v2_predictions p ON p.event_id=c.event_id "
        "JOIN v2_samples s ON s.event_id=p.event_id "
        "WHERE c.model_version=? AND p.model_version=? "
        "AND p.stream_id=? AND p.spec_id=? "
        "AND p.policy=? AND p.cost_version=? AND s.source=? "
        "AND p.resolution='labeled' AND p.decision_ts>? "
        "AND s.label_available_ts>? ORDER BY p.decision_ts,p.event_id",
        (
            artifact["model_version"],
            artifact["control_model_version"],
            v2_store.STREAM_ID,
            v2_model.MAIN_SPEC.spec_id,
            v2_model.POLICY,
            v2_model.COST_SCENARIOS["30bp"].version,
            v2_store.SOURCE,
            cutoff,
            cutoff,
        ),
    ).fetchall()
    challenger_returns: list[float] = []
    control_returns: list[float] = []
    probabilities: list[float] = []
    outcomes: list[int] = []
    would_accept_count = 0
    execution_gated_count = 0
    control_versions: set[str] = set()
    for row in rows:
        (
            event_id,
            decision_ts,
            control_version,
            control_accepted,
            prediction_created_ts,
            prediction_features,
            label_available_ts,
            detail,
            net_return,
            probability,
            threshold,
            would_accept,
            execution_gate,
            accepted,
            feature_version,
            feature_digest,
            score_created_ts,
        ) = row
        if int(decision_ts) <= cutoff or int(label_available_ts) <= cutoff:
            raise ValueError("Pre-cutoff evidence entered challenger evaluation.")
        if int(score_created_ts) >= int(label_available_ts):
            raise ValueError("Challenger score was not frozen before its label.")
        if int(score_created_ts) != int(prediction_created_ts):
            raise ValueError("Challenger and control scores were not frozen together.")
        if feature_version != v2_model.FEATURE_VERSION:
            raise ValueError("Challenger score feature contract is incompatible.")
        try:
            frozen_vector = json.loads(prediction_features)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Production prediction features are corrupt.") from exc
        interaction_vector(frozen_vector)
        if feature_digest != _feature_digest(frozen_vector):
            raise ValueError("Challenger feature digest does not match the event.")
        probability = _finite(probability, "score")
        threshold = _finite(threshold, "threshold")
        if not 0 <= probability <= 1 or threshold != float(artifact["threshold"]):
            raise ValueError("Frozen challenger score contract is invalid.")
        if bool(would_accept) != (probability >= threshold):
            raise ValueError("Frozen challenger threshold decision is invalid.")
        if bool(accepted) != (bool(would_accept) and bool(execution_gate)):
            raise ValueError("Frozen challenger acceptance fields disagree.")
        result_40bp = _return_40bp(detail)
        would_accept_count += int(bool(would_accept))
        execution_gated_count += int(bool(accepted))
        if accepted:
            challenger_returns.append(result_40bp)
        if control_accepted:
            control_returns.append(result_40bp)
        if control_version is not None:
            control_versions.add(str(control_version))
        probabilities.append(probability)
        outcomes.append(int(float(net_return) > 0))

    challenger_metrics = v2_model._trade_metrics(challenger_returns)
    control_metrics = v2_model._trade_metrics(control_returns)
    base_rate = float(artifact["fitted"]["training_base_rate"])
    brier = (
        fmean((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes))
        if probabilities
        else None
    )
    baseline_brier = (
        fmean((base_rate - outcome) ** 2 for outcome in outcomes)
        if outcomes
        else None
    )
    bootstrap_lower = (
        bootstrap_mean_lower_bound(
            challenger_returns,
            iterations=BOOTSTRAP_ITERATIONS,
            seed=v2_model.TRAINING_SEED,
        )
        if challenger_returns
        else None
    )
    profit_factor = challenger_metrics["profit_factor"]
    profit_factor_pass = (
        float(profit_factor) >= MIN_PROFIT_FACTOR
        if profit_factor is not None
        else float(challenger_metrics["sum_return"]) > 0
    )
    checks = {
        "matched_future_events_200": len(rows) >= MIN_MATCHED_FUTURE_EVENTS,
        "would_accept_30": would_accept_count >= MIN_WOULD_ACCEPT,
        "execution_gated_accepts_30": (
            execution_gated_count >= MIN_EXECUTION_GATED_ACCEPTS
        ),
        "40bp_compounded_positive": (
            float(challenger_metrics["compounded_return"]) > 0
        ),
        "40bp_profit_factor_at_least_1_2": profit_factor_pass,
        "bootstrap_lower_above_zero": (
            bootstrap_lower is not None and bootstrap_lower > 0
        ),
        "brier_improves_training_baseline": (
            brier is not None
            and baseline_brier is not None
            and brier < baseline_brier
        ),
        "outperforms_control_40bp": (
            float(challenger_metrics["compounded_return"])
            > float(control_metrics["compounded_return"])
        ),
    }
    candidate = all(checks.values())
    collecting = (
        len(rows) < MIN_MATCHED_FUTURE_EVENTS
        or would_accept_count < MIN_WOULD_ACCEPT
        or execution_gated_count < MIN_EXECUTION_GATED_ACCEPTS
    )
    return {
        "model_version": artifact["model_version"],
        "cohort_id": artifact["cohort_id"],
        "family": artifact["family"],
        "status": (
            "micro_probe_candidate"
            if candidate
            else ("collecting" if collecting else "shadow_rejected")
        ),
        "micro_probe_candidate": candidate,
        "capital_mutation": False,
        "training_cutoff_label_ts": cutoff,
        "matched_future_events": len(rows),
        "would_accept": would_accept_count,
        "execution_gated_accepts": execution_gated_count,
        "control_accepted": len(control_returns),
        "control_model_versions": sorted(control_versions),
        "required_control_model_version": artifact["control_model_version"],
        "challenger_40bp": challenger_metrics,
        "control_40bp": control_metrics,
        "brier": brier,
        "baseline_brier": baseline_brier,
        "bootstrap_mean_lower_95": bootstrap_lower,
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }


def status(
    db: sqlite3.Connection, model_version: str | None = None
) -> dict[str, object]:
    """Return forward evidence and isolated paper-account status."""

    v2_store.ensure_tables(db)
    if model_version is None:
        versions = [
            str(row[0])
            for row in db.execute(
                "SELECT model_version FROM v2_challenger_models "
                "ORDER BY created_ts,model_version"
            ).fetchall()
        ]
    else:
        versions = [_required_text(model_version, "model_version")]
    models = []
    for version in versions:
        try:
            model_status = evaluate_model(db, version)
            model_status["isolated_paper_portfolio"] = paper_portfolio_snapshot(
                db, version
            )
            models.append(model_status)
        except ValueError as exc:
            models.append(
                {
                    "model_version": version,
                    "status": "invalid_artifact",
                    "micro_probe_candidate": False,
                    "capital_mutation": False,
                    "error": str(exc),
                }
            )
    return {
        "kind": "bollinger_15m_v2_challenger_status",
        "capital_enabled": False,
        "main_1000_usd_capital_enabled": False,
        "isolated_paper_capital_enabled": True,
        "isolated_paper_initial_usd_per_model": PAPER_INITIAL_USD,
        "execution_writes_enabled": True,
        "execution_table": "v2_challenger_executions",
        "risk_fraction": PAPER_RISK_FRACTION,
        "allocation_cap": PAPER_ALLOCATION_CAP,
        "models": models,
    }


__all__ = [
    "MODEL_SCHEMA",
    "MODEL_KIND",
    "FAMILY",
    "SCORE_KIND",
    "STATUS",
    "TRANSFORM_VERSION",
    "TRAINING_PROTOCOL_VERSION",
    "DEFAULT_C",
    "DEFAULT_THRESHOLD",
    "MIN_MATCHED_FUTURE_EVENTS",
    "MIN_WOULD_ACCEPT",
    "MIN_EXECUTION_GATED_ACCEPTS",
    "PAPER_INITIAL_USD",
    "PAPER_RISK_FRACTION",
    "PAPER_ALLOCATION_CAP",
    "PAPER_DAILY_LOSS_LIMIT",
    "PAPER_DRAWDOWN_LIMIT",
    "INTERACTION_FEATURE_NAMES",
    "interaction_vector",
    "train_frozen_cohort",
    "build_frozen_cohort",
    "validate_artifact",
    "predict_probability",
    "register_model",
    "load_model",
    "registered_model_versions",
    "frozen_models",
    "record_event_score",
    "ensure_paper_portfolio",
    "paper_portfolio_snapshot",
    "open_paper_position",
    "paper_tick",
    "evaluate_model",
    "status",
]
