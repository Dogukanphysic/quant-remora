"""Fixed Bollinger-zone sensitivity study; public history only, no orders."""

import json

import numpy as np

from ada_bb_execution_audit import atr14, simulate
from dual_market_strategy_audit import ROOT, indicators, load

ZONE_FRACTIONS = (0.0, 0.10, 0.20, 0.35)


def zone_signals(prices, fraction, require_trend=False):
    if fraction not in ZONE_FRACTIONS:
        raise ValueError('Unregistered Bollinger zone')
    lower, upper, trend, _ = indicators(prices)
    width = upper - lower
    entry = prices['low'] <= lower + fraction * width
    exit_ = prices['high'] >= upper - fraction * width
    valid = np.isfinite(width) & (width > 0)
    if require_trend:
        entry &= trend
    return entry & valid, exit_ & valid


def research():
    ts, prices = load('adausdt')
    atr = atr14(prices)
    cuts = [int(len(ts) * x) for x in (0, .25, .5, .75, 1)]
    output = {'symbol': 'ADAUSDT', 'candle_count': len(ts),
              'cost_per_side': 0.0015,
              'execution': 'closed candle signal, next open fill; 2ATR stop, 4ATR target, 192h max hold',
              'previously_inspected_archive': True,
              'independent_forward_evidence': False,
              'live_authorized': False,
              'variants': {}}
    for fraction in ZONE_FRACTIONS:
        for require_trend in (False, True):
            entry, exit_ = zone_signals(prices, fraction, require_trend)
            name = f'{fraction:g}' + ('_trend' if require_trend else '')
            output['variants'][name] = {
                'entry_signal_count': int(np.count_nonzero(entry)),
                'folds': [simulate(prices, entry, exit_, atr, cuts[i], cuts[i+1])
                          for i in range(4)],
            }
    return output


def main():
    result = research()
    (ROOT/'reports/ada-bollinger-zone-research.json').write_text(
        json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    for fraction, variant in result['variants'].items():
        print(fraction, 'signals', variant['entry_signal_count'],
              'folds', [(round(f['return_pct'], 2), f['trades'])
                        for f in variant['folds']])


if __name__ == '__main__':
    main()
