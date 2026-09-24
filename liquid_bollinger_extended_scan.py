"""Extend the fixed 15m Bollinger audit to other currently liquid USDT pairs."""

from datetime import datetime, timezone
import json

from liquid_bollinger_coin_research import download, arrays, evaluate, get_json, ROOT

SYMBOLS = ('ZECUSDT', 'NEARUSDT', 'UNIUSDT', 'SUIUSDT',
           'BCHUSDT', 'TAOUSDT', 'PEPEUSDT', 'AVAXUSDT')


def main():
    ticker = {x['symbol']: x for x in get_json('ticker/24hr')}
    books = {x['symbol']: x for x in get_json('ticker/bookTicker')}
    report = {'as_of_utc': datetime.now(timezone.utc).isoformat(),
              'history_days': 365, 'markets': {}, 'eligible': []}
    for symbol in SYMBOLS:
        rows, path = download(symbol, days=365)
        variants = evaluate(arrays(rows))
        bid, ask = float(books[symbol]['bidPrice']), float(books[symbol]['askPrice'])
        market = {
            'quote_volume_usdt_24h': float(ticker[symbol]['quoteVolume']),
            'spread_bps_snapshot': (ask/bid-1)*10_000,
            'candles': len(rows), 'data_file': str(path.relative_to(ROOT)),
            'variants': variants,
        }
        report['markets'][symbol] = market
        for name, folds in variants.items():
            if all(f['return_pct'] > 0 and f['trades'] >= 20 and
                   f['max_drawdown_pct'] < 20 for f in folds):
                report['eligible'].append([symbol, name])
        print(symbol, round(market['quote_volume_usdt_24h']/1e6, 1), 'm',
              [(name, [(round(x['return_pct'], 2), x['trades']) for x in folds])
               for name, folds in variants.items()])
    (ROOT/'reports/liquid-bollinger-extended-scan.json').write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
    print('eligible', report['eligible'])


if __name__ == '__main__':
    main()
