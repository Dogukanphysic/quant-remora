"""One-hour proxy learning companion; never controls execution."""
import json
import time
from strategy_research import ROOT,load,resample


def refresh(market):
    import trend4h_learning as learner
    if not learner.HOURLY:
        raise ValueError('Hourly learner requires explicit hourly policy mode')
    try:
        if not learner.PATH.exists():
            rows = load(ROOT/'data/binance-spot-btcusdt-15m-through-20260916.csv','binance_spot','BTCUSDT')
            if learner.FRAME == '1h':
                rows = resample(rows,1)
            learner.update(rows,seed=True)
        raw = market.klines(interval=learner.FRAME,limit=1000,symbol='BTCUSDT')
        now = time.time()
        rows = [dict(ts=int(r[0]),open=float(r[1]),high=float(r[2]),low=float(r[3]),
                     close=float(r[4]),volume=float(r[5])) for r in raw if int(r[6]) < now*1000]
        if len(rows)<205 or any(b['ts']-a['ts'] != learner.STEP for a,b in zip(rows,rows[1:])):
            raise ValueError('Incomplete hourly history')
        result = learner.update(rows,now)
    except Exception as exc:
        result = dict(status='error',error=type(exc).__name__)
    (ROOT/f'state/testnet{learner.FRAME}-learning-status.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result
