"""Public ADA market snapshot. No account, credentials or order capabilities."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
ENDPOINTS = frozenset({'exchangeInfo?symbol=ADAUSDT', 'ticker/bookTicker?symbol=ADAUSDT'})


def public(endpoint):
    if endpoint not in ENDPOINTS:
        raise ValueError('Only ADA public read endpoints are allowed')
    with urlopen('https://data-api.binance.vision/api/v3/'+endpoint,timeout=20) as response:
        return json.load(response)


def estimate(book, declared_quantity):
    if book.get('symbol') != 'ADAUSDT':
        raise ValueError('Wrong market')
    bid, ask, qty = (Decimal(str(v)) for v in (book['bidPrice'],book['askPrice'],declared_quantity))
    if not all(x.is_finite() for x in (bid,ask,qty)) or not 0 < bid <= ask or qty <= 0:
        raise ValueError('Invalid book or quantity')
    return dict(declared_ada=str(qty),bid=str(bid),ask=str(ask),
                indicative_bid_value_usdt=str(qty*bid),
                full_spread_bps=str((ask-bid)/((ask+bid)/2)*10000),
                account_balance_verified=False,fees_and_slippage_included=False)


def main():
    config = json.loads((ROOT/'config/ada-mainnet-readonly.json').read_text())
    if config['real_orders_enabled'] is not False or config['automatic_execution_enabled'] is not False:
        raise ValueError('This tool cannot enable execution')
    info = public('exchangeInfo?symbol=ADAUSDT')
    book = public('ticker/bookTicker?symbol=ADAUSDT')
    symbols = [s for s in info['symbols'] if s['symbol']=='ADAUSDT']
    if len(symbols)!=1:
        raise ValueError('ADAUSDT market missing or ambiguous')
    report = dict(captured_utc=datetime.now(timezone.utc).isoformat(),
                  valuation=estimate(book,config['declared_initial_ada']),
                  market_status=symbols[0]['status'],filters=symbols[0]['filters'],
                  orders_submitted=0,execution_enabled=False)
    (ROOT/'reports/ada-mainnet-readonly.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__ == '__main__':
    main()
