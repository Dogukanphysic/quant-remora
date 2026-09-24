import json
import sqlite3
from decimal import Decimal

from testnet_performance_audit import audit


def fixture(tmp_path, *, sell_pnl='1'):
    path = tmp_path / 'ledger.sqlite3'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE worker_learning_round_trips (
            record_id TEXT, status TEXT, entry_client_id TEXT, exit_client_id TEXT,
            exact_pnl INTEGER, realized_pnl_usdt TEXT, entry_cost_usdt TEXT,
            entry_candle_close_ms INTEGER, exit_candle_close_ms INTEGER,
            entry_feature_json TEXT, created_ms INTEGER);
        CREATE TABLE worker_order_intents (client_id TEXT,side TEXT,state TEXT,realized_pnl_usdt TEXT);
        CREATE TABLE worker_decisions (candle_close_ms INTEGER,close_latest TEXT);
        CREATE TABLE worker_state (completed_round_trips INTEGER,realized_pnl_usdt TEXT);
    ''')
    db.execute('INSERT INTO worker_learning_round_trips VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               ('one','closed','buy','sell',1,'1','10',1000000,1900000,
                json.dumps({'decision_owner':'testnet_exploration'}),1900001))
    db.executemany('INSERT INTO worker_order_intents VALUES (?,?,?,?)',
                   [('buy','BUY','filled',None),('sell','SELL','filled',sell_pnl)])
    db.executemany('INSERT INTO worker_decisions VALUES (?,?)',
                   [(1000000,'100'),(1900000,'110')])
    db.execute('INSERT INTO worker_state VALUES (?,?)',(1,'1'))
    db.commit()
    db.close()
    return path


def test_exact_round_trip_and_price_proxy(tmp_path):
    result = audit(fixture(tmp_path))
    assert result['errors'] == []
    assert result['realized_pnl_usdt'] == '1'
    assert result['groups']['testnet_exploration']['trades'] == 1
    assert Decimal(result['groups']['testnet_exploration']['buy_hold_proxy']) == 1


def test_mismatched_fill_is_not_counted_as_profit(tmp_path):
    result = audit(fixture(tmp_path, sell_pnl='2'))
    assert result['closed_round_trips'] == 0
    assert result['realized_pnl_usdt'] == '0'
    assert any(item.startswith('pnl_mismatch:') for item in result['errors'])
