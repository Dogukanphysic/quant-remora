"""Compare liquid Binance Spot USDT pairs under fixed 15m Bollinger rules.

Public endpoints only. This module cannot access credentials or submit orders.
"""

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np

from ada_bb_execution_audit import atr14, simulate
from dual_market_strategy_audit import indicators

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data/liquid-bollinger-15m'
REPORT_JSON = ROOT / 'reports/liquid-bollinger-coin-research.json'
REPORT_MD = ROOT / 'reports/liquid-bollinger-coin-research.md'
CANDIDATES = ('BTCUSDT', 'ETHUSDT', 'XRPUSDT', 'SOLUSDT', 'DOGEUSDT', 'BNBUSDT', 'ADAUSDT')
STABLE_BASES = {'USDC', 'FDUSD', 'TUSD', 'USDP', 'USD1', 'RLUSD', 'DAI'}
INTERVAL_MS = 900_000
HISTORY_DAYS = 365


def get_json(path, params=None):
    query = urlencode(params or {})
    url = 'https://api.binance.com/api/v3/' + path + ('?' + query if query else '')
    with urlopen(url, timeout=20) as response:
        return json.load(response)


def volume_snapshot():
    rows = get_json('ticker/24hr')
    by_symbol = {row['symbol']: row for row in rows}
    books = {row['symbol']: row for row in get_json('ticker/bookTicker')}
    output = {}
    for symbol in CANDIDATES:
        row, book = by_symbol[symbol], books[symbol]
        bid, ask = float(book['bidPrice']), float(book['askPrice'])
        output[symbol] = {
            'quote_volume_usdt_24h': float(row['quoteVolume']),
            'trade_count_24h': int(row['count']),
            'last_price': float(row['lastPrice']),
            'spread_bps_snapshot': (ask / bid - 1) * 10_000,
        }
    return output


def download(symbol, *, now_ms=None, days=HISTORY_DAYS):
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    end = now_ms - now_ms % INTERVAL_MS - 1
    start = end - days * 86_400_000
    rows = []
    cursor = start
    while cursor <= end:
        batch = get_json('klines', {'symbol': symbol, 'interval': '15m',
                                    'startTime': cursor, 'endTime': end, 'limit': 1000})
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + INTERVAL_MS
        if next_cursor <= cursor:
            raise ValueError(f'{symbol}: non-advancing API response')
        cursor = next_cursor
        time.sleep(.03)
    clean = []
    seen = set()
    for row in rows:
        ts = int(row[0])
        if ts in seen or int(row[6]) > now_ms:
            continue
        seen.add(ts)
        clean.append((ts, *map(float, row[1:6])))
    clean.sort()
    if len(clean) < days * 90 or any(clean[i][0] - clean[i-1][0] != INTERVAL_MS
                                     for i in range(1, len(clean))):
        raise ValueError(f'{symbol}: incomplete 15m history ({len(clean)} rows)')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f'{symbol.lower()}-{days}d.csv'
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(('ts', 'open', 'high', 'low', 'close', 'volume'))
        writer.writerows(clean)
    return clean, path


def arrays(rows):
    values = np.asarray([row[1:] for row in rows], dtype=float)
    return {name: values[:, i] for i, name in enumerate(('open', 'high', 'low', 'close', 'volume'))}


def variants(prices):
    lower, upper, trend, _ = indicators(prices)
    width = upper - lower
    valid = np.isfinite(width) & (width > 0)
    exact = (prices['low'] <= lower) & valid
    zone10 = (prices['low'] <= lower + .10 * width) & valid
    reclaim = exact & (prices['close'] > lower)
    exit_ = (prices['high'] >= upper) & valid
    return {
        'exact_touch': exact,
        'lower_zone10': zone10,
        'exact_reclaim': reclaim,
        'exact_reclaim_4h_trend': reclaim & trend,
    }, exit_


def evaluate(prices):
    entries, exit_ = variants(prices)
    atr = atr14(prices)
    n = len(prices['close'])
    cuts = [int(n * x) for x in (0, .25, .5, .75, 1)]
    return {name: [simulate(prices, entry, exit_, atr, cuts[i], cuts[i+1])
                   for i in range(4)] for name, entry in entries.items()}


def eligible(folds):
    return all(fold['return_pct'] > 0 and fold['trades'] >= 20 and
               fold['max_drawdown_pct'] < 20 for fold in folds)


def research(days=HISTORY_DAYS):
    snapshot = volume_snapshot()
    report = {
        'as_of_utc': datetime.now(timezone.utc).isoformat(),
        'source': 'Binance Spot public REST API',
        'history_days': days,
        'method': '15m closed signal, next-open fill, 2ATR stop, 4ATR target, 192h timeout, 0.15% each side',
        'selection_gate': 'positive return, >=20 trades and <20% drawdown in every one of four chronological folds',
        'live_change_authorized': False,
        'markets': {},
    }
    for symbol in CANDIDATES:
        rows, path = download(symbol, days=days)
        results = evaluate(arrays(rows))
        report['markets'][symbol] = {
            **snapshot[symbol], 'candles': len(rows),
            'start_utc': datetime.fromtimestamp(rows[0][0]/1000, timezone.utc).isoformat(),
            'end_utc': datetime.fromtimestamp(rows[-1][0]/1000, timezone.utc).isoformat(),
            'data_file': str(path.relative_to(ROOT)),
            'variants': results,
            'eligible_variants': [name for name, folds in results.items() if eligible(folds)],
        }
    eligible_pairs = [(symbol, name) for symbol, market in report['markets'].items()
                      for name in market['eligible_variants']]
    report['eligible_pairs'] = eligible_pairs
    return report


def markdown(report):
    lines = ['# Yüksek hacimli coinlerde 15m Bollinger karşılaştırması', '',
             f"Tarih: {report['as_of_utc']}. Veri: Binance Spot halka açık REST API.", '',
             'Her hücre dört kronolojik bölümdeki net sermaye getirisini gösterir. '
             'Parantez içi tamamlanan sanal işlem sayısıdır. Yön başına %0,15 maliyet, '
             'sonraki mum açılışı, 2 ATR stop, 4 ATR hedef ve 192 saat azami tutma kullanıldı.', '',
             '| Parite | 24s hacim (milyon USDT) | Spread (bp) | Kural | Dört bölüm |',
             '|---|---:|---:|---|---|']
    for symbol, market in report['markets'].items():
        for name, folds in market['variants'].items():
            cells = ' / '.join(f"{f['return_pct']:+.2f}% ({f['trades']})" for f in folds)
            lines.append(f"| {symbol} | {market['quote_volume_usdt_24h']/1e6:.1f} | "
                         f"{market['spread_bps_snapshot']:.3f} | {name} | {cells} |")
    lines += ['', f"Geçiş kapısını geçen parite/kural: **{report['eligible_pairs'] or 'yok'}**.", '',
              '24 saatlik hacim ve tek spread örneği zamanla değişir. Geçmiş test gelecekteki '
              'kârı kanıtlamaz; IOC kısmi dolumu ve piyasa etkisi simüle edilmez. Bu çalışma '
              'anahtar kullanmadı, emir göndermedi ve çalışan agentı değiştirmedi.']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=HISTORY_DAYS)
    args = parser.parse_args()
    report = research(args.days)
    REPORT_JSON.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    REPORT_MD.write_text(markdown(report), encoding='utf-8')
    for symbol, market in report['markets'].items():
        print(symbol, round(market['quote_volume_usdt_24h']/1e6, 1), 'm USDT',
              'eligible', market['eligible_variants'])
        for name, folds in market['variants'].items():
            print(' ', name, [(round(f['return_pct'], 2), f['trades']) for f in folds])


if __name__ == '__main__':
    main()
