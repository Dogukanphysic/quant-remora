"""Fixed ADA Bollinger upper-band continuation research; no order capability."""

import json

import numpy as np

from ada_bb_execution_audit import atr14, simulate
from dual_market_strategy_audit import ROOT, indicators, load


def breakout_signals(prices):
    lower, upper, trend, _ = indicators(prices)
    middle = (lower + upper) / 2
    close = prices['close']
    valid = np.isfinite(upper) & (upper > lower)
    upper_close = valid & (close > upper)
    first_upper_close = upper_close & ~np.r_[False, upper_close[:-1]]
    exits = {'middle_close': valid & (close < middle),
             'lower_touch': valid & (prices['low'] <= lower)}
    entries = {'upper_close': upper_close,
               'first_upper_close': first_upper_close,
               'upper_close_trend': upper_close & trend,
               'first_upper_close_trend': first_upper_close & trend}
    return entries, exits


def research():
    ts, prices = load('adausdt')
    entries, exits = breakout_signals(prices)
    atr = atr14(prices)
    cuts = [int(len(ts) * x) for x in (0, .25, .5, .75, 1)]
    output = {'symbol': 'ADAUSDT', 'candles': len(ts),
              'cost_per_side': 0.0015,
              'previously_inspected_archive': True,
              'independent_forward_evidence': False,
              'live_authorized': False, 'variants': {}}
    for entry_name, entry in entries.items():
        for exit_name, exit_ in exits.items():
            name = f'{entry_name}__{exit_name}'
            output['variants'][name] = {
                'entry_signal_count': int(np.count_nonzero(entry)),
                'folds': [simulate(prices, entry, exit_, atr, cuts[i], cuts[i + 1])
                          for i in range(4)],
            }
    return output


def main():
    report = research()
    (ROOT/'reports/ada-bollinger-breakout-research.json').write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    for name, variant in report['variants'].items():
        print(name, 'signals', variant['entry_signal_count'],
              'folds', [(round(f['return_pct'], 2), f['trades'])
                        for f in variant['folds']])


if __name__ == '__main__':
    main()
