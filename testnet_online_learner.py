"""Leakage-resistant online challenger learner for Binance Spot Testnet.

Without a frozen candidate, ``learn`` selects a threshold from closed
development labels and returns an immutable artifact.  Later calls pass that
artifact, the unchanged development prefix, and strictly post-freeze labels.
The frozen threshold is evaluated on that suffix and is never reselected.

This module has no network, exchange, database, filesystem, order, or active
configuration capability.  Passing results are review-only; all deployment
flags remain false.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    InvalidOperation,
    ROUND_HALF_EVEN,
    localcontext,
)
import hashlib
import hmac
import json
import re
from typing import Mapping, Sequence


SCHEMA = 2
LEARNER_ID = "binance_testnet_daily_momentum_online_challenger_v2"
IMPLEMENTATION_REVISION = 5
DAY_MS = 86_400_000
CANDIDATE_THRESHOLDS = tuple(
    Decimal(value) for value in ("0.03", "0.05", "0.10", "0.15", "0.20")
)
ONE_WAY_TRANSITION_COST = Decimal("0.002")
CHRONOLOGICAL_FOLDS = 3
MIN_TRAINING_SAMPLES = 60
MIN_PROMOTION_OOS_SAMPLES = 200
MIN_TRUE_FORWARD_AFTER_FREEZE = 60
MIN_ACTUAL_ROUND_TRIPS = 8
MIN_DEVELOPMENT_TRANSITIONS = 4
MIN_HOLDOUT_TRANSITIONS = 4
MIN_HOLDOUT_PROFIT_FACTOR = Decimal("1.15")
MAX_HOLDOUT_DRAWDOWN = Decimal("0.15")
DECIMAL_PRECISION = 80
DECIMAL_ROUNDING = ROUND_HALF_EVEN
_DECIMAL_CONTEXT = Context(
    prec=DECIMAL_PRECISION,
    rounding=DECIMAL_ROUNDING,
    Emin=-999_999,
    Emax=999_999,
)

_SAFE_FLAGS = {
    "testnet_execution_eligible": False,
    "paper_eligible": False,
    "real_money_eligible": False,
    "real_orders_enabled": False,
    "live_trading_enabled": False,
    "automatic_activation_enabled": False,
    "writes_active_config": False,
}
_SPEC = {
    "schema": SCHEMA,
    "learner_id": LEARNER_ID,
    "implementation_revision": IMPLEMENTATION_REVISION,
    "daily_horizon_ms": DAY_MS,
    "candidate_thresholds": [format(value, ".2f")
                             for value in CANDIDATE_THRESHOLDS],
    "one_way_transition_cost": format(ONE_WAY_TRANSITION_COST, ".3f"),
    "profit_factor_basis": "compounded_equity_pnl",
    "decimal_arithmetic": {
        "precision": DECIMAL_PRECISION,
        "rounding": str(DECIMAL_ROUNDING),
        "emin": _DECIMAL_CONTEXT.Emin,
        "emax": _DECIMAL_CONTEXT.Emax,
    },
    "chronological_folds": CHRONOLOGICAL_FOLDS,
    "minimum_training_samples": MIN_TRAINING_SAMPLES,
    "review_gates": {
        "minimum_total_forward_collected_daily_labels_for_data_sufficiency": (
            MIN_PROMOTION_OOS_SAMPLES),
        "minimum_true_forward_after_freeze": MIN_TRUE_FORWARD_AFTER_FREEZE,
        "minimum_immutable_actual_round_trips": MIN_ACTUAL_ROUND_TRIPS,
        "actual_round_trip_net_return": "strictly_positive",
        "actual_round_trip_realized_pnl": "strictly_positive",
        "minimum_actual_round_trip_profit_factor": format(
            MIN_HOLDOUT_PROFIT_FACTOR, ".2f"),
        "maximum_actual_round_trip_drawdown": format(
            MAX_HOLDOUT_DRAWDOWN, ".2f"),
        "minimum_holdout_profit_factor": format(
            MIN_HOLDOUT_PROFIT_FACTOR, ".2f"),
        "maximum_holdout_drawdown": format(MAX_HOLDOUT_DRAWDOWN, ".2f"),
        "minimum_holdout_transitions": MIN_HOLDOUT_TRANSITIONS,
        "challenger_and_excess_holdout_return": "strictly_positive",
        "safety_violations": 0,
    },
    "selection": (
        "development_net_return_then_profit_factor_then_lower_drawdown_then_"
        "threshold_closest_to_incumbent"
    ),
    "freeze_contract": (
        "immutable_prefix_optional_single_prebind_embargo_and_strictly_"
        "post_freeze_suffix"
    ),
    "maximum_prebind_embargo_samples": 1,
    "pre_registration_embargo_provenance": (
        "false_oos_boundary_allowed_only_after_store_verified_historical_seed"
    ),
    "deployment": "proposal_only_manual_review_required",
}

_DAILY_REQUIRED = {
    "sample_id", "decision_ts", "label_available_ts", "momentum",
    "forward_return", "closed", "out_of_sample",
    "true_forward_after_freeze",
}
_DAILY_OPTIONAL = {"active_target_long", "action", "freeze_id"}
_TRIP_REQUIRED = {
    "round_trip_id", "entry_ts", "exit_ts", "entry_client_id",
    "exit_client_id", "entry_cost_usdt", "exit_proceeds_usdt",
    "realized_pnl_usdt", "net_return",
    "environment", "symbol", "freeze_id", "closed",
}
_FROZEN_REQUIRED = {
    "schema", "kind", "learner_id", "learner_version",
    "incumbent_threshold", "challenger_threshold", "freeze_cutoff_ts",
    "development_sample_count", "development_dataset_version",
    "development_first_hash", "development_last_hash",
    "one_way_transition_cost", "requires_manual_review", *_SAFE_FLAGS.keys(),
}
_HASH_FIELD = "immutable_sha256"
_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


LEARNER_VERSION = hashlib.sha256(_canonical_bytes(_SPEC)).hexdigest()


def _format_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Numeric fields must be finite.")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _metric_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Calculated metric is not finite.")
    with localcontext(_DECIMAL_CONTEXT):
        rounded = +value
        # Quantize ordinary metrics to twelve decimal places. Very large finite
        # values retain the fixed-context significant-digit rounding instead
        # of requiring a data-dependent precision.
        if rounded == 0 or rounded.copy_abs().adjusted() + 13 <= DECIMAL_PRECISION:
            rounded = rounded.quantize(Decimal("0.000000000001"))
    return _format_decimal(rounded)


def _decimal_text(value: object, field: str) -> tuple[Decimal, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a canonical decimal string.")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} must be a canonical decimal string.") from exc
    return number, _format_decimal(number)


def _safe_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must contain 1-128 safe ASCII characters.")
    return value


def _normalize_daily(sample: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(sample, Mapping):
        raise ValueError("Every daily sample must be a mapping.")
    keys = set(sample)
    missing = _DAILY_REQUIRED - keys
    unknown = keys - _DAILY_REQUIRED - _DAILY_OPTIONAL
    if missing:
        raise ValueError(f"Daily sample is missing fields: {sorted(missing)}.")
    if unknown:
        raise ValueError(f"Daily sample has unknown fields: {sorted(unknown)}.")
    sample_id = _safe_id(sample["sample_id"], "sample_id")
    decision_ts, label_ts = sample["decision_ts"], sample["label_available_ts"]
    if (isinstance(decision_ts, bool) or not isinstance(decision_ts, int)
            or decision_ts <= 0 or isinstance(label_ts, bool)
            or not isinstance(label_ts, int) or label_ts - decision_ts != DAY_MS):
        raise ValueError(f"Every daily label must span exactly DAY_MS={DAY_MS}.")
    if sample["closed"] is not True:
        raise ValueError("Only finalized daily samples with closed=true are accepted.")
    if not isinstance(sample["out_of_sample"], bool):
        raise ValueError("out_of_sample must be boolean.")
    if not isinstance(sample["true_forward_after_freeze"], bool):
        raise ValueError("true_forward_after_freeze must be boolean.")
    momentum, momentum_text = _decimal_text(sample["momentum"], "momentum")
    forward_return, return_text = _decimal_text(
        sample["forward_return"], "forward_return")
    if not Decimal("-1") < momentum <= Decimal("100"):
        raise ValueError("momentum is outside the accepted finite range.")
    if not Decimal("-1") < forward_return <= Decimal("100"):
        raise ValueError("forward_return is outside the accepted finite range.")
    is_true = sample["true_forward_after_freeze"]
    freeze_id = sample.get("freeze_id")
    if is_true:
        if not sample["out_of_sample"]:
            raise ValueError("A post-freeze true-forward label must be out of sample.")
        if not isinstance(freeze_id, str) or not _HASH_PATTERN.fullmatch(freeze_id):
            raise ValueError("Post-freeze labels require a valid freeze_id.")
    elif freeze_id is not None:
        raise ValueError("Pre-freeze labels must not carry a freeze_id.")
    normalized: dict[str, object] = {
        "sample_id": sample_id,
        "decision_ts": decision_ts,
        "label_available_ts": label_ts,
        "momentum": momentum_text,
        "forward_return": return_text,
        "closed": True,
        "out_of_sample": sample["out_of_sample"],
        "true_forward_after_freeze": is_true,
    }
    if freeze_id is not None:
        normalized["freeze_id"] = freeze_id
    if "active_target_long" in sample:
        if not isinstance(sample["active_target_long"], bool):
            raise ValueError("active_target_long must be boolean when supplied.")
        normalized["active_target_long"] = sample["active_target_long"]
    if "action" in sample:
        action = sample["action"]
        if (not isinstance(action, str) or not action or len(action) > 64
                or any(ord(character) < 32 for character in action)):
            raise ValueError("action must be a printable string up to 64 chars.")
        normalized["action"] = action
    return normalized


def seal_sample(sample: Mapping[str, object]) -> dict[str, object]:
    """Canonicalize and hash one finalized daily causal label."""
    if _HASH_FIELD in sample:
        raise ValueError("seal_sample expects an unhashed payload.")
    payload = _normalize_daily(sample)
    return dict(payload, immutable_sha256=hashlib.sha256(
        _canonical_bytes(payload)).hexdigest())


@dataclass(frozen=True)
class _Daily:
    payload: Mapping[str, object]
    momentum: Decimal
    forward_return: Decimal


def _verify_sealed(raw: Mapping[str, object], normalizer, name: str) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"Every {name} must be a mapping.")
    supplied = raw.get(_HASH_FIELD)
    if not isinstance(supplied, str) or not _HASH_PATTERN.fullmatch(supplied):
        raise ValueError(f"{name} immutable_sha256 is invalid.")
    payload_raw = {key: value for key, value in raw.items() if key != _HASH_FIELD}
    normalized = normalizer(payload_raw)
    if payload_raw != normalized:
        raise ValueError(f"Sealed {name} payload is not canonical.")
    expected = hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError(f"{name} immutable_sha256 does not match its payload.")
    return dict(normalized, immutable_sha256=supplied)


def _validate_samples(samples: Sequence[Mapping[str, object]]) -> tuple[_Daily, ...]:
    if isinstance(samples, (str, bytes)) or not isinstance(samples, Sequence):
        raise ValueError("samples must be a chronological sequence.")
    result: list[_Daily] = []
    ids: set[str] = set()
    hashes: set[str] = set()
    previous_label: int | None = None
    oos_started = true_started = False
    for raw in samples:
        payload = _verify_sealed(raw, _normalize_daily, "daily sample")
        sample_id, digest = str(payload["sample_id"]), str(payload[_HASH_FIELD])
        if sample_id in ids or digest in hashes:
            raise ValueError("Duplicate daily sample detected.")
        ids.add(sample_id)
        hashes.add(digest)
        decision_ts = int(payload["decision_ts"])
        if previous_label is not None and decision_ts != previous_label:
            raise ValueError("Daily samples must be exactly contiguous without gaps.")
        previous_label = int(payload["label_available_ts"])
        is_oos = bool(payload["out_of_sample"])
        is_true = bool(payload["true_forward_after_freeze"])
        if is_oos:
            oos_started = True
        elif oos_started:
            raise ValueError("Out-of-sample labels must form a suffix.")
        if is_true:
            true_started = True
        elif true_started:
            raise ValueError("Post-freeze true-forward labels must form a suffix.")
        result.append(_Daily(
            payload, Decimal(str(payload["momentum"])),
            Decimal(str(payload["forward_return"]))))
    return tuple(result)


def _dataset_version(samples: Sequence[_Daily]) -> str:
    return hashlib.sha256(_canonical_bytes(
        [sample.payload[_HASH_FIELD] for sample in samples])).hexdigest()


def _threshold_text(value: Decimal) -> str:
    return format(value, ".2f")


def _strategy_metric_values(
        samples: Sequence[_Daily], threshold: Decimal) -> dict[str, object]:
    """Calculate exact values used for selection and review gates.

    These values are deliberately kept separate from the rounded strings in
    reports so display quantization can never change a decision at a gate
    boundary.
    """
    with localcontext(_DECIMAL_CONTEXT):
        equity = peak = Decimal("1")
        positive = negative = max_drawdown = Decimal("0")
        position = transitions = entries = exits = exposed = wins = losses = 0
        for sample in samples:
            target = int(sample.momentum > threshold)
            change = abs(target - position)
            if change:
                transitions += change
                entries += int(position == 0 and target == 1)
                exits += int(position == 1 and target == 0)
            factor = Decimal("1") - ONE_WAY_TRANSITION_COST * change
            if target:
                factor *= Decimal("1") + sample.forward_return
            equity_before = equity
            equity *= factor
            period_pnl = equity - equity_before
            if period_pnl > 0:
                positive += period_pnl
                wins += 1
            elif period_pnl < 0:
                negative += -period_pnl
                losses += 1
            exposed += target
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, Decimal("1") - equity / peak)
            position = target
        if position:
            transitions += 1
            exits += 1
            equity_before = equity
            equity *= Decimal("1") - ONE_WAY_TRANSITION_COST
            negative += equity_before - equity
            losses += 1
            max_drawdown = max(max_drawdown, Decimal("1") - equity / peak)
        return {
            "sample_count": len(samples),
            "net_return": equity - Decimal("1"),
            "profit_factor": None if negative == 0 else positive / negative,
            "profit_factor_no_losses": negative == 0 and positive > 0,
            "profit_factor_basis": "compounded_equity_pnl",
            "max_drawdown": max_drawdown,
            "transitions": transitions,
            "completed_signal_round_trips": exits,
            "entries": entries,
            "exposed_samples": exposed,
            "winning_periods": wins,
            "losing_periods": losses,
            "one_way_transition_cost": ONE_WAY_TRANSITION_COST,
        }


def _serialize_strategy_metrics(
        values: Mapping[str, object]) -> dict[str, object]:
    """Render exact strategy metrics as stable, display-only report fields."""
    return {
        **values,
        "net_return": _metric_decimal(values["net_return"]),
        "profit_factor": (
            None if values["profit_factor"] is None
            else _metric_decimal(values["profit_factor"])
        ),
        "max_drawdown": _metric_decimal(values["max_drawdown"]),
        "one_way_transition_cost": format(
            values["one_way_transition_cost"], ".3f"),
    }


def _metrics(samples: Sequence[_Daily], threshold: Decimal) -> dict[str, object]:
    """Return serialized metrics for reports and compatibility with callers."""
    return _serialize_strategy_metrics(
        _strategy_metric_values(samples, threshold))


def _profit_factor_at_least(metrics: Mapping[str, object], minimum: Decimal) -> bool:
    if metrics["profit_factor_no_losses"]:
        return_value = metrics.get(
            "net_return", metrics.get("compounded_net_return", Decimal("0")))
        return return_value > 0
    value = metrics["profit_factor"]
    return value is not None and value >= minimum


def _evaluate_development_exact(
        samples: Sequence[_Daily], threshold: Decimal
) -> tuple[dict[str, object], dict[str, object]]:
    edges = [index * len(samples) // CHRONOLOGICAL_FOLDS
             for index in range(CHRONOLOGICAL_FOLDS + 1)]
    if any(edges[index] == edges[index + 1]
           for index in range(CHRONOLOGICAL_FOLDS)):
        raise ValueError("Development set is too short for chronological folds.")
    aggregate_values = _strategy_metric_values(samples, threshold)
    aggregate = _serialize_strategy_metrics(aggregate_values)
    folds = []
    fold_values = []
    for start, end in zip(edges[:-1], edges[1:]):
        subset = samples[start:end]
        values = _strategy_metric_values(subset, threshold)
        fold_values.append(values)
        folds.append({
            "first_sample_id": subset[0].payload["sample_id"],
            "last_sample_id": subset[-1].payload["sample_id"],
            "metrics": _serialize_strategy_metrics(values),
        })
    positive_folds = sum(
        item["net_return"] > 0 for item in fold_values)
    checks = {
        "net_return_positive": aggregate_values["net_return"] > 0,
        "profit_factor_at_least_1_05": _profit_factor_at_least(
            aggregate_values, Decimal("1.05")),
        "max_drawdown_at_most_25_percent": (
            aggregate_values["max_drawdown"] <= Decimal("0.25")),
        "transitions_at_least_4": (
            int(aggregate_values["transitions"])
            >= MIN_DEVELOPMENT_TRANSITIONS),
        "positive_chronological_folds_at_least_2_of_3": positive_folds >= 2,
    }
    report = {
        "threshold": _threshold_text(threshold),
        "aggregate": aggregate,
        "chronological_folds": folds,
        "positive_fold_count": positive_folds,
        "training_gate_checks": checks,
        "training_eligible": all(checks.values()),
    }
    return report, aggregate_values


def _evaluate_development(
        samples: Sequence[_Daily], threshold: Decimal) -> dict[str, object]:
    return _evaluate_development_exact(samples, threshold)[0]


def _selection_key(item: Mapping[str, object], incumbent: Decimal,
                   metrics: Mapping[str, object]):
    with localcontext(_DECIMAL_CONTEXT):
        profit_factor = (Decimal("Infinity") if metrics["profit_factor_no_losses"]
                         else metrics["profit_factor"] or Decimal("0"))
        threshold = Decimal(str(item["threshold"]))
        return (metrics["net_return"], profit_factor,
                -metrics["max_drawdown"],
                -abs(threshold - incumbent), threshold)


def _select(samples: Sequence[_Daily], incumbent: Decimal):
    evaluated = [_evaluate_development_exact(samples, threshold)
                 for threshold in CANDIDATE_THRESHOLDS]
    evaluations = [item[0] for item in evaluated]
    eligible = [item for item in evaluated if item[0]["training_eligible"]]
    selected = (max(
        eligible,
        key=lambda item: _selection_key(item[0], incumbent, item[1]),
    )[0] if eligible else None)
    return evaluations, selected


def _validate_incumbent(value: object) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("incumbent_threshold must be pre-registered.")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("incumbent_threshold must be pre-registered.") from exc
    if not result.is_finite() or result not in CANDIDATE_THRESHOLDS:
        raise ValueError("incumbent_threshold is outside the pre-registered grid.")
    return result


def _validate_violations(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError("safety_violations must be a sequence of strings.")
    result = []
    for value in values:
        if (not isinstance(value, str) or not value or value != value.strip()
                or len(value) > 256):
            raise ValueError("Safety violations must be short non-empty strings.")
        result.append(value)
    if len(result) != len(set(result)):
        raise ValueError("Duplicate safety violations are not accepted.")
    return tuple(sorted(result))


def _frozen_payload(development: Sequence[_Daily], incumbent: Decimal,
                    challenger: Decimal) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "kind": "frozen_testnet_online_challenger",
        "learner_id": LEARNER_ID,
        "learner_version": LEARNER_VERSION,
        "incumbent_threshold": _threshold_text(incumbent),
        "challenger_threshold": _threshold_text(challenger),
        "freeze_cutoff_ts": int(development[-1].payload["label_available_ts"]),
        "development_sample_count": len(development),
        "development_dataset_version": _dataset_version(development),
        "development_first_hash": development[0].payload[_HASH_FIELD],
        "development_last_hash": development[-1].payload[_HASH_FIELD],
        "one_way_transition_cost": format(ONE_WAY_TRANSITION_COST, ".3f"),
        "requires_manual_review": True,
        **_SAFE_FLAGS,
    }


def _seal_frozen(payload: Mapping[str, object]) -> dict[str, object]:
    return dict(payload, immutable_sha256=hashlib.sha256(
        _canonical_bytes(payload)).hexdigest())


def _validate_frozen(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("frozen_candidate must be a mapping.")
    supplied = value.get(_HASH_FIELD)
    payload = {key: item for key, item in value.items() if key != _HASH_FIELD}
    if set(payload) != _FROZEN_REQUIRED:
        raise ValueError("Frozen candidate fields do not match the contract.")
    if (not isinstance(supplied, str) or not _HASH_PATTERN.fullmatch(supplied)
            or not hmac.compare_digest(supplied, hashlib.sha256(
                _canonical_bytes(payload)).hexdigest())):
        raise ValueError("Frozen candidate immutable_sha256 does not match.")
    if (payload["schema"] != SCHEMA
            or payload["kind"] != "frozen_testnet_online_challenger"
            or payload["learner_id"] != LEARNER_ID
            or payload["learner_version"] != LEARNER_VERSION
            or payload["one_way_transition_cost"]
            != format(ONE_WAY_TRANSITION_COST, ".3f")
            or payload["requires_manual_review"] is not True):
        raise ValueError("Frozen candidate contract/version is incompatible.")
    if any(payload[key] is not False for key in _SAFE_FLAGS):
        raise ValueError("Frozen candidate must keep activation flags false.")
    incumbent = _validate_incumbent(payload["incumbent_threshold"])
    challenger = _validate_incumbent(payload["challenger_threshold"])
    if challenger == incumbent:
        raise ValueError("Frozen challenger must differ from the incumbent.")
    if (isinstance(payload["freeze_cutoff_ts"], bool)
            or not isinstance(payload["freeze_cutoff_ts"], int)
            or payload["freeze_cutoff_ts"] <= 0
            or isinstance(payload["development_sample_count"], bool)
            or not isinstance(payload["development_sample_count"], int)
            or payload["development_sample_count"] < MIN_TRAINING_SAMPLES):
        raise ValueError("Frozen candidate cutoff/sample count is invalid.")
    for key in ("development_dataset_version", "development_first_hash",
                "development_last_hash"):
        if not isinstance(payload[key], str) or not _HASH_PATTERN.fullmatch(payload[key]):
            raise ValueError(f"Frozen candidate {key} is invalid.")
    return dict(payload, immutable_sha256=supplied)


def _normalize_trip(record: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(record, Mapping):
        raise ValueError("Every round trip must be a mapping.")
    missing, unknown = _TRIP_REQUIRED - set(record), set(record) - _TRIP_REQUIRED
    if missing:
        raise ValueError(f"Round trip is missing fields: {sorted(missing)}.")
    if unknown:
        raise ValueError(f"Round trip has unknown fields: {sorted(unknown)}.")
    trip_id = _safe_id(record["round_trip_id"], "round_trip_id")
    entry_client = _safe_id(record["entry_client_id"], "entry_client_id")
    exit_client = _safe_id(record["exit_client_id"], "exit_client_id")
    if entry_client == exit_client:
        raise ValueError("Round-trip entry and exit client IDs must differ.")
    entry_ts, exit_ts = record["entry_ts"], record["exit_ts"]
    if (isinstance(entry_ts, bool) or not isinstance(entry_ts, int)
            or isinstance(exit_ts, bool) or not isinstance(exit_ts, int)
            or entry_ts <= 0 or exit_ts <= entry_ts):
        raise ValueError("Round-trip timestamps are invalid.")
    if record["environment"] != "binance_spot_testnet":
        raise ValueError("Only Binance Spot Testnet round trips are accepted.")
    if record["symbol"] != "BTCUSDT":
        raise ValueError("Only BTCUSDT round trips are accepted.")
    if record["closed"] is not True:
        raise ValueError("Only closed round trips are accepted.")
    freeze_id = record["freeze_id"]
    if not isinstance(freeze_id, str) or not _HASH_PATTERN.fullmatch(freeze_id):
        raise ValueError("Round trip freeze_id is invalid.")
    numeric: dict[str, Decimal] = {}
    texts: dict[str, str] = {}
    for field in ("entry_cost_usdt", "exit_proceeds_usdt",
                  "realized_pnl_usdt", "net_return"):
        numeric[field], texts[field] = _decimal_text(record[field], field)
    if numeric["entry_cost_usdt"] <= 0 or numeric["exit_proceeds_usdt"] < 0:
        raise ValueError("Round-trip cost/proceeds are invalid.")
    if any(abs(value) > Decimal("1000000000") for value in numeric.values()):
        raise ValueError("Round-trip numeric value is outside the accepted range.")
    # Both values are cash-accounting amounts: entry cost includes BUY fees and
    # exit proceeds are net of SELL fees. This matches the execution ledger's
    # exact realized-PnL reconciliation without estimating fee conversions.
    with localcontext(_DECIMAL_CONTEXT):
        expected_pnl = (
            numeric["exit_proceeds_usdt"] - numeric["entry_cost_usdt"])
        expected_return = _metric_decimal(
            expected_pnl / numeric["entry_cost_usdt"])
    if numeric["realized_pnl_usdt"] != expected_pnl:
        raise ValueError("Round-trip realized PnL does not reconcile exactly.")
    if texts["net_return"] != expected_return:
        raise ValueError("Round-trip net return does not reconcile to PnL/cost.")
    if not Decimal("-1") < numeric["net_return"] <= Decimal("100"):
        raise ValueError("Round-trip net return is outside the accepted range.")
    return {
        "round_trip_id": trip_id,
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "entry_client_id": entry_client,
        "exit_client_id": exit_client,
        **texts,
        "environment": "binance_spot_testnet",
        "symbol": "BTCUSDT",
        "freeze_id": freeze_id,
        "closed": True,
    }


def seal_round_trip(record: Mapping[str, object]) -> dict[str, object]:
    """Reconcile, canonicalize, and hash an actual closed Testnet round trip."""
    if _HASH_FIELD in record:
        raise ValueError("seal_round_trip expects an unhashed payload.")
    payload = _normalize_trip(record)
    return dict(payload, immutable_sha256=hashlib.sha256(
        _canonical_bytes(payload)).hexdigest())


def _validate_round_trips(records: Sequence[Mapping[str, object]],
                          frozen: Mapping[str, object],
                          latest_label_ts: int | None):
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise ValueError("actual_round_trips must be a sequence.")
    result, ids, client_ids, hashes = [], set(), set(), set()
    previous_exit = None
    for raw in records:
        payload = _verify_sealed(raw, _normalize_trip, "round trip")
        if payload["round_trip_id"] in ids or payload[_HASH_FIELD] in hashes:
            raise ValueError("Duplicate actual round trip detected.")
        ids.add(payload["round_trip_id"])
        hashes.add(payload[_HASH_FIELD])
        for key in ("entry_client_id", "exit_client_id"):
            if payload[key] in client_ids:
                raise ValueError("A client order ID appears in multiple round trips.")
            client_ids.add(payload[key])
        if payload["freeze_id"] != frozen[_HASH_FIELD]:
            raise ValueError("Round trip is not bound to the frozen candidate.")
        if int(payload["entry_ts"]) <= int(frozen["freeze_cutoff_ts"]):
            raise ValueError("Round trip must begin strictly after the freeze cutoff.")
        if latest_label_ts is None or int(payload["exit_ts"]) > latest_label_ts:
            raise ValueError("Round trip extends beyond finalized daily evidence.")
        if previous_exit is not None and int(payload["entry_ts"]) < previous_exit:
            raise ValueError("Actual round trips must be chronological and non-overlapping.")
        previous_exit = int(payload["exit_ts"])
        result.append(payload)
    return tuple(result)


def _actual_trip_metric_values(
        trips: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Calculate exact actual-outcome values used by review gates."""
    with localcontext(_DECIMAL_CONTEXT):
        geometric_equity = Decimal("1")
        positive_pnl = negative_pnl = total_pnl = total_cost = Decimal("0")
        entry_costs = [Decimal(str(trip["entry_cost_usdt"])) for trip in trips]
        cash_equity = max(entry_costs, default=Decimal("0"))
        starting_cash = cash_peak = cash_equity
        max_drawdown = Decimal("0")
        wins = losses = 0
        for trip in trips:
            pnl = Decimal(str(trip["realized_pnl_usdt"]))
            cost = Decimal(str(trip["entry_cost_usdt"]))
            # The sealed per-trip return is a canonical 12-decimal evidence
            # field. Gate math derives the return again from the exact cash
            # fields so that evidence serialization cannot flip a zero boundary.
            net_return = pnl / cost
            geometric_equity *= Decimal("1") + net_return
            total_pnl += pnl
            total_cost += cost
            cash_equity += pnl
            if pnl > 0:
                positive_pnl += pnl
                wins += 1
            elif pnl < 0:
                negative_pnl += -pnl
                losses += 1
            cash_peak = max(cash_peak, cash_equity)
            drawdown = (Decimal("1") - cash_equity / cash_peak
                        if cash_peak > 0 else Decimal("1"))
            max_drawdown = max(max_drawdown, drawdown)
        return {
            "count": len(trips),
            "compounded_net_return": geometric_equity - Decimal("1"),
            "compounded_return_basis": "sequential_per_trip_net_returns",
            "total_realized_pnl_usdt": total_pnl,
            "total_entry_cost_usdt": total_cost,
            "capital_weighted_net_return": (
                total_pnl / total_cost if total_cost > 0 else Decimal("0")),
            "profit_factor": (None if negative_pnl == 0
                              else positive_pnl / negative_pnl),
            "profit_factor_no_losses": negative_pnl == 0 and positive_pnl > 0,
            "profit_factor_basis": "realized_pnl_usdt",
            "max_drawdown": max_drawdown,
            "max_drawdown_basis": "cash_pnl_curve_starting_at_max_entry_cost",
            "drawdown_starting_cash_usdt": starting_cash,
            "winning_round_trips": wins,
            "losing_round_trips": losses,
        }


def _serialize_actual_trip_metrics(
        values: Mapping[str, object]) -> dict[str, object]:
    """Render exact actual-outcome values as display-only report fields."""
    result = dict(values)
    for key in (
        "compounded_net_return",
        "total_realized_pnl_usdt",
        "total_entry_cost_usdt",
        "capital_weighted_net_return",
        "max_drawdown",
        "drawdown_starting_cash_usdt",
    ):
        result[key] = _metric_decimal(values[key])
    result["profit_factor"] = (
        None if values["profit_factor"] is None
        else _metric_decimal(values["profit_factor"])
    )
    return result


def _actual_trip_metrics(trips: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Summarize immutable actual outcomes for serialized reports."""
    return _serialize_actual_trip_metrics(_actual_trip_metric_values(trips))


def _base_report(samples: Sequence[_Daily], incumbent: Decimal,
                 violations: tuple[str, ...]) -> dict[str, object]:
    dataset_version = _dataset_version(samples)
    return {
        "schema": SCHEMA,
        "kind": "testnet_online_learning_report",
        "learner_id": LEARNER_ID,
        "learner_version": LEARNER_VERSION,
        "report_version": None,
        "dataset_version": dataset_version,
        "incumbent_threshold": _threshold_text(incumbent),
        "contract": json.loads(_canonical_bytes(_SPEC).decode("utf-8")),
        "safety_violations": list(violations),
        "evaluations": [],
        "development": None,
        "frozen_candidate": None,
        "prebind_embargo": None,
        "untouched_true_forward": None,
        "matched_holdout_comparison": None,
        "actual_round_trip_evidence": None,
        "review_gate_checks": {},
        "proposal_ready_for_review": False,
        **_SAFE_FLAGS,
    }


def _finalize_report(report: dict[str, object]) -> dict[str, object]:
    """Bind report_version to every final field except report_version itself."""
    content = dict(report)
    content.pop("report_version", None)
    report["report_version"] = hashlib.sha256(_canonical_bytes(content)).hexdigest()
    return report


def learn(samples: Sequence[Mapping[str, object]], *,
          incumbent_threshold: str | Decimal = "0.10",
          frozen_candidate: Mapping[str, object] | None = None,
          actual_round_trips: Sequence[Mapping[str, object]] = (),
          safety_violations: Sequence[str] = (),
          verified_pre_registration_embargo: bool = False,
          ) -> dict[str, object]:
    """Freeze a challenger, or evaluate it on strictly future evidence."""
    if not isinstance(verified_pre_registration_embargo, bool):
        raise TypeError("verified_pre_registration_embargo must be boolean.")
    daily = _validate_samples(samples)
    incumbent = _validate_incumbent(incumbent_threshold)
    violations = _validate_violations(safety_violations)

    if frozen_candidate is None:
        if actual_round_trips:
            raise ValueError("Round-trip evidence requires a frozen candidate.")
        if any(item.payload["true_forward_after_freeze"] for item in daily):
            raise ValueError("Cannot fit with post-freeze true-forward samples.")
        report = _base_report(daily, incumbent, violations)
        if violations:
            report["status"] = "blocked_by_safety_violation"
            report["review_gate_checks"] = {
                "no_safety_violations": False,
                "challenger_freeze_allowed": False,
            }
            return _finalize_report(report)
        if len(daily) < MIN_TRAINING_SAMPLES:
            report["status"] = "collecting_development_labels"
            report["review_gate_checks"] = {
                "development_samples_at_least_60": False,
                "no_safety_violations": not violations,
            }
            return _finalize_report(report)
        evaluations, selected = _select(daily, incumbent)
        report["evaluations"] = evaluations
        report["development"] = {
            "role": "threshold_selection_not_untouched_performance_evidence",
            "sample_count": len(daily),
            "first_sample_id": daily[0].payload["sample_id"],
            "last_sample_id": daily[-1].payload["sample_id"],
            "dataset_version": _dataset_version(daily),
            "selection_uses_future_evidence": False,
            "selected_threshold": None if selected is None else selected["threshold"],
        }
        if selected is None:
            report["status"] = "no_viable_challenger"
            return _finalize_report(report)
        challenger = Decimal(str(selected["threshold"]))
        if challenger == incumbent:
            report["status"] = "incumbent_retained"
            return _finalize_report(report)
        artifact = _seal_frozen(_frozen_payload(daily, incumbent, challenger))
        report["status"] = "candidate_frozen_awaiting_true_forward"
        report["frozen_candidate"] = artifact
        report["review_gate_checks"] = {
            "development_samples_at_least_60": True,
            "development_candidate_found": True,
            "challenger_differs_from_incumbent": True,
            "no_safety_violations": not violations,
            "post_freeze_evidence_evaluated": False,
        }
        return _finalize_report(report)

    frozen = _validate_frozen(frozen_candidate)
    if frozen["incumbent_threshold"] != _threshold_text(incumbent):
        raise ValueError("Frozen candidate incumbent does not match evaluation.")
    development_count = int(frozen["development_sample_count"])
    if len(daily) < development_count:
        raise ValueError("Daily evidence is missing the frozen development prefix.")
    development, evaluation_tail = (
        daily[:development_count], daily[development_count:]
    )
    if (_dataset_version(development) != frozen["development_dataset_version"]
            or development[0].payload[_HASH_FIELD] != frozen["development_first_hash"]
            or development[-1].payload[_HASH_FIELD] != frozen["development_last_hash"]
            or int(development[-1].payload["label_available_ts"])
            != int(frozen["freeze_cutoff_ts"])):
        raise ValueError("Frozen development prefix was changed or replaced.")
    if any(item.payload["true_forward_after_freeze"] for item in development):
        raise ValueError("Frozen development prefix contains post-freeze labels.")
    freeze_id = str(frozen[_HASH_FIELD])
    embargo: tuple[_Daily, ...] = ()
    if (
        evaluation_tail
        and not evaluation_tail[0].payload["true_forward_after_freeze"]
    ):
        candidate = evaluation_tail[0]
        cutoff = int(frozen["freeze_cutoff_ts"])
        if (
            int(candidate.payload["decision_ts"]) != cutoff
            or int(candidate.payload["label_available_ts"]) != cutoff + DAY_MS
            or (
                not candidate.payload["out_of_sample"]
                and not verified_pre_registration_embargo
            )
            or candidate.payload.get("freeze_id") is not None
        ):
            raise ValueError(
                "The optional pre-bind embargo sample does not match the freeze boundary."
            )
        embargo = (candidate,)
        evaluation_tail = evaluation_tail[1:]
    forward = evaluation_tail
    if any(not item.payload["true_forward_after_freeze"]
           or item.payload.get("freeze_id") != freeze_id for item in forward):
        raise ValueError("Every post-freeze sample must be bound to the artifact.")
    evaluations, selected = _select(development, incumbent)
    if selected is None or selected["threshold"] != frozen["challenger_threshold"]:
        raise ValueError("Frozen challenger no longer matches its development prefix.")
    latest_label_ts = (int(forward[-1].payload["label_available_ts"])
                       if forward else None)
    trips = _validate_round_trips(actual_round_trips, frozen, latest_label_ts)
    report = _base_report(daily, incumbent, violations)
    report["evaluations"] = evaluations
    report["development"] = {
        "role": "threshold_selection_not_untouched_performance_evidence",
        "sample_count": len(development),
        "first_sample_id": development[0].payload["sample_id"],
        "last_sample_id": development[-1].payload["sample_id"],
        "dataset_version": _dataset_version(development),
        "selection_uses_future_evidence": False,
        "selected_threshold": frozen["challenger_threshold"],
    }
    report["frozen_candidate"] = frozen
    report["prebind_embargo"] = {
        "role": "excluded_boundary_sample_created_before_source_binding",
        "sample_count": len(embargo),
        "sample_id": embargo[0].payload["sample_id"] if embargo else None,
        "immutable_sha256": (
            embargo[0].payload[_HASH_FIELD] if embargo else None
        ),
        "included_in_performance_metrics": False,
    }
    forward_digest = hashlib.sha256(_canonical_bytes(
        [item.payload[_HASH_FIELD] for item in forward])).hexdigest()
    report["untouched_true_forward"] = {
        "role": "untouched_challenger_performance_evidence",
        "sample_count": len(forward),
        "dataset_version": forward_digest,
        "first_sample_id": forward[0].payload["sample_id"] if forward else None,
        "last_sample_id": forward[-1].payload["sample_id"] if forward else None,
        "all_out_of_sample": all(
            bool(item.payload["out_of_sample"]) for item in forward),
        "freeze_id": freeze_id,
    }
    trip_metric_values = _actual_trip_metric_values(trips)
    trip_metrics = _serialize_actual_trip_metrics(trip_metric_values)
    report["actual_round_trip_evidence"] = {
        "role": "operational_execution_evidence_not_threshold_selection",
        "dataset_version": hashlib.sha256(_canonical_bytes(
            [item[_HASH_FIELD] for item in trips])).hexdigest(),
        "all_hash_and_arithmetic_checks_passed": True,
        **trip_metrics,
    }
    challenger = Decimal(str(frozen["challenger_threshold"]))
    challenger_metric_values, incumbent_metric_values = (
        _strategy_metric_values(forward, challenger),
        _strategy_metric_values(forward, incumbent),
    )
    challenger_metrics, incumbent_metrics = (
        _serialize_strategy_metrics(challenger_metric_values),
        _serialize_strategy_metrics(incumbent_metric_values),
    )
    challenger_net = challenger_metric_values["net_return"]
    incumbent_net = incumbent_metric_values["net_return"]
    with localcontext(_DECIMAL_CONTEXT):
        challenger_excess = challenger_net - incumbent_net
    report["matched_holdout_comparison"] = {
        "matched_sample_count": len(forward),
        "matched_dataset_version": forward_digest,
        "challenger_threshold": _threshold_text(challenger),
        "incumbent_threshold": _threshold_text(incumbent),
        "challenger": challenger_metrics,
        "incumbent": incumbent_metrics,
        "challenger_excess_net_return": _metric_decimal(challenger_excess),
    }
    oos_count = sum(bool(item.payload["out_of_sample"]) for item in daily)
    checks = {
        "development_candidate_was_frozen": True,
        "development_excludes_true_forward_suffix": True,
        "prebind_embargo_samples_at_most_1": len(embargo) <= 1,
        "true_forward_labels_at_least_60": (
            len(forward) >= MIN_TRUE_FORWARD_AFTER_FREEZE),
        "total_oos_daily_labels_at_least_200": (
            oos_count >= MIN_PROMOTION_OOS_SAMPLES),
        "true_forward_holdout_all_out_of_sample": all(
            bool(item.payload["out_of_sample"]) for item in forward),
        "immutable_reconciled_round_trips_at_least_8": (
            len(trips) >= MIN_ACTUAL_ROUND_TRIPS),
        "actual_round_trips_net_positive": (
            trip_metric_values["compounded_net_return"] > 0),
        "actual_round_trips_realized_pnl_positive": (
            trip_metric_values["total_realized_pnl_usdt"] > 0),
        "actual_round_trips_profit_factor_at_least_1_15": (
            _profit_factor_at_least(
                trip_metric_values, MIN_HOLDOUT_PROFIT_FACTOR)),
        "actual_round_trips_max_drawdown_at_most_15_percent": (
            trip_metric_values["max_drawdown"]
            <= MAX_HOLDOUT_DRAWDOWN),
        "challenger_holdout_net_positive_after_cost": challenger_net > 0,
        "challenger_holdout_profit_factor_at_least_1_15": (
            _profit_factor_at_least(
                challenger_metric_values, MIN_HOLDOUT_PROFIT_FACTOR)),
        "challenger_holdout_max_drawdown_at_most_15_percent": (
            challenger_metric_values["max_drawdown"]
            <= MAX_HOLDOUT_DRAWDOWN),
        "challenger_holdout_transitions_at_least_4": (
            int(challenger_metric_values["transitions"])
            >= MIN_HOLDOUT_TRANSITIONS),
        "challenger_outperforms_incumbent_on_holdout": challenger_net > incumbent_net,
        "no_safety_violations": not violations,
    }
    ready = all(checks.values())
    report["review_gate_checks"] = checks
    report["proposal_ready_for_review"] = ready
    if violations:
        status = "blocked_by_safety_violation"
    elif (len(forward) < MIN_TRUE_FORWARD_AFTER_FREEZE
          or oos_count < MIN_PROMOTION_OOS_SAMPLES
          or len(trips) < MIN_ACTUAL_ROUND_TRIPS):
        status = "collecting_review_evidence"
    elif not all((checks["actual_round_trips_net_positive"],
                  checks["actual_round_trips_realized_pnl_positive"],
                  checks["actual_round_trips_profit_factor_at_least_1_15"],
                  checks["actual_round_trips_max_drawdown_at_most_15_percent"])):
        status = "actual_execution_evidence_failed"
    elif ready:
        status = "proposal_ready_for_review"
    else:
        status = "challenger_failed_true_forward"
    report["status"] = status
    return _finalize_report(report)


__all__ = [
    "SCHEMA", "LEARNER_ID", "IMPLEMENTATION_REVISION", "LEARNER_VERSION", "DAY_MS",
    "CANDIDATE_THRESHOLDS", "ONE_WAY_TRANSITION_COST",
    "MIN_TRAINING_SAMPLES", "MIN_PROMOTION_OOS_SAMPLES",
    "MIN_TRUE_FORWARD_AFTER_FREEZE", "MIN_ACTUAL_ROUND_TRIPS",
    "seal_sample", "seal_round_trip", "learn",
]
