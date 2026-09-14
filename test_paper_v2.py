import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import paper
import paper_v2
import v2_challengers
import v2_model
import v2_store
from v2_engine import round_trip_net_return
from strategies import BAR_MS, BAR_SECONDS


def bars(count=240):
    return [{'ts': index * BAR_MS, 'open': 100., 'high': 101., 'low': 99.,
             'close': 100. + (index % 3) * .01, 'volume': 10.}
            for index in range(count)]


def artifact(eligible=False):
    return {
        'eligible': eligible, 'status': 'paper_eligible' if eligible else 'shadow',
        'model_version': 'model-test',
        'selection': {'threshold': .6, 'cash_selected': False},
        'fitted': {'training_events': 300},
    }


class PaperV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'paper.sqlite3'
        self.db = paper.connect(self.path)
        v2_store.ensure_tables(self.db)
        with self.db:
            paper.put(self.db, 'policy_epoch', paper_v2.POLICY)
            paper.put(self.db, 'stop', False)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def step(self, count, **patches):
        now = count * BAR_SECONDS + 10
        q = {'bid': 100., 'ask': 100.01, 'timestamp': now}
        return paper_v2.tick(self.db, q, bars(count), now, paper.DEFAULT_BUDGETS)

    @staticmethod
    def fake_decision(db, rows, model_state, now_ms):
        v2_store.ensure_tables(db)
        decision_ts = int(rows[-1]['ts'])
        event_id = v2_store.stable_event_id('breakout', decision_ts)
        db.execute(
            'INSERT OR IGNORE INTO v2_predictions '
            '(event_id,stream_id,spec_id,policy,cost_version,strategy,decision_ts,fill_ts,atr,'
            'model_version,score,threshold,feature_version,features,accepted,created_ts,resolved,resolved_ts,resolution) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)',
            (event_id, v2_store.STREAM_ID, paper_v2.MAIN_SPEC.spec_id,
             paper_v2.POLICY, paper_v2.DEFAULT_COST.version, 'breakout',
             decision_ts, decision_ts + BAR_MS, 1., 'model-test', .7, .6,
             'causal-features-v1', '[0.1]', 0, now_ms))
        return [event_id]

    def add_resolved_forward(self, model_version='model-test', accepted=True):
        decision_ts = 220 * BAR_MS
        fill_ts = decision_ts + BAR_MS
        exit_ts = fill_ts + BAR_MS
        available_ts = exit_ts + BAR_MS
        created_ts = decision_ts + 1_000
        event_id = v2_store.stable_event_id('breakout', decision_ts)
        vector = [float(index) / 100 for index in range(len(v2_model.FEATURE_NAMES))]
        net_return = round_trip_net_return(100., 101., paper_v2.DEFAULT_COST)
        detail = {
            'id': event_id, 'strategy': 'breakout',
            'decision_ts': decision_ts, 'fill_ts': fill_ts,
            'exit_ts': exit_ts, 'label_available_ts': available_ts,
            'entry_reference': 100., 'exit_reference': 101.,
            'entry_price': 100.15, 'exit_price': 100.85,
            'stop': 98., 'target': 102., 'exit_reason': 'vertical',
            'gross_return': .01, 'net_return': net_return,
            'holding_bars': 2, 'accepted': False, 'x': vector,
            'metadata': {
                'spec_id': paper_v2.MAIN_SPEC.spec_id,
                'policy': paper_v2.POLICY,
                'cost_version': paper_v2.DEFAULT_COST.version,
                'source': v2_store.SOURCE,
                'stream_id': v2_store.STREAM_ID,
                'signal_version': paper_v2.MAIN_SPEC.signal_version,
                'horizon_bars': paper_v2.MAIN_SPEC.horizon_bars,
                'fill_ts': fill_ts, 'label_available_ts': available_ts,
                'dataset_hash': v2_store.STREAM_ID,
                'model_version': model_version,
                'prediction': {
                    'model_version': model_version, 'score': .7,
                    'threshold': .6, 'feature_version': v2_model.FEATURE_VERSION,
                    'accepted': accepted, 'created_ts': created_ts,
                },
            },
        }
        with self.db:
            self.db.execute(
                'INSERT INTO v2_predictions '
                '(event_id,stream_id,spec_id,policy,cost_version,strategy,decision_ts,fill_ts,atr,'
                'model_version,score,threshold,feature_version,features,accepted,created_ts,'
                'resolved,resolved_ts,resolution) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)',
                (event_id, v2_store.STREAM_ID, paper_v2.MAIN_SPEC.spec_id,
                 paper_v2.POLICY, paper_v2.DEFAULT_COST.version, 'breakout',
                 decision_ts, fill_ts, 1., model_version, .7, .6,
                 v2_model.FEATURE_VERSION, json.dumps(vector), int(accepted),
                 created_ts, available_ts, 'labeled'))
            self.db.execute(
                'INSERT INTO v2_samples '
                '(event_id,stream_id,spec_id,policy,cost_version,strategy,fill_ts,exit_ts,'
                'label_available_ts,source,net_return,detail,created_ts) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (event_id, v2_store.STREAM_ID, paper_v2.MAIN_SPEC.spec_id,
                 paper_v2.POLICY, paper_v2.DEFAULT_COST.version, 'breakout',
                 fill_ts, exit_ts, available_ts, v2_store.SOURCE, net_return,
                 json.dumps(detail), available_ts))
        return event_id, detail

    @patch('paper_v2.load_artifact', return_value=None)
    def test_base_portfolios_are_cash_only_even_when_shadow_decision_exists(self, _model):
        self.step(240)
        before = {name: paper.get(self.db, name)['cash_usd'] for name in paper_v2.BASE_PORTFOLIOS}
        with patch('paper_v2.v2_store.record_decisions', side_effect=self.fake_decision):
            self.step(241)
        for name in paper_v2.BASE_PORTFOLIOS:
            state = paper.get(self.db, name)
            self.assertEqual(state['cash_usd'], before[name])
            self.assertIsNone(state['position'])
            self.assertEqual(state['capital_mode'], 'shadow_no_capital')

    @patch('paper_v2.load_artifact', return_value=None)
    def test_pristine_v2_initialization_allocates_exactly_one_thousand(self, _model):
        self.step(240)
        states = [paper.get(self.db, name) for name in paper_v2.PORTFOLIOS]
        self.assertAlmostEqual(sum(state['initial_usd'] for state in states), 1000.)
        self.assertAlmostEqual(sum(state['cash_usd'] for state in states), 1000.)
        self.assertIsNotNone(
            paper.get(self.db, paper_v2.PORTFOLIOS_INITIALIZED_KEY))

    @patch('paper_v2.load_artifact', return_value=None)
    def test_partial_active_v2_database_recovers_missing_books_with_zero_cash(self, _model):
        with self.db:
            paper.put(
                self.db,
                'trend',
                paper_v2._initial_state(24., None, '1970-01-01'),
            )
        self.step(240)
        states = {name: paper.get(self.db, name) for name in paper_v2.PORTFOLIOS}
        self.assertAlmostEqual(sum(state['cash_usd'] for state in states.values()), 24.)
        for name in paper_v2.PORTFOLIOS:
            if name != 'trend':
                self.assertEqual(states[name]['initial_usd'], 0.)
                self.assertEqual(states[name]['cash_usd'], 0.)

    @patch('paper_v2.load_artifact', return_value=None)
    def test_ineligible_model_cannot_open_normal_trade(self, _model):
        self.step(240)
        with patch('paper_v2.v2_store.record_decisions', side_effect=self.fake_decision):
            self.step(241)
        self.assertTrue(all(paper.get(self.db, name)['position'] is None
                            for name in paper_v2.PORTFOLIOS))

    @patch('paper_v2.load_artifact', return_value=artifact(False))
    def test_one_micro_probe_uses_h8_expiry_and_small_risk(self, _model):
        with self.db:
            paper.put(self.db, 'exploration_enabled', True)
        self.step(240)
        with patch('paper_v2.v2_store.record_decisions', side_effect=self.fake_decision):
            self.step(241)
        position = paper.get(self.db, 'learned_breakout')['position']
        self.assertIsNotNone(position)
        self.assertEqual(position['entry_mode'], 'v2_micro_probe')
        self.assertLessEqual(position['allocation_cap'], paper_v2.PROBE_ALLOCATION_CAP)
        self.assertLessEqual(position['risk_fraction'], paper_v2.PROBE_RISK_FRACTION)
        self.assertEqual(position['expires_ts'] - position['ts'],
                         paper_v2.POSITION_HOLD_SECONDS)
        self.assertEqual(sum(bool(paper.get(self.db, name)['position'])
                             for name in paper_v2.PORTFOLIOS), 1)

    @patch('paper_v2.load_artifact', return_value=None)
    def test_missing_model_artifact_blocks_untrainable_micro_probe(self, _model):
        with self.db:
            paper.put(self.db, 'exploration_enabled', True)
        self.step(240)
        with patch('paper_v2.v2_store.record_decisions',
                   side_effect=self.fake_decision) as record:
            self.step(241)
        record.assert_not_called()
        self.assertTrue(all(paper.get(self.db, name)['position'] is None
                            for name in paper_v2.PORTFOLIOS))

    def test_auto_retrain_checks_have_persisted_fifteen_minute_cooldown(self):
        now = 10_000.
        self.assertTrue(paper_v2._auto_retrain_check_due(self.db, now))
        with self.db:
            paper.put(self.db, paper_v2.AUTO_RETRAIN_CHECK_STATE_KEY, now)
        self.assertFalse(paper_v2._auto_retrain_check_due(
            self.db, now + paper_v2.AUTO_RETRAIN_CHECK_INTERVAL_SECONDS - 1))
        self.assertTrue(paper_v2._auto_retrain_check_due(
            self.db, now + paper_v2.AUTO_RETRAIN_CHECK_INTERVAL_SECONDS))

    @patch('paper_v2.load_artifact', return_value=artifact(False))
    def test_shadow_micro_probe_prioritizes_model_accepted_candidate(self, _model):
        with self.db:
            paper.put(self.db, 'exploration_enabled', True)
        self.step(240)

        def two_decisions(db, rows, model_state, now_ms):
            ids = []
            decision_ts = int(rows[-1]['ts'])
            for strategy, accepted in (('breakout', 0), ('reentry', 1)):
                event_id = v2_store.stable_event_id(strategy, decision_ts)
                db.execute(
                    'INSERT INTO v2_predictions '
                    '(event_id,stream_id,spec_id,policy,cost_version,strategy,'
                    'decision_ts,fill_ts,atr,model_version,score,threshold,'
                    'feature_version,features,accepted,created_ts,resolved,'
                    'resolved_ts,resolution) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)',
                    (event_id, v2_store.STREAM_ID, paper_v2.MAIN_SPEC.spec_id,
                     paper_v2.POLICY, paper_v2.DEFAULT_COST.version, strategy,
                     decision_ts, decision_ts + BAR_MS, 1., 'model-test', .7, .6,
                     v2_model.FEATURE_VERSION, json.dumps([.1] * 17),
                     accepted, now_ms))
                ids.append(event_id)
            return ids

        with patch('paper_v2.v2_store.record_decisions', side_effect=two_decisions):
            self.step(241)
        self.assertIsNone(paper.get(self.db, 'learned_breakout')['position'])
        position = paper.get(self.db, 'learned_reversion')['position']
        self.assertIsNotNone(position)
        self.assertEqual(position['candidate_strategy'], 'reentry')

    @patch('paper_v2.load_artifact', return_value=artifact(True))
    def test_rotated_artifact_cannot_use_stale_prediction_for_any_capital(self, _model):
        with self.db:
            paper.put(self.db, 'exploration_enabled', True)
        self.step(240)

        def stale_decision(db, rows, model_state, now_ms):
            decision_ts = int(rows[-1]['ts'])
            event_id = v2_store.stable_event_id('breakout', decision_ts)
            db.execute(
                'INSERT INTO v2_predictions '
                '(event_id,stream_id,spec_id,policy,cost_version,strategy,'
                'decision_ts,fill_ts,atr,model_version,score,threshold,'
                'feature_version,features,accepted,created_ts,resolved,'
                'resolved_ts,resolution) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)',
                (event_id, v2_store.STREAM_ID, paper_v2.MAIN_SPEC.spec_id,
                 paper_v2.POLICY, paper_v2.DEFAULT_COST.version, 'breakout',
                 decision_ts, decision_ts + BAR_MS, 1., 'model-before-rotation',
                 .8, .6, v2_model.FEATURE_VERSION,
                 json.dumps([.1] * len(v2_model.FEATURE_NAMES)), 1, now_ms))
            return [event_id]

        with patch('paper_v2.v2_store.record_decisions', side_effect=stale_decision):
            self.step(241)
        position = paper.get(self.db, 'learned_breakout')['position']
        self.assertIsNone(position)

    def test_cost_room_gate_blocks_quiet_candidate(self):
        self.assertIsNone(paper_v2.size_position(100., 100., .05, probe=True))
        self.assertIsNotNone(paper_v2.size_position(100., 100., 1., probe=True))

    def test_challenger_risk_overrides_do_not_change_control_sizing(self):
        control = paper_v2.size_position(100., 100., 1., probe=False)
        challenger = paper_v2.size_position(
            100., 100., 1., probe=False,
            risk_fraction_override=v2_challengers.PAPER_RISK_FRACTION,
            allocation_cap_override=v2_challengers.PAPER_ALLOCATION_CAP)
        self.assertEqual(control['risk_fraction'], paper_v2.NORMAL_RISK_FRACTION)
        self.assertEqual(control['allocation_cap'], paper_v2.NORMAL_ALLOCATION_CAP)
        self.assertEqual(challenger['risk_fraction'], .002)
        self.assertEqual(challenger['allocation_cap'], .10)
        self.assertGreater(challenger['cost'], control['cost'])

    @patch('paper_v2.v2_challengers.open_paper_position', return_value=True)
    @patch('paper_v2.v2_challengers.paper_portfolio_snapshot',
           return_value={'cash_usd': 100.})
    @patch('paper_v2.v2_challengers.ensure_paper_portfolio')
    @patch('paper_v2.v2_challengers.record_event_score')
    @patch('paper_v2.v2_challengers.load_model', return_value={
        'control_model_version': 'model-test'})
    @patch('paper_v2.v2_challengers.registered_model_versions',
           return_value=['challenger-test'])
    def test_challenger_hook_pairs_score_and_isolated_paper_entry(
            self, _versions, _load, record_score, ensure_portfolio,
            _portfolio, open_position):
        decision_ts = 300 * BAR_MS
        created_ts = decision_ts + BAR_MS
        event_id = v2_store.stable_event_id('breakout', decision_ts)
        with self.db:
            self.db.execute(
                'INSERT INTO v2_predictions '
                '(event_id,stream_id,spec_id,policy,cost_version,strategy,'
                'decision_ts,fill_ts,atr,model_version,score,threshold,'
                'feature_version,features,accepted,created_ts,resolved,'
                'resolved_ts,resolution) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)',
                (event_id, v2_store.STREAM_ID, paper_v2.MAIN_SPEC.spec_id,
                 paper_v2.POLICY, paper_v2.DEFAULT_COST.version, 'breakout',
                 decision_ts, decision_ts + BAR_MS, 1., 'model-test', .4, .6,
                 v2_model.FEATURE_VERSION,
                 json.dumps([0.] * len(v2_model.FEATURE_NAMES)), 0,
                 created_ts))
        quote = {
            'bid': 100., 'ask': 100.01,
            'timestamp': decision_ts / 1000 + BAR_SECONDS,
        }
        callback = paper_v2.challenger_after_insert_callback(
            self.db, artifact(False), quote)
        self.assertIsNotNone(callback)
        callback(
            db=self.db, event_id=event_id, strategy='breakout',
            decision_ts=decision_ts, created_ts=created_ts,
            frozen={'model_version': 'model-test'})
        record_score.assert_called_once_with(
            self.db, event_id, 'challenger-test', execution_gate=True,
            created_ts=created_ts, manage_transaction=False)
        ensure_portfolio.assert_called_once_with(
            self.db, 'challenger-test', created_ts, manage_transaction=False)
        open_position.assert_called_once()
        self.assertEqual(open_position.call_args.args[:3],
                         (self.db, event_id, 'challenger-test'))
        self.assertEqual(open_position.call_args.kwargs['strategy'], 'breakout')
        self.assertFalse(open_position.call_args.kwargs['manage_transaction'])
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM v2_executions').fetchone()[0],
            0)

    @patch('paper_v2.load_artifact', return_value=artifact(False))
    def test_tick_passes_matching_challenger_hook_into_atomic_insert(self, _model):
        self.step(240)
        hook = MagicMock()
        with patch('paper_v2.challenger_after_insert_callback',
                   return_value=hook), patch(
                       'paper_v2.v2_store.record_decisions',
                       return_value=[]) as record:
            self.step(241)
        self.assertIs(record.call_args.kwargs['after_insert'], hook)

    @patch('paper_v2.v2_challengers.evaluate_model', return_value={
        'status': 'collecting', 'matched_future_events': 12,
        'would_accept': 3})
    @patch('paper_v2.v2_challengers.load_model', return_value={
        'control_model_version': 'model-test'})
    @patch('paper_v2.v2_challengers.registered_model_versions',
           return_value=['challenger-test'])
    def test_challenger_collecting_freezes_control_retraining(
            self, _versions, _load, _evaluate):
        result = paper_v2.auto_retrain_status(self.db, artifact(False))
        self.assertFalse(result['due'])
        self.assertEqual(
            result['reason'],
            'challenger_control_frozen_for_fair_forward_test')
        self.assertEqual(result['challenger_matched_future_events'], 12)
        self.assertEqual(
            result['challenger_required_future_events'],
            v2_challengers.MIN_MATCHED_FUTURE_EVENTS)

    @patch('paper_v2.v2_model.causal_feature_vector', return_value=[0.] * 17)
    @patch('paper_v2.v2_model.assess', return_value={
        'probability': .8, 'threshold': .6, 'would_accept': True})
    def test_frozen_acceptance_includes_timestamp_spread_and_cost_room(
            self, _assess, _features):
        model = {'model_version': 'model-test'}
        rows = [{'ts': 0}]
        indicators = {'atr': [1.]}
        narrow = {'bid': 100., 'ask': 100.01, 'timestamp': BAR_SECONDS}
        accepted = paper_v2.model_callback(model, narrow)(
            strategy='breakout', rows=rows, features=indicators,
            decision_index=0)
        self.assertTrue(accepted['accepted'])
        stale = {**narrow, 'timestamp': BAR_SECONDS - 1}
        self.assertFalse(paper_v2.model_callback(model, stale)(
            strategy='breakout', rows=rows, features=indicators,
            decision_index=0)['accepted'])
        wide = {'bid': 99., 'ask': 100., 'timestamp': BAR_SECONDS}
        self.assertFalse(paper_v2.model_callback(model, wide)(
            strategy='breakout', rows=rows, features=indicators,
            decision_index=0)['accepted'])
        quiet = {'atr': [.01]}
        self.assertFalse(paper_v2.model_callback(model, narrow)(
            strategy='breakout', rows=rows, features=quiet,
            decision_index=0)['accepted'])

    def test_forward_labels_feed_training_and_version_frozen_promotion(self):
        event_id, detail = self.add_resolved_forward()
        samples = paper_v2.forward_training_samples(self.db)
        self.assertEqual([sample['id'] for sample in samples], [event_id])
        self.assertEqual(samples[0]['x'], detail['x'])
        self.assertEqual(samples[0]['source'], v2_store.SOURCE)
        evidence = paper_v2.prequential_evidence(self.db, 'model-test')
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['event_id'], event_id)
        self.assertTrue(evidence[0]['accepted'])
        self.assertFalse(evidence[0]['executed'])
        self.assertLess(evidence[0]['prediction_ts'], evidence[0]['label_available_ts'])
        v2_store.record_execution(
            self.db, event_id, 'model-test', detail['fill_ts'],
            detail['label_available_ts'] + 1_000, 'v2_micro_probe',
            -.01, -.05, 5., 'stop')
        executed = paper_v2.prequential_evidence(self.db, 'model-test')[0]
        self.assertTrue(executed['executed'])
        self.assertEqual(executed['execution_net_return'], -.01)

    def test_close_state_failure_rolls_back_execution_and_sell_event(self):
        event_id = v2_store.stable_event_id('breakout', 0)
        position = {
            'quantity': .01, 'cost': 1., 'entry': 100.,
            'stop': 90., 'target': 110., 'ts': BAR_SECONDS,
            'entry_ts_ms': BAR_MS, 'expires_ts': 20 * BAR_SECONDS,
            'entry_mode': 'model', 'candidate_strategy': 'breakout',
            'prediction_event_id': event_id, 'model_version': 'model-test',
            'fee': paper_v2.DEFAULT_COST.fee_each_side,
            'slippage': paper_v2.DEFAULT_COST.slippage_each_side,
        }
        state = paper_v2._initial_state(100., 0, '1970-01-01')
        state['cash_usd'] = 99.
        state['position'] = position
        with self.db:
            self.db.execute(
                'INSERT INTO v2_predictions '
                '(event_id,stream_id,spec_id,policy,cost_version,strategy,'
                'decision_ts,fill_ts,atr,model_version,score,threshold,'
                'feature_version,features,accepted,created_ts,resolved,'
                'resolved_ts,resolution) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)',
                (event_id, v2_store.STREAM_ID, paper_v2.MAIN_SPEC.spec_id,
                 paper_v2.POLICY, paper_v2.DEFAULT_COST.version, 'breakout',
                 0, BAR_MS, 1., 'model-test', .8, .6,
                 v2_model.FEATURE_VERSION,
                 json.dumps([.1] * len(v2_model.FEATURE_NAMES)), 1, BAR_MS))
            paper.put(self.db, 'learned_breakout', state)

        real_put = paper_v2._put

        def fail_close_state(db, key, value):
            if key == 'learned_breakout':
                raise RuntimeError('simulated close-state write failure')
            return real_put(db, key, value)

        q = {'bid': 105., 'ask': 105.01, 'timestamp': 2 * BAR_SECONDS}
        with self.assertRaisesRegex(RuntimeError, 'close-state'), \
             patch('paper_v2._put', side_effect=fail_close_state):
            with self.db:
                closing_state = paper.get(self.db, 'learned_breakout')
                paper_v2._close(
                    self.db, closing_state, 'learned_breakout',
                    closing_state['position'], q, 2 * BAR_SECONDS, 'target')
                paper_v2._put(self.db, 'learned_breakout', closing_state)

        self.assertIsNone(v2_store.get_execution(self.db, event_id))
        persisted = paper.get(self.db, 'learned_breakout')
        self.assertEqual(persisted['cash_usd'], 99.)
        self.assertIsNotNone(persisted['position'])
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM events WHERE strategy=? AND kind='sell'",
                ('learned_breakout',),
            ).fetchone()[0],
            0,
        )

    def test_auto_retrain_plan_preserves_non_cash_model_for_forward_test(self):
        cash = artifact(False)
        cash['selection']['cash_selected'] = True
        self.assertFalse(paper_v2._auto_retrain_plan(cash, 24, 0, 0)['due'])
        self.assertTrue(paper_v2._auto_retrain_plan(cash, 25, 0, 0)['due'])
        shadow = artifact(False)
        self.assertFalse(paper_v2._auto_retrain_plan(shadow, 100, 0, 99)['due'])
        self.assertTrue(paper_v2._auto_retrain_plan(shadow, 100, 0, 100)['due'])
        promoted = artifact(True)
        self.assertFalse(paper_v2._auto_retrain_plan(promoted, 200, 0, 200)['due'])

    def test_quarantined_forward_row_does_not_make_retrain_due(self):
        event_id, detail = self.add_resolved_forward()
        detail.pop('x')
        with self.db:
            self.db.execute('UPDATE v2_samples SET detail=? WHERE event_id=?',
                            (json.dumps(detail), event_id))
        cash = artifact(False)
        cash['selection']['cash_selected'] = True
        with patch('paper_v2._read_training_report', return_value={
                'label_contract': {'forward_training_events': 0}}):
            status = paper_v2.auto_retrain_status(self.db, cash)
        self.assertFalse(status['due'])
        self.assertEqual(status['raw_forward_labels'], 1)
        self.assertEqual(status['quarantined_forward_labels'], 1)

    def test_migration_preserves_total_equity_and_archives_v1(self):
        day = '1970-01-01'
        for name, initial in paper.DEFAULT_BUDGETS.items():
            with self.db:
                paper.put(self.db, name, {
                    'initial_usd': initial, 'cash_usd': initial,
                    'position': None, 'last_candle': 123, 'day': day,
                    'day_start_equity': initial, 'daily_halt': False,
                    'drawdown_halt': False, 'realized_pnl_usd': 0.,
                    'completed_trades': 0, 'peak_equity_usd': initial,
                    'equity_usd': initial, 'policy_id': 'bollinger_15m_v1'})
        with self.db:
            paper.put(self.db, 'policy_epoch', 'bollinger_15m_v1')
        self.db.close()
        with patch('paper.acquire_control_lock', return_value=MagicMock()), \
             patch('paper.acquire_lock', return_value=MagicMock()), \
             patch('paper.quote', return_value={'bid': 100., 'ask': 100.01, 'timestamp': 1000.}), \
             patch('paper_v2.time.time', return_value=1000.):
            cutover = paper_v2.migrate(self.path)
        self.db = paper.connect(self.path)
        self.assertAlmostEqual(cutover['equity_usd'], 1000.)
        self.assertEqual(paper.get(self.db, 'policy_epoch'), paper_v2.POLICY)
        self.assertIsNotNone(paper.get(self.db, 'archive_bollinger_v1_before_v2'))
        self.assertTrue(all(paper.get(self.db, name)['position'] is None
                            for name in paper_v2.PORTFOLIOS))

    def test_partial_migration_does_not_create_missing_portfolio_cash(self):
        with self.db:
            paper.put(self.db, 'trend', {
                'initial_usd': 25., 'cash_usd': 24., 'position': None,
                'last_candle': 123, 'day': '1970-01-01',
                'day_start_equity': 25., 'daily_halt': False,
                'drawdown_halt': False, 'realized_pnl_usd': -1.,
                'completed_trades': 1, 'peak_equity_usd': 25.,
                'equity_usd': 24., 'policy_id': 'bollinger_15m_v1'})
            paper.put(self.db, 'policy_epoch', 'bollinger_15m_v1')
        self.db.close()
        with patch('paper.acquire_control_lock', return_value=MagicMock()), \
             patch('paper.acquire_lock', return_value=MagicMock()), \
             patch('paper.quote', return_value={'bid': 100., 'ask': 100.01, 'timestamp': 1000.}), \
             patch('paper_v2.time.time', return_value=1000.):
            cutover = paper_v2.migrate(self.path)
        self.db = paper.connect(self.path)
        self.assertAlmostEqual(cutover['equity_usd'], 24.)
        total_cash = sum(paper.get(self.db, name)['cash_usd']
                         for name in paper_v2.PORTFOLIOS)
        self.assertAlmostEqual(total_cash, 24.)
        self.assertTrue(all(paper.get(self.db, name) is not None
                            for name in paper_v2.PORTFOLIOS))

    def test_migration_does_not_treat_present_empty_state_as_new_database(self):
        with self.db:
            paper.put(self.db, 'trend', {})
            paper.put(self.db, 'policy_epoch', 'bollinger_15m_v1')
        self.db.close()
        with patch('paper.acquire_control_lock', return_value=MagicMock()), \
             patch('paper.acquire_lock', return_value=MagicMock()), \
             patch('paper.quote', return_value={
                 'bid': 100., 'ask': 100.01, 'timestamp': 1000.}), \
             patch('paper_v2.time.time', return_value=1000.):
            with self.assertRaisesRegex(ValueError, 'durumu bozuk'):
                paper_v2.migrate(self.path)
        self.db = paper.connect(self.path)
        self.assertEqual(paper.get(self.db, 'policy_epoch'), 'bollinger_15m_v1')
        self.assertIsNone(paper.get(self.db, 'policy_cutover_v2'))

    def test_migration_refuses_when_worker_lock_is_held(self):
        with patch('paper.acquire_control_lock', return_value=MagicMock()), \
             patch('paper.acquire_lock', return_value=None), \
             patch('paper.quote') as quote:
            with self.assertRaisesRegex(ValueError, 'paper-stop'):
                paper_v2.migrate(self.path)
        quote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
