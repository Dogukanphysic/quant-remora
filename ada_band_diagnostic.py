"""Read-only comparison of recent ADA 15m candles with the live BB rule.

Uses public Binance candles and the local ledger. No credentials or order API.
"""

from datetime import datetime, timezone
import json
import sqlite3
from urllib.parse import urlencode
from urllib.request import urlopen

from ada_live import DB, bollinger_touch

STEP_MS = 900_000
PUBLIC_API = 'https://data-api.binance.vision/api/v3/'


def public_json(endpoint, **params):
    if endpoint not in ('time', 'klines'):
        raise ValueError('Only public market read endpoints are allowed')
    url = PUBLIC_API + endpoint + ('?' + urlencode(params) if params else '')
    with urlopen(url, timeout=15) as response:
        return json.load(response)


def closed_candles(raw, server_ms):
    rows = []
    for candle in raw:
        if int(candle[6]) >= server_ms:
            continue
        rows.append({'ts': int(candle[0]), 'high': float(candle[2]),
                     'low': float(candle[3]), 'close': float(candle[4])})
    if any(rows[i]['ts'] - rows[i-1]['ts'] != STEP_MS for i in range(1, len(rows))):
        raise ValueError('Recent public candles are not contiguous')
    return rows


def analyze(state, rows, server_ms):
    if state.get('strategy_mode') != 'bollinger_touch_15m_v1':
        raise ValueError('Ledger is not in ADA 15m Bollinger mode')
    if len(rows) < 21:
        raise ValueError('Need at least 21 closed candles')
    changed_ms = int(state['strategy_changed_at'] * 1000)
    transition_bar = changed_ms // STEP_MS * STEP_MS - STEP_MS
    eligible = []
    for i in range(20, len(rows)):
        row = rows[i]
        if row['ts'] <= transition_bar:
            continue
        band = bollinger_touch(rows[i-20:i+1])
        eligible.append({'candle_open_utc': datetime.fromtimestamp(row['ts']/1000, timezone.utc).isoformat(),
                         'candle_open_ms': row['ts'], 'low': row['low'], 'high': row['high'],
                         'lower': band['lower'], 'upper': band['upper'],
                         'lower_touched': band['lower_touched'],
                         'upper_touched': band['upper_touched']})
    lower = [item for item in eligible if item['lower_touched']]
    upper = [item for item in eligible if item['upper_touched']]
    return {
        'mode': state['strategy_mode'],
        'agent_phase': state['phase'],
        'agent_stopped': state.get('stopped'),
        'agent_halted': state.get('halted'),
        'last_poll_age_seconds': round(server_ms/1000 - state['last_poll'], 1),
        'agent_last_candle_open_ms': state['last_bar'],
        'public_last_closed_candle_open_ms': rows[-1]['ts'],
        'eligible_closed_candles_in_window': len(eligible),
        'lower_touch_count': len(lower),
        'upper_touch_count': len(upper),
        'recent_lower_touches': lower[-5:],
        'recent_upper_touches': upper[-5:],
        'latest_public_candle': eligible[-1] if eligible else None,
        'note': 'Touches are market events, not proof of submitted or filled orders. '
                'The worker buys only a fresh lower touch while cash; a missed close window, '
                'sizing rules or IOC non-fill can prevent execution.',
        'orders_submitted': 0,
    }


def main():
    db = sqlite3.connect(DB.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        record = db.execute('SELECT value FROM state WHERE id=1').fetchone()
    finally:
        db.close()
    if not record:
        raise ValueError('ADA ledger has no state')
    state = json.loads(record[0])
    server_ms = int(public_json('time')['serverTime'])
    rows = closed_candles(public_json('klines', symbol='ADAUSDT', interval='15m', limit=1000),
                          server_ms)
    print(json.dumps(analyze(state, rows, server_ms), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
