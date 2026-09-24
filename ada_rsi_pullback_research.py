"""Research-only 15m ADA RSI pullback candidates; never places orders."""

from datetime import datetime, timezone
import json

import numpy as np

from ada_bb_execution_audit import atr14, simulate
from dual_market_strategy_audit import ROOT, indicators, load


def rsi14(close):
    """Wilder RSI using only information available at each closed candle."""
    result = np.full(len(close), np.nan)
    if len(close) < 15:
        return result
    changes = np.diff(close)
    gains = np.maximum(changes, 0)
    losses = np.maximum(-changes, 0)
    average_gain = float(np.mean(gains[:14]))
    average_loss = float(np.mean(losses[:14]))
    for i in range(14, len(close)):
        if i > 14:
            average_gain = (13 * average_gain + gains[i - 1]) / 14
            average_loss = (13 * average_loss + losses[i - 1]) / 14
        denominator = average_gain + average_loss
        result[i] = 50.0 if denominator == 0 else 100 * average_gain / denominator
    return result


def research():
    ts, prices = load('adausdt')
    rsi = rsi14(prices['close'])
    lower, upper, trend, _ = indicators(prices)
    atr = atr14(prices)
    valid = np.isfinite(rsi) & np.isfinite(lower) & np.isfinite(upper)
    previous_rsi = np.r_[np.nan, rsi[:-1]]
    # Three fixed hypotheses: recovering pullback in a 4h uptrend, with and
    # without a lower-band proximity check, plus a more eager RSI crossing.
    candidates = {
        'rsi40_recovery_trend': (previous_rsi < 40) & (rsi >= 40) & trend,
        'rsi45_recovery_trend': (previous_rsi < 45) & (rsi >= 45) & trend,
        'rsi40_recovery_trend_near_lower':
            (previous_rsi < 40) & (rsi >= 40) & trend &
            (prices['low'] <= lower + .25 * (upper - lower)),
    }
    exit_signal = (rsi >= 60) | (prices['high'] >= upper)
    cuts = [int(len(ts) * x) for x in (0, .25, .5, .75, 1)]
    output = {
        'as_of_utc': datetime.now(timezone.utc).isoformat(),
        'symbol': 'ADAUSDT', 'candles': len(ts),
        'archive_previously_inspected': True,
        'independent_forward_evidence': False,
        'live_eligible': False,
        'assumptions': 'closed 15m signal, next open; 0.15% per side; '
                       '2ATR stop, 4ATR target, 192h timeout; full capital',
        'variants': {},
    }
    for name, entry in candidates.items():
        entry &= valid
        output['variants'][name] = {
            'signal_count': int(np.count_nonzero(entry)),
            'folds': [simulate(prices, entry, exit_signal, atr, cuts[i], cuts[i + 1])
                      for i in range(4)],
        }
    return output


def main():
    result = research()
    path = ROOT / 'reports/ada-rsi-pullback-research.json'
    path.write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    for name, item in result['variants'].items():
        print(name, 'signals', item['signal_count'],
              'folds', [(round(f['return_pct'], 2), f['trades'])
                        for f in item['folds']])


if __name__ == '__main__':
    main()
