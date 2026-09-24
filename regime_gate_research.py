"""Research-only causal cash gate for the three-condition ADA strategy."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import numpy as np

from dual_market_strategy_audit import FEE, ROOT, SYMBOLS, load, simulate
from genetic_confluence_research import condition_library

GENOME = ('lower_touch', 'ma20_gt_ma100', 'rsi35_recovery')
LOOKBACK_DAYS = (7, 30, 60)
CANDLES_PER_DAY = 96


def shadow_equity(prices: dict[str, np.ndarray], buy: np.ndarray,
                  sell: np.ndarray, fee: float = FEE) -> np.ndarray:
    """Candle-close mark from orders triggered at the previous close.

    Shadow trades continue while the gated strategy is in cash. At close i,
    the result includes only fills through open i and prices through close i.
    """
    open_, close = prices['open'], prices['close']
    cash, qty = 1000.0, 0.0
    equity = np.empty(len(close))
    for i in range(len(close)):
        if i:
            if qty and sell[i - 1]:
                cash = qty * open_[i] * (1 - fee)
                qty = 0.0
            elif not qty and buy[i - 1]:
                qty = cash * (1 - fee) / open_[i]
                cash = 0.0
        equity[i] = cash + qty * close[i] * (1 - fee)
    return equity


def profitable_window(equity: np.ndarray, lookback: int) -> np.ndarray:
    """Gate computed at close i; any order is filled no earlier than open i+1."""
    gate = np.zeros(len(equity), dtype=bool)
    gate[lookback:] = equity[lookback:] > equity[:-lookback]
    return gate


def research() -> dict:
    markets = {}
    for symbol in SYMBOLS:
        ts, prices = load(symbol)
        setup, trend, trigger, exit_signal = condition_library(prices)
        base_entry = setup[GENOME[0]] & trend[GENOME[1]] & trigger[GENOME[2]]
        shadow = shadow_equity(prices, base_entry, exit_signal)
        cuts = [int(len(ts) * x) for x in (0, .25, .5, .75, 1)]
        base = [simulate(prices, base_entry, exit_signal, cuts[i], cuts[i + 1])
                for i in range(4)]
        windows = {}
        for days in LOOKBACK_DAYS:
            gate = profitable_window(shadow, days * CANDLES_PER_DAY)
            # If the observed shadow period has turned negative, flatten at the
            # next open. The shadow keeps trading so the gate can later reopen.
            enter = base_entry & gate
            leave = exit_signal | ~gate
            windows[str(days)] = {
                'folds': [simulate(prices, enter, leave, cuts[i], cuts[i + 1])
                          for i in range(4)],
                'gate_open_fraction_by_fold': [
                    float(gate[cuts[i]:cuts[i + 1]].mean()) for i in range(4)
                ],
            }
        markets[symbol] = {'baseline_folds': base, 'windows': windows}
    return {
        'as_of_utc': datetime.now(timezone.utc).isoformat(),
        'genome': '+'.join(GENOME),
        'method': 'Shadow strategy runs continuously. At closed candle i, gate is open only if shadow net marked equity exceeds its value 7/30/60 days ago. Gated orders use next candle open. Costs 0.15% each side.',
        'parameter_search': False,
        'execution_eligible': False,
        'automatic_activation': False,
        'markets': markets,
    }


def main() -> None:
    report = research()
    (ROOT / 'reports/regime-gate-research.json').write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    for symbol, market in report['markets'].items():
        print(symbol.upper(), 'baseline',
              [round(f['return_pct'], 2) for f in market['baseline_folds']])
        for days, item in market['windows'].items():
            print(f'{days}d', [round(f['return_pct'], 2) for f in item['folds']])


if __name__ == '__main__':
    main()
