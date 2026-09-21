"""Offline next-open ADA target/stop learning. Never imports the live executor."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from strategy_research import features, load
from trend4h_learning import fit, predict

ROOT = Path(__file__).resolve().parent
COSTS = (.0015, .0025)


def outcome(rows, i, horizon, atr):
    entry = rows[i+1]['open']
    stop, target = entry-1.5*atr, entry+3*atr
    if stop <= 0:
        raise ValueError('Invalid stop')
    for j in range(i+1, i+horizon+1):
        bar = rows[j]
        # Gaps first; ambiguous intrabar target+stop is conservatively a stop.
        if bar['open'] <= stop:
            price, reason = bar['open'], 'stop'
        elif bar['open'] >= target:
            price, reason = target, 'target'
        elif bar['low'] <= stop:
            price, reason = stop, 'stop'
        elif bar['high'] >= target:
            price, reason = target, 'target'
        else:
            continue
        return price/entry-1, j, reason
    return rows[i+horizon]['close']/entry-1, i+horizon, 'timeout'


def prepare(rows):
    f = features(rows)
    x, gates = {}, {}
    for i in range(204, len(rows)):
        c, a = rows[i]['close'], f['atr'][i]
        window = rows[i-19:i+1]
        sigma = float(np.std([r['close'] for r in window]))
        vmean = float(np.mean([r['volume'] for r in window]))
        x[i] = [c/rows[i-n]['close']-1 for n in (1,4,16,96)] + [
            f['fast'][i]/f['slow'][i]-1, c/f['trend'][i]-1, a/c,
            (c-f['middle'][i])/max(sigma, 1e-9),
            rows[i]['volume']/max(vmean, 1e-9)-1,
            (c-rows[i]['open'])/max(a, 1e-9)]
        trend = c > f['trend'][i] and f['fast'][i] > f['slow'][i]
        gates[i] = dict(trend=trend,
                        reversion=c < f['middle'][i]-sigma and c > rows[i-1]['close'],
                        breakout=trend and c > max(r['high'] for r in rows[i-20:i]))
    return x, gates, f


def train_before(rows, x, labels, start):
    # Purge even outcomes that finish during the first decision candle.
    indices = [i for i in labels if labels[i][1] < start]
    model = fit(np.array([x[i] for i in indices]), np.array([labels[i][0] for i in indices]))
    return model, max(labels[i][1] for i in indices)


def simulate(rows, x, gates, labels, model, family, start, end, cost):
    equity = peak = 1.0
    drawdown = 0.0
    available = start
    trades, wins = [], 0
    for i in range(start, end):
        if i < available or i not in labels or labels[i][1] >= end or not gates[i][family]:
            continue
        if (1+predict(model, x[i]))*(1-cost)/(1+cost) <= 1:
            continue
        gross, exit_index, reason = labels[i]
        net = (1+gross)*(1-cost)/(1+cost)-1
        # Mark downside conservatively at bar lows, including exit-bar low.
        # This can overstate drawdown after an earlier target fill.
        entry = rows[i+1]['open']
        for j in range(i+1, exit_index+1):
            low_mark = equity*rows[j]['low']/entry*(1-cost)/(1+cost)
            drawdown = max(drawdown, 1-low_mark/peak)
            peak = max(peak, equity*rows[j]['close']/entry*(1-cost)/(1+cost))
        equity *= 1+net
        peak = max(peak, equity)
        drawdown = max(drawdown, 1-equity/peak)
        trades.append(net)
        wins += net > 0
        available = exit_index+1
    losses = -sum(t for t in trades if t < 0)
    return dict(trades=len(trades), net_return=equity-1, conservative_drawdown=drawdown,
                win_rate=wins/len(trades) if trades else None,
                profit_factor=sum(t for t in trades if t > 0)/losses if losses else None,
                cost_per_side=cost,
                buy_hold_return=rows[end-1]['close']/rows[start+1]['open']*(1-cost)/(1+cost)-1)


def research(path, output):
    rows = load(path, 'binance_spot', 'ADAUSDT')
    if len(rows) < 30000:
        raise ValueError('Need substantial history (30000 candles minimum)')
    x, gates, f = prepare(rows)
    a, b, c = [int(len(rows)*p) for p in (.6,.7,.8)]
    candidates, models, all_labels = [], {}, {}
    for horizon in (4,8,16):
        labels = {i: outcome(rows,i,horizon,f['atr'][i]) for i in range(204,len(rows)-horizon)}
        all_labels[horizon] = labels
        model, last_label = train_before(rows,x,labels,a)
        models[horizon] = model
        for family in ('trend','reversion','breakout'):
            folds = [[simulate(rows,x,gates,labels,model,family,s,e,cost) for cost in COSTS]
                     for s,e in ((a,b),(b,c))]
            passed = all(fold[1]['trades'] >= 20 and fold[1]['net_return'] > 0
                         and fold[1]['conservative_drawdown'] < .15
                         and (fold[1]['profit_factor'] or 0) >= 1.15 for fold in folds)
            candidates.append(dict(horizon=horizon, family=family, validation=folds,
                                   train_last_outcome=last_label, validation_start=a,
                                   passed=passed, score=min(fold[1]['net_return'] for fold in folds)))
    eligible = [r for r in candidates if r['passed']]
    selected = max(eligible or candidates, key=lambda r:r['score'])
    h = selected['horizon']
    # Freeze selection before touching final 20%; refit only earlier completed labels.
    frozen, last = train_before(rows,x,all_labels[h],c)
    final = [simulate(rows,x,gates,all_labels[h],frozen,selected['family'],c,len(rows),cost) for cost in COSTS]
    report = dict(generated_utc=datetime.now(timezone.utc).isoformat(), candles=len(rows),
                  start_ts=rows[0]['ts'], end_ts=rows[-1]['ts'],
                  dataset_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  split='60% train, two 10% validation blocks, final 20% diagnostic',
                  target='Next-open trade: 1.5 ATR stop, 3 ATR target, bounded holding horizon',
                  cost_assumption='0.1% commission plus 0.05%/0.15% execution cost per side; spread not measured',
                  evidence='Retrospective backtest, not live or forward proof; full capital, no volume impact model',
                  candidates=candidates, selected=dict(horizon=h, family=selected['family']),
                  development_passed=bool(eligible), final=final,
                  final_train_last_outcome=last, final_start=c,
                  status='requires_forward_validation' if eligible else 'no_candidate_passed',
                  execution_eligible=False, live_modified=False)
    output.mkdir(parents=True,exist_ok=True)
    artifact = dict(model=frozen, horizon=h, family=selected['family'],
                    version='ada-barrier-research-v1', execution_eligible=False,
                    dataset_sha256=report['dataset_sha256'], report_status=report['status'])
    (output/'candidate.json').write_text(json.dumps(artifact,indent=2,allow_nan=False),encoding='utf-8')
    (output/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(report,indent=2,allow_nan=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT/'data/adausdt-15m-long.csv')
    parser.add_argument('--output', type=Path, default=ROOT/'state/ada-barrier-research')
    args = parser.parse_args()
    research(args.data,args.output)
