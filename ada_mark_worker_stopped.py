"""Mark an already stopped ADA worker in the local ledger; never contacts Binance."""

import json
import sqlite3
import time

from ada_live import DB, singleton


def main():
    with singleton(DB.with_suffix('.lock')):
        db = sqlite3.connect(DB, timeout=10)
        try:
            with db:
                state_row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
                if not state_row:
                    raise ValueError('ADA ledger is empty')
                state = json.loads(state_row[0])
                if state.get('pending') or db.execute(
                    'SELECT COUNT(*) FROM orders WHERE applied=0'
                ).fetchone()[0]:
                    raise ValueError('Unresolved ADA order; stopped state not changed')
                if not state.get('stopped'):
                    state['stopped'] = True
                    state['operator_stopped_at'] = time.time()
                    db.execute('UPDATE state SET value=? WHERE id=1',
                               (json.dumps(state, allow_nan=False),))
                print(json.dumps({'stopped': state['stopped'],
                                  'pending': state.get('pending'),
                                  'halted': state.get('halted'),
                                  'orders_submitted': 0}))
        finally:
            db.close()


if __name__ == '__main__':
    main()
