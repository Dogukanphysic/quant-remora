"""Causal Bollinger 15-minute research and forward-label core.

The module deliberately has no broker or database side effects.  It turns closed
OHLCV bars into auditable candidate events and conservative triple-barrier
labels.  A signal is decided at bar ``t`` close and can only fill at ``t+1``
open.  This makes the same code suitable for historical research and a live
paper-trading shadow path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import random
from statistics import fmean, pstdev
from typing import Iterable, Mapping, Sequence


BAR_MS = 900_000
POLICY = "bollinger_15m_v2"
SIGNAL_SPEC_VERSION = "bb-causal-signals-v2"


@dataclass(frozen=True)
class CostModel:
    """Round-trip execution assumptions, expressed as fractions of price."""

    fee_each_side: float = 0.001
    slippage_each_side: float = 0.0005
    spread: float = 0.0
    schema: str = "cost-v1"

    def validate(self) -> None:
        values = (self.fee_each_side, self.slippage_each_side, self.spread)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Execution costs must be finite.")
        if not (0 <= self.fee_each_side < 1):
            raise ValueError("Fee must be in [0, 1).")
        if not (0 <= self.slippage_each_side < 1):
            raise ValueError("Slippage must be in [0, 1).")
        if not (0 <= self.spread < 2):
            raise ValueError("Full relative spread must be in [0, 2).")
        if self.slippage_each_side + self.spread / 2 >= 1:
            raise ValueError("Sell-side execution adjustment must stay positive.")

    @property
    def version(self) -> str:
        self.validate()
        return (
            f"{self.schema}:fee={self.fee_each_side:.10g}:"
            f"slip={self.slippage_each_side:.10g}:spread={self.spread:.10g}"
        )


@dataclass(frozen=True)
class BarrierSpec:
    """Immutable signal and label specification.

    ``horizon_bars`` counts the fill bar.  H=8 therefore observes at most eight
    complete 15-minute bars (two hours) after the t+1 open fill.
    """

    horizon_bars: int = 8
    stop_atr: float = 1.5
    target_atr: float = 2.0
    bollinger_period: int = 20
    bollinger_deviations: float = 2.0
    atr_period: int = 14
    regime_period: int = 200
    require_rising_regime: bool = True
    policy: str = POLICY
    signal_version: str = SIGNAL_SPEC_VERSION

    def validate(self) -> None:
        if self.horizon_bars <= 0:
            raise ValueError("Horizon must contain at least one bar.")
        if self.bollinger_period < 2 or self.atr_period < 2 or self.regime_period < 2:
            raise ValueError("Indicator periods must be at least two.")
        numeric = (self.stop_atr, self.target_atr, self.bollinger_deviations)
        if not all(math.isfinite(value) and value > 0 for value in numeric):
            raise ValueError("Barrier and band multipliers must be positive and finite.")

    @property
    def spec_id(self) -> str:
        self.validate()
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{self.policy}:h{self.horizon_bars}:{digest}"


MAIN_SPEC = BarrierSpec(horizon_bars=8)
STRESS_SPEC = replace(MAIN_SPEC, horizon_bars=16)
DEFAULT_COST = CostModel()
MAX_ENTRY_SPREAD = 0.001
MIN_TARGET_NET_RETURN = 0.003
EXECUTION_POLICY_VERSION = (
    f"paper-entry-v1:cost={DEFAULT_COST.version}:max-spread={MAX_ENTRY_SPREAD:.10g}:"
    f"min-target-net={MIN_TARGET_NET_RETURN:.10g}"
)


class LabelNotAvailableError(ValueError):
    """The event is valid, but its vertical horizon has not closed yet."""


@dataclass(frozen=True)
class CandidateEvent:
    event_id: str
    strategy: str
    decision_index: int
    decision_ts: int
    fill_index: int
    fill_ts: int
    atr: float
    spec_id: str
    policy: str
    dataset_hash: str


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def validate_rows(rows: Sequence[Mapping[str, object]]) -> None:
    """Reject malformed or irregular data before it can create a label."""

    previous_ts = None
    for index, row in enumerate(rows):
        missing = {field for field in ("ts", "open", "high", "low", "close", "volume") if field not in row}
        if missing:
            raise ValueError(f"Bar {index} is missing fields: {sorted(missing)}")
        if not all(_finite_number(row[field]) for field in ("ts", "open", "high", "low", "close", "volume")):
            raise ValueError(f"Bar {index} contains a non-finite value.")
        timestamp = int(row["ts"])
        if timestamp != row["ts"]:
            raise ValueError(f"Bar {index} timestamp must be an integer millisecond value.")
        if previous_ts is not None and timestamp <= previous_ts:
            raise ValueError("Bars must be strictly chronological.")
        if previous_ts is not None and timestamp - previous_ts != BAR_MS:
            raise ValueError("Bars must be continuous 15-minute candles.")
        open_price, high, low, close = (float(row[k]) for k in ("open", "high", "low", "close"))
        if min(open_price, high, low, close) <= 0 or float(row["volume"]) < 0:
            raise ValueError(f"Bar {index} has an invalid price or volume.")
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise ValueError(f"Bar {index} has inconsistent OHLC values.")
        previous_ts = timestamp


def dataset_digest(rows: Sequence[Mapping[str, object]]) -> str:
    """Hash only canonical OHLCV content, independent of dict insertion order."""

    validate_rows(rows)
    canonical = [
        [int(row["ts"])] + [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
        for row in rows
    ]
    encoded = json.dumps(canonical, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rolling_mean(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            result[index] = running / period
    return result


def _wilder_average(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    result[period - 1] = sum(values[:period]) / period
    for index in range(period, len(values)):
        previous = result[index - 1]
        assert previous is not None
        result[index] = (previous * (period - 1) + values[index]) / period
    return result


def indicators(rows: Sequence[Mapping[str, object]], spec: BarrierSpec = MAIN_SPEC) -> dict[str, list[float | None]]:
    """Compute causal indicators; value i depends on rows 0..i only."""

    spec.validate()
    validate_rows(rows)
    closes = [float(row["close"]) for row in rows]
    middle = _rolling_mean(closes, spec.bollinger_period)
    regime = _rolling_mean(closes, spec.regime_period)
    upper: list[float | None] = [None] * len(rows)
    lower: list[float | None] = [None] * len(rows)
    bandwidth: list[float | None] = [None] * len(rows)
    period = spec.bollinger_period
    for index in range(period - 1, len(rows)):
        window = closes[index - period + 1 : index + 1]
        deviation = pstdev(window)
        mid = middle[index]
        assert mid is not None
        upper[index] = mid + spec.bollinger_deviations * deviation
        lower[index] = mid - spec.bollinger_deviations * deviation
        bandwidth[index] = (upper[index] - lower[index]) / mid if mid else 0.0

    true_ranges = []
    for index, row in enumerate(rows):
        high, low = float(row["high"]), float(row["low"])
        if index == 0:
            true_ranges.append(high - low)
        else:
            previous_close = closes[index - 1]
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = _wilder_average(true_ranges, spec.atr_period)
    return {
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "bandwidth": bandwidth,
        "atr": atr,
        "regime": regime,
    }


def candidate_signals(
    rows: Sequence[Mapping[str, object]],
    decision_index: int,
    spec: BarrierSpec = MAIN_SPEC,
    features: Mapping[str, Sequence[float | None]] | None = None,
) -> tuple[str, ...]:
    """Return causal candidates known exactly at ``decision_index`` close.

    Breakout follows an upper-band cross while bandwidth expands.  Reentry
    requires the prior close outside the lower band, the current close back
    inside and below the middle band, plus a long-regime guard.
    """

    spec.validate()
    if decision_index <= 0 or decision_index >= len(rows):
        return ()
    f = features if features is not None else indicators(rows[: decision_index + 1], spec)
    i, p = decision_index, decision_index - 1
    required = (f["upper"][i], f["upper"][p], f["lower"][i], f["lower"][p], f["middle"][i], f["bandwidth"][i], f["bandwidth"][p], f["atr"][i])
    if any(value is None for value in required):
        return ()
    close = float(rows[i]["close"])
    previous_close = float(rows[p]["close"])
    signals = []
    if (
        previous_close <= float(f["upper"][p])
        and close > float(f["upper"][i])
        and float(f["bandwidth"][i]) > float(f["bandwidth"][p])
    ):
        signals.append("breakout")

    regime_now = f["regime"][i]
    regime_previous = f["regime"][p]
    regime_ok = regime_now is not None and close >= float(regime_now)
    if spec.require_rising_regime:
        regime_ok = regime_ok and regime_previous is not None and float(regime_now) >= float(regime_previous)
    if (
        regime_ok
        and previous_close < float(f["lower"][p])
        and float(f["lower"][i]) <= close < float(f["middle"][i])
    ):
        signals.append("reentry")
    return tuple(signals)


def generate_candidates(
    rows: Sequence[Mapping[str, object]],
    spec: BarrierSpec = MAIN_SPEC,
    *,
    data_hash: str | None = None,
) -> list[CandidateEvent]:
    """Generate every causal candidate; overlap is resolved during labeling."""

    spec.validate()
    validate_rows(rows)
    digest = data_hash or dataset_digest(rows)
    f = indicators(rows, spec)
    events = []
    for decision_index in range(1, len(rows) - 1):
        fill_index = decision_index + 1
        if float(rows[fill_index]["volume"]) <= 0:
            continue
        for strategy in candidate_signals(rows, decision_index, spec, f):
            payload = f"{spec.spec_id}|{digest}|{strategy}|{int(rows[decision_index]['ts'])}"
            events.append(
                CandidateEvent(
                    event_id=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24],
                    strategy=strategy,
                    decision_index=decision_index,
                    decision_ts=int(rows[decision_index]["ts"]),
                    fill_index=fill_index,
                    fill_ts=int(rows[fill_index]["ts"]),
                    atr=float(f["atr"][decision_index]),
                    spec_id=spec.spec_id,
                    policy=spec.policy,
                    dataset_hash=digest,
                )
            )
    return events


def execution_prices(entry_reference: float, exit_reference: float, cost: CostModel = DEFAULT_COST) -> tuple[float, float]:
    """Apply spread and slippage exactly once to each market side."""

    cost.validate()
    if not all(math.isfinite(price) and price > 0 for price in (entry_reference, exit_reference)):
        raise ValueError("Reference prices must be positive and finite.")
    half_spread = cost.spread / 2
    entry = entry_reference * (1 + cost.slippage_each_side + half_spread)
    exit_price = exit_reference * (1 - cost.slippage_each_side - half_spread)
    return entry, exit_price


def round_trip_net_return(entry_reference: float, exit_reference: float, cost: CostModel = DEFAULT_COST) -> float:
    """Return cash-on-cash P&L with one fee and adjustment on each side."""

    entry, exit_price = execution_prices(entry_reference, exit_reference, cost)
    cash_out = entry * (1 + cost.fee_each_side)
    cash_in = exit_price * (1 - cost.fee_each_side)
    return cash_in / cash_out - 1


def label_candidate(
    rows: Sequence[Mapping[str, object]],
    candidate: CandidateEvent,
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
) -> dict[str, object]:
    """Create one conservative triple-barrier label.

    If stop and target are both touched within one OHLC candle, stop wins because
    the intrabar path is unknowable.  Gaps fill at the observed bar open.
    """

    spec.validate()
    cost.validate()
    if candidate.spec_id != spec.spec_id or candidate.policy != spec.policy:
        raise ValueError("Candidate belongs to a different label specification.")
    if candidate.fill_index != candidate.decision_index + 1:
        raise ValueError("Candidate fill must be the next bar open.")
    if candidate.fill_index >= len(rows):
        raise ValueError("Candidate has no fill bar.")
    if candidate.fill_ts != int(rows[candidate.fill_index]["ts"]):
        raise ValueError("Candidate fill timestamp does not match its source bar.")
    if candidate.decision_ts != int(rows[candidate.decision_index]["ts"]):
        raise ValueError("Candidate decision timestamp does not match its source bar.")
    expected_last_index = candidate.fill_index + spec.horizon_bars - 1
    last_index = min(expected_last_index, len(rows) - 1)
    entry_reference = float(rows[candidate.fill_index]["open"])
    distance = candidate.atr
    if not math.isfinite(distance) or distance <= 0:
        raise ValueError("Candidate ATR must be positive and finite.")
    stop = entry_reference - spec.stop_atr * distance
    target = entry_reference + spec.target_atr * distance
    if stop <= 0:
        raise ValueError("Stop barrier must remain positive.")

    exit_index = last_index
    exit_reference = float(rows[last_index]["close"])
    reason = "vertical"
    for index in range(candidate.fill_index, last_index + 1):
        row = rows[index]
        open_price, high, low = (float(row[k]) for k in ("open", "high", "low"))
        # Stop is evaluated before every target condition.  This remains
        # conservative even for a candle that gaps above target and later spans
        # the stop, because OHLC alone cannot prove the favorable path/order.
        if open_price <= stop or low <= stop:
            gap = open_price <= stop
            exit_index, exit_reference, reason = index, (open_price if gap else stop), ("stop_gap" if gap else "stop")
            break
        if open_price >= target or high >= target:
            gap = open_price >= target
            exit_index, exit_reference, reason = index, (open_price if gap else target), ("target_gap" if gap else "target")
            break

    if reason == "vertical" and expected_last_index >= len(rows):
        raise LabelNotAvailableError("The vertical barrier has not closed yet.")

    entry_exec, exit_exec = execution_prices(entry_reference, exit_reference, cost)
    label_available_ts = int(rows[exit_index]["ts"]) + BAR_MS
    metadata = {
        "spec_id": spec.spec_id,
        "policy": spec.policy,
        "cost_version": cost.version,
        "fill_ts": candidate.fill_ts,
        "label_available_ts": label_available_ts,
        "dataset_hash": candidate.dataset_hash,
        "signal_version": spec.signal_version,
        "horizon_bars": spec.horizon_bars,
    }
    return {
        "id": candidate.event_id,
        "strategy": candidate.strategy,
        "decision_ts": candidate.decision_ts,
        "fill_ts": candidate.fill_ts,
        "exit_ts": int(rows[exit_index]["ts"]),
        "label_available_ts": label_available_ts,
        "entry_reference": entry_reference,
        "entry_price": entry_exec,
        "exit_reference": exit_reference,
        "exit_price": exit_exec,
        "stop": stop,
        "target": target,
        "exit_reason": reason,
        "gross_return": exit_reference / entry_reference - 1,
        "net_return": round_trip_net_return(entry_reference, exit_reference, cost),
        "holding_bars": exit_index - candidate.fill_index + 1,
        "accepted": False,
        "metadata": metadata,
        # Private research indexes are intentionally explicit; integrations may
        # omit them when persisting the public sample.
        "_fill_index": candidate.fill_index,
        "_exit_index": exit_index,
    }


def label_candidates(
    rows: Sequence[Mapping[str, object]],
    candidates: Iterable[CandidateEvent],
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
) -> list[dict[str, object]]:
    """Label a one-position-per-strategy stream without overlapping trades."""

    busy_through: dict[str, int] = {}
    labels = []
    ordered = sorted(candidates, key=lambda event: (event.fill_index, event.strategy, event.event_id))
    for candidate in ordered:
        if candidate.fill_index <= busy_through.get(candidate.strategy, -1):
            continue
        try:
            label = label_candidate(rows, candidate, spec, cost)
        except LabelNotAvailableError:
            continue
        busy_through[candidate.strategy] = int(label["_exit_index"])
        labels.append(label)
    return labels


def generate_labeled_samples(
    rows: Sequence[Mapping[str, object]],
    spec: BarrierSpec = MAIN_SPEC,
    cost: CostModel = DEFAULT_COST,
    *,
    data_hash: str | None = None,
) -> list[dict[str, object]]:
    """Convenience pipeline used by both offline research and shadow replay."""

    candidates = generate_candidates(rows, spec, data_hash=data_hash)
    return label_candidates(rows, candidates, spec, cost)


def select_compatible_samples(
    samples: Iterable[Mapping[str, object]],
    *,
    spec_id: str,
    policy: str,
    cost_version: str,
    dataset_hash: str | None = None,
) -> list[Mapping[str, object]]:
    """Keep old policies, costs, and datasets from silently mixing together."""

    selected = []
    for sample in samples:
        metadata = sample.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        compatible = (
            metadata.get("spec_id") == spec_id
            and metadata.get("policy") == policy
            and metadata.get("cost_version") == cost_version
        )
        if dataset_hash is not None:
            compatible = compatible and metadata.get("dataset_hash") == dataset_hash
        if compatible:
            selected.append(sample)
    return selected


def purged_expanding_walk_forward(
    samples: Sequence[Mapping[str, object]],
    *,
    folds: int = 5,
    min_train_events: int = 100,
    embargo_ms: int = 0,
) -> list[dict[str, object]]:
    """Build chronological expanding folds and purge unresolved train labels.

    A training label is usable only if it was available no later than the first
    validation fill minus the requested embargo.  Validation blocks never move
    backward and every later fold expands the prior training history.
    """

    if folds < 2 or min_train_events < 1 or embargo_ms < 0:
        raise ValueError("Invalid walk-forward split parameters.")
    ordered = sorted(samples, key=lambda sample: (int(sample["fill_ts"]), str(sample.get("id", ""))))
    if len(ordered) < min_train_events + folds:
        return []
    remaining = len(ordered) - min_train_events
    base, extra = divmod(remaining, folds)
    boundaries = [min_train_events]
    for fold in range(folds):
        boundaries.append(boundaries[-1] + base + (1 if fold < extra else 0))

    result = []
    for fold in range(folds):
        validation = ordered[boundaries[fold] : boundaries[fold + 1]]
        if not validation:
            continue
        validation_start = int(validation[0]["fill_ts"])
        cutoff = validation_start - embargo_ms
        train_pool = ordered[: boundaries[fold]]
        train = [sample for sample in train_pool if int(sample["label_available_ts"]) <= cutoff]
        result.append(
            {
                "fold": fold + 1,
                "train": train,
                "validation": validation,
                "validation_start_ts": validation_start,
                "purge_cutoff_ts": cutoff,
                "purged_count": len(train_pool) - len(train),
            }
        )
    return result


def bootstrap_mean_lower_bound(
    returns: Sequence[float],
    *,
    iterations: int = 2_000,
    confidence: float = 0.95,
    seed: int = 15_008,
) -> float:
    """Deterministic one-sided bootstrap lower bound for mean trade return."""

    if not returns:
        return float("-inf")
    if iterations < 100 or not (0.5 < confidence < 1):
        raise ValueError("Invalid bootstrap settings.")
    values = [float(value) for value in returns]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Bootstrap returns must be finite.")
    rng = random.Random(seed)
    count = len(values)
    means = sorted(fmean(values[rng.randrange(count)] for _ in range(count)) for _ in range(iterations))
    index = max(0, min(iterations - 1, math.floor((1 - confidence) * iterations)))
    return means[index]


def promotion_gate(
    *,
    total_events: int,
    accepted_returns: Sequence[float],
    fold_net_returns: Sequence[float],
    brier: float,
    baseline_brier: float,
    true_forward_count: int,
    bootstrap_iterations: int = 2_000,
    bootstrap_seed: int = 15_008,
) -> dict[str, object]:
    """Apply the complete v2 shadow-to-paper promotion contract.

    The gate can fail honestly when there is no edge; it does not relax a
    threshold to manufacture a positive result.
    """

    returns = [float(value) for value in accepted_returns]
    folds = [float(value) for value in fold_net_returns]
    if total_events < 0 or true_forward_count < 0:
        raise ValueError("Counts cannot be negative.")
    if len(returns) > total_events or true_forward_count > total_events:
        raise ValueError("Accepted and forward counts cannot exceed total events.")
    if not all(math.isfinite(value) for value in returns + folds + [brier, baseline_brier]):
        raise ValueError("Promotion metrics must be finite.")
    if not (0 <= brier <= 1 and 0 <= baseline_brier <= 1):
        raise ValueError("Brier scores must be in [0, 1].")
    gains = sum(max(value, 0.0) for value in returns)
    losses = -sum(min(value, 0.0) for value in returns)
    profit_factor = gains / losses if losses else None
    profit_factor_pass = profit_factor > 1.2 if profit_factor is not None else gains > 0
    positive_folds = sum(value > 0 for value in folds)
    required_positive_folds = max(4, math.ceil(0.8 * len(folds))) if len(folds) >= 5 else 4
    bootstrap_lower = (
        bootstrap_mean_lower_bound(returns, iterations=bootstrap_iterations, seed=bootstrap_seed)
        if returns
        else None
    )
    checks = {
        "events_200": total_events >= 200,
        "accepted_30": len(returns) >= 30,
        "positive_folds_4_of_5": len(folds) >= 5 and positive_folds >= required_positive_folds,
        "profit_factor_above_1_2": profit_factor_pass,
        "brier_improves_baseline": brier < baseline_brier,
        "bootstrap_lower_above_zero": bootstrap_lower is not None and bootstrap_lower > 0,
        "true_forward_50": true_forward_count >= 50,
    }
    return {
        "eligible": all(checks.values()),
        "status": "paper_eligible" if all(checks.values()) else "shadow",
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "metrics": {
            "total_events": total_events,
            "accepted": len(returns),
            "folds": len(folds),
            "positive_folds": positive_folds,
            "required_positive_folds": required_positive_folds,
            "profit_factor": profit_factor,
            "brier": brier,
            "baseline_brier": baseline_brier,
            "bootstrap_mean_lower_95": bootstrap_lower,
            "true_forward_count": true_forward_count,
        },
    }


__all__ = [
    "BAR_MS",
    "POLICY",
    "SIGNAL_SPEC_VERSION",
    "CostModel",
    "BarrierSpec",
    "MAIN_SPEC",
    "STRESS_SPEC",
    "DEFAULT_COST",
    "MAX_ENTRY_SPREAD",
    "MIN_TARGET_NET_RETURN",
    "EXECUTION_POLICY_VERSION",
    "CandidateEvent",
    "LabelNotAvailableError",
    "validate_rows",
    "dataset_digest",
    "indicators",
    "candidate_signals",
    "generate_candidates",
    "execution_prices",
    "round_trip_net_return",
    "label_candidate",
    "label_candidates",
    "generate_labeled_samples",
    "select_compatible_samples",
    "purged_expanding_walk_forward",
    "bootstrap_mean_lower_bound",
    "promotion_gate",
]
