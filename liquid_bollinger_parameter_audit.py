"""Development-only causal Bollinger grid with a final chronological check."""

import csv
import json
from itertools import product

import numpy as np

from ada_bb_execution_audit import atr14, simulate
from dual_market_strategy_audit import ROOT, indicators
from liquid_bollinger_coin_research import CANDIDATES, DATA_DIR, HISTORY_DAYS

WINDOWS = (20, 40, 80, 160)
DEVIATIONS = (1.5, 2.0, 2.5)


def load(symbol):
    with (DATA_DIR/f'{symbol.lower()}-{HISTORY_DAYS}d.csv').open(newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    return {key: np.asarray([float(row[key]) for row in rows])
            for key in ('open', 'high', 'low', 'close', 'volume')}


def bands(close, window, deviations):
    lower = np.full(len(close), np.nan)
    middle = np.full(len(close), np.nan)
    upper = np.full(len(close), np.nan)
    total = np.r_[0.0, np.cumsum(close)]
    squares = np.r_[0.0, np.cumsum(close*close)]
    for i in range(window, len(close)):
        mean = (total[i]-total[i-window])/window
        variance = max(0.0, (squares[i]-squares[i-window])/window-mean*mean)
        width = deviations*np.sqrt(variance)
        lower[i], middle[i], upper[i] = mean-width, mean, mean+width
    return lower, middle, upper


def configs(prices):
    _, _, trend, _ = indicators(prices)
    close, low, high = prices['close'], prices['low'], prices['high']
    for window, deviations, entry_kind, exit_kind, trend_required in product(
            WINDOWS, DEVIATIONS, ('touch', 'reclaim'), ('middle', 'upper'), (False, True)):
        lower, middle, upper = bands(close, window, deviations)
        entry = low <= lower
        if entry_kind == 'reclaim':
            entry &= close > lower
        if trend_required:
            entry &= trend
        exit_ = high >= (middle if exit_kind == 'middle' else upper)
        name = f'w{window}_d{deviations:g}_{entry_kind}_{exit_kind}' + ('_trend' if trend_required else '')
        yield name, entry, exit_


def evaluate(prices):
    atr = atr14(prices)
    n = len(prices['close'])
    cuts = [int(n*x) for x in (0, .25, .5, .75, 1)]
    rows = []
    for name, entry, exit_ in configs(prices):
        folds = [simulate(prices, entry, exit_, atr, cuts[i], cuts[i+1]) for i in range(4)]
        development = folds[:3]
        development_score = min(f['return_pct'] for f in development)
        sufficient = all(f['trades'] >= 10 for f in development)
        rows.append({'name': name, 'folds': folds,
                     'development_score': development_score,
                     'development_sufficient': sufficient})
    rows.sort(key=lambda x: (x['development_sufficient'], x['development_score']), reverse=True)
    return rows


def main():
    report = {'method': '96 fixed causal configurations per pair; rank first 3 folds, report fourth separately; 0.15% each side',
              'automatic_live_activation': False, 'markets': {}}
    for symbol in CANDIDATES:
        rows = evaluate(load(symbol))
        report['markets'][symbol] = {'best_development': rows[0], 'top10': rows[:10]}
        best = rows[0]
        print(symbol, best['name'], 'dev', round(best['development_score'], 2),
              'folds', [(round(f['return_pct'], 2), f['trades']) for f in best['folds']])
    path = ROOT/'reports/liquid-bollinger-parameter-audit.json'
    path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
