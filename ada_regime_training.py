"""Fixed offline experiment: rolling ridge, trees and regime-specific trees."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from sklearn.ensemble import ExtraTreesRegressor
from ada_barrier_training import prepare, outcome, COSTS
from strategy_research import load
from trend4h_learning import fit, predict

ROOT = Path(__file__).resolve().parent
CONTRACT = dict(version=1, horizons=[4,8,16], models=['ridge','trees','regime_trees'],
                folds=[[.5,.6],[.6,.7],[.7,.8]], train_window_bars=180*96,
                minimum_regime_samples=1000, n_estimators=100, max_depth=6,
                min_samples_leaf=100, random_state=20260922,
                costs=list(COSTS), minimum_trades_per_fold=20,
                minimum_profit_factor=1.15, maximum_drawdown=.15,
                execution_eligible=False, excluded_tail_fraction=.2)


def regime(v):
    # Fixed, causal price/EMA direction; no clustering fitted on future data.
    return 'up' if v[4] > 0 and v[5] > 0 else 'down' if v[4] < 0 and v[5] < 0 else 'range'


def training_indices(labels, start):
    return [i for i in labels if max(204,start-CONTRACT['train_window_bars']) <= i < start
            and labels[i][1] < start]


def tree():
    return ExtraTreesRegressor(n_estimators=CONTRACT['n_estimators'],
                              max_depth=CONTRACT['max_depth'],
                              min_samples_leaf=CONTRACT['min_samples_leaf'],
                              random_state=CONTRACT['random_state'], n_jobs=2)


def estimates(x, labels, start, end, kind):
    indices = training_indices(labels,start)
    test = list(range(start,end))
    xx = np.array([x[i] for i in indices])
    yy = np.array([labels[i][0] for i in indices])
    if len(indices) < 1000:
        raise ValueError('Insufficient training history')
    if kind == 'ridge':
        model = fit(xx,yy)
        pred = np.array([predict(model,x[i]) for i in test])
    else:
        model = tree().fit(xx,yy)
        pred = model.predict(np.array([x[i] for i in test]))
        if kind == 'regime_trees':
            for name in ('up','down','range'):
                train_mask = [j for j,i in enumerate(indices) if regime(x[i]) == name]
                test_mask = [j for j,i in enumerate(test) if regime(x[i]) == name]
                if len(train_mask) < CONTRACT['minimum_regime_samples'] or not test_mask:
                    continue  # Global fallback uses only the same training window.
                local = tree().fit(xx[train_mask],yy[train_mask])
                pred[test_mask] = local.predict(np.array([x[test[j]] for j in test_mask]))
    return dict(zip(test,map(float,pred))), dict(samples=len(indices),
                    last_training_outcome=max(labels[i][1] for i in indices),
                    first_evaluation_decision=start,
                    regime_counts={name:sum(regime(x[i])==name for i in indices)
                                   for name in ('up','down','range')})


def simulate(rows, labels, predictions, horizon, start, end, cost):
    equity = peak = 1.
    dd = 0.
    available = start
    trades = []
    factor = (1-cost)/(1+cost)
    by_reason = {'target':0,'stop':0,'timeout':0}
    for i in range(start,end-horizon):
        # Boundary eligibility is fixed by horizon, never by realized exit time.
        if i < available or (1+predictions[i])*(1-cost)/(1+cost) <= 1:
            continue
        gross,j,reason = labels[i]
        entry = rows[i+1]['open']
        for k in range(i+1,j+1):
            # Exit-bar lows overstate risk after an earlier fill: conservative proxy.
            dd = max(dd,1-equity*rows[k]['low']/entry*factor/peak)
            if k < j:
                peak = max(peak,equity*rows[k]['close']/entry*factor)
        net = (1+gross)*factor-1
        equity *= 1+net
        peak = max(peak,equity)
        dd = max(dd,1-equity/peak)
        trades.append(net)
        by_reason[reason] += 1
        available = j+1
    profit = sum(t for t in trades if t>0)
    loss = -sum(t for t in trades if t<0)
    return dict(trades=len(trades),net_return=equity-1,conservative_drawdown=dd,
                profit_factor=profit/loss if loss else None,
                win_rate=sum(t>0 for t in trades)/len(trades) if trades else None,
                exits=by_reason,cost_per_side=cost,
                buy_hold_return=rows[end-1]['close']/rows[start+1]['open']*factor-1)


def passed(folds):
    return all(f['results'][1]['trades'] >= CONTRACT['minimum_trades_per_fold']
               and f['results'][1]['net_return'] > 0
               and f['results'][1]['conservative_drawdown'] < CONTRACT['maximum_drawdown']
               and (f['results'][1]['profit_factor'] or 0) >= CONTRACT['minimum_profit_factor']
               for f in folds)


def research(path, output):
    output.mkdir(parents=True,exist_ok=True)
    # Persist parameters before computing results; do not tune after evaluation.
    contract_bytes = json.dumps(CONTRACT,sort_keys=True).encode()
    (output/'contract.json').write_bytes(contract_bytes)
    original = load(path,'binance_spot','ADAUSDT')
    total = len(original)
    if total < 30000:
        raise ValueError('Need at least 30000 candles')
    rows = original[:int(total*.8)]
    x, _, features = prepare(rows)
    candidates = []
    for h in CONTRACT['horizons']:
        labels = {i:outcome(rows,i,h,features['atr'][i]) for i in range(204,len(rows)-h)}
        for kind in CONTRACT['models']:
            folds = []
            for lo,hi in CONTRACT['folds']:
                start,end = int(total*lo),int(total*hi)
                predictions,evidence = estimates(x,labels,start,end,kind)
                results = [simulate(rows,labels,predictions,h,start,end,cost) for cost in COSTS]
                folds.append(dict(start=start,end=end,training=evidence,results=results))
            candidate = dict(horizon=h,model=kind,folds=folds,passed=passed(folds))
            candidates.append(candidate)
            print(f'{kind} {h*15}m: passed={candidate["passed"]}',flush=True)
    report = dict(created_utc=datetime.now(timezone.utc).isoformat(),contract=CONTRACT,
                  contract_sha256=hashlib.sha256(contract_bytes).hexdigest(),
                  source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  source_candles=total,used_candles=len(rows),excluded_tail=total-len(rows),
                  candidates=candidates,passing_candidates=sum(c['passed'] for c in candidates),
                  evidence='Reused development history only; no independent forward proof',
                  execution_eligible=False,live_modified=False)
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'passing_candidates':report['passing_candidates'],'used_candles':len(rows)}))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,default=ROOT/'data/adausdt-15m-long.csv')
    parser.add_argument('--output',type=Path,default=ROOT/'state/ada-regime-research')
    args = parser.parse_args()
    research(args.data,args.output)
