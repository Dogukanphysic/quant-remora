"""Translate the supplied Bollinger article into fixed, causal long-only rules."""

import csv
from datetime import datetime, timezone
import json

import numpy as np

from ada_bb_execution_audit import atr14, simulate
from ada_rsi_pullback_research import rsi14
from dual_market_strategy_audit import ROOT, indicators
from liquid_bollinger_coin_research import DATA_DIR, HISTORY_DAYS


def load_all():
    for path in sorted(DATA_DIR.glob(f'*-{HISTORY_DAYS}d.csv')):
        symbol = path.name.split('-')[0].upper()
        with path.open(newline='', encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        prices = {key: np.asarray([float(row[key]) for row in rows])
                  for key in ('open', 'high', 'low', 'close', 'volume')}
        yield symbol, prices


def trailing_median(values, window):
    out = np.full(len(values), np.nan)
    for i in range(window, len(values)):
        out[i] = np.median(values[i-window:i])
    return out


def trailing_quantile(values, window, q):
    out = np.full(len(values), np.nan)
    for i in range(window, len(values)):
        history = values[i-window:i]
        history = history[np.isfinite(history)]
        if len(history) == window:
            out[i] = np.quantile(history, q)
    return out


def mfi14(prices):
    typical = (prices['high'] + prices['low'] + prices['close']) / 3
    flow = typical * prices['volume']
    direction = np.diff(typical, prepend=typical[0])
    positive = np.where(direction > 0, flow, 0.0)
    negative = np.where(direction < 0, flow, 0.0)
    out = np.full(len(typical), np.nan)
    for i in range(14, len(typical)):
        pos = float(np.sum(positive[i-13:i+1]))
        neg = float(np.sum(negative[i-13:i+1]))
        out[i] = 100.0 if neg == 0 and pos > 0 else (50.0 if pos+neg == 0 else 100-100/(1+pos/neg))
    return out


def w_bottom_confirmation(prices, percent_b, volume_median):
    """Causal W: first low outside, second low inside/lower volume, then confirmation."""
    low, high, close, volume = (prices[k] for k in ('low', 'high', 'close', 'volume'))
    signal = np.zeros(len(close), dtype=bool)
    for i in range(35, len(close)):
        second = i-1
        if not (percent_b[second] > 0 and close[i] > high[second] and
                volume[i] >= 1.5*volume_median[i]):
            continue
        candidates = [j for j in range(i-32, i-7)
                      if percent_b[j] < 0 and low[j] <= np.min(low[max(0,j-2):j+3])]
        if not candidates:
            continue
        first = min(candidates, key=lambda j: low[j])
        if low[second] <= low[first]*1.03 and volume[second] < volume[first]:
            signal[i] = True
    return signal


def ema(values, length):
    out = np.empty(len(values))
    out[0] = values[0]
    alpha = 2/(length+1)
    for i in range(1, len(values)):
        out[i] = out[i-1] + alpha*(values[i]-out[i-1])
    return out


def tradingview_pullback_confirmation(close, middle, trend, rsi, bars=2):
    signal = np.zeros(len(close), dtype=bool)
    trigger = (close > middle) & (np.r_[np.nan, close[:-1]] <= np.r_[np.nan, middle[:-1]]) & trend & (rsi > 55)
    for i in range(len(close)-bars):
        if trigger[i] and np.all(close[i:i+bars+1] > middle[i:i+bars+1]) and np.all(rsi[i:i+bars+1] > 55):
            signal[i+bars] = True
    # Five-bar cooldown mirrors the catalog's anti-stacking idea.
    last = -99
    for i in np.flatnonzero(signal):
        if i-last < 5:
            signal[i] = False
        else:
            last = i
    return signal


def rules(prices):
    close, low, high, volume = (prices[k] for k in ('close', 'low', 'high', 'volume'))
    lower, upper, trend, _ = indicators(prices)
    middle = (lower + upper) / 2
    width = (upper-lower)/middle
    percent_b = (close-lower)/(upper-lower)
    squeeze = width <= trailing_quantile(width, 96, .20)
    volume_confirmed = volume >= 1.5*trailing_median(volume, 20)
    rsi = rsi14(close)
    mfi = mfi14(prices)
    volume_median = trailing_median(volume, 20)
    prior_close = np.r_[np.nan, close[:-1]]
    prior_lower = np.r_[np.nan, lower[:-1]]
    prior_upper = np.r_[np.nan, upper[:-1]]
    prior_rsi = np.r_[np.nan, rsi[:-1]]
    squeeze_recent = np.zeros(len(close), dtype=bool)
    for i in range(4, len(close)):
        squeeze_recent[i] = np.any(squeeze[i-4:i])

    # Article interpretation 1: squeeze gives no direction; enter only after
    # an upper breakout confirmed by both volume and the causal 4h trend.
    breakout = (close > upper) & (prior_close <= prior_upper) & squeeze_recent & volume_confirmed & trend
    breakout_exit = close < middle

    # Article interpretation 2: a move outside then back inside can indicate
    # reversal. RSI must recover from oversold and price must reclaim the band.
    reversal = (prior_close < prior_lower) & (close >= lower) & (prior_rsi < 35) & (rsi >= 35)
    reversal_exit = (high >= middle) | (rsi >= 60)

    # The second supplied article's explicit trend-start condition: %B > .8
    # and MFI > 80. Add a separate 4h trend indicator and require a fresh
    # crossing so one band walk does not create repeated entries.
    trend_condition = (percent_b > .8) & (mfi > 80) & trend
    prior_condition = np.r_[False, trend_condition[:-1]]
    percent_b_mfi = trend_condition & ~prior_condition
    percent_b_mfi_exit = (percent_b < .5) | (mfi < 50)

    w_bottom = w_bottom_confirmation(prices, percent_b, volume_median)
    w_bottom_exit = (percent_b >= .9) | (close < middle)

    # TradingView catalog synthesis: center-line trend pullback confirmed by
    # two additional closed bars, plus a BB-inside-Keltner squeeze release
    # whose direction is confirmed by improving MACD momentum.
    pullback = tradingview_pullback_confirmation(close, middle, trend, rsi)
    pullback_exit = close < middle
    atr = atr14(prices)
    center_ema = ema(close, 20)
    kc_upper, kc_lower = center_ema + 1.5*atr, center_ema - 1.5*atr
    bb_inside_kc = (upper < kc_upper) & (lower > kc_lower)
    macd = ema(close, 12)-ema(close, 26)
    macd_signal = ema(macd, 9)
    histogram = macd-macd_signal
    prior_squeeze = np.r_[False, bb_inside_kc[:-1]]
    squeeze_release = prior_squeeze & ~bb_inside_kc & (close > upper) & trend & \
                      (histogram > 0) & (histogram > np.r_[np.nan, histogram[:-1]])
    squeeze_release_exit = (close < middle) | (histogram < 0)

    return {'article_squeeze_volume_trend': (breakout, breakout_exit),
            'article_reentry_rsi': (reversal, reversal_exit),
            'article_percent_b_mfi_trend': (percent_b_mfi, percent_b_mfi_exit),
            'article_w_bottom_volume': (w_bottom, w_bottom_exit),
            'tradingview_center_pullback_confirmed': (pullback, pullback_exit),
            'tradingview_bb_kc_macd_release': (squeeze_release, squeeze_release_exit)}


def evaluate(prices):
    atr = atr14(prices)
    n = len(prices['close'])
    cuts = [int(n*x) for x in (0, .25, .5, .75, 1)]
    return {name: [simulate(prices, entry, exit_, atr, cuts[i], cuts[i+1])
                   for i in range(4)]
            for name, (entry, exit_) in rules(prices).items()}


def main():
    report = {'as_of_utc': datetime.now(timezone.utc).isoformat(),
              'source_interpretation': 'TradingView/Cointelegraph Bollinger education: squeeze+volume confirmation; outside-to-inside reversal+RSI',
              'method': '15m closed signal, next open, 2ATR stop, 4ATR target, 192h timeout, 0.15% each side',
              'automatic_live_activation': False, 'markets': {}, 'eligible': []}
    for symbol, prices in load_all():
        variants = evaluate(prices)
        report['markets'][symbol] = variants
        for name, folds in variants.items():
            if all(f['return_pct'] > 0 and f['trades'] >= 10 and
                   f['max_drawdown_pct'] < 20 for f in folds):
                report['eligible'].append([symbol, name])
        print(symbol, [(name, [(round(f['return_pct'], 2), f['trades']) for f in folds])
                       for name, folds in variants.items()])
    print('eligible', report['eligible'])
    (ROOT/'reports/article-bollinger-interpretation-research.json').write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
