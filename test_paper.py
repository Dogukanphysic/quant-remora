import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from paper import (connect, tick, get, put, check_quote, DEFAULT_BUDGETS,
                   TOTAL_PAPER_BUDGET_USD, status_snapshot, exploration_rotation,
                   EXPLORATION_STATE_KEY, EXPLORATION_HOLD_SECONDS,
                   EXPLORATION_SAMPLE_MAX_HOLD_SECONDS,
                   control, migrate_to_bollinger, _worker_fetch_count)
import learning
from strategies import BAR_MS, BAR_SECONDS, MODEL_KEYS, POLICY_ID


def candles(n=200):
    return [dict(ts=i*BAR_MS, open=100., high=101., low=99., close=100., volume=10.) for i in range(n)]


class PaperTests(unittest.TestCase):
    @patch('paper_v3.required_fetch_count', return_value=1234)
    @patch('v2_store.required_fetch_count', return_value=1100)
    def test_worker_combines_v2_and_v3_recovery_fetch_windows(self, v2_count, v3_count):
        self.assertEqual(
            _worker_fetch_count(self.db, 'bollinger_15m_v2', 123_000), 1234)
        v2_count.assert_called_once_with(self.db, 123_000)
        self.assertEqual(v3_count.call_args.kwargs["base_count"], 1100)

    def test_default_portfolio_budgets_total_one_thousand(self):
        self.assertAlmostEqual(sum(DEFAULT_BUDGETS.values()), TOTAL_PAPER_BUDGET_USD)
        self.assertEqual(len(DEFAULT_BUDGETS), 6)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'test.sqlite3'
        self.db = connect(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def step(self, n, bid=100., ask=100.1):
        now = n*BAR_SECONDS+10
        rows = candles(n)
        if get(self.db, EXPLORATION_STATE_KEY, False):
            rows[-1].update(open=90., high=91., low=89., close=90.)
        tick(self.db, {'bid':bid, 'ask':ask, 'timestamp':now}, rows, now)

    @patch('paper.signal', return_value=(True, False))
    def test_restart_does_not_repeat_buy(self, decision):
        self.step(200)
        self.assertIsNone(get(self.db, 'trend')['position'])
        self.step(201)
        cash = get(self.db, 'trend')['cash_usd']
        self.db.close()
        self.db = connect(self.path)
        self.step(201)
        self.assertEqual(get(self.db, 'trend')['cash_usd'], cash)
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='buy' AND strategy='trend'").fetchone()[0], 1)

    @patch('paper.signal', return_value=(True, False))
    def test_normal_entry_requires_positive_volume(self, decision):
        self.step(200)
        rows = candles(201)
        rows[-1]['volume'] = 0
        now = 201*BAR_SECONDS+10
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':now}, rows, now)
        self.assertFalse(any(get(self.db, name)['position'] for name in DEFAULT_BUDGETS))
        blocked = self.db.execute(
            "SELECT count(*) FROM events WHERE kind='entry_blocked' "
            "AND detail LIKE '%zero_volume%'"
        ).fetchone()[0]
        self.assertEqual(blocked, 3)

    @patch('paper.signal', return_value=(True, False))
    def test_protective_exit_without_candle_data(self, decision):
        self.step(200)
        self.step(201)
        now = 201*BAR_SECONDS+40
        tick(self.db, {'bid':95., 'ask':95.1, 'timestamp':now}, None, now)
        state = get(self.db, 'trend')
        self.assertIsNone(state['position'])
        self.assertEqual(state['completed_trades'], 1)
        self.assertLess(state['realized_pnl_usd'], 0)

    @patch('paper.signal', return_value=(True, False))
    def test_daily_halt_blocks_reentry(self, decision):
        self.step(200)
        self.step(201)
        with self.db:
            state = get(self.db, 'trend')
            state['cash_usd'] -= state['initial_usd']*.03
            put(self.db, 'trend', state)
        self.step(202)
        self.assertTrue(get(self.db,'trend')['daily_halt'])
        self.step(203)
        self.assertIsNone(get(self.db,'trend')['position'])

    def test_stale_quote_rejected_without_state_changes(self):
        self.step(200)
        state = get(self.db,'trend')
        with self.assertRaises(ValueError):
            tick(self.db, {'bid':100.,'ask':101.,'timestamp':1}, candles(201), 201*BAR_SECONDS)
        self.assertEqual(get(self.db,'trend'),state)

    def test_stop_flag_blocks_update(self):
        with self.db:
            put(self.db,'stop',True)
        self.step(200)
        self.assertIsNone(get(self.db,'trend'))

    @patch('paper.signal', return_value=(True, False))
    def test_total_drawdown_halt_survives_new_day(self, decision):
        self.step(200)
        with self.db:
            state=get(self.db,'trend')
            state['cash_usd']=state['peak_equity_usd']*.91
            put(self.db,'trend',state)
        self.step(201)
        self.assertTrue(get(self.db,'trend')['drawdown_halt'])
        self.step(288)
        self.assertTrue(get(self.db,'trend')['drawdown_halt'])
        self.assertIsNone(get(self.db,'trend')['position'])

    def test_status_reports_one_aggregate_balance(self):
        self.step(200)
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        snapshot=status_snapshot(self.db)
        aggregate=snapshot['aggregate']
        self.assertAlmostEqual(aggregate['initialized_total_initial_usd'],1000.)
        self.assertAlmostEqual(aggregate['equity_usd'],1000.)
        self.assertEqual(aggregate['open_positions'],0)
        self.assertTrue(snapshot['exploration']['enabled'])
        self.assertEqual(snapshot['exploration']['training_risk_fraction'], .0005)

    @patch('paper.signal', return_value=(True, False))
    def test_late_signal_not_backfilled(self, decision):
        self.step(200)
        now = 201*BAR_SECONDS+600
        tick(self.db, {'bid':100.,'ask':100.1,'timestamp':now}, candles(201), now)
        self.assertIsNone(get(self.db,'trend')['position'])

    @patch('paper.signal', return_value=(False, False))
    def test_exploration_opens_one_tiny_position_and_is_idempotent(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        self.step(201)
        open_states = [(name, get(self.db, name)) for name in DEFAULT_BUDGETS
                       if name.startswith('learned_') and get(self.db, name)['position']]
        self.assertEqual(len(open_states), 1)
        name, state = open_states[0]
        p = state['position']
        self.assertEqual(p['entry_mode'], 'exploration')
        self.assertLessEqual(p['cost'], state['initial_usd']*.025 + 1e-9)
        self.assertLessEqual(p['planned_loss_usd'], state['initial_usd']*.0005 + 1e-9)
        cash = state['cash_usd']
        self.step(201)
        self.assertEqual(get(self.db, name)['cash_usd'], cash)
        buys = self.db.execute(
            "SELECT count(*) FROM events WHERE kind='buy' AND detail LIKE '%exploration%'"
        ).fetchone()[0]
        self.assertEqual(buys, 1)

    @patch('paper.signal', return_value=(False, False))
    def test_exploration_waits_one_bar_then_records_separate_sample(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        self.step(201)
        name = next(name for name in DEFAULT_BUDGETS
                    if name.startswith('learned_') and get(self.db, name)['position'])
        p = get(self.db, name)['position']
        entry_x = p['learning_x']
        before_expiry = p['expires_ts']-1
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':before_expiry}, None, before_expiry)
        self.assertIsNotNone(get(self.db, name)['position'])
        at_expiry = p['ts']+EXPLORATION_HOLD_SECONDS
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':at_expiry}, None, at_expiry)
        self.assertIsNone(get(self.db, name)['position'])
        rows = self.db.execute(
            "SELECT strategy,source,detail FROM learning_samples WHERE source='paper_exploration_bb15'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], (learning.EXPLORATION_MODEL, 'paper_exploration_bb15'))
        import json
        sample = json.loads(rows[0][2])
        self.assertEqual(sample['x'], entry_x)
        self.assertEqual(sample['metadata']['exit_reason'], 'exploration_timeout')
        self.assertEqual(sample['metadata']['risk_tier'], 'training')
        self.assertEqual(sample['metadata']['holding_seconds'], EXPLORATION_HOLD_SECONDS)
        learning.refresh(self.db, force=True)
        self.assertEqual(learning.model_state(
            self.db, learning.EXPLORATION_MODEL)['sample_count'], 1)
        self.assertEqual(learning.model_state(self.db, MODEL_KEYS['trend'])['sample_count'], 0)
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':at_expiry+30}, None, at_expiry+30)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM learning_samples WHERE source='paper_exploration_bb15'"
        ).fetchone()[0], 1)

    @patch('paper.signal', return_value=(False, False))
    def test_overlong_exploration_result_is_not_a_training_label(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        self.step(201)
        name = next(name for name in DEFAULT_BUDGETS
                    if name.startswith('learned_') and get(self.db, name)['position'])
        p = get(self.db, name)['position']
        late = p['ts'] + EXPLORATION_SAMPLE_MAX_HOLD_SECONDS + 1
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':late}, None, late)
        self.assertIsNone(get(self.db, name)['position'])
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM learning_samples WHERE source='paper_exploration_bb15'"
        ).fetchone()[0], 0)
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM learning_quarantine').fetchone()[0], 1)
        skipped = self.db.execute(
            "SELECT detail FROM events WHERE strategy=? AND kind='learning_sample_skipped'",
            (name,)).fetchone()
        self.assertIn('exploration_label_horizon_exceeded', skipped[0])

    @patch('paper.signal', return_value=(False, False))
    def test_expired_probe_closes_before_next_probe_opens(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        self.step(201)
        self.step(202)
        open_count = sum(bool(get(self.db, name)['position']) for name in DEFAULT_BUDGETS
                         if name.startswith('learned_'))
        self.assertEqual(open_count, 1)
        events = self.db.execute(
            "SELECT id,kind FROM events WHERE kind IN ('buy','sell') ORDER BY id"
        ).fetchall()
        self.assertEqual([kind for _,kind in events], ['buy', 'sell', 'buy'])

    @patch('paper.signal', return_value=(False, False))
    def test_exploration_respects_volume_and_spread_guards(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        zero_volume = candles(201)
        zero_volume[-1]['volume'] = 0
        now = 201*BAR_SECONDS+10
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':now}, zero_volume, now)
        self.assertFalse(any(get(self.db, name)['position'] for name in DEFAULT_BUDGETS
                             if name.startswith('learned_')))

        self.step(202, bid=100., ask=102.)
        self.assertFalse(any(get(self.db, name)['position'] for name in DEFAULT_BUDGETS
                             if name.startswith('learned_')))

    @patch('paper.signal', return_value=(False, False))
    def test_eligible_exploration_model_reduces_risk_in_rejected_context(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        collecting = {'status':'collecting', 'model':None, 'eligible':False,
                      'sample_count':0, 'forward_count':0}
        eligible = {'status':'paper_eligible', 'model':{'schema':4}, 'eligible':True,
                    'sample_count':200, 'forward_count':200, 'version':'test'}
        with patch('paper.learning.model_state',
                   side_effect=lambda db, name: eligible if name == learning.EXPLORATION_MODEL else collecting), \
             patch('paper.learning.assess', return_value={
                 'accept':False, 'reason':'insufficient_net_edge', 'score':.4}):
            self.step(201)
        p = next(get(self.db, name)['position'] for name in DEFAULT_BUDGETS
                 if name.startswith('learned_') and get(self.db, name)['position'])
        self.assertEqual(p['risk_fraction'], .0001)
        self.assertEqual(p['allocation_cap'], .005)

    @patch('paper.signal', return_value=(False, False))
    def test_base_model_eligibility_does_not_stop_separate_exploration(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        eligible = {'status':'paper_eligible', 'model':None, 'eligible':True,
                    'sample_count':200, 'forward_count':200}
        collecting = {'status':'collecting', 'model':None, 'eligible':False,
                      'sample_count':0, 'forward_count':0}
        with patch('paper.learning.model_state',
                   side_effect=lambda db, name: collecting if name == learning.EXPLORATION_MODEL else eligible):
            self.step(201)
        self.assertEqual(sum(bool(get(self.db, name)['position']) for name in DEFAULT_BUDGETS
                             if name.startswith('learned_')), 1)

    @patch('paper.signal', return_value=(True, False))
    def test_accepted_normal_model_entry_has_priority_over_exploration(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        eligible = {'status':'paper_eligible', 'model':{'schema':4}, 'eligible':True,
                    'sample_count':200, 'forward_count':200, 'version':'test'}
        collecting = {'status':'collecting', 'model':None, 'eligible':False,
                      'sample_count':0, 'forward_count':0}
        with patch('paper.learning.model_state',
                   side_effect=lambda db, name: collecting if name == learning.EXPLORATION_MODEL else eligible), \
             patch('paper.learning.assess', return_value={
                 'accept':True, 'reason':'accepted', 'score':.8}):
            self.step(201)
        learned_positions = [get(self.db, name)['position'] for name in DEFAULT_BUDGETS
                             if name.startswith('learned_')]
        self.assertTrue(all(p and p['entry_mode'] == 'model' for p in learned_positions))
        self.assertFalse(any(p.get('learning_policy') == learning.EXPLORATION_MODEL
                             for p in learned_positions))

    @patch('paper.signal', return_value=(False, False))
    def test_exploration_does_not_backfill_a_stale_closed_candle(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        now = 201*BAR_SECONDS+600
        stale_rows = candles(201)
        stale_rows[-1].update(open=90., high=91., low=89., close=90.)
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':now}, stale_rows, now)
        self.assertFalse(any(get(self.db, name)['position'] for name in DEFAULT_BUDGETS
                             if name.startswith('learned_')))

    @patch('paper.signal', return_value=(False, True))
    def test_base_exit_signal_cannot_change_exploration_label_horizon(self, decision):
        with self.db:
            put(self.db, EXPLORATION_STATE_KEY, True)
        self.step(200)
        self.step(201)
        name = next(name for name in DEFAULT_BUDGETS
                    if name.startswith('learned_') and get(self.db, name)['position'])
        p = get(self.db, name)['position']
        just_before_expiry = p['expires_ts']-1
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':just_before_expiry},
             candles(202), just_before_expiry)
        self.assertIsNotNone(get(self.db, name)['position'])
        tick(self.db, {'bid':100., 'ask':100.1, 'timestamp':p['expires_ts']},
             None, p['expires_ts'])
        sell = self.db.execute(
            "SELECT detail FROM events WHERE strategy=? AND kind='sell'", (name,)
        ).fetchone()[0]
        import json
        self.assertEqual(json.loads(sell)['reason'], 'exploration_timeout')

    def test_three_day_rotation_does_not_pin_accounts_to_utc_hours(self):
        for slot in range(96):
            first_choices = {
                exploration_rotation((day*96+slot)*BAR_MS)[0]
                for day in range(3)
            }
            self.assertEqual(first_choices, {name for name in DEFAULT_BUDGETS
                                             if name.startswith('learned_')})

    def test_migration_preserves_budget_and_censors_legacy_open_trade(self):
        day = '1970-01-01'
        for name, initial in DEFAULT_BUDGETS.items():
            state = {'initial_usd':initial, 'cash_usd':initial,
                     'position':None, 'last_candle':123, 'day':day,
                     'day_start_equity':initial, 'daily_halt':False,
                     'drawdown_halt':False, 'realized_pnl_usd':0.,
                     'completed_trades':0, 'peak_equity_usd':initial,
                     'equity_usd':initial}
            if name == 'learned_trend':
                state['cash_usd'] -= 1
                state['position'] = {'quantity':.01, 'cost':1., 'entry':100.,
                                     'stop':90., 'target':110., 'ts':900.,
                                     'entry_mode':'exploration',
                                     'learning_policy':'exploration_1h',
                                     'learning_x':[0.]*7}
            with self.db:
                put(self.db, name, state)
        self.db.close()
        with patch('paper.acquire_control_lock', return_value=MagicMock()), \
             patch('paper.acquire_lock', return_value=MagicMock()), \
             patch('paper.time.time', return_value=1000.), \
             patch('paper.quote', return_value={'bid':100., 'ask':100.1, 'timestamp':1000.}):
            migrate_to_bollinger(self.path)
        self.db = connect(self.path)
        self.assertEqual(get(self.db, 'policy_epoch'), POLICY_ID)
        self.assertTrue(all(get(self.db, name)['last_candle'] is None
                            for name in DEFAULT_BUDGETS))
        self.assertTrue(all(get(self.db, name)['position'] is None
                            for name in DEFAULT_BUDGETS))
        aggregate = status_snapshot(self.db)['aggregate']
        expected = 999. + .01*100.*(1-.0005)*(1-.001)
        self.assertAlmostEqual(aggregate['equity_usd'], expected)
        self.assertAlmostEqual(aggregate['initialized_total_initial_usd'], 1000.)
        self.assertEqual(self.db.execute('SELECT count(*) FROM learning_samples').fetchone()[0], 0)
        archived = get(self.db, 'archive_hourly_state_before_bb15')
        self.assertIsNotNone(archived['portfolios']['learned_trend']['position'])

    def test_v1_migration_refuses_to_downgrade_active_v2_epoch(self):
        with self.db:
            put(self.db, 'policy_epoch', 'bollinger_15m_v2')
        self.db.close()
        with patch('paper.acquire_control_lock', return_value=MagicMock()), \
             patch('paper.acquire_lock', return_value=MagicMock()), \
             patch('paper.quote') as quote:
            with self.assertRaisesRegex(ValueError, 'v1 politikasına geri'):
                migrate_to_bollinger(self.path)
        quote.assert_not_called()
        self.db = connect(self.path)
        self.assertEqual(get(self.db, 'policy_epoch'), 'bollinger_15m_v2')

    def test_start_on_empty_database_requires_explicit_v2_initialization(self):
        with patch('paper.connect', return_value=self.db), \
             patch('paper.acquire_control_lock', return_value=MagicMock()), \
             patch('paper.running', return_value=False), \
             patch('paper.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'migrate-bollinger-v2'):
                control('start')
        spawn.assert_not_called()
        self.db = connect(self.path)
        self.assertIsNone(get(self.db, 'policy_epoch'))


if __name__ == '__main__':
    unittest.main()
