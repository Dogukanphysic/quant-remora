"""Offline multi-horizon ADA training. No credentials, orders or live DB writes."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import numpy as np
from trend4h_learning import fit, predict

ROOT = Path(__file__).resolve().parent
STEP = 900000
HORIZONS = (1, 4, 8, 16)
# Commission observed previously: 0.1%/side. Extra execution costs are assumptions,
# not measured spread: base 0.05%/side, stress 0.15%/side.
COSTS = (0.0015, 0.0025)


def dataset(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        rows = db.execute('SELECT ts,x,y,label_end FROM samples WHERE y IS NOT NULL ORDER BY ts').fetchall()
    if len(rows) < 600:
        raise ValueError('At least 600 completed samples required')
    x = np.asarray([json.loads(r[1]) for r in rows], dtype=float)
    gross = np.asarray([(1+r[2])*1.0025/.9975 for r in rows])
    if not np.isfinite(x).all() or not np.isfinite(gross).all() or (gross <= 0).any():
        raise ValueError('Invalid source values')
    labels = {}
    for h in HORIZONS:
        labels[h] = {i: float(np.prod(gross[i:i+h])-1)
                     for i in range(len(rows)-h+1)
                     if all(rows[j][0] == rows[i][0]+(j-i)*STEP
                            and rows[j][3] == rows[j][0]+2*STEP
                            for j in range(i, i+h))}
    return rows, x, labels


def evaluate(rows, x, labels, horizon, start, end):
    boundary = rows[start][0]+STEP
    train = [i for i in labels if rows[i+horizon-1][3] < boundary]
    # Never let an evaluation outcome cross into the next partition.
    limit = rows[end][0]+STEP if end < len(rows) else rows[-1][3]+1
    test = [i for i in labels if start <= i < end and rows[i+horizon-1][3] < limit]
    if len(train) < 200 or not test:
        raise ValueError('Insufficient purged partition')
    model = fit(x[train], np.asarray([labels[i] for i in train]))
    estimates = {i: predict(model, x[i]) for i in test}
    results = []
    for cost in COSTS:
        equity = peak = 1.0
        drawdown = 0.0
        trades = []
        available = -1
        for i in test:
            if i < available or (1+estimates[i])*(1-cost)/(1+cost) <= 1:
                continue
            net = (1+labels[i])*(1-cost)/(1+cost)-1
            trades.append(net)
            equity *= 1+net
            peak = max(peak, equity)
            drawdown = max(drawdown, 1-equity/peak)
            available = i+horizon
        results.append(dict(cost_per_side=cost, trades=len(trades), net_return=equity-1,
                            closed_trade_drawdown=drawdown,
                            win_rate=float(np.mean(np.asarray(trades)>0)) if trades else None))
    actual = np.asarray([labels[i] for i in test])
    return model, dict(train_samples=len(train), test_samples=len(test),
                       train_last_label=max(rows[i+horizon-1][3] for i in train),
                       first_test_decision=rows[test[0]][0]+STEP,
                       test_last_label=max(rows[i+horizon-1][3] for i in test),
                       mse=float(np.mean((actual-np.asarray(list(estimates.values())))**2)),
                       constant_mse=float(np.mean((actual-np.mean([labels[i] for i in train]))**2)),
                       costs=results)


def train(path):
    rows, x, labels = dataset(path)
    cuts = [int(len(rows)*p) for p in (.4, .6, .8)] + [len(rows)]
    candidates = []
    for h in HORIZONS:
        folds = [evaluate(rows, x, labels[h], h, cuts[j], cuts[j+1])[1] for j in range(2)]
        # Selection sees development only, never the final diagnostic partition.
        score = min(f['costs'][1]['net_return'] for f in folds)
        eligible = all(f['costs'][0]['trades'] >= 5 and f['costs'][1]['trades'] >= 3
                       and f['costs'][0]['net_return'] > 0 and f['costs'][1]['net_return'] > 0
                       for f in folds)
        candidates.append(dict(horizon_bars=h, development=folds, score=score,
                               development_passed=eligible))
    passing = [c for c in candidates if c['development_passed']]
    # Keep a research candidate even when none pass; never call it eligible.
    selected = max(passing or candidates, key=lambda c: c['score'])
    h = selected['horizon_bars']
    model, final = evaluate(rows, x, labels[h], h, cuts[2], cuts[3])
    artifact = dict(version='ada-gross-horizon-research-v1', decision_interval='15m',
                    horizon_bars=h, target='gross_close_to_close_return', model=model,
                    trained_through=final['train_last_label'], execution_eligible=False,
                    automatic_activation=False, cost_scenarios=COSTS)
    artifact['digest'] = hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()
    report = dict(generated_utc=datetime.now(timezone.utc).isoformat(), samples=len(rows),
                  source_start=rows[0][0], source_end=rows[-1][3],
                  evidence='Retrospective reused history, not untouched holdout or forward proof',
                  fill_assumption='Close fills; no intrabar stops; drawdown at trade closes only',
                  costs='0.1% commission/side plus assumed 0.05% or 0.15% execution cost/side',
                  candidates=candidates, selected_horizon_minutes=h*15,
                  development_passed=selected['development_passed'], final_diagnostic=final,
                  status='research_only_requires_forward_validation' if passing else 'no_candidate_passed_development',
                  execution_eligible=False, live_model_modified=False)
    return report, artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT/'state/ada-live-15m-learning.sqlite3')
    parser.add_argument('--output', type=Path, default=ROOT/'reports/ada-challenger-training')
    args = parser.parse_args()
    report, artifact = train(args.db)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in [('report.json', report), ('candidate.json', artifact)]:
        (args.output/name).write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
