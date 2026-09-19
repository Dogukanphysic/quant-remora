"""Fast 4h return challenger. Proxy labels never count as executed trades."""
import hashlib
import json
import sqlite3
import time
import os
import numpy as np
from strategy_research import ROOT, features, load, resample

HOURLY = os.getenv('BINANCE_TESTNET_POLICY_MODE') == 'hourly'
FRAME = os.getenv('BINANCE_TESTNET_DECISION_INTERVAL','1h') if HOURLY else '4h'
if FRAME not in ('15m','1h','4h'):
    raise ValueError('Unsupported learning interval')
PATH = ROOT/(f'state/testnet{FRAME}-learning.sqlite3' if HOURLY else 'state/trend4h-learning.sqlite3')
STEP = {'15m':900000,'1h':3600000,'4h':14400000}[FRAME]
VERSION = f'ridge-next{FRAME}-v1'


def connect(path=PATH):
    db = sqlite3.connect(path, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS samples(ts INTEGER PRIMARY KEY,x TEXT,first_seen REAL,origin TEXT,y REAL,label_end INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS models(id INTEGER PRIMARY KEY,created REAL,last_label INTEGER,value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS predictions(ts INTEGER PRIMARY KEY,model_id INTEGER,prediction REAL,created REAL)')
    return db


def vectors(rows):
    f = features(rows)
    result = {}
    for i in range(204,len(rows)):
        c = rows[i]['close']
        result[rows[i]['ts']] = [c/rows[i-1]['close']-1, c/rows[i-6]['close']-1,
                               c/rows[i-24]['close']-1, f['fast'][i]/f['slow'][i]-1,
                               c/f['trend'][i]-1, f['atr'][i]/c]
    return result


def ingest(db, rows, now, seed=False):
    xs = vectors(rows)
    for i in range(204, len(rows)):
        r = rows[i]
        if r['ts']+STEP > now*1000:
            continue
        origin = ('historical' if seed else 'observed' if 0 <= now*1000-(r['ts']+STEP) <= 300000 else 'backfill')
        db.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?,NULL,NULL)',
                   (r['ts'],json.dumps(xs[r['ts']]),now,origin))
    # A label needs the next complete, adjacent candle. No fabricated gap labels.
    for a,b in zip(rows,rows[1:]):
        if b['ts']-a['ts'] == STEP and b['ts']+STEP <= now*1000:
            y = b['close']/a['close']*(1-.0025)/(1+.0025)-1
            db.execute('UPDATE samples SET y=?,label_end=? WHERE ts=? AND y IS NULL',
                       (y,b['ts']+STEP,a['ts']))


def fit(x,y):
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-9] = 1
    z = np.column_stack([np.ones(len(x)),(x-mean)/scale])
    penalty = np.eye(z.shape[1]); penalty[0,0] = 0
    beta = np.linalg.solve(z.T@z+penalty*10, z.T@y)
    return dict(mean=mean.tolist(),scale=scale.tolist(),beta=beta.tolist())


def predict(model,x):
    z = [1.]+[(v-m)/s for v,m,s in zip(x,model['mean'],model['scale'])]
    return float(np.dot(z,model['beta']))


def train(db, clock=None):
    records = db.execute('SELECT ts,x,y,label_end FROM samples WHERE y IS NOT NULL ORDER BY ts').fetchall()
    previous = db.execute('SELECT last_label FROM models ORDER BY id DESC LIMIT 1').fetchone()
    if len(records)<200 or (previous and previous[0] >= records[-1][3]):
        return
    records = records[-3000:]
    cut = int(len(records)*.8)
    # One sample embargo: training outcome precedes first validation decision.
    learning, validation = records[:cut-1],records[cut:]
    x = np.asarray([json.loads(r[1]) for r in learning]); y = np.asarray([r[2] for r in learning])
    model = fit(x,y)
    actual = np.asarray([r[2] for r in validation])
    estimates = np.asarray([predict(model,json.loads(r[1])) for r in validation])
    accepted = actual[estimates > 0]
    model.update(version=VERSION,samples=len(records),train_samples=len(learning),
                 validation_samples=len(validation),validation_mse=float(np.mean((actual-estimates)**2)),
                 constant_mse=float(np.mean((actual-y.mean())**2)),
                 accepted_proxy_samples=len(accepted),
                 accepted_mean_net_return=float(accepted.mean()) if len(accepted) else None,
                 train_last_label=learning[-1][3],validation_start=validation[0][0]+STEP,
                 target=f'next_{FRAME}_close_to_close_net_proxy_not_execution',
                 automatic_activation=False,execution_eligible=False,
                 evidence='rolling_reused_validation_not_independent_promotion')
    model['digest'] = hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest()
    db.execute('INSERT INTO models(created,last_label,value) VALUES (?,?,?)',
               ((clock or time.time)(),records[-1][3],json.dumps(model,allow_nan=False)))


def update(rows, now=None, seed=False, path=PATH, clock=None):
    now = time.time() if now is None else now
    db = connect(path)
    try:
        with db:
            ingest(db,rows,now,seed)
            train(db,clock=clock)
            latest = db.execute('SELECT id,value FROM models ORDER BY id DESC LIMIT 1').fetchone()
            if latest and not seed and rows[-1]['ts']+STEP <= now*1000:
                x = db.execute('SELECT x,origin FROM samples WHERE ts=?',(rows[-1]['ts'],)).fetchone()
                if x and x[1]=='observed':
                    prediction = predict(json.loads(latest[1]),json.loads(x[0]))
                    created = clock() if clock else now
                    db.execute('INSERT OR IGNORE INTO predictions VALUES (?,?,?,?)',
                               (rows[-1]['ts'],latest[0],prediction,created))
        return status(db)
    finally:
        db.close()


def status(db):
    latest = db.execute('SELECT id,created,value FROM models ORDER BY id DESC LIMIT 1').fetchone()
    counts = dict(db.execute('SELECT origin,COUNT(*) FROM samples WHERE y IS NOT NULL GROUP BY origin'))
    forward = db.execute('SELECT COUNT(*) FROM predictions p JOIN samples s ON s.ts=p.ts WHERE s.y IS NOT NULL AND p.created*1000 < s.label_end').fetchone()[0]
    return dict(status='trained_challenger' if latest else 'collecting',label_counts=counts,
                model_id=latest[0] if latest else None,last_training=latest[1] if latest else None,
                model=json.loads(latest[2]) if latest else None,
                forward_scored_predictions=forward,retrain_every_new_labels=1,
                nominal_new_labels_per_day=86400000//STEP,execution_eligible=False)


if __name__ == '__main__':
    path = ROOT/'data/binance-spot-btcusdt-15m-through-20260916.csv'
    rows = resample(load(path,'binance_spot','BTCUSDT'),4)
    print(json.dumps(update(rows,seed=True),indent=2))
