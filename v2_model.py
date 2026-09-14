"""Auditable offline meta-filter for :mod:`v2_engine`.

Training uses scikit-learn behind a lazy import.  Saved models contain only
JSON numbers and metadata, so paper inference needs the Python standard
library.  A model is always shadow-only until matching, prequential forward
records satisfy the v2 promotion contract.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import fmean
import tempfile
from typing import Mapping, Sequence

from v2_engine import (
    BAR_MS,
    MAIN_SPEC,
    POLICY,
    BarrierSpec,
    CostModel,
    EXECUTION_POLICY_VERSION,
    MIN_TARGET_NET_RETURN,
    dataset_digest,
    generate_labeled_samples,
    indicators,
    promotion_gate,
    purged_expanding_walk_forward,
    round_trip_net_return,
    validate_rows,
)


MODEL_SCHEMA = 1
MODEL_KIND = "bollinger_15m_v2_logistic_meta_filter"
FEATURE_VERSION = "causal-features-v1"
FORWARD_SAMPLE_SOURCE = "true_forward_shadow"
FORWARD_STREAM_ID = "bitstamp-btcusd-live-v2"
TRAINING_SEED = 15_008
TRAINING_PROTOCOL_VERSION = "purged-expanding-v2:inner-embargo=1bar:outer-embargo=1bar"
REGULARIZATION_GRID = (0.05, 0.2, 1.0)
THRESHOLD_GRID = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75)

FEATURE_NAMES = (
    "bb_percent_b",
    "bb_bandwidth",
    "bb_bandwidth_change",
    "bb_middle_slope_1",
    "bb_middle_slope_4",
    "atr_fraction",
    "rsi14",
    "relative_volume20",
    "return_1",
    "return_4",
    "return_8",
    "return_16",
    "sma200_distance",
    "sma200_slope_8",
    "candle_body_fraction",
    "candle_range_fraction",
    "is_breakout",
)

COST_SCENARIOS = {
    "30bp": CostModel(fee_each_side=0.001, slippage_each_side=0.0005),
    "40bp": CostModel(fee_each_side=0.001, slippage_each_side=0.0010),
    "60bp": CostModel(fee_each_side=0.001, slippage_each_side=0.0020),
}


def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _assert_finite_json(value: object, path: str = "root") -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
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
    raise ValueError(f"Unsupported JSON value at {path}: {type(value).__name__}.")


def _rsi14(rows: Sequence[Mapping[str, object]]) -> list[float | None]:
    closes = [float(row["close"]) for row in rows]
    result: list[float | None] = [None] * len(rows)
    if len(rows) <= 14:
        return result
    gains = [max(closes[index] - closes[index - 1], 0.0) for index in range(1, len(rows))]
    losses = [max(closes[index - 1] - closes[index], 0.0) for index in range(1, len(rows))]
    average_gain = sum(gains[:14]) / 14
    average_loss = sum(losses[:14]) / 14
    result[14] = 1.0 if average_loss == 0 else 1.0 - 1.0 / (1.0 + average_gain / average_loss)
    for index in range(15, len(rows)):
        average_gain = (average_gain * 13 + gains[index - 1]) / 14
        average_loss = (average_loss * 13 + losses[index - 1]) / 14
        result[index] = 1.0 if average_loss == 0 else 1.0 - 1.0 / (1.0 + average_gain / average_loss)
    return result


def _feature_context(
    rows: Sequence[Mapping[str, object]], spec: BarrierSpec = MAIN_SPEC
) -> dict[str, Sequence[float | None]]:
    validate_rows(rows)
    context = dict(indicators(rows, spec))
    context["rsi14"] = _rsi14(rows)
    return context


def _vector_from_context(
    rows: Sequence[Mapping[str, object]],
    decision_index: int,
    strategy: str,
    context: Mapping[str, Sequence[float | None]],
) -> list[float]:
    # SMA200 slope over eight bars needs the current 200-bar mean plus the
    # corresponding mean eight bars earlier: indices 0..207 at minimum.
    if decision_index < 207 or decision_index >= len(rows):
        raise ValueError("A feature vector needs at least 208 completed candles.")
    i = decision_index
    close = float(rows[i]["close"])
    open_price = float(rows[i]["open"])
    middle = context["middle"]
    upper = context["upper"]
    lower = context["lower"]
    bandwidth = context["bandwidth"]
    atr = context["atr"]
    regime = context["regime"]
    required = [
        middle[i],
        middle[i - 1],
        middle[i - 4],
        upper[i],
        lower[i],
        bandwidth[i],
        bandwidth[i - 1],
        atr[i],
        regime[i],
        regime[i - 8],
        context["rsi14"][i],
    ]
    if any(value is None or not _is_number(value) for value in required):
        raise ValueError("Indicators are unavailable or non-finite at the decision candle.")
    width = float(upper[i]) - float(lower[i])
    if width <= 0 or close <= 0 or open_price <= 0 or float(middle[i]) <= 0 or float(regime[i]) <= 0:
        raise ValueError("Feature denominators must be positive.")
    volume_window = [float(row["volume"]) for row in rows[i - 19 : i + 1]]
    average_volume = fmean(volume_window)
    relative_volume = float(rows[i]["volume"]) / average_volume if average_volume > 0 else 0.0

    def price_return(bars: int) -> float:
        earlier = float(rows[i - bars]["close"])
        if earlier <= 0:
            raise ValueError("Historical close must be positive.")
        return close / earlier - 1.0

    previous_bandwidth = float(bandwidth[i - 1])
    vector = [
        (close - float(lower[i])) / width,
        float(bandwidth[i]),
        float(bandwidth[i]) / previous_bandwidth - 1.0 if previous_bandwidth > 0 else 0.0,
        float(middle[i]) / float(middle[i - 1]) - 1.0,
        float(middle[i]) / float(middle[i - 4]) - 1.0,
        float(atr[i]) / close,
        float(context["rsi14"][i]),
        relative_volume,
        price_return(1),
        price_return(4),
        price_return(8),
        price_return(16),
        close / float(regime[i]) - 1.0,
        float(regime[i]) / float(regime[i - 8]) - 1.0,
        (close - open_price) / open_price,
        (float(rows[i]["high"]) - float(rows[i]["low"])) / close,
        1.0 if strategy == "breakout" else 0.0,
    ]
    if strategy not in {"breakout", "reentry"}:
        raise ValueError(f"Unknown v2 strategy: {strategy}.")
    if len(vector) != len(FEATURE_NAMES) or not all(_is_number(value) for value in vector):
        raise ValueError("Feature vector contains a non-finite value.")
    return vector


def causal_feature_vector(
    rows: Sequence[Mapping[str, object]],
    decision_index: int,
    strategy: str,
    spec: BarrierSpec = MAIN_SPEC,
) -> list[float]:
    """Return features known at decision candle close.

    The explicit prefix makes the causality contract easy to audit and test:
    mutations after ``decision_index`` cannot reach this calculation.
    """

    prefix = list(rows[: decision_index + 1])
    context = _feature_context(prefix, spec)
    return _vector_from_context(prefix, decision_index, strategy, context)


def build_historical_samples(
    rows: Sequence[Mapping[str, object]],
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = COST_SCENARIOS["30bp"],
) -> list[dict[str, object]]:
    """Generate H8 v2 triple-barrier labels and attach causal features."""

    if spec.horizon_bars != 8:
        raise ValueError("The v2 meta-filter training contract requires H8 labels.")
    validate_rows(rows)
    digest = dataset_digest(rows)
    labels = generate_labeled_samples(rows, spec, cost, data_hash=digest)
    by_timestamp = {int(row["ts"]): index for index, row in enumerate(rows)}
    context = _feature_context(rows, spec)
    enriched = []
    for label in labels:
        decision_index = by_timestamp[int(label["decision_ts"])]
        # Breakout signals can exist after 20 bars, while the model feature
        # contract deliberately includes the slower 200-bar regime.  Those
        # early labels remain valid engine research records but cannot train
        # this feature schema.
        if decision_index < 207:
            continue
        sample = dict(label)
        sample["x"] = _vector_from_context(rows, decision_index, str(label["strategy"]), context)
        sample["source"] = "historical_v2_h8"
        enriched.append(sample)
    return enriched


def _integer_timestamp(sample: Mapping[str, object], field: str, index: int) -> int:
    value = sample.get(field)
    if not _is_number(value) or int(value) != value:
        raise ValueError(f"Forward sample {index} has invalid {field}.")
    return int(value)


def _canonical_forward_sample(sample: Mapping[str, object], index: int) -> dict[str, object]:
    """Validate and canonicalize one immutable live-shadow training outcome.

    Forward observations are accepted only when their saved label and prediction
    metadata match the active H8 feature, signal, and cost contracts.  Keeping
    this boundary strict prevents an older strategy or a post-outcome score from
    silently entering a later retrain.
    """

    event_id = sample.get("id")
    strategy = sample.get("strategy")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError(f"Forward sample {index} has an invalid id.")
    if strategy not in {"breakout", "reentry"}:
        raise ValueError(f"Forward sample {index} has an invalid strategy.")

    decision_ts = _integer_timestamp(sample, "decision_ts", index)
    fill_ts = _integer_timestamp(sample, "fill_ts", index)
    exit_ts = _integer_timestamp(sample, "exit_ts", index)
    label_available_ts = _integer_timestamp(sample, "label_available_ts", index)
    if fill_ts != decision_ts + BAR_MS:
        raise ValueError(f"Forward sample {index} was not filled at t+1 open.")
    if not fill_ts <= exit_ts <= fill_ts + (MAIN_SPEC.horizon_bars - 1) * BAR_MS:
        raise ValueError(f"Forward sample {index} exits outside the H8 horizon.")
    if label_available_ts != exit_ts + BAR_MS:
        raise ValueError(f"Forward sample {index} has an invalid label availability time.")

    vector = sample.get("x")
    if not isinstance(vector, (list, tuple)) or len(vector) != len(FEATURE_NAMES):
        raise ValueError(f"Forward sample {index} has an invalid feature vector.")
    if not all(_is_number(value) for value in vector):
        raise ValueError(f"Forward sample {index} has a non-finite feature value.")

    entry_reference = sample.get("entry_reference")
    exit_reference = sample.get("exit_reference")
    target = sample.get("target")
    net_return = sample.get("net_return")
    if not all(_is_number(value) for value in (
            entry_reference, exit_reference, target, net_return)):
        raise ValueError(f"Forward sample {index} has invalid return fields.")
    if (float(entry_reference) <= 0 or float(exit_reference) <= 0
            or float(target) <= float(entry_reference)):
        raise ValueError(f"Forward sample {index} has invalid reference prices.")
    expected_return = round_trip_net_return(
        float(entry_reference), float(exit_reference), COST_SCENARIOS["30bp"]
    )
    if not math.isclose(float(net_return), expected_return, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(f"Forward sample {index} net return does not match the current cost model.")

    metadata = sample.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError(f"Forward sample {index} has no metadata object.")
    required_metadata = {
        "spec_id": MAIN_SPEC.spec_id,
        "policy": POLICY,
        "cost_version": COST_SCENARIOS["30bp"].version,
        "signal_version": MAIN_SPEC.signal_version,
        "horizon_bars": MAIN_SPEC.horizon_bars,
        "source": FORWARD_SAMPLE_SOURCE,
        "stream_id": FORWARD_STREAM_ID,
        "dataset_hash": FORWARD_STREAM_ID,
    }
    for field, expected in required_metadata.items():
        if metadata.get(field) != expected:
            raise ValueError(f"Forward sample {index} has incompatible metadata field {field}.")
    if metadata.get("fill_ts") != fill_ts or metadata.get("label_available_ts") != label_available_ts:
        raise ValueError(f"Forward sample {index} metadata timestamps do not match its label.")

    prediction = metadata.get("prediction")
    if not isinstance(prediction, Mapping):
        raise ValueError(f"Forward sample {index} has no frozen prediction metadata.")
    if prediction.get("feature_version") != FEATURE_VERSION:
        raise ValueError(f"Forward sample {index} has an incompatible feature version.")
    model_version = prediction.get("model_version")
    if not isinstance(model_version, str) or not model_version:
        raise ValueError(f"Forward sample {index} has no model version.")
    prediction_ts = prediction.get("created_ts")
    if not _is_number(prediction_ts) or int(prediction_ts) != prediction_ts:
        raise ValueError(f"Forward sample {index} has an invalid prediction timestamp.")
    if not decision_ts <= int(prediction_ts) < label_available_ts:
        raise ValueError(f"Forward sample {index} prediction was not frozen before its label.")

    return {
        "id": event_id,
        "strategy": strategy,
        "decision_ts": decision_ts,
        "fill_ts": fill_ts,
        "exit_ts": exit_ts,
        "label_available_ts": label_available_ts,
        "entry_reference": float(entry_reference),
        "exit_reference": float(exit_reference),
        "target": float(target),
        "net_return": float(net_return),
        "x": [float(value) for value in vector],
        "source": FORWARD_SAMPLE_SOURCE,
        "metadata": {
            **required_metadata,
            "prediction": {
                "model_version": model_version,
                "feature_version": FEATURE_VERSION,
                "created_ts": int(prediction_ts),
            },
        },
    }


def _merge_training_samples(
    historical_samples: Sequence[Mapping[str, object]],
    forward_samples: Sequence[Mapping[str, object]],
    *,
    historical_digest: str,
    historical_last_ts: int,
) -> tuple[list[Mapping[str, object]], str]:
    """Return chronological training rows and their deterministic corpus hash."""

    _validate_samples(historical_samples)
    if not isinstance(historical_digest, str) or len(historical_digest) != 64:
        raise ValueError("Historical digest must be a SHA-256 string.")
    canonical_forward = [
        _canonical_forward_sample(sample, index) for index, sample in enumerate(forward_samples)
    ]
    seen: set[str] = set()
    for source, rows in (("historical", historical_samples), ("forward", canonical_forward)):
        for index, sample in enumerate(rows):
            event_id = sample.get("id")
            if not isinstance(event_id, str) or not event_id:
                raise ValueError(f"{source.title()} sample {index} has an invalid id.")
            if event_id in seen:
                raise ValueError(f"Duplicate training sample id: {event_id}.")
            seen.add(event_id)

    # A refreshed historical snapshot may eventually cover observations that were
    # originally collected in the true-forward stream.  Keep those rows for the
    # separate promotion audit, but exclude them from this fit so the same market
    # event is never counted once as history and again as forward training data.
    included_forward = [
        sample for sample in canonical_forward
        if int(sample["decision_ts"]) > historical_last_ts
    ]
    ordered_forward = sorted(
        included_forward, key=lambda sample: (int(sample["fill_ts"]), str(sample["id"]))
    )
    combined: list[Mapping[str, object]] = sorted(
        [*historical_samples, *ordered_forward],
        key=lambda sample: (int(sample["fill_ts"]), str(sample["id"])),
    )
    _validate_samples(combined)
    if not ordered_forward:
        # Backward compatibility is intentional: without true-forward samples,
        # the artifact provenance and therefore model_version remain unchanged.
        return combined, historical_digest
    corpus_payload = {
        "schema": "bollinger-v2-training-corpus-v1",
        "historical_ohlcv_digest": historical_digest,
        "forward_samples": ordered_forward,
    }
    encoded = json.dumps(corpus_payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return combined, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_samples(samples: Sequence[Mapping[str, object]]) -> None:
    for index, sample in enumerate(samples):
        vector = sample.get("x")
        if not isinstance(vector, (list, tuple)) or len(vector) != len(FEATURE_NAMES):
            raise ValueError(f"Sample {index} has an invalid feature vector.")
        if not all(_is_number(value) for value in vector):
            raise ValueError(f"Sample {index} has a non-finite feature value.")
        for name in ("fill_ts", "exit_ts", "label_available_ts", "net_return",
                     "entry_reference", "exit_reference", "target"):
            if not _is_number(sample.get(name)):
                raise ValueError(f"Sample {index} has invalid {name}.")
        if float(sample["entry_reference"]) <= 0 or float(sample["exit_reference"]) <= 0:
            raise ValueError(f"Sample {index} has invalid reference prices.")
        if int(sample["label_available_ts"]) < int(sample["exit_ts"]):
            raise ValueError(f"Sample {index} label availability precedes its outcome.")


def fit_logistic_model(samples: Sequence[Mapping[str, object]], c_value: float = 0.2) -> dict[str, object]:
    """Fit a deterministic standardized L2 logistic model (offline only)."""

    _validate_samples(samples)
    if not _is_number(c_value) or c_value <= 0:
        raise ValueError("Regularization C must be positive and finite.")
    outcomes = [int(float(sample["net_return"]) > 0) for sample in samples]
    if len(samples) < 20 or len(set(outcomes)) < 2:
        raise ValueError("Logistic fitting needs 20 events and both outcome classes.")
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:  # pragma: no cover - depends on offline runtime
        raise RuntimeError("Offline training requires scikit-learn; JSON inference does not.") from exc

    matrix = [[float(value) for value in sample["x"]] for sample in samples]
    scaler = StandardScaler()
    standardized = scaler.fit_transform(matrix)
    estimator = LogisticRegression(
        C=float(c_value),
        solver="liblinear",
        random_state=TRAINING_SEED,
        max_iter=2_000,
    )
    estimator.fit(standardized, outcomes)
    scale = [float(value) if float(value) > 0 else 1.0 for value in scaler.scale_]
    fitted = {
        "algorithm": "sklearn.StandardScaler+LogisticRegression(L2,liblinear)",
        "c": float(c_value),
        "mean": [float(value) for value in scaler.mean_],
        "scale": scale,
        "coefficients": [float(value) for value in estimator.coef_[0]],
        "intercept": float(estimator.intercept_[0]),
        "training_events": len(samples),
        "training_base_rate": fmean(outcomes),
        "training_last_label_available_ts": max(int(sample["label_available_ts"]) for sample in samples),
    }
    _assert_finite_json(fitted)
    return fitted


def _constant_shadow_model() -> dict[str, object]:
    return {
        "algorithm": "untrained_constant_shadow",
        "c": None,
        "mean": [0.0] * len(FEATURE_NAMES),
        "scale": [1.0] * len(FEATURE_NAMES),
        "coefficients": [0.0] * len(FEATURE_NAMES),
        "intercept": 0.0,
        "training_events": 0,
        "training_base_rate": 0.5,
        "training_last_label_available_ts": None,
    }


def _sigmoid(value: float) -> float:
    if value >= 0:
        inverse = math.exp(-value)
        return 1.0 / (1.0 + inverse)
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def predict_fitted(fitted: Mapping[str, object], vector: Sequence[float]) -> float:
    if len(vector) != len(FEATURE_NAMES) or not all(_is_number(value) for value in vector):
        raise ValueError("Inference vector must contain only finite v2 features.")
    mean = fitted.get("mean")
    scale = fitted.get("scale")
    coefficients = fitted.get("coefficients")
    intercept = fitted.get("intercept")
    if not all(isinstance(values, list) and len(values) == len(FEATURE_NAMES) for values in (mean, scale, coefficients)):
        raise ValueError("Fitted model dimensions do not match the v2 feature schema.")
    numeric = [*mean, *scale, *coefficients, intercept]
    if not all(_is_number(value) for value in numeric) or not all(float(value) > 0 for value in scale):
        raise ValueError("Fitted model contains invalid numeric values.")
    logit = float(intercept)
    for value, center, width, coefficient in zip(vector, mean, scale, coefficients):
        logit += ((float(value) - float(center)) / float(width)) * float(coefficient)
    return _sigmoid(logit)


def _sample_return(sample: Mapping[str, object], cost: CostModel) -> float:
    return round_trip_net_return(float(sample["entry_reference"]), float(sample["exit_reference"]), cost)


def _accepted(
    samples: Sequence[Mapping[str, object]], probabilities: Sequence[float], threshold: float | None
) -> list[tuple[Mapping[str, object], float]]:
    if threshold is None:
        return []
    if len(samples) != len(probabilities):
        raise ValueError("Samples and probabilities must have equal length.")
    ordered = sorted(
        zip(samples, probabilities),
        key=lambda item: (int(item[0]["fill_ts"]), str(item[0].get("id", ""))),
    )
    result = []
    busy_through: dict[str, int] = {}
    for sample, probability in ordered:
        strategy = str(sample.get("strategy", ""))
        target = sample.get("target")
        feasible = (
            _is_number(target)
            and round_trip_net_return(
                float(sample["entry_reference"]),
                float(target),
                COST_SCENARIOS["30bp"],
            ) >= MIN_TARGET_NET_RETURN
        )
        if (probability < threshold or not feasible
                or int(sample["fill_ts"]) <= busy_through.get(strategy, -1)):
            continue
        result.append((sample, probability))
        busy_through[strategy] = int(sample["exit_ts"])
    return result


def _trade_metrics(returns: Sequence[float]) -> dict[str, object]:
    values = [float(value) for value in returns]
    if not all(math.isfinite(value) and value > -1 for value in values):
        raise ValueError("Trade returns must be finite and greater than -100%.")
    if not values:
        return {
            "trades": 0,
            "compounded_return": 0.0,
            "sum_return": 0.0,
            "mean_return": None,
            "win_rate": None,
            "profit_factor": None,
            "max_drawdown": 0.0,
        }
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    gains = sum(max(value, 0.0) for value in values)
    losses = -sum(min(value, 0.0) for value in values)
    return {
        "trades": len(values),
        "compounded_return": equity - 1.0,
        "sum_return": sum(values),
        "mean_return": fmean(values),
        "win_rate": sum(value > 0 for value in values) / len(values),
        "profit_factor": gains / losses if losses > 0 else None,
        "max_drawdown": max_drawdown,
    }


def _cost_metrics(accepted: Sequence[tuple[Mapping[str, object], float]]) -> dict[str, dict[str, object]]:
    return {
        label: _trade_metrics([_sample_return(sample, cost) for sample, _ in accepted])
        for label, cost in COST_SCENARIOS.items()
    }


def _selection_fingerprint(fitted: Mapping[str, object], selection: Mapping[str, object]) -> str:
    payload = {
        "fitted": fitted,
        "c": selection["c"],
        "threshold": selection["threshold"],
        "cash_selected": selection["cash_selected"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _inner_select(train_samples: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Select C/threshold on an inner chronological block; cash is explicit."""

    ordered = sorted(train_samples, key=lambda sample: (int(sample["fill_ts"]), str(sample.get("id", ""))))
    fallback = {
        "c": 0.2,
        "threshold": 0.70,
        "cash_selected": True,
        "reason": "insufficient_inner_history",
        "evaluated": 0,
        "passing": 0,
    }
    if len(ordered) < 80:
        return fallback
    split = max(40, int(len(ordered) * 0.70))
    validation = ordered[split:]
    if len(validation) < 20:
        return fallback
    validation_start = int(validation[0]["fill_ts"])
    inner_cutoff = validation_start - BAR_MS
    inner_train = [
        sample for sample in ordered[:split]
        if int(sample["label_available_ts"]) <= inner_cutoff
    ]
    if len(inner_train) < 40 or len({float(sample["net_return"]) > 0 for sample in inner_train}) < 2:
        return fallback

    best_passing = None
    best_any = None
    evaluated = 0
    passing = 0
    midpoint = int(validation[len(validation) // 2]["fill_ts"])
    for c_value in REGULARIZATION_GRID:
        fitted = fit_logistic_model(inner_train, c_value)
        probabilities = [predict_fitted(fitted, sample["x"]) for sample in validation]
        for threshold in THRESHOLD_GRID:
            evaluated += 1
            accepted = _accepted(validation, probabilities, threshold)
            metrics = _cost_metrics(accepted)
            m30, m40 = metrics["30bp"], metrics["40bp"]
            first = [(sample, probability) for sample, probability in accepted if int(sample["fill_ts"]) < midpoint]
            second = [(sample, probability) for sample, probability in accepted if int(sample["fill_ts"]) >= midpoint]
            halves_positive = all(_cost_metrics(part)["40bp"]["sum_return"] > 0 for part in (first, second))
            pf40 = m40["profit_factor"] or 0.0
            qualifies = (
                m40["trades"] >= 5
                and m30["sum_return"] > 0
                and m40["sum_return"] > 0
                and pf40 >= 1.10
                and halves_positive
            )
            score = float(m40["sum_return"]) - 0.25 * float(m40["max_drawdown"])
            candidate = {
                "c": c_value,
                "threshold": threshold,
                "cash_selected": not qualifies,
                "reason": "inner_cost_stability_pass" if qualifies else "inner_cost_stability_failed",
                "score": score,
                "inner_train_events": len(inner_train),
                "inner_validation_events": len(validation),
                "inner_metrics": metrics,
            }
            if best_any is None or score > best_any[0]:
                best_any = (score, candidate)
            if qualifies:
                passing += 1
                if best_passing is None or score > best_passing[0]:
                    best_passing = (score, candidate)
    chosen = dict((best_passing or best_any)[1])
    chosen["cash_selected"] = best_passing is None
    chosen["reason"] = "inner_cost_stability_pass" if best_passing is not None else "cash_outperformed_admissible_candidates"
    chosen["evaluated"] = evaluated
    chosen["passing"] = passing
    return chosen


def walk_forward_evaluate(
    samples: Sequence[Mapping[str, object]],
    *,
    folds: int = 5,
    min_train_events: int = 100,
) -> dict[str, object]:
    """Run purged expanding walk-forward evaluation with nested selection."""

    _validate_samples(samples)
    split_folds = purged_expanding_walk_forward(
        samples,
        folds=folds,
        min_train_events=min_train_events,
        embargo_ms=BAR_MS,
    )
    fold_reports = []
    aggregate_pairs = {label: [] for label in COST_SCENARIOS}
    brier_sum = 0.0
    baseline_brier_sum = 0.0
    probability_count = 0
    for fold in split_folds:
        train = list(fold["train"])
        validation = list(fold["validation"])
        selection = _inner_select(train)
        try:
            fitted = fit_logistic_model(train, float(selection["c"]))
        except ValueError:
            fitted = _constant_shadow_model()
            selection = {**selection, "cash_selected": True, "reason": "outer_train_not_fit_capable"}
        probabilities = [predict_fitted(fitted, sample["x"]) for sample in validation]
        outcomes = [int(float(sample["net_return"]) > 0) for sample in validation]
        base_rate = float(fitted["training_base_rate"])
        brier = fmean((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes))
        baseline_brier = fmean((base_rate - outcome) ** 2 for outcome in outcomes)
        threshold = None if selection["cash_selected"] else float(selection["threshold"])
        accepted = _accepted(validation, probabilities, threshold)
        metrics = _cost_metrics(accepted)
        for label, cost in COST_SCENARIOS.items():
            aggregate_pairs[label].extend(_sample_return(sample, cost) for sample, _ in accepted)
        brier_sum += brier * len(validation)
        baseline_brier_sum += baseline_brier * len(validation)
        probability_count += len(validation)
        fold_reports.append(
            {
                "fold": int(fold["fold"]),
                "train_events": len(train),
                "validation_events": len(validation),
                "validation_start_ts": int(fold["validation_start_ts"]),
                "purged_count": int(fold["purged_count"]),
                "selection": selection,
                "selection_fingerprint": _selection_fingerprint(fitted, selection),
                "brier": brier,
                "baseline_brier": baseline_brier,
                "accepted_ids": [str(sample.get("id", "")) for sample, _ in accepted],
                "cost_metrics": metrics,
            }
        )
    aggregate = {label: _trade_metrics(values) for label, values in aggregate_pairs.items()}
    return {
        "folds_requested": folds,
        "folds_completed": len(fold_reports),
        "folds": fold_reports,
        "aggregate_cost_metrics": aggregate,
        "brier": brier_sum / probability_count if probability_count else 1.0,
        "baseline_brier": baseline_brier_sum / probability_count if probability_count else 0.25,
        "evaluated_probabilities": probability_count,
    }


def _version_payload(artifact: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": artifact["schema"],
        "kind": artifact["kind"],
        "policy": artifact["policy"],
        "spec_id": artifact["spec_id"],
        "signal_version": artifact["signal_version"],
        "feature_version": artifact["feature_version"],
        "feature_names": artifact["feature_names"],
        "cost_version": artifact["cost_version"],
        "execution_policy_version": artifact["execution_policy_version"],
        "training_protocol_version": artifact["training_protocol_version"],
        "dataset_hash": artifact["dataset_hash"],
        "selection": artifact["selection"],
        "fitted": artifact["fitted"],
    }


def _model_version(artifact: Mapping[str, object]) -> str:
    encoded = json.dumps(_version_payload(artifact), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assemble_model_artifact(
    fitted: Mapping[str, object],
    selection: Mapping[str, object],
    *,
    dataset_hash: str,
    data_path: str,
    spec: BarrierSpec = MAIN_SPEC,
) -> dict[str, object]:
    artifact = {
        "schema": MODEL_SCHEMA,
        "kind": MODEL_KIND,
        "policy": POLICY,
        "spec_id": spec.spec_id,
        "signal_version": spec.signal_version,
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "cost_version": COST_SCENARIOS["30bp"].version,
        "execution_policy_version": EXECUTION_POLICY_VERSION,
        "training_protocol_version": TRAINING_PROTOCOL_VERSION,
        "dataset_hash": dataset_hash,
        "data_path": data_path,
        "selection": {
            "c": selection.get("c"),
            "threshold": selection.get("threshold"),
            "cash_selected": bool(selection.get("cash_selected", True)),
            "reason": str(selection.get("reason", "unspecified")),
        },
        "fitted": dict(fitted),
        "eligible": False,
        "status": "shadow",
        "promotion": None,
    }
    artifact["model_version"] = _model_version(artifact)
    validate_model_artifact(artifact)
    return artifact


def validate_model_artifact(artifact: Mapping[str, object]) -> None:
    if not isinstance(artifact, Mapping):
        raise ValueError("Model artifact must be a JSON object.")
    _assert_finite_json(dict(artifact))
    if artifact.get("schema") != MODEL_SCHEMA or artifact.get("kind") != MODEL_KIND:
        raise ValueError("Unsupported v2 model schema.")
    compatibility = {
        "policy": POLICY,
        "spec_id": MAIN_SPEC.spec_id,
        "signal_version": MAIN_SPEC.signal_version,
        "feature_version": FEATURE_VERSION,
        "cost_version": COST_SCENARIOS["30bp"].version,
        "execution_policy_version": EXECUTION_POLICY_VERSION,
        "training_protocol_version": TRAINING_PROTOCOL_VERSION,
    }
    for field, expected in compatibility.items():
        if artifact.get(field) != expected:
            raise ValueError(f"Model {field} is incompatible with the active v2 contract.")
    if artifact.get("feature_names") != list(FEATURE_NAMES):
        raise ValueError("Model feature order is incompatible.")
    digest = artifact.get("dataset_hash")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("Dataset hash must be lowercase SHA-256.")
    selection = artifact.get("selection")
    fitted = artifact.get("fitted")
    if not isinstance(selection, Mapping) or not isinstance(fitted, Mapping):
        raise ValueError("Model selection and fitted payloads are required.")
    threshold = selection.get("threshold")
    if threshold is not None and (not _is_number(threshold) or not 0 < float(threshold) < 1):
        raise ValueError("Decision threshold must be null or inside (0, 1).")
    for field in ("mean", "scale", "coefficients"):
        values = fitted.get(field)
        if not isinstance(values, list) or len(values) != len(FEATURE_NAMES) or not all(_is_number(x) for x in values):
            raise ValueError(f"Invalid fitted {field}.")
    if not all(float(value) > 0 for value in fitted["scale"]) or not _is_number(fitted.get("intercept")):
        raise ValueError("Invalid fitted scaling or intercept.")
    c_value = fitted.get("c")
    if c_value is not None and (not _is_number(c_value) or float(c_value) <= 0):
        raise ValueError("Invalid fitted regularization value.")
    if artifact.get("status") not in {"shadow", "paper_eligible"}:
        raise ValueError("Unknown model status.")
    if bool(artifact.get("eligible")) != (artifact.get("status") == "paper_eligible"):
        raise ValueError("Eligibility and model status disagree.")
    if artifact.get("model_version") != _model_version(artifact):
        raise ValueError("Model version digest does not match its coefficients and provenance.")


def save_model(path: str | Path, artifact: Mapping[str, object]) -> None:
    validate_model_artifact(artifact)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        destination,
        json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False),
    )


def load_model(path: str | Path) -> dict[str, object]:
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load v2 model JSON: {exc}") from exc
    validate_model_artifact(artifact)
    return artifact


def predict_probability(artifact: Mapping[str, object], vector: Sequence[float]) -> float:
    """Pure-stdlib probability inference from an audited JSON artifact."""

    validate_model_artifact(artifact)
    return predict_fitted(artifact["fitted"], vector)


def assess(artifact: Mapping[str, object], vector: Sequence[float]) -> dict[str, object]:
    probability = predict_probability(artifact, vector)
    selection = artifact["selection"]
    threshold = selection["threshold"]
    would_accept = threshold is not None and not selection["cash_selected"] and probability >= float(threshold)
    accepted = bool(artifact["eligible"]) and would_accept
    return {
        "probability": probability,
        "threshold": threshold,
        "would_accept": would_accept,
        "accept": accepted,
        "status": artifact["status"],
        "reason": "accepted" if accepted else ("shadow_only" if would_accept else "cash_guard"),
        "model_version": artifact["model_version"],
    }


def _valid_prequential_evidence(
    evidence: Sequence[Mapping[str, object]], model_version: str
) -> tuple[list[Mapping[str, object]], list[dict[str, object]]]:
    accepted = []
    rejected = []
    seen = set()
    for index, record in enumerate(evidence):
        reason = None
        event_id = record.get("event_id")
        numeric_fields = ("decision_ts", "prediction_ts", "label_available_ts", "probability", "net_return")
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            reason = "invalid_or_duplicate_event_id"
        elif record.get("source") != "paper_v2_prequential":
            reason = "not_prequential_source"
        elif record.get("model_version") != model_version:
            reason = "model_version_mismatch"
        elif not all(_is_number(record.get(field)) for field in numeric_fields):
            reason = "invalid_numeric_field"
        elif not (0 <= float(record["probability"]) <= 1):
            reason = "invalid_probability"
        elif not (
            int(record["decision_ts"]) <= int(record["prediction_ts"]) < int(record["label_available_ts"])
        ):
            reason = "prediction_not_recorded_before_label"
        elif not isinstance(record.get("accepted"), bool):
            reason = "invalid_acceptance_flag"
        elif not isinstance(record.get("executed", False), bool):
            reason = "invalid_execution_flag"
        elif record.get("executed", False) and not all(
                _is_number(record.get(field))
                for field in ("execution_net_return", "execution_label_available_ts")):
            reason = "invalid_execution_outcome"
        elif (record.get("executed", False)
              and not float(record["execution_net_return"]) > -1):
            reason = "invalid_execution_return"
        elif (record.get("executed", False)
              and not int(record["prediction_ts"]) < int(record["execution_label_available_ts"])):
            reason = "execution_not_after_prediction"
        if reason is None:
            seen.add(event_id)
            accepted.append(record)
        else:
            rejected.append({"index": index, "event_id": event_id, "reason": reason})
    return accepted, rejected


def evaluate_promotion(
    artifact: Mapping[str, object],
    walk_forward: Mapping[str, object],
    true_forward_evidence: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Apply v2 promotion using matching prequential records only."""

    validate_model_artifact(artifact)
    valid, rejected = _valid_prequential_evidence(true_forward_evidence, str(artifact["model_version"]))
    accepted_returns = [
        float(record["execution_net_return"])
        for record in valid
        if record["accepted"] and record.get("executed", False)
    ]
    outcomes = [int(float(record["net_return"]) > 0) for record in valid]
    probabilities = [float(record["probability"]) for record in valid]
    base_rate = float(artifact["fitted"]["training_base_rate"])
    brier = fmean((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) if valid else 1.0
    baseline_brier = fmean((base_rate - outcome) ** 2 for outcome in outcomes) if valid else 0.25
    fold_returns = [
        float(fold["cost_metrics"]["30bp"]["compounded_return"])
        for fold in walk_forward.get("folds", [])
    ]
    gate = promotion_gate(
        total_events=max(int(artifact["fitted"]["training_events"]) + len(valid), len(accepted_returns)),
        accepted_returns=accepted_returns,
        fold_net_returns=fold_returns,
        brier=brier,
        baseline_brier=baseline_brier,
        true_forward_count=len(valid),
        bootstrap_iterations=500,
        bootstrap_seed=TRAINING_SEED,
    )
    gate["prequential"] = {
        "valid_count": len(valid),
        "rejected_count": len(rejected),
        "rejected": rejected,
        "accepted_count": len(accepted_returns),
        "model_accepted_count": sum(bool(record["accepted"]) for record in valid),
        "executed_count": sum(bool(record.get("executed", False)) for record in valid),
        "executed_accepted_count": len(accepted_returns),
        "accepted_return_source": "frozen_actual_paper_execution",
        "required_model_version": artifact["model_version"],
    }
    return gate


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(destination: Path, text: str) -> None:
    """Durably stage a complete UTF-8 document, then atomically replace it."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _write_json(path: str | Path, payload: Mapping[str, object]) -> None:
    _assert_finite_json(dict(payload))
    destination = Path(path)
    _atomic_write_text(
        destination,
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False),
    )


def train_and_report(
    rows: Sequence[Mapping[str, object]],
    data_path: str | Path,
    model_path: str | Path,
    report_path: str | Path,
    *,
    true_forward_evidence: Sequence[Mapping[str, object]] = (),
    forward_samples: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Train H8 meta-filter from history plus validated later shadow labels."""

    validate_rows(rows)
    historical_digest = dataset_digest(rows)
    historical_samples = build_historical_samples(rows, MAIN_SPEC, COST_SCENARIOS["30bp"])
    samples, training_corpus_hash = _merge_training_samples(
        historical_samples,
        forward_samples,
        historical_digest=historical_digest,
        historical_last_ts=int(rows[-1]["ts"]),
    )
    included_forward_events = sum(
        int(sample["decision_ts"]) > int(rows[-1]["ts"])
        for sample in forward_samples
    )
    overlap_filtered_events = len(forward_samples) - included_forward_events
    walk_forward = walk_forward_evaluate(samples) if samples else walk_forward_evaluate([], min_train_events=100)
    selection = _inner_select(samples)
    try:
        fitted = fit_logistic_model(samples, float(selection["c"]))
    except ValueError:
        fitted = _constant_shadow_model()
        selection = {**selection, "cash_selected": True, "reason": "insufficient_fit_evidence"}
    artifact = assemble_model_artifact(
        fitted,
        selection,
        dataset_hash=training_corpus_hash,
        data_path=str(Path(data_path)),
        spec=MAIN_SPEC,
    )
    promotion = evaluate_promotion(artifact, walk_forward, true_forward_evidence)
    artifact["promotion"] = promotion
    artifact["eligible"] = bool(promotion["eligible"])
    artifact["status"] = "paper_eligible" if artifact["eligible"] else "shadow"
    validate_model_artifact(artifact)
    save_model(model_path, artifact)
    source_path = Path(data_path)
    report = {
        "schema": MODEL_SCHEMA,
        "kind": "bollinger_15m_v2_training_report",
        "status": artifact["status"],
        "eligible": artifact["eligible"],
        "model_version": artifact["model_version"],
        "dataset": {
            "path": str(source_path),
            "rows": len(rows),
            "ohlcv_digest": historical_digest,
            "training_corpus_hash": training_corpus_hash,
            "file_sha256": _sha256_file(source_path),
        },
        "label_contract": {
            "engine": "v2_engine.generate_labeled_samples",
            "spec_id": MAIN_SPEC.spec_id,
            "horizon_bars": MAIN_SPEC.horizon_bars,
            "cost_version": COST_SCENARIOS["30bp"].version,
            "execution_policy_version": EXECUTION_POLICY_VERSION,
            "training_protocol_version": TRAINING_PROTOCOL_VERSION,
            "historical_events": len(historical_samples),
            # This count is the processed forward watermark used by automatic
            # retraining.  Included/filtered counts explain the actual fit size.
            "forward_training_events": len(forward_samples),
            "forward_training_events_included": included_forward_events,
            "forward_training_events_overlap_filtered": overlap_filtered_events,
            "total_training_events": len(samples),
        },
        "features": list(FEATURE_NAMES),
        "final_selection": artifact["selection"],
        "walk_forward": walk_forward,
        "promotion": promotion,
        "inference_contract": "JSON coefficients; Python standard library only; eligible=false blocks orders",
    }
    _write_json(report_path, report)
    return {"artifact": artifact, "report": report}


def _read_csv(path: Path) -> list[dict[str, object]]:
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "ts": int(row["ts"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Train the auditable Bollinger 15M v2 meta-filter.")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = train_and_report(_read_csv(args.data), args.data, args.model, args.report)
    summary = {
        "status": result["report"]["status"],
        "eligible": result["report"]["eligible"],
        "model_version": result["report"]["model_version"],
        "historical_events": result["report"]["label_contract"]["historical_events"],
        "walk_forward_30bp": result["report"]["walk_forward"]["aggregate_cost_metrics"]["30bp"],
    }
    print(json.dumps(summary, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()


__all__ = [
    "MODEL_SCHEMA",
    "MODEL_KIND",
    "FEATURE_VERSION",
    "FEATURE_NAMES",
    "TRAINING_PROTOCOL_VERSION",
    "COST_SCENARIOS",
    "causal_feature_vector",
    "build_historical_samples",
    "fit_logistic_model",
    "predict_fitted",
    "walk_forward_evaluate",
    "assemble_model_artifact",
    "validate_model_artifact",
    "save_model",
    "load_model",
    "predict_probability",
    "assess",
    "evaluate_promotion",
    "train_and_report",
]
