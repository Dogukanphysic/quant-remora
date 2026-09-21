"""Offline ADA horizon audit. Reads learner DB; never changes live state or trades."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import numpy as np
from trend4h_learning import fit, predict

ROOT=Path(__file__).resolve().parent
STEP=900000


def audit(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        samples=db.execute('SELECT ts,x,y,label_end FROM samples WHERE y IS NOT NULL ORDER BY ts').fetchall()
    if len(samples)<400:
        raise ValueError('Need at least 400 completed samples')
    samples=samples[-3000:]
    boundary=samples[int(len(samples)*.7)][0]+STEP
    xs=[json.loads(row[1]) for row in samples]
    # Undo the exact old proxy fee transform to reconstruct close-to-close factors.
    gross=[(row[2]+1)*(1+.0025)/(1-.0025) for row in samples]
    output=[]
    for horizon in (1,4,8,16):
        labels={}
        for i in range(len(samples)-horizon+1):
            segment=samples[i:i+horizon]
            if all(r[0]==segment[0][0]+j*STEP and r[3]==r[0]+2*STEP for j,r in enumerate(segment)):
                labels[i]=float(np.prod(gross[i:i+horizon])-1)
        train=[i for i in labels if samples[i+horizon-1][3] < boundary]
        test=[i for i in labels if samples[i][0]+STEP >= boundary]
        if len(train)<200 or not test:
            continue
        model=fit(np.array([xs[i] for i in train]),np.array([labels[i] for i in train]))
        estimated={i:predict(model,xs[i]) for i in test}
        for cost in (.001,.002,.0025):
            # First scenario: observed 0.1% commission only. Others add slippage stress.
            factor=(1-cost)/(1+cost)
            positions=[]; next_entry=-1
            for i in test:
                if i<next_entry or (1+estimated[i])*factor<=1:
                    continue
                positions.append((1+labels[i])*factor-1)
                next_entry=i+horizon
            output.append(dict(horizon_bars=horizon,horizon_minutes=horizon*15,
                cost_per_side=cost,train_samples=len(train),test_samples=len(test),
                train_last_label_end=max(samples[i+horizon-1][3] for i in train),
                first_test_decision=min(samples[i][0]+STEP for i in test),
                simulated_round_trips=len(positions),
                net_return=float(np.prod([1+x for x in positions])-1) if positions else 0.,
                mean_trade_return=float(np.mean(positions)) if positions else None,
                positive_trade_count=sum(x>0 for x in positions)))
    return dict(generated_utc=datetime.now(timezone.utc).isoformat(),source_samples=len(samples),
                split='chronological 70/30, training label ends strictly before evaluation decisions',
                evidence='single reused holdout comparison; close fills assumed; not independent forward proof',
                execution_enabled=False,results=output)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',type=Path,default=ROOT/'state/ada-live-15m-learning.sqlite3')
    args=parser.parse_args()
    result=audit(args.db)
    path=ROOT/'reports/ada-horizon-audit.json'
    path.write_text(json.dumps(result,indent=2),encoding='utf-8')
    lines=['# ADA nakit bekleyişi ve getiri ufku deneyi','',result['generated_utc'],'',
           'Yalnız çevrimdışı analiz; canlı modele veya sermaye defterine yazmaz.',
           'Tek kronolojik bölüm; kapanışta dolum varsayımı ve sabit maliyetler. Kârlılık/terfi kanıtı değildir.',
           'İlk maliyet senaryosu yalnız %0,1 komisyon; diğerleri kayma stresini de içerir. Sonuçlar 15m karar sıklığında getiri ufkunu değiştirir.','',
           '| Ufuk | Tek yön maliyet | Simüle tur | Net getiri |',
           '|---|---|---|---|']
    for r in result['results']:
        lines.append(f"| {r['horizon_minutes']} dk | %{100*r['cost_per_side']:.2f} | {r['simulated_round_trips']} | %{100*r['net_return']:.3f} |")
    (ROOT/'reports/ada-horizon-audit.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
