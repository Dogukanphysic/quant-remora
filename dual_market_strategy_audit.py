"""Fixed, no-optimization ADA/BTC research comparison. Never submits orders."""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
SYMBOLS = ('adausdt', 'btcusdt')
NAMES = ('bb_touch', 'bb_reclaim_trend', 'bb_touch_trend',
         'trend_4h', 'momentum_30d')
FEE = 0.0015  # 0.1% typical commission + 0.05% execution stress, each side.


def load(symbol):
    path = ROOT / f'data/{symbol}-15m-long.csv'
    with path.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    ts = np.asarray([int(row['ts']) for row in rows], dtype=np.int64)
    if len(rows) < 20000 or np.any(np.diff(ts) != 900000):
        raise ValueError(f'{symbol}: incomplete 15m archive')
    arrays = {key: np.asarray([float(row[key]) for row in rows])
              for key in ('open', 'high', 'low', 'close')}
    if any(not np.isfinite(a).all() or np.any(a <= 0) for a in arrays.values()):
        raise ValueError(f'{symbol}: invalid OHLC')
    return ts, arrays


def indicators(prices):
    close = prices['close']
    n = len(close)
    lower = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    cumulative = np.r_[0.0, np.cumsum(close)]
    squares = np.r_[0.0, np.cumsum(close*close)]
    for i in range(20, n):
        mean = (cumulative[i]-cumulative[i-20])/20
        variance = max(0.0, (squares[i]-squares[i-20])/20-mean*mean)
        width = 2*np.sqrt(variance)
        lower[i], upper[i] = mean-width, mean+width
    fast = slow = long = None
    trend = np.zeros(n, dtype=bool)
    weak = np.zeros(n, dtype=bool)
    for i in range(n):
        if i % 16 == 15:
            value = close[i]
            fast = value if fast is None else fast + 2/21*(value-fast)
            slow = value if slow is None else slow + 2/51*(value-slow)
            long = value if long is None else long + 2/201*(value-long)
        if i >= 3200 and fast is not None:
            trend[i] = fast > slow and close[i] > long
            weak[i] = fast < slow or close[i] < long
    return lower, upper, trend, weak


def signals(name, prices, lower, upper, trend, weak):
    close = prices['close']
    low = prices['low']
    high = prices['high']
    n = len(close)
    buy = np.zeros(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)
    if name.startswith('bb_'):
        buy = low <= lower
        sell = high >= upper
        if name != 'bb_touch':
            buy &= trend
            sell |= weak
        if name == 'bb_reclaim_trend':
            buy &= close > lower
    elif name == 'trend_4h':
        buy = trend.copy()
        sell = weak.copy()
    elif name == 'momentum_30d':
        for i in range(2880, n):
            if i % 96 == 95:
                ratio = close[i]/close[i-2880]-1
                buy[i] = ratio > .02
                sell[i] = ratio < 0
    else:
        raise ValueError(name)
    return buy, sell


def simulate(prices, buy, sell, start, end, fee=FEE):
    open_, close = prices['open'], prices['close']
    cash, qty, entry_cost = 1000.0, 0.0, 0.0
    peak, drawdown = 1000.0, 0.0
    trades = []
    for i in range(start, end-1):
        price = open_[i+1]
        if qty and sell[i]:
            cash = qty*price*(1-fee)
            trades.append(cash-entry_cost)
            qty = 0.0
        elif not qty and buy[i]:
            entry_cost = cash
            qty = cash*(1-fee)/price
            cash = 0.0
        equity = cash + qty*close[i]*(1-fee)
        peak = max(peak, equity)
        drawdown = max(drawdown, 1-equity/peak)
    if qty:
        cash = qty*close[end-1]*(1-fee)
        trades.append(cash-entry_cost)
    profits = sum(max(0, p) for p in trades)
    losses = -sum(min(0, p) for p in trades)
    return dict(return_pct=float((cash/1000-1)*100), trades=len(trades),
                wins=int(sum(p > 0 for p in trades)),
                profit_factor=float(profits/losses) if losses else None,
                max_drawdown_pct=float(drawdown*100))


def research():
    output = dict(as_of_utc=datetime.now(timezone.utc).isoformat(),
                  method='15m closed candle decision, next 15m open execution, full allocation, 0.15% cost each side',
                  selection='Five named rules fixed before the diagnostic last quarter; no parameter search',
                  execution_eligible=False, automatic_activation=False, markets={})
    for symbol in SYMBOLS:
        ts, prices = load(symbol)
        lower, upper, trend, weak = indicators(prices)
        cuts = [int(len(ts)*fraction) for fraction in (0, .25, .5, .75, 1)]
        market = dict(rows=len(ts), start_utc=datetime.fromtimestamp(ts[0]/1000,timezone.utc).isoformat(),
                      end_utc=datetime.fromtimestamp(ts[-1]/1000,timezone.utc).isoformat(), rules={})
        for name in NAMES:
            buy, sell = signals(name, prices, lower, upper, trend, weak)
            market['rules'][name] = [simulate(prices,buy,sell,cuts[i],cuts[i+1]) for i in range(4)]
        market['buy_hold'] = [
            float((prices['close'][cuts[i+1]-1]/prices['open'][cuts[i]]*(1-FEE)/(1+FEE)-1)*100)
            for i in range(4)]
        output['markets'][symbol] = market
    return output


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=ROOT/'reports/dual-market-strategy-audit.json')
    args = parser.parse_args()
    report = research()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    for symbol, market in report['markets'].items():
        print(symbol.upper(), 'buy_hold', [round(x,2) for x in market['buy_hold']])
        for name, folds in market['rules'].items():
            print(name, [(round(f['return_pct'],2),f['trades'],round(f['max_drawdown_pct'],1)) for f in folds])


if __name__ == '__main__':
    main()
