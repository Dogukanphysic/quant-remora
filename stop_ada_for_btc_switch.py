"""Stop the ADA decision loop in its local ledger before BTC takes the USDT allocation."""
import json
import sqlite3
import time

from ada_live import DB, singleton


def main():
    with singleton(DB.with_suffix('.lock')):
        db = sqlite3.connect(DB, timeout=10)
        try:
            row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
            if not row:
                raise ValueError('ADA ledger is empty')
            state = json.loads(row[0])
            unresolved = db.execute('SELECT COUNT(*) FROM orders WHERE applied=0').fetchone()[0]
            if state.get('pending') or unresolved:
                raise ValueError('ADA has an unresolved order; BTC switch refused')
            if state.get('phase') != 'cash':
                raise ValueError('ADA ledger is not in cash; BTC switch refused')
            state.update(halted='OperatorSwitchToBTC',
                         halt_reason='Operator switched allocated USDT from ADAUSDT to BTCUSDT',
                         stopped=True,operator_stopped_at=time.time())
            with db:
                db.execute('UPDATE state SET value=? WHERE id=1',
                           (json.dumps(state, allow_nan=False),))
            print(json.dumps({'ok': True, 'phase': state['phase'], 'pending': None,
                              'orders_submitted': 0, 'halted': state['halted']}))
        finally:
            db.close()


if __name__ == '__main__':
    main()
