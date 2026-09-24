"""Research-only ADA Bollinger execution approximation with live-style exits.

Reads historical public candles; never accesses credentials or order state.
"""

from datetime import datetime, timezone
import json

import numpy as np

from dual_market_strategy_audit import FEE, ROOT, indicators, load

MAX_HOLD_BARS = 768  # 192 hours of 15m bars
ROOM_FLOOR = 0.006  # 0.30% round-trip assumed cost plus 0.30% cushion


def atr14(prices):
    high, low, close = (prices[k] for k in ('high', 'low', 'close'))
    n = len(close)
    result = np.full(n, np.nan)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    tr[1:] = np.maximum.reduce((high[1:] - low[1:],
                                np.abs(high[1:] - close[:-1]),
                                np.abs(low[1:] - close[:-1])))
    if n >= 14:
        result[13] = float(np.mean(tr[:14]))
        for i in range(14, n):
            result[i] = (13 * result[i-1] + tr[i]) / 14
    return result


def entry_variants(prices):
    close, low = prices['close'], prices['low']
    lower, upper, trend, _ = indicators(prices)
    touch = low <= lower
    reclaim = touch & (close > lower)
    room = upper / close - 1 >= ROOM_FLOOR
    def first_touch(signal):
        return signal & ~np.r_[False, signal[:-1]]

    variants = {
        'touch': touch,
        'touch_once': first_touch(touch),
        'reclaim': reclaim,
        'reclaim_once': first_touch(reclaim),
        'touch_trend': touch & trend,
        'reclaim_trend': reclaim & trend,
        'reclaim_trend_once': first_touch(reclaim & trend),
        'reclaim_room': reclaim & room,
        'reclaim_trend_room': reclaim & trend & room,
        'reclaim_trend_room_once': first_touch(reclaim & trend & room),
    }
    exit_signal = prices['high'] >= upper
    return variants, exit_signal


def simulate(prices, entry_signal, exit_signal, atr, start, end, fee=FEE):
    """Closed-candle signal, next-open entry/BB exit, conservative intrabar stop.

    Both stop and target hit in one bar is resolved at the stop. IOC fills,
    polling delays, spread, lot/min-notional and external cash flows are absent.
    """
    open_, high, low, close = (prices[k] for k in ('open', 'high', 'low', 'close'))
    cash, qty, cost, stop, target, entered = 1000.0, 0.0, 0.0, 0.0, 0.0, -1
    peak, max_dd, trades, wins = 1000.0, 0.0, 0, 0
    exits = {'band_or_timeout': 0, 'stop': 0, 'target': 0, 'end': 0}

    def sell(price, reason):
        nonlocal cash, qty, trades, wins
        cash = qty * price * (1 - fee)
        trades += 1
        wins += int(cash > cost)
        qty = 0.0
        exits[reason] += 1

    for i in range(start, end):
        had_position = qty > 0
        if had_position and i > start and (exit_signal[i-1] or i - entered >= MAX_HOLD_BARS):
            sell(open_[i], 'band_or_timeout')
        if not had_position and i > start and entry_signal[i-1] and np.isfinite(atr[i-1]) and atr[i-1] > 0:
            cost = cash
            qty = cash * (1 - fee) / open_[i]
            cash = 0.0
            entered = i
            stop = open_[i] - 2 * atr[i-1]
            target = open_[i] + 4 * atr[i-1]
        if qty:
            if low[i] <= stop:
                sell(min(open_[i], stop), 'stop')
            elif high[i] >= target:
                sell(target, 'target')
        equity = cash + qty * close[i] * (1 - fee)
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
    if qty:
        sell(close[end-1], 'end')
    return {'return_pct': float((cash / 1000 - 1) * 100), 'trades': trades,
            'wins': wins, 'max_drawdown_pct': float(max_dd * 100), 'exits': exits}


def research():
    ts, prices = load('adausdt')
    variants, exit_signal = entry_variants(prices)
    atr = atr14(prices)
    cuts = [int(len(ts) * x) for x in (0, .25, .5, .75, 1)]
    return {
        'as_of_utc': datetime.now(timezone.utc).isoformat(),
        'market': 'ADAUSDT',
        'candle_count': len(ts),
        'method': '15m closed signal, next-open entry and upper-band exit, 2ATR stop/4ATR target, 192h timeout, stop-first intrabar ambiguity, 0.15% each side',
        'room_floor': ROOM_FLOOR,
        'selection_or_execution_eligible': False,
        'variants': {name: [simulate(prices, signal, exit_signal, atr, cuts[i], cuts[i+1])
                            for i in range(4)] for name, signal in variants.items()},
    }


def main():
    report = research()
    (ROOT/'reports/ada-bb-execution-audit.json').write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    for name, folds in report['variants'].items():
        print(name, [(round(f['return_pct'], 2), f['trades'],
                      round(f['max_drawdown_pct'], 2)) for f in folds])


if __name__ == '__main__':
    main()
