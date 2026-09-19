"""Isolated forward-only 4h paper experiment. No credentials or order endpoints."""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from strategy_research import features, signals
from spot_cost_audit import public

ROOT = Path(__file__).resolve().parent
DB = ROOT/'state/trend4h-paper.sqlite3'
COST = .0025


def connect(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, value TEXT)')
    if not db.execute('SELECT 1 FROM state').fetchone():
        db.execute('INSERT INTO state VALUES (1,?)', (json.dumps(dict(
            mode='isolated_paper', policy='trend_hysteresis_4h_balanced_forward_v1',
            desired=False, cash=1000., position=None, realized=0., trades=0,
            last_bar=None, equity=1000., heartbeat=None, last_error=None)),))
        db.commit()
    return db


def read(db):
    return json.loads(db.execute('SELECT value FROM state WHERE id=1').fetchone()[0])


def write(db, s):
    db.execute('UPDATE state SET value=? WHERE id=1', (json.dumps(s),))


def advance(s, bar, price, enter, leave, atr, now):
    """Use observed price only; never invent missed historical fills."""
    events = []
    fresh = s['last_bar'] is not None and bar > s['last_bar']
    p = s['position']
    exited = False
    if p:
        reason = ('stop' if price <= p['stop'] else 'target' if price >= p['target']
                  else 'signal' if fresh and leave else 'timeout'
                  if now-p['opened'] >= 48*4*3600 else None)
        if reason:
            proceeds = p['qty']*price*(1-COST)
            pnl = proceeds-p['cost']
            s['cash'] += proceeds
            s['realized'] += pnl
            s['trades'] += 1
            events.append(dict(side='SELL', reason=reason, price=price, qty=p['qty'], pnl=pnl))
            s['position'] = None
            exited = True
    # Only the latest just-closed bar may trigger entry. No startup/backfill entry.
    timely = 0 <= now-(bar/1000+4*3600) <= 300
    if fresh and timely and enter and not exited and s['position'] is None and price > 2*atr > 0:
        stop = price-2*atr
        qty = min(s['cash']*.2/(price*(1+COST)),
                  s['cash']*.0025/(price*(1+COST)-stop*(1-COST)))
        amount = qty*price*(1+COST)
        s['cash'] -= amount
        s['position'] = dict(qty=qty, cost=amount, stop=stop, target=price+4*atr, opened=now)
        events.append(dict(side='BUY', price=price, qty=qty, cost=amount))
    s['last_bar'] = max(bar, s['last_bar'] or bar)
    s['equity'] = s['cash']+(s['position']['qty']*price*(1-COST) if s['position'] else 0)
    s.update(heartbeat=now, last_error=None, latest_signal=bool(enter),
             latest_price=price, entry_window_valid=timely)
    return events


def run():
    import msvcrt
    lock = open(ROOT/'state/trend4h-paper.lock', 'a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0'); lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return
    db = connect()
    try:
        while read(db)['desired']:
            try:
                now = time.time()
                raw = public('klines?symbol=BTCUSDT&interval=4h&limit=1000')
                rows = [dict(ts=int(r[0]), open=float(r[1]), high=float(r[2]),
                             low=float(r[3]), close=float(r[4]), volume=float(r[5]))
                        for r in raw if int(r[6]) < now*1000]
                if len(rows) < 205 or any(b['ts']-a['ts'] != 14400000 for a,b in zip(rows,rows[1:])):
                    raise ValueError('Incomplete candle history')
                if now-rows[-1]['ts']/1000-14400 > 300:
                    # Most polls are between bar boundaries; valid latest bar can be up to 4h old.
                    if now-rows[-1]['ts']/1000 > 28800:
                        raise ValueError('Stale candles')
                ticker = public('ticker/price?symbol=BTCUSDT')
                price = float(ticker['price'])
                if not 0 < price < float('inf'):
                    raise ValueError('Invalid price')
                f = features(rows)
                entry, exit_ = signals(rows, f, 'trend_hysteresis', 'balanced')
                # Challenger failure must not prevent managing an open paper position.
                try:
                    from trend4h_learning import update
                    learning = update(rows)
                except Exception as exc:
                    learning = {'status': 'error', 'error': type(exc).__name__}
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    s = read(db)
                    if not s['desired']:
                        break
                    events = advance(s, rows[-1]['ts'], price, entry[-1], exit_[-1], f['atr'][-1], time.time())
                    s['pid'] = os.getpid()
                    s['learning'] = learning
                    for event in events:
                        db.execute('INSERT INTO events(ts,kind,value) VALUES (?,?,?)',
                                   (int(time.time()), event['side'], json.dumps(event)))
                    write(db, s)
            except Exception as exc:
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    s = read(db)
                    s.update(last_error=type(exc).__name__, last_attempt=time.time())
                    write(db, s)
            time.sleep(60)
    finally:
        db.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start','stop','status','run'])
    action = parser.parse_args().action
    db = connect()
    if action in ('start', 'stop'):
        with db:
            db.execute('BEGIN IMMEDIATE')
            s = read(db)
            s['desired'] = action == 'start'
            write(db,s)
        if action == 'start':
            with open(ROOT/'state/trend4h-paper.log', 'ab') as log:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run'],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    if action == 'run':
        db.close()
        run()
        return
    s = read(db)
    s['heartbeat_fresh'] = bool(s['heartbeat'] and time.time()-s['heartbeat'] < 180)
    print(json.dumps(s, indent=2))
    db.close()


if __name__ == '__main__':
    main()
