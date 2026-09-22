"""Offline paired experiment: does BTC context improve ADA predictions?"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from ada_barrier_training import prepare, outcome, COSTS
from ada_regime_training import estimates, simulate, passed
from strategy_research import load

ROOT = Path(__file__).resolve().parent


def augment(ada,btc,x):
    if len(ada)!=len(btc) or any(a['ts']!=b['ts'] for a,b in zip(ada,btc)):
        raise ValueError('Exact candle alignment required')
    result = {}
    ac = np.array([r['close'] for r in ada])
    bc = np.array([r['close'] for r in btc])
    ar,br = np.diff(np.log(ac)),np.diff(np.log(bc))
    for i,vector in x.items():
        returns = [float(bc[i]/bc[i-n]-1) for n in (1,4,16,96)]
        aw,bw = ar[i-96:i],br[i-96:i]
        va,vb = float(aw.std()),float(bw.std())
        corr = float(np.corrcoef(aw,bw)[0,1]) if min(va,vb)>1e-10 else 0.
        relative = [float(ac[i]/ac[i-n]-bc[i]/bc[i-n]) for n in (4,16)]
        result[i] = vector+returns+relative+[corr,va/max(vb,1e-9)]
    return result


def research(output):
    ada_path,btc_path = ROOT/'data/adausdt-15m-long.csv',ROOT/'data/btcusdt-15m-long.csv'
    ada = load(ada_path,'binance_spot','ADAUSDT')
    btc = load(btc_path,'binance_spot','BTCUSDT')
    contract = dict(horizons=[8,16],features=['ada_only','ada_btc'],model='trees',
                    folds=[[.5,.6],[.6,.7],[.7,.8]],costs=list(COSTS),
                    evidence='Paired reused-development experiment; no final-tail evaluation',
                    execution_eligible=False)
    output.mkdir(parents=True,exist_ok=True)
    (output/'contract.json').write_text(json.dumps(contract,indent=2),encoding='utf-8')
    total = len(ada)
    if total<30000 or len(btc)!=total:
        raise ValueError('Matching long histories required')
    rows = ada[:int(total*.8)]
    btc = btc[:len(rows)]
    baseline,_,f = prepare(rows)
    extended = augment(rows,btc,baseline)
    candidates = []
    for h in contract['horizons']:
        labels = {i:outcome(rows,i,h,f['atr'][i]) for i in range(204,len(rows)-h)}
        for name,x in [('ada_only',baseline),('ada_btc',extended)]:
            folds = []
            for lo,hi in contract['folds']:
                start,end = int(total*lo),int(total*hi)
                prediction,evidence = estimates(x,labels,start,end,'trees')
                results = [simulate(rows,labels,prediction,h,start,end,cost) for cost in COSTS]
                folds.append(dict(training=evidence,results=results))
            candidates.append(dict(horizon=h,features=name,folds=folds,passed=passed(folds)))
            print(f'{name} {h*15}m: passed={candidates[-1]["passed"]}',flush=True)
    report = dict(created_utc=datetime.now(timezone.utc).isoformat(),contract=contract,
                  source_candles=total,used_candles=len(rows),candidates=candidates,
                  hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (ada_path,btc_path)},
                  execution_eligible=False,live_modified=False)
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'state/ada-market-context')
    args = parser.parse_args()
    research(args.output)
