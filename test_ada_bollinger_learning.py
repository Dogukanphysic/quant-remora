import json
import math
import sqlite3
from unittest.mock import patch

import ada_bollinger_learning as learner
import trend4h_learning as legacy


def candles(count):
    result = []
    for i in range(count):
        close = 1 + .01*math.sin(i*.3)
        result.append(dict(ts=i*learner.STEP,open=close,close=close,
                           low=.8 if i % 8 == 0 else close-.001,
                           high=1.2 if i % 8 == 4 else close+.001))
    return result


def test_features_are_causal_and_labels_wait_for_exit():
    rows = candles(150)
    early = {r[0]: r for r in learner.events(rows[:100])}
    later = {r[0]: r for r in learner.events(rows)}
    assert early
    assert all(json.loads(r[1]) == json.loads(later[ts][1]) for ts, r in early.items())
    assert any(r[3] is None and later[ts][3] is not None for ts, r in early.items())


def test_training_is_separate_and_never_activates_execution(tmp_path):
    path = tmp_path/'bollinger.sqlite3'
    rows = candles(3000)
    db = learner.connect(path)
    try:
        with db:
            learner.ingest(db, rows, 'historical')
            assert learner.train(db)
        result = learner.status(db)
        assert result['model']['version'] == learner.VERSION
        assert result['model']['train_last_exit_ms'] <= rows[-1]['ts']
        assert not result['execution_eligible']
        assert not result['model']['automatic_activation']
        with db:
            assert not learner.train(db)
    finally:
        db.close()


def test_gap_does_not_create_a_future_trade_label():
    rows = candles(100)
    rows[90:] = [dict(row, ts=row['ts'] + 2*learner.STEP) for row in rows[90:]]
    output = learner.events(rows)
    assert all(not (row[0] < rows[90]['ts'] and row[2] is not None and
                    row[2] >= rows[90]['ts']) for row in output)


def test_live_ada_route_selects_separate_learner(tmp_path):
    state = tmp_path/'state'
    state.mkdir()
    live = sqlite3.connect(state/'ada-live.sqlite3')
    live.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,value TEXT)')
    live.execute('INSERT INTO state VALUES (1,?)',
                 (json.dumps({'strategy_mode':'bollinger_touch_15m_v1'}),))
    live.commit()
    live.close()
    with patch.object(legacy,'ROOT',tmp_path), patch.object(learner,'update_live',return_value={'status':'ok'}) as updater:
        result = legacy.update(candles(30), now=30*learner.STEP/1000,
                               path=state/'ada-live-15m-learning.sqlite3')
    assert result == {'status':'ok'}
    updater.assert_called_once()
