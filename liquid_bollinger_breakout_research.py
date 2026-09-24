"""Fixed Bollinger breakout/reversion exits on cached liquid-pair 15m data."""

import csv
from datetime import datetime, timezone
import json

import numpy as np

from ada_bb_execution_audit import atr14, simulate
from dual_market_strategy_audit import ROOT, indicators
from liquid_bollinger_coin_research import CANDIDATES, DATA_DIR, HISTORY_DAYS


def load(symbol):
    path = DATA_DIR / f'{symbol.lower()}-{HISTORY_DAYS}d.csv'
    with path.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    prices = {key: np.asarray([float(row[key]) for row in rows])
              for key in ('open', 'high', 'low', 'close', 'volume')}
    return prices


def rolling_quantile(values, window, quantile):
    out = np.full(len(values), np.nan)
    for i in range(window, len(values)):
        history = values[i-window:i]
        history = history[np.isfinite(history)]
        if len(history) == window:
            out[i] = np.quantile(history, quantile)
    return out


def rules(prices):
    close = prices['close']
    lower, upper, trend, _ = indicators(prices)
    middle = (lower + upper) / 2
    width_pct = (upper - lower) / middle
    squeeze_limit = rolling_quantile(width_pct, 96, .20)
    prior_close = np.r_[np.nan, close[:-1]]
    cross_upper = (close > upper) & (prior_close <= np.r_[np.nan, upper[:-1]])
    squeeze_recent = np.zeros(len(close), dtype=bool)
    for i in range(4, len(close)):
        values = width_pct[i-4:i]
        limits = squeeze_limit[i-4:i]
        squeeze_recent[i] = np.any(np.isfinite(values) & np.isfinite(limits) & (values <= limits))
    exact_reclaim = (prices['low'] <= lower) & (close > lower)
    return {
        'upper_breakout_4h_trend': (cross_upper & trend, close < middle),
        'squeeze_upper_breakout_4h_trend': (cross_upper & trend & squeeze_recent, close < middle),
        'lower_reclaim_to_middle': (exact_reclaim, prices['high'] >= middle),
        'lower_reclaim_4h_trend_to_middle': (exact_reclaim & trend, prices['high'] >= middle),
    }


def evaluate(prices):
    atr = atr14(prices)
    n = len(prices['close'])
    cuts = [int(n*x) for x in (0, .25, .5, .75, 1)]
    return {name: [simulate(prices, entry, exit_, atr, cuts[i], cuts[i+1])
                   for i in range(4)]
            for name, (entry, exit_) in rules(prices).items()}


def main():
    report = {'as_of_utc': datetime.now(timezone.utc).isoformat(),
              'method': 'cached Binance Spot 15m, next open, 2ATR stop, 4ATR target, 192h timeout, 0.15% each side',
              'live_eligible': False, 'markets': {}}
    for symbol in CANDIDATES:
        report['markets'][symbol] = evaluate(load(symbol))
    eligible = []
    for symbol, variants in report['markets'].items():
        for name, folds in variants.items():
            if all(f['return_pct'] > 0 and f['trades'] >= 10 and f['max_drawdown_pct'] < 20
                   for f in folds):
                eligible.append([symbol, name])
    report['eligible'] = eligible
    path = ROOT / 'reports/liquid-bollinger-breakout-research.json'
    path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    for symbol, variants in report['markets'].items():
        print(symbol)
        for name, folds in variants.items():
            print(' ', name, [(round(f['return_pct'], 2), f['trades']) for f in folds])
    print('eligible', eligible)


if __name__ == '__main__':
    main()
