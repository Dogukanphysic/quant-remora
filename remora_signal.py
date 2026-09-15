"""Causal Quant Remora v5 signal context for the current spot-paper data subset.

The SDD targets Binance Futures.  This module deliberately marks derivatives,
full order-book, short execution, and exchange reconciliation as unavailable
instead of fabricating them from Bitstamp OHLCV.
"""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Mapping, Sequence

from strategies import BAR_MS, indicators
from v2_engine import validate_rows


PROFILE = "quant_remora_v5_spot_ohlcv_paper_subset"
MIN_BARS = 820
VWAP_TOLERANCE_ATR = 0.30
ENTRY_STOCH_LOWER = 0.20
ENTRY_STOCH_UPPER = 0.80
PROBE_STOCH_LOWER = 0.30
PROBE_STOCH_UPPER = 0.70
PROBE_SCHEDULE_BARS = 4
PROBE_MIN_STOCH_CHANGE = 0.05


def _cross_side(previous: float, current: float, lower: float, upper: float) -> str | None:
    """Return one causal oscillator crossing side for a closed bar."""
    if previous <= lower < current:
        return "long"
    if previous >= upper > current:
        return "short"
    return None


def _probe_candidate(previous: float, current: float, decision_ts: int) -> tuple[str | None, str | None]:
    """Select one pre-registered probe without duplicating a decision bar."""
    capital_crossing = _cross_side(
        previous, current, ENTRY_STOCH_LOWER, ENTRY_STOCH_UPPER)
    if capital_crossing is not None:
        return capital_crossing, "capital_stoch_rsi_cross_20_80_h8"
    crossing = _cross_side(
        previous, current, PROBE_STOCH_LOWER, PROBE_STOCH_UPPER)
    if crossing is not None:
        return crossing, "stoch_rsi_cross_30_70_h8"
    hourly_close_offset = (PROBE_SCHEDULE_BARS - 1) * BAR_MS
    if (
        decision_ts % (PROBE_SCHEDULE_BARS * BAR_MS) == hourly_close_offset
        and abs(current - previous) >= PROBE_MIN_STOCH_CHANGE
    ):
        return ("long" if current > previous else "short"), "hourly_stoch_direction_h8"
    return None, None


def _ema(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    result[period - 1] = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        previous = result[index - 1]
        assert previous is not None
        result[index] = alpha * values[index] + (1 - alpha) * previous
    return result


def _complete_hours(rows: Sequence[Mapping[str, object]]) -> list[dict[str, float]]:
    groups: dict[int, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        groups[int(row["ts"]) // (4 * BAR_MS)].append(row)
    result = []
    for hour in sorted(groups):
        group = sorted(groups[hour], key=lambda row: int(row["ts"]))
        offsets = {int(row["ts"]) % (4 * BAR_MS) for row in group}
        if len(group) != 4 or offsets != {0, BAR_MS, 2 * BAR_MS, 3 * BAR_MS}:
            continue
        result.append({
            "ts": float(hour * 4 * BAR_MS),
            "open": float(group[0]["open"]),
            "high": max(float(row["high"]) for row in group),
            "low": min(float(row["low"]) for row in group),
            "close": float(group[-1]["close"]),
            "volume": sum(float(row["volume"]) for row in group),
        })
    return result


def _stoch_rsi(rsi: Sequence[float | None], index: int, period: int = 14) -> float | None:
    if index < period - 1:
        return None
    window = rsi[index - period + 1:index + 1]
    if any(value is None for value in window):
        return None
    values = [float(value) for value in window if value is not None]
    low, high = min(values), max(values)
    return 0.5 if high == low else (values[-1] - low) / (high - low)


def _session_vwap(rows: Sequence[Mapping[str, object]]) -> float:
    day_ms = 86_400_000
    day = int(rows[-1]["ts"]) // day_ms
    session = [row for row in rows if int(row["ts"]) // day_ms == day]
    volume = sum(float(row["volume"]) for row in session)
    if volume <= 0:
        raise ValueError("Remora session VWAP volume is unavailable.")
    return sum(
        ((float(row["high"]) + float(row["low"]) + float(row["close"])) / 3)
        * float(row["volume"])
        for row in session
    ) / volume


def evaluate(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Build one explainable, look-ahead-free 1H-context/15M-trigger decision."""
    validate_rows(rows)
    if len(rows) < MIN_BARS:
        raise ValueError(f"Quant Remora needs at least {MIN_BARS} closed 15M candles.")
    f = indicators(rows)
    i = len(rows) - 1
    hours = _complete_hours(rows)
    if len(hours) < 201:
        raise ValueError("Quant Remora needs at least 201 complete 1H candles.")
    hourly_close = [row["close"] for row in hours]
    ema20, ema50, ema200 = (_ema(hourly_close, period) for period in (20, 50, 200))
    h = len(hours) - 1
    required = (ema20[h], ema20[h - 1], ema50[h], ema50[h - 1],
                ema200[h], ema200[h - 1], f["atr"][i], f["rsi"][i])
    if any(value is None for value in required):
        raise ValueError("Quant Remora indicators are unavailable.")

    close = float(rows[i]["close"])
    previous_close = float(rows[i - 1]["close"])
    atr = float(f["atr"][i])
    atr_fraction = atr / close
    atr_history = [
        float(f["atr"][j]) / float(rows[j]["close"])
        for j in range(max(13, i - 199), i + 1) if f["atr"][j] is not None
    ]
    atr_percentile = sum(value <= atr_fraction for value in atr_history) / len(atr_history)
    e20, p20, e50, p50, e200, p200 = (float(value) for value in required[:6])
    hourly_price = hourly_close[h]
    bullish = hourly_price > e200 and e20 > e50 > e200 and e20 > p20 and e50 > p50
    bearish = hourly_price < e200 and e20 < e50 < e200 and e20 < p20 and e50 < p50
    trend = "bullish" if bullish else "bearish" if bearish else "uncertain"

    one_bar_return = close / previous_close - 1
    if atr_percentile >= 0.98 or abs(one_bar_return) >= max(0.03, 4 * atr_fraction):
        volatility = "extreme"
    elif atr_percentile >= 0.90:
        volatility = "high"
    elif atr_percentile <= 0.15:
        volatility = "low"
    else:
        volatility = "normal"
    trend_separation = abs(e20 / e50 - 1)
    if volatility == "extreme":
        regime = "extreme"
    elif volatility == "low":
        regime = "low_volatility"
    elif trend != "uncertain" and trend_separation >= 0.002:
        regime = "trending"
    else:
        regime = "ranging"

    stoch = _stoch_rsi(f["rsi"], i)
    previous_stoch = _stoch_rsi(f["rsi"], i - 1)
    if stoch is None or previous_stoch is None:
        raise ValueError("Quant Remora StochRSI is unavailable.")
    side = _cross_side(
        previous_stoch, stoch, ENTRY_STOCH_LOWER, ENTRY_STOCH_UPPER)
    probe_side, probe_profile = _probe_candidate(
        previous_stoch, stoch, int(rows[i]["ts"]))
    long_trigger = side == "long"
    short_trigger = side == "short"
    vwap = _session_vwap(rows)
    vwap_distance_atr = abs(close - vwap) / atr
    vwap_pass = vwap_distance_atr <= VWAP_TOLERANCE_ATR
    data_quality = float(rows[i]["volume"]) > 0
    components = {
        "data_quality": "PASS" if data_quality else "FAIL",
        "trend": "PASS" if trend in {"bullish", "bearish"} else "FAIL",
        "regime": "PASS" if regime == "trending" else "FAIL",
        "volatility": "PASS" if volatility == "normal" else "FAIL",
        "stoch_rsi": "PASS" if long_trigger or short_trigger else "NEUTRAL",
        "vwap": "PASS" if vwap_pass else "FAIL",
        "frvp": "UNAVAILABLE",
        "open_interest": "UNAVAILABLE",
        "long_short_ratio": "UNAVAILABLE",
        "funding": "UNAVAILABLE",
        "order_book_depth": "UNAVAILABLE",
        "liquidity": "PARTIAL_QUOTE_SPREAD_ONLY",
    }
    eligible_long = bool(
        data_quality and side == "long" and trend == "bullish"
        and regime == "trending" and volatility == "normal" and vwap_pass
    )
    failed = [key for key in ("data_quality", "trend", "regime", "volatility", "stoch_rsi", "vwap")
              if components[key] not in {"PASS"}]
    return {
        "profile": PROFILE,
        "full_sdd_data_available": False,
        "paper_market": "bitstamp_btcusd_spot",
        "decision_ts": int(rows[i]["ts"]),
        "side": side,
        "probe_side": probe_side,
        "probe_profile": probe_profile,
        "eligible_long": eligible_long,
        "short_execution_supported": False,
        "trend": trend,
        "regime": regime,
        "volatility": volatility,
        "atr_percentile": atr_percentile,
        "stoch_rsi": stoch,
        "previous_stoch_rsi": previous_stoch,
        "session_vwap": vwap,
        "vwap_distance_atr": vwap_distance_atr,
        "components": components,
        "failed_gates": failed,
        "features": {
            "ema20_1h_distance": hourly_price / e20 - 1,
            "ema50_1h_distance": hourly_price / e50 - 1,
            "ema200_1h_distance": hourly_price / e200 - 1,
            "ema20_1h_slope": e20 / p20 - 1,
            "ema50_1h_slope": e50 / p50 - 1,
            "ema200_1h_slope": e200 / p200 - 1,
            "atr_percentile": atr_percentile,
            "stoch_rsi": stoch,
            "previous_stoch_rsi": previous_stoch,
            "vwap_distance_atr": vwap_distance_atr,
            "trend_bullish": float(bullish),
            "trend_bearish": float(bearish),
            "regime_trending": float(regime == "trending"),
            "volatility_extreme": float(volatility == "extreme"),
        },
    }


__all__ = [
    "PROFILE", "MIN_BARS", "VWAP_TOLERANCE_ATR", "ENTRY_STOCH_LOWER",
    "ENTRY_STOCH_UPPER", "PROBE_STOCH_LOWER", "PROBE_STOCH_UPPER",
    "PROBE_SCHEDULE_BARS", "PROBE_MIN_STOCH_CHANGE", "evaluate",
]
