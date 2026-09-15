"""Cost-stressed, leakage-aware 4H trend research for Binance USD-M data."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Mapping, Sequence

import binance_archive


ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT = ROOT / "reports/binance-long-horizon-research.json"
FOUR_HOURS_MS = 4 * 60 * 60 * 1000
ONE_WAY_STRESSED_COST = 0.002  # 15bp fee/slippage assumption + 5bp stress.


def resample_4h(rows: Sequence[Mapping[str, object]]) -> list[dict[str, float | int]]:
    if len(rows) < 16:
        raise ValueError("4H araştırması için en az 16 adet 15m mum gerekir.")
    result = []
    for start in range(0, len(rows), 16):
        group = rows[start:start + 16]
        if len(group) < 16:
            break
        first_ts = int(group[0]["ts"])
        if first_ts % FOUR_HOURS_MS or any(
                int(row["ts"]) != first_ts + index * 900_000
                for index, row in enumerate(group)):
            raise ValueError("15m mumlar tam UTC 4H bloklarına hizalı değil.")
        result.append({
            "ts": first_ts, "open": float(group[0]["open"]),
            "high": max(float(row["high"]) for row in group),
            "low": min(float(row["low"]) for row in group),
            "close": float(group[-1]["close"]),
            "volume": sum(float(row["volume"]) for row in group),
        })
    return result


def _ema(closes: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return result
    result[period - 1] = sum(closes[:period]) / period
    alpha = 2 / (period + 1)
    for index in range(period, len(closes)):
        previous = result[index - 1]
        assert previous is not None
        result[index] = alpha * closes[index] + (1 - alpha) * previous
    return result


def _metrics(closes: Sequence[float], positions: Sequence[int],
             start: int, end: int) -> dict[str, object]:
    values = []
    previous_position = 0
    turnover = 0
    for index in range(max(1, start), end):
        position = int(positions[index - 1])
        change = abs(position - previous_position)
        turnover += change
        values.append(
            position * (closes[index] / closes[index - 1] - 1)
            - change * ONE_WAY_STRESSED_COST
        )
        previous_position = position
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= max(1e-9, 1 + value)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
    mean = statistics.mean(values) if values else 0.0
    deviation = statistics.pstdev(values) if values else 0.0
    return {
        "bars": len(values), "return": equity - 1,
        "annualized_sharpe": mean / deviation * math.sqrt(6 * 365)
        if deviation > 1e-12 else 0.0,
        "max_drawdown": max_drawdown, "turnover_units": turnover,
    }


def _ema_positions(closes, fast_values, slow_values, buffer, allow_short):
    result, state = [0] * len(closes), 0
    for index in range(len(closes)):
        if fast_values[index] is None or slow_values[index] is None:
            continue
        ratio = float(fast_values[index]) / float(slow_values[index]) - 1
        if ratio > buffer:
            state = 1
        elif ratio < -buffer:
            state = -1 if allow_short else 0
        result[index] = state
    return result


def _momentum_positions(closes, lookback, threshold, allow_short):
    result = [0] * len(closes)
    for index in range(lookback, len(closes)):
        momentum = closes[index] / closes[index - lookback] - 1
        result[index] = 1 if momentum > threshold else (
            -1 if allow_short and momentum < -threshold else 0)
    return result


def _donchian_positions(closes, entry, exit_, allow_short):
    result, state = [0] * len(closes), 0
    for index in range(max(entry, exit_), len(closes)):
        entry_high, entry_low = max(closes[index-entry:index]), min(closes[index-entry:index])
        exit_high, exit_low = max(closes[index-exit_:index]), min(closes[index-exit_:index])
        if state == 0:
            if closes[index] > entry_high:
                state = 1
            elif allow_short and closes[index] < entry_low:
                state = -1
        elif state == 1 and closes[index] < exit_low:
            state = 0
        elif state == -1 and closes[index] > exit_high:
            state = 0
        result[index] = state
    return result


def research(data_path: Path, report_path: Path = DEFAULT_REPORT) -> dict[str, object]:
    rows = binance_archive.read_dataset(data_path, "um", "BTCUSDT")
    bars = resample_4h(rows)
    if len(bars) < 5_000:
        raise ValueError("Uzun ufuk araştırması için en az 5.000 adet 4H mum gerekir.")
    closes = [float(row["close"]) for row in bars]
    periods = (6, 12, 24, 42, 48, 72, 120, 180, 240, 360)
    emas = {period: _ema(closes, period) for period in periods}
    configurations = []
    for allow_short in (False, True):
        mode = "long_short" if allow_short else "long_cash"
        for fast in (6, 12, 24, 42):
            for slow in (48, 72, 120, 180, 240, 360):
                for buffer in (0.0, 0.002, 0.005, 0.01):
                    configurations.append({
                        "family": "ema", "mode": mode, "fast": fast, "slow": slow,
                        "buffer": buffer,
                        "positions": _ema_positions(
                            closes, emas[fast], emas[slow], buffer, allow_short),
                    })
        for lookback in (42, 72, 120, 180, 240, 360, 540):
            for threshold in (0.0, 0.02, 0.05, 0.1):
                configurations.append({
                    "family": "momentum", "mode": mode, "lookback": lookback,
                    "threshold": threshold,
                    "positions": _momentum_positions(
                        closes, lookback, threshold, allow_short),
                })
        for entry in (24, 42, 72, 120, 180, 240, 360):
            for exit_ in (12, 24, 42, 72, 120):
                if exit_ >= entry:
                    continue
                configurations.append({
                    "family": "donchian", "mode": mode, "entry": entry, "exit": exit_,
                    "positions": _donchian_positions(closes, entry, exit_, allow_short),
                })

    development_end = len(bars) * 2 // 5
    selection_end = len(bars) * 4 // 5
    evaluated = []
    for configuration in configurations:
        positions = configuration.pop("positions")
        development = _metrics(closes, positions, 0, development_end)
        selection = _metrics(closes, positions, development_end, selection_end)
        passed = bool(
            development["return"] > 0 and selection["return"] > 0
            and development["annualized_sharpe"] >= 0.5
            and selection["annualized_sharpe"] >= 0.5
            and development["max_drawdown"] <= 0.35
            and selection["max_drawdown"] <= 0.35
            and selection["turnover_units"] >= 4
        )
        evaluated.append({
            "configuration": configuration, "positions": positions,
            "development": development, "selection": selection,
            "selection_gate_pass": passed,
            "robust_score": min(development["annualized_sharpe"],
                                selection["annualized_sharpe"]),
        })
    eligible = [item for item in evaluated if item["selection_gate_pass"]]
    selected = max(eligible, key=lambda item: item["robust_score"]) if eligible else None
    selected_public = holdout = None
    holdout_pass = False
    if selected:
        holdout = _metrics(closes, selected["positions"], selection_end, len(bars))
        holdout_pass = bool(
            holdout["return"] > 0 and holdout["annualized_sharpe"] >= 0.5
            and holdout["max_drawdown"] <= 0.35)
        selected_public = {key: value for key, value in selected.items() if key != "positions"}
    buy_hold = [1] * len(bars)
    report = {
        "kind": "binance_btcusdt_4h_trend_research",
        "data_sha256": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
        "candles_15m": len(rows), "bars_4h": len(bars),
        "configuration_count": len(evaluated),
        "split": {"development_bars": development_end,
                  "selection_bars": selection_end - development_end,
                  "holdout_bars": len(bars) - selection_end},
        "one_way_stressed_cost": ONE_WAY_STRESSED_COST,
        "selection_gate": "positive return, Sharpe>=0.5, drawdown<=35% in development and selection",
        "eligible_before_holdout": len(eligible), "selected": selected_public,
        "holdout": holdout, "holdout_pass": holdout_pass,
        "buy_hold": {
            "development": _metrics(closes, buy_hold, 0, development_end),
            "selection": _metrics(closes, buy_hold, development_end, selection_end),
            "holdout": _metrics(closes, buy_hold, selection_end, len(bars)),
        },
        "deployed": False, "real_orders_enabled": False,
        "note": "Offline research; a holdout failure blocks paper deployment.",
    }
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


__all__ = ["DEFAULT_REPORT", "ONE_WAY_STRESSED_COST", "resample_4h", "research"]
