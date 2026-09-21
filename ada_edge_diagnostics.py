"""Read-only fee audit, public spread observations and offline trade attribution."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
import urllib.request
import numpy as np
from ada_regime_training import CONTRACT, estimates, regime, simulate
from ada_barrier_training import prepare, outcome
from strategy_research import load

ROOT = Path(__file__).resolve().parent


def fee_audit(path):
    rates = []
    unsupported = 0
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        records = db.execute('SELECT response FROM orders WHERE applied=1 AND response IS NOT NULL').fetchall()
    for (raw,) in records:
        order = json.loads(raw)
        fills = order.get('fills',[])
        notional = sum(float(f['qty'])*float(f['price']) for f in fills)
        if notional <= 0:
            continue
        if any(f['commissionAsset'] not in ('USDT','ADA') for f in fills):
            unsupported += 1
            continue
        fees = sum(float(f['commission'])*(float(f['price']) if f['commissionAsset']=='ADA' else 1)
                   for f in fills)
        rates.append(dict(side=order.get('side'),rate=fees/notional))
    return dict(filled_orders=len(rates),rates=rates,unsupported_fee_orders=unsupported,
                evidence='Historical fill commission only; not current account fee or measured slippage')


def spread_samples(output, count=15):
    if not 1 <= count <= 300:
        raise ValueError('Bounded sample count required')
    result = []
    for _ in range(count):
        started = time.monotonic()
        with urllib.request.urlopen('https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=ADAUSDT',timeout=15) as response:
            book = json.load(response)
        bid,ask = float(book['bidPrice']),float(book['askPrice'])
        if book.get('symbol') != 'ADAUSDT' or not all(map(math.isfinite,(bid,ask))) or not 0 < bid <= ask:
            raise ValueError('Invalid book')
        result.append(dict(observed_utc=datetime.now(timezone.utc).isoformat(),
                           round_trip_spread_bps=(ask/bid-1)*10000,
                           request_seconds=time.monotonic()-started))
        if len(result)<count:
            time.sleep(2)
    report = dict(samples=result,median_spread_bps=float(np.median([r['round_trip_spread_bps'] for r in result])),
                  evidence='Short current public top-of-book sample, not historical fills or depth slippage')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


def attribution(trades):
    if not trades:
        return dict(trades=0,net_return=0.,without_best_trade=0.,exits={})
    returns = [t['net_return'] for t in trades]
    best = max(range(len(returns)),key=returns.__getitem__)
    return dict(trades=len(trades),net_return=float(np.prod([1+t for t in returns])-1),
                without_best_trade=float(np.prod([1+t for i,t in enumerate(returns) if i != best])-1),
                gross_positive_trades=sum(t['gross_return']>0 for t in trades),
                gross_positive_but_net_negative=sum(t['gross_return']>0 and t['net_return']<=0 for t in trades),
                exits={reason:dict(count=sum(t['reason']==reason for t in trades),
                         sum_net_returns=sum(t['net_return'] for t in trades if t['reason']==reason))
                       for reason in ('target','stop','timeout')})


def research(output):
    original = load(ROOT/'data/adausdt-15m-long.csv','binance_spot','ADAUSDT')
    total = len(original)
    rows = original[:int(total*.8)]
    x,_,f = prepare(rows)
    results = []
    for h in CONTRACT['horizons']:
        labels = {i:outcome(rows,i,h,f['atr'][i]) for i in range(204,len(rows)-h)}
        for kind in CONTRACT['models']:
            folds = []
            for lo,hi in CONTRACT['folds']:
                start,end = int(total*lo),int(total*hi)
                pred,evidence = estimates(x,labels,start,end,kind)
                scenarios = []
                # Fourth scenario is exploratory sensitivity near today's short
                # spread sample, not a claim about historical execution costs.
                for cost in (.001,.0015,.0025,.00125):
                    trades = []
                    metrics = simulate(rows,labels,pred,h,start,end,cost,trade_records=trades)
                    scenarios.append(dict(cost_per_side=cost,metrics=metrics,
                        attribution=attribution(trades),
                        regimes={name:attribution([t for t in trades if regime(x[t['index']])==name])
                                 for name in ('up','down','range')}))
                folds.append(dict(start=start,end=end,scenarios=scenarios))
            results.append(dict(horizon=h,model=kind,folds=folds))
            print(f'Attributed {kind} {h*15}m',flush=True)
    report = dict(generated_utc=datetime.now(timezone.utc).isoformat(),fees=fee_audit(ROOT/'state/ada-live.sqlite3'),
                  candidates=results,evidence='Reused development attribution; costs change selected trades; no new selection',
                  extra_cost_scenario='0.125%/side sensitivity only; short current spread cannot establish historical costs',
                  execution_eligible=False,live_modified=False)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample-spread',action='store_true')
    args = parser.parse_args()
    if args.sample_spread:
        spread_samples(ROOT/'state/ada-edge-diagnostics/spread.json')
    else:
        research(ROOT/'state/ada-edge-diagnostics/report.json')
