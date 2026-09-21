"""Offline robustness experiment; no live model or execution integration."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from ada_barrier_training import prepare, outcome, COSTS
from ada_regime_training import training_indices, tree, simulate
from strategy_research import load

ROOT = Path(__file__).resolve().parent
CONTRACT = dict(version=1,horizons=[4,8,16],targets=['raw','winsor','atr_units'],
                folds=[[.5,.6],[.6,.7],[.7,.8]],train_window_days=180,
                winsor_quantiles=[.01,.99],atr_target_clip=[-3.,3.],
                position_modes=['full','risk_scaled'],risk_fraction=.005,allocation_cap=.25,
                costs=list(COSTS),minimum_trades=20,minimum_profit_factor=1.15,
                maximum_drawdown=.15,execution_eligible=False)


def transform(y, volatility, target):
    if target == 'raw':
        return y.copy(), None
    if target == 'winsor':
        bounds = np.quantile(y,CONTRACT['winsor_quantiles'])
        return np.clip(y,*bounds),bounds.tolist()
    if target == 'atr_units':
        return np.clip(y/volatility,*CONTRACT['atr_target_clip']),CONTRACT['atr_target_clip']
    raise ValueError('Unknown target')


def estimates(x,labels,start,end,target):
    indices = training_indices(labels,start)
    if len(indices)<1000:
        raise ValueError('Insufficient training samples')
    xx = np.asarray([x[i] for i in indices])
    yy = np.asarray([labels[i][0] for i in indices])
    vol = np.maximum(xx[:,6],1e-9)
    transformed,bounds = transform(yy,vol,target)
    model = tree().fit(xx,transformed)
    test = np.asarray([x[i] for i in range(start,end)])
    pred = model.predict(test)
    if target == 'atr_units':
        pred *= np.maximum(test[:,6],1e-9)
    return dict(zip(range(start,end),map(float,pred))),dict(
        train_samples=len(indices),last_training_outcome=max(labels[i][1] for i in indices),
        first_decision=start,training_bounds=bounds,
        transformed_samples=int(np.count_nonzero(transformed != (yy/vol if target=='atr_units' else yy))))


def fraction(vol,cost,mode):
    if mode == 'full':
        return 1.
    if mode != 'risk_scaled' or vol < 0:
        raise ValueError('Invalid sizing input')
    # Known signal-bar ATR/close; no future fill price used to choose allocation.
    return min(CONTRACT['allocation_cap'],CONTRACT['risk_fraction']/max(1.5*vol+2*cost,1e-9))


def portfolio(rows,x,trades,cost,mode):
    equity = peak = 1.
    dd = 0.
    returns,allocations = [],[]
    factor = (1-cost)/(1+cost)
    for trade in trades:
        i,j = trade['index'],trade['exit_index']
        weight = fraction(x[i][6],cost,mode)
        entry = rows[i+1]['open']
        for k in range(i+1,j+1):
            low_mark = equity*((1-weight)+weight*rows[k]['low']/entry*factor)
            dd = max(dd,1-low_mark/peak)
            if k < j:
                mark = equity*((1-weight)+weight*rows[k]['close']/entry*factor)
                peak = max(peak,mark)
        net = weight*trade['net_return']
        equity *= 1+net
        peak = max(peak,equity)
        dd = max(dd,1-equity/peak)
        returns.append(net)
        allocations.append(weight)
    losses = -sum(t for t in returns if t<0)
    best = max(range(len(returns)),key=returns.__getitem__) if returns else None
    return dict(trades=len(returns),net_return=equity-1,conservative_drawdown=dd,
                without_best_trade=float(np.prod([1+t for i,t in enumerate(returns) if i!=best])-1),
                average_allocation=float(np.mean(allocations)) if allocations else 0.,
                profit_factor=sum(t for t in returns if t>0)/losses if losses else None)


def research(path,output):
    output.mkdir(parents=True,exist_ok=True)
    contract = json.dumps(CONTRACT,sort_keys=True).encode()
    (output/'contract.json').write_bytes(contract)
    original = load(path,'binance_spot','ADAUSDT')
    total = len(original)
    if total<30000:
        raise ValueError('Need substantial history')
    rows = original[:int(total*.8)]
    x,_,f = prepare(rows)
    candidates = []
    for h in CONTRACT['horizons']:
        labels = {i:outcome(rows,i,h,f['atr'][i]) for i in range(204,len(rows)-h)}
        for target in CONTRACT['targets']:
            folds = []
            for lo,hi in CONTRACT['folds']:
                start,end = int(total*lo),int(total*hi)
                pred,evidence = estimates(x,labels,start,end,target)
                scenarios = []
                for cost in COSTS:
                    trades = []
                    simulate(rows,labels,pred,h,start,end,cost,trade_records=trades)
                    scenarios.append(dict(cost=cost,portfolios={mode:portfolio(rows,x,trades,cost,mode)
                                                               for mode in CONTRACT['position_modes']}))
                folds.append(dict(training=evidence,scenarios=scenarios))
            passing = {}
            for mode in CONTRACT['position_modes']:
                metrics = [fold['scenarios'][1]['portfolios'][mode] for fold in folds]
                passing[mode] = all(m['trades']>=20 and m['net_return']>0 and m['without_best_trade']>0
                                    and m['conservative_drawdown']<.15 and (m['profit_factor'] or 0)>=1.15
                                    for m in metrics)
            candidates.append(dict(horizon=h,target=target,folds=folds,passing=passing))
            print(f'{target} {h*15}m: {passing}',flush=True)
    report = dict(created_utc=datetime.now(timezone.utc).isoformat(),contract=CONTRACT,
                  contract_sha256=hashlib.sha256(contract).hexdigest(),
                  source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  source_candles=total,used_candles=len(rows),candidates=candidates,
                  evidence='Reused development history, no untouched or forward proof; costs assumed',
                  limitations='Continuous quantities; no minimum notional, volume impact or partial fills; conservative exit-bar lows',
                  execution_eligible=False,live_modified=False)
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=ROOT/'data/adausdt-15m-long.csv')
    parser.add_argument('--output',type=Path,default=ROOT/'state/ada-robust-research')
    args = parser.parse_args()
    research(args.data,args.output)
