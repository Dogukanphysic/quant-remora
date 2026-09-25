"""Selected rule from reports/unified-futures-research (status: Testnet exploration only).

4h Donchian breakout, long only: enter when the closed bar's close exceeds the
previous 100-bar high; exit when it closes below the previous 50-bar low.
Position notional is volatility targeted: min(1, 0.40 / annualised 30d vol).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

ENTRY_N = 100
EXIT_N = 50
VOL_TARGET = 0.40
MAX_STOP_DISTANCE = Decimal("0.25")
STRATEGY_ID = "donchian100-4h-long-voltarget-v1"


def configure(bar_hours: int) -> None:
    """4h (default, the researched Donchian rule) or 1h (experimental model mode only)."""
    global INTERVAL, STEP_MS, VOL_BARS, BARS_PER_YEAR, BAR_HOURS
    if bar_hours not in (1, 4):
        raise ValueError("supported intervals: 1h, 4h")
    BAR_HOURS, INTERVAL, STEP_MS = bar_hours, f"{bar_hours}h", bar_hours * 3600 * 1000
    VOL_BARS = 30 * 24 // bar_hours          # 30 days
    BARS_PER_YEAR = 24 // bar_hours * 365


configure(4)


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    bar_ts: int
    close: float
    entry_channel: float
    exit_channel: float
    breakout: bool
    breakdown: bool
    vol_scale: float
    vol_annual: float


def required_bars() -> int:
    return max(ENTRY_N, VOL_BARS) + 2


def evaluate(bars: Sequence[Bar]) -> Signal:
    """Signal for the last (closed) bar using only bars[:-1] for the channels."""
    if len(bars) < required_bars():
        raise ValueError("insufficient closed 4h history")
    for a, b in zip(bars, bars[1:]):
        if b.ts - a.ts != STEP_MS:
            raise ValueError("4h history is not contiguous")
    last = bars[-1]
    prior = bars[:-1]
    hi = max(b.high for b in prior[-ENTRY_N:])
    lo = min(b.low for b in prior[-EXIT_N:])
    closes = [b.close for b in bars[-(VOL_BARS + 1):]]
    rets = [c1 / c0 - 1 for c0, c1 in zip(closes, closes[1:])]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    vol = sd * math.sqrt(BARS_PER_YEAR)
    scale = min(1.0, VOL_TARGET / vol) if vol > 0 else 0.0
    return Signal(bar_ts=int(last.ts), close=float(last.close), entry_channel=float(hi), exit_channel=float(lo),
                  breakout=bool(last.close > hi), breakdown=bool(last.close < lo), vol_scale=float(scale),
                  vol_annual=float(vol))


def model_stop_distance(vol_annual: float) -> float:
    """Experimental model mode: 3x daily volatility, clamped to 3%..15%."""
    return min(0.15, max(0.03, 3 * vol_annual / math.sqrt(365)))


def protective_stop(entry_price: Decimal, exit_channel: float) -> Decimal:
    """Exchange-side stop: the exit channel, but never farther than 25%."""
    floor = entry_price * (1 - MAX_STOP_DISTANCE)
    channel = Decimal(str(exit_channel))
    return max(channel, floor) if channel < entry_price else floor
