"""Research-only three-condition strategy search on ADA/BTC Spot archives.

Inspired by configurable indicator/operator conjunctions, not a Freqtrade port.
Never imports a live worker, accesses credentials, or submits orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import product
import json
from pathlib import Path

import numpy as np

from dual_market_strategy_audit import FEE, ROOT, SYMBOLS, indicators, load, simulate


def sma(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if len(values) < period:
        return result
    total = np.r_[0.0, np.cumsum(values)]
    result[period - 1:] = (total[period:] - total[:-period]) / period
    return result


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    """Causal simple-window RSI; thresholds are fixed before selection."""
    gains = np.maximum(np.diff(values), 0.0)
    losses = np.maximum(-np.diff(values), 0.0)
    avg_gain = sma(gains, period)
    avg_loss = sma(losses, period)
    result = np.full(len(values), np.nan)
    denominator = avg_gain + avg_loss
    valid = denominator > 0
    result[1:][valid] = 100 * avg_gain[valid] / denominator[valid]
    return result


def condition_library(prices: dict[str, np.ndarray]) -> tuple[dict, dict, dict, np.ndarray]:
    close, low, high = prices['close'], prices['low'], prices['high']
    lower, upper, _, _ = indicators(prices)
    ma5, ma20, ma50, ma100 = (sma(close, period) for period in (5, 20, 50, 100))
    strength = rsi(close)
    previous_close = np.r_[np.nan, close[:-1]]
    previous_rsi = np.r_[np.nan, strength[:-1]]
    setup = {
        'lower_touch': low <= lower,
        'lower_reclaim': (low <= lower) & (close > lower),
        'below_lower_close': close < lower,
    }
    trend = {
        'ma20_gt_ma100': ma20 > ma100,
        'close_gt_ma100': close > ma100,
        'ma20_gt_ma50': ma20 > ma50,
    }
    trigger = {
        'rsi35_recovery': (previous_rsi <= 35) & (strength > 35),
        'up_close': close > previous_close,
        'close_gt_ma5': close > ma5,
    }
    # Same predeclared exit for every genome, so search cannot cherry-pick exits.
    exit_signal = (high >= upper) | (strength >= 70)
    return setup, trend, trigger, exit_signal


def choose(train_results: dict[str, list[dict]]) -> str | None:
    eligible = []
    for name, folds in train_results.items():
        if min(fold['trades'] for fold in folds) < 5:
            continue
        if max(fold['max_drawdown_pct'] for fold in folds) > 35:
            continue
        # Sort by worst train fold, then mean, then name for reproducibility.
        returns = [fold['return_pct'] for fold in folds]
        if min(returns) <= 0:
            continue
        eligible.append((min(returns), sum(returns) / len(returns), name))
    return max(eligible)[2] if eligible else None


def research() -> dict:
    markets = {}
    for symbol in SYMBOLS:
        ts, prices = load(symbol)
        setup, trend, trigger, exit_signal = condition_library(prices)
        cuts = [int(len(ts) * x) for x in (0, .25, .5, .75, 1)]
        results = {}
        for parts in product(setup, trend, trigger):
            name = '+'.join(parts)
            entry = setup[parts[0]] & trend[parts[1]] & trigger[parts[2]]
            results[name] = [
                simulate(prices, entry, exit_signal, cuts[i], cuts[i + 1])
                for i in range(4)
            ]
        winner = choose({name: folds[:2] for name, folds in results.items()})
        markets[symbol] = {
            'rows': len(ts),
            'train_periods': [[int(ts[cuts[i]]), int(ts[cuts[i + 1] - 1])] for i in (0, 1)],
            'validation_period': [int(ts[cuts[2]]), int(ts[cuts[3] - 1])],
            'audit_period': [int(ts[cuts[3]]), int(ts[-1])],
            'selected_genome': winner,
            'selected_folds': results[winner] if winner else None,
            'genomes_evaluated': len(results),
            'genomes': results,
        }
    return {
        'as_of_utc': datetime.now(timezone.utc).isoformat(),
        'method': 'Three fixed categories; train on first two quarters, inspect third and fourth chronologically; 15m closed candle/next open; 0.15% each side',
        'selection': 'Positive return and minimum five trades in each training fold; train max drawdown <=35%; maximize worst training return',
        'validation_is_independent_forward': False,
        'execution_eligible': False,
        'automatic_activation': False,
        'markets': markets,
    }


def main() -> None:
    report = research()
    path = ROOT / 'reports/genetic-confluence-research.json'
    path.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    for symbol, market in report['markets'].items():
        print(symbol.upper(), market['selected_genome'])
        if market['selected_folds']:
            print([(round(f['return_pct'], 2), f['trades'],
                    round(f['max_drawdown_pct'], 2)) for f in market['selected_folds']])


if __name__ == '__main__':
    main()
