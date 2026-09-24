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

INTERVAL = "4h"
STEP_MS = 4 * 3600 * 1000
ENTRY_N = 100
EXIT_N = 50
VOL_BARS = 180                      # 30 days of 4h bars
VOL_TARGET = 0.40
BARS_PER_YEAR = 6 * 365
MAX_STOP_DISTANCE = Decimal("0.25")
STRATEGY_ID = "donchian100-4h-long-voltarget-v1"


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
                  breakout=bool(last.close > hi), breakdown=bool(last.close < lo), vol_scale=float(scale))


def protective_stop(entry_price: Decimal, exit_channel: float) -> Decimal:
    """Exchange-side stop: the exit channel, but never farther than 25%."""
    floor = entry_price * (1 - MAX_STOP_DISTANCE)
    channel = Decimal(str(exit_channel))
    return max(channel, floor) if channel < entry_price else floor
