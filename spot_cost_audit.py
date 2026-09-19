"""Read-only public Spot execution audit and frozen-rule baseline comparison."""
import json
import hashlib
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from strategy_research import ROOT, load, resample, features, signals, simulate, compact


def public(path):
    with urllib.request.urlopen('https://data-api.binance.vision/api/v3/'+path, timeout=20) as response:
        return json.load(response)


def depth_fill(levels, quote):
    remaining, quantity = quote, 0.
    for price, amount in levels:
        price, amount = float(price), float(amount)
        spend = min(remaining, price*amount)
        quantity += spend/price
        remaining -= spend
        if remaining < 1e-8:
            break
    return None if remaining > 1e-8 else quote/quantity


def rounded_quantity(budget, price, filters):
    qty = Decimal(str(budget))/Decimal(str(price))
    for kind in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
        rule = next((r for r in filters if r['filterType'] == kind), None)
        if rule:
            step = Decimal(rule['stepSize'])
            if step:
                qty = (qty/step).to_integral_value(rounding=ROUND_DOWN)*step
    for kind in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
        rule = next((r for r in filters if r['filterType'] == kind), None)
        if rule:
            if qty < Decimal(rule['minQty']) or (Decimal(rule['maxQty']) and qty > Decimal(rule['maxQty'])):
                return {'quantity': str(qty), 'quantity_valid': False}
            if Decimal(rule['stepSize']) and qty % Decimal(rule['stepSize']):
                return {'quantity': str(qty), 'quantity_valid': False}
    return {'quantity': str(qty), 'quantity_valid': qty > 0,
            'estimated_notional': str(qty*Decimal(str(price))),
            'exchange_acceptance_verified': False}


def buy_hold(rows, start, end, cost):
    # Same initial 20% cash allocation, no rebalance. Strategy uses a 20% cap,
    # not constant 20% exposure; risk is therefore not matched.
    quantity = 200/(rows[start]['open']*(1+cost))
    peak, dd = 1000., 0.
    for r in rows[start:end]:
        low = 800+quantity*r['low']*(1-cost)
        dd = max(dd, 1-low/peak)
        peak = max(peak, 800+quantity*r['close']*(1-cost))
    return {'pnl_usd': 800+quantity*rows[end-1]['close']*(1-cost)-1000,
            'max_drawdown': dd, 'initial_allocation': .2}


def run():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out = ROOT/'reports'/('spot-cost-audit-'+stamp)
    out.mkdir(parents=True)
    def save(name, obj):
        (out/name).write_text(json.dumps(obj, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    contract = {'rule': 'trend_hysteresis_4h_balanced', 'costs_each_side': [0., .0015, .0025],
                'account_commission': None, 'historical_spread_slippage': None,
                'costs_are_assumptions': True, 'historical_evidence': 'retrospective',
                'quantity_rounding_in_historical_simulator': False,
                'historical_exchange_filters_available': False,
                'deployed': False, 'capital': 1000, 'allocation_cap': .2}
    save('contract.json', contract)
    observation = {'captured_utc': stamp, 'account_commission': None,
                   'account_commission_status': 'not_queried_no_production_credentials',
                   'order_submission': False}
    for name, endpoint in [('exchange_info', 'exchangeInfo?symbol=BTCUSDT'),
                           ('depth', 'depth?symbol=BTCUSDT&limit=100')]:
        try:
            observation[name] = public(endpoint)
        except Exception as exc:
            observation[name] = {'error': type(exc).__name__}
    depth = observation['depth']
    if 'bids' in depth and 'asks' in depth and depth['bids'] and depth['asks']:
        bid, ask = float(depth['bids'][0][0]), float(depth['asks'][0][0])
        mid = (bid+ask)/2
        observation['spread_bps'] = (ask-bid)/mid*10000
        observation['visible_book_buy_impact'] = {}
        for quote in (15, 200):
            vwap = depth_fill(depth['asks'], quote)
            observation['visible_book_buy_impact'][str(quote)] = {
                'vwap': vwap, 'above_ask_bps': None if vwap is None else (vwap/ask-1)*10000}
        info = observation['exchange_info']
        if 'symbols' in info:
            filters = info['symbols'][0]['filters']
            observation['quantity_examples'] = {str(q): rounded_quantity(q, ask, filters) for q in (15, 200)}
    save('public-observation.json', observation)
    results = {}
    datasets = [('bitstamp_selection', 'bitstamp-btc-usd-15m-200000.csv', 'bitstamp', 'BTC/USD'),
                ('binance_spot_history', 'binance-spot-btcusdt-15m-through-20260916.csv', 'binance_spot', 'BTCUSDT')]
    for name, filename, exchange, symbol in datasets:
        path = ROOT/'data'/filename
        rows = resample(load(path, exchange, symbol), 4)
        start, end = (int(len(rows)*.6), int(len(rows)*.8)) if name == 'bitstamp_selection' else (204, len(rows))
        f = features(rows)
        entry, exit_ = signals(rows, f, 'trend_hysteresis', 'balanced')
        results[name] = {'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'start_ts': rows[start]['ts'], 'end_ts': rows[end-1]['ts'], 'scenarios': {}}
        for cost in contract['costs_each_side']:
            results[name]['scenarios'][str(cost)] = {
                'strategy': compact(simulate(rows, f, entry, exit_, start, end, cost, 48)),
                'buy_hold': buy_hold(rows, start, end, cost), 'cash_pnl_usd': 0.}
    save('comparison.json', results)
    lines = ['# Spot maliyet ve referans karşılaştırması', '',
             'Sabit 4h strateji; 1000 USD, %20 tahsis tavanı. Al-tut başlangıçta %20 alır, yeniden dengelemez.',
             'Aynı tahsis sınırı eşit risk/ortalama pozisyon demek değildir. Nakit getirisi 0 USD.', '',
             '| Veri | Tek yön varsayım | Strateji USD | Al-tut USD | Strateji işlem |',
             '|---|---:|---:|---:|---:|']
    for name, value in results.items():
        for cost, r in value['scenarios'].items():
            lines.append(f"| {name} | {float(cost):.2%} | {r['strategy']['pnl_usd']:.2f} | {r['buy_hold']['pnl_usd']:.2f} | {r['strategy']['trades']} |")
    lines += ['', 'Hesaba özel komisyon bilinmiyor. Güncel spread bp: '+str(observation.get('spread_bps', 'alınamadı')),
              'Emir defteri yalnız tek anlık görüntüdür; geçmiş spread, gerçekleşmiş kayma veya dolum garantisi değildir.',
              'Güncel exchangeInfo filtreleri kaydedildi; tarihsel filtreler ve miktar yuvarlama simülasyona uygulanmadı.',
              'Bu nedenle sonuç ekonomik araştırmadır, borsada uygulanabilirlik doğrulaması tamamlandı denemez.',
              'Binance bölümü daha önce araştırılmış geçmiş veride sabit aday tanısıdır; yeni holdout değildir.',
              'Kaynaklar: https://developers.binance.com/en/docs/products/spot/filters',
              'https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market',
              'https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'directory': str(out), 'spread_bps': observation.get('spread_bps'), 'results': results}, indent=2))


if __name__ == '__main__':
    run()
