"""Research-only ADA 15m Bollinger event learner. No exchange or order access."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import time

import numpy as np

from trend4h_learning import fit, predict

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'state/ada-bollinger-15m-learning.sqlite3'
ARCHIVE = ROOT / 'data/adausdt-15m-long.csv'
STEP = 900000
MAX_HOLD = 768  # 192 hours, matching the live maximum hold.
FEE = 0.001  # Research assumption per side; not the account's actual commission.
VERSION = 'ada-bollinger-touch-event-ridge-v1'
NAMES = ('percent_b', 'band_width', 'lower_penetration', 'upper_distance',
         'return_1', 'return_4', 'return_16', 'band_slope')


def connect(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS events (
        ts INTEGER PRIMARY KEY, x TEXT NOT NULL, source TEXT NOT NULL,
        exit_open_ms INTEGER, y REAL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS models (
        id INTEGER PRIMARY KEY, created REAL NOT NULL, labeled_count INTEGER NOT NULL,
        value TEXT NOT NULL)''')
    return db


def load_archive(path=ARCHIVE):
    with path.open(newline='', encoding='utf-8') as handle:
        rows = []
        for row in csv.DictReader(handle):
            rows.append({key: int(row[key]) if key == 'ts' else float(row[key])
                         for key in ('ts', 'open', 'high', 'low', 'close')})
    return rows


def events(rows):
    """A signal sees only closed bar i and bands known before bar i began."""
    if len(rows) < 21:
        return []
    for left, right in zip(rows, rows[1:]):
        if right['ts'] <= left['ts']:
            raise ValueError('Candles must be strictly increasing')
    bands = [None] * len(rows)
    lower_touch = [False] * len(rows)
    upper_touch = [False] * len(rows)
    for i in range(20, len(rows)):
        if any(rows[j]['ts'] - rows[j-1]['ts'] != STEP for j in range(i-19, i+1)):
            continue
        closes = [row['close'] for row in rows[i-20:i]]
        middle = statistics.fmean(closes)
        width = 2 * statistics.pstdev(closes)
        if middle <= 0 or width <= 0:
            continue
        lower, upper = middle-width, middle+width
        bands[i] = (lower, middle, upper)
        lower_touch[i] = rows[i]['low'] <= lower
        upper_touch[i] = rows[i]['high'] >= upper
    next_upper = [None] * len(rows)
    nearest = None
    for i in range(len(rows)-1, -1, -1):
        next_upper[i] = nearest
        if upper_touch[i]:
            nearest = i
        if i and rows[i]['ts'] - rows[i-1]['ts'] != STEP:
            nearest = None
    result = []
    for i in range(20, len(rows)):
        if not lower_touch[i]:
            continue
        lower, middle, upper = bands[i]
        close = rows[i]['close']
        previous = bands[i-1]
        x = ((close-lower)/(upper-lower), (upper-lower)/middle,
             (lower-rows[i]['low'])/close, (upper-close)/close,
             close/rows[i-1]['close']-1, close/rows[i-4]['close']-1,
             close/rows[i-16]['close']-1,
             (middle-previous[1])/close if previous else 0)
        end = next_upper[i]
        timeout = i + MAX_HOLD
        if end is None or end > timeout:
            end = timeout
        y = exit_ms = None
        if end+1 < len(rows) and rows[end+1]['ts'] - rows[i]['ts'] == (end+1-i)*STEP:
            entry = rows[i+1]['open']
            exit_price = rows[end+1]['open']
            if entry > 0 and exit_price > 0:
                y = exit_price*(1-FEE)/(entry*(1+FEE))-1
                exit_ms = rows[end+1]['ts']
        result.append((rows[i]['ts'], json.dumps(x), exit_ms, y))
    return result


def ingest(db, rows, source):
    output = events(rows)
    for ts, x, exit_ms, y in output:
        db.execute('INSERT OR IGNORE INTO events(ts,x,source,exit_open_ms,y) VALUES (?,?,?,?,?)',
                   (ts, x, source, exit_ms, y))
        if y is not None:
            db.execute('UPDATE events SET exit_open_ms=?,y=? WHERE ts=? AND y IS NULL',
                       (exit_ms, y, ts))
    return len(output)


def metrics(records, model):
    if not records:
        return dict(events=0, baseline_trades=0, baseline_compounded_return=0,
                    selected_trades=0, selected_compounded_return=0)
    predicted = [predict(model, json.loads(row[1])) for row in records]
    def run(choose):
        equity, count, available = 1.0, 0, -1
        for row, selection in zip(records, choose):
            if not selection or row[0] < available:
                continue
            equity *= 1 + row[3]
            count += 1
            available = row[2]
        return count, equity-1
    base_count, base_return = run([True]*len(records))
    selected_count, selected_return = run([value > 0 for value in predicted])
    return dict(events=len(records), baseline_trades=base_count,
                baseline_compounded_return=base_return,
                selected_trades=selected_count, selected_compounded_return=selected_return,
                mse=float(np.mean((np.asarray(predicted)-np.asarray([r[3] for r in records]))**2)))


def train(db, clock=time.time):
    records = db.execute('SELECT ts,x,exit_open_ms,y FROM events WHERE y IS NOT NULL ORDER BY ts').fetchall()
    previous = db.execute('SELECT labeled_count FROM models ORDER BY id DESC LIMIT 1').fetchone()
    if len(records) < 200 or previous and previous[0] == len(records):
        return False
    cut1, cut2 = int(len(records)*.6), int(len(records)*.8)
    validation_start = records[cut1][0]
    train_rows = [r for r in records[:cut1] if r[2] <= validation_start]
    if len(train_rows) < 200:
        return False
    x = np.asarray([json.loads(r[1]) for r in train_rows], dtype=float)
    y = np.asarray([r[3] for r in train_rows], dtype=float)
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError('Nonfinite Bollinger training data')
    model = fit(x, y)
    validation = metrics(records[cut1:cut2], model)
    holdout = metrics(records[cut2:], model)
    artifact = dict(version=VERSION, feature_names=NAMES,
                    target='next_open_to_upper_touch_or_192h_net_proxy_not_execution',
                    model=model, labeled_events=len(records), train_events=len(train_rows),
                    train_last_exit_ms=max(r[2] for r in train_rows),
                    validation=validation, holdout_diagnostic=holdout,
                    cost_per_side=FEE, execution_eligible=False,
                    automatic_activation=False,
                    evidence='retrospective validation and holdout; live fill proof absent')
    artifact['digest'] = hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()
    db.execute('INSERT INTO models(created,labeled_count,value) VALUES (?,?,?)',
               (clock(), len(records), json.dumps(artifact, allow_nan=False)))
    return True


def status(db):
    counts = dict(db.execute('SELECT source,COUNT(*) FROM events WHERE y IS NOT NULL GROUP BY source'))
    row = db.execute('SELECT id,value FROM models ORDER BY id DESC LIMIT 1').fetchone()
    latest = json.loads(row[1]) if row else None
    return dict(status='trained_challenger' if latest else 'collecting',
                model_id=row[0] if row else None, label_counts=counts,
                forward_scored_predictions=0, model=latest,
                decision_authority=False, execution_eligible=False)


def seed(path=DB, archive=ARCHIVE):
    rows = load_archive(archive)
    db = connect(path)
    try:
        with db:
            ingest(db, rows, 'historical')
            train(db)
        return status(db)
    finally:
        db.close()


def update_live(rows, now, path=DB):
    if rows and rows[-1]['ts']+STEP > now*1000:
        raise ValueError('Unclosed ADA candle in Bollinger learner')
    db = connect(path)
    try:
        with db:
            ingest(db, rows, 'live_backfill')
            train(db)
        return status(db)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('seed','status'))
    parser.add_argument('--db', type=Path, default=DB)
    parser.add_argument('--archive', type=Path, default=ARCHIVE)
    args = parser.parse_args()
    if args.action == 'seed':
        report = seed(args.db, args.archive)
    else:
        db = connect(args.db)
        try:
            report = status(db)
        finally:
            db.close()
    print(json.dumps(dict(as_of_utc=datetime.now(timezone.utc).isoformat(), **report),
                     indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
