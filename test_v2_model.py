import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import v2_model
from v2_engine import BAR_MS, MAIN_SPEC, POLICY, dataset_digest, round_trip_net_return


def bars(count=260):
    rows = []
    for index in range(count):
        price = 100 + index * .03 + (index % 7) * .02
        rows.append({'ts': index * BAR_MS, 'open': price - .01,
                     'high': price + .3, 'low': price - .3,
                     'close': price, 'volume': 10 + index % 5})
    return rows


def sample(index, positive=None):
    positive = bool(index % 2) if positive is None else positive
    entry = 100.
    exit_price = 101. if positive else 99.
    return {
        'id': f's{index}', 'strategy': 'breakout' if index % 3 else 'reentry',
        'fill_ts': index * 20_000, 'exit_ts': index * 20_000 + 5_000,
        'label_available_ts': index * 20_000 + 6_000,
        'entry_reference': entry, 'exit_reference': exit_price,
        'target': entry * 1.02,
        'net_return': .006 if positive else -.012,
        'x': [float((index + feature) % 5) / 5 for feature in range(len(v2_model.FEATURE_NAMES))],
    }


def forward_sample(index, decision_ts, positive=None):
    positive = bool(index % 2) if positive is None else positive
    entry = 100.
    exit_price = 101. if positive else 99.
    fill_ts = decision_ts + BAR_MS
    exit_ts = fill_ts + 2 * BAR_MS
    label_available_ts = exit_ts + BAR_MS
    cost = v2_model.COST_SCENARIOS['30bp']
    return {
        'id': f'f{index}',
        'strategy': 'breakout' if index % 2 else 'reentry',
        'decision_ts': decision_ts,
        'fill_ts': fill_ts,
        'exit_ts': exit_ts,
        'label_available_ts': label_available_ts,
        'entry_reference': entry,
        'exit_reference': exit_price,
        'target': entry * 1.02,
        'net_return': round_trip_net_return(entry, exit_price, cost),
        'x': [float((index + feature) % 7) / 7
              for feature in range(len(v2_model.FEATURE_NAMES))],
        'metadata': {
            'spec_id': MAIN_SPEC.spec_id,
            'policy': POLICY,
            'cost_version': cost.version,
            'signal_version': MAIN_SPEC.signal_version,
            'horizon_bars': MAIN_SPEC.horizon_bars,
            'source': v2_model.FORWARD_SAMPLE_SOURCE,
            'stream_id': v2_model.FORWARD_STREAM_ID,
            'dataset_hash': v2_model.FORWARD_STREAM_ID,
            'fill_ts': fill_ts,
            'label_available_ts': label_available_ts,
            'prediction': {
                'model_version': 'frozen-model-v1',
                'feature_version': v2_model.FEATURE_VERSION,
                'created_ts': fill_ts,
            },
        },
    }


class V2ModelTests(unittest.TestCase):
    def test_feature_vector_is_causal(self):
        rows = bars(280)
        decision = 230
        before = v2_model.causal_feature_vector(rows, decision, 'breakout')
        changed = copy.deepcopy(rows)
        for row in changed[decision + 1:]:
            row.update(open=500, high=800, low=400, close=700, volume=9999)
        after = v2_model.causal_feature_vector(changed, decision, 'breakout')
        self.assertEqual(before, after)

    def test_early_engine_labels_are_skipped_when_slow_features_do_not_exist(self):
        rows = bars(260)
        label = {'id': 'early', 'strategy': 'breakout',
                 'decision_ts': rows[100]['ts'], 'fill_ts': rows[101]['ts'],
                 'exit_ts': rows[105]['ts'],
                 'label_available_ts': rows[105]['ts'] + BAR_MS,
                 'entry_reference': 100., 'exit_reference': 101.,
                 'net_return': .005, 'metadata': {}}
        with patch('v2_model.generate_labeled_samples', return_value=[label]):
            self.assertEqual(v2_model.build_historical_samples(rows), [])

    def test_json_round_trip_keeps_stdlib_prediction_exact(self):
        data = [sample(index) for index in range(80)]
        fitted = v2_model.fit_logistic_model(data, .2)
        selection = {'c': .2, 'threshold': .6, 'cash_selected': False,
                     'reason': 'test'}
        artifact = v2_model.assemble_model_artifact(
            fitted, selection, dataset_hash='a' * 64, data_path='test.csv')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.json'
            v2_model.save_model(path, artifact)
            loaded = v2_model.load_model(path)
        vector = data[3]['x']
        self.assertEqual(v2_model.predict_probability(artifact, vector),
                         v2_model.predict_probability(loaded, vector))
        self.assertEqual(artifact['model_version'], loaded['model_version'])

    def test_model_artifact_rejects_stale_spec_signal_and_cost_contracts(self):
        data = [sample(index) for index in range(30)]
        fitted = v2_model.fit_logistic_model(data, .2)
        artifact = v2_model.assemble_model_artifact(
            fitted, {'c': .2, 'threshold': .6, 'cash_selected': False,
                     'reason': 'test'},
            dataset_hash='e' * 64, data_path='test.csv')
        for field in ('spec_id', 'signal_version', 'cost_version',
                      'execution_policy_version', 'training_protocol_version'):
            with self.subTest(field=field):
                stale = copy.deepcopy(artifact)
                stale[field] = 'stale-contract'
                with self.assertRaisesRegex(ValueError, field):
                    v2_model.validate_model_artifact(stale)

    def test_later_validation_labels_do_not_change_first_fold_model_selection(self):
        data = [sample(index) for index in range(300)]
        before = v2_model.walk_forward_evaluate(data, folds=5, min_train_events=100)
        changed = copy.deepcopy(data)
        for row in changed[-35:]:
            row['net_return'] *= -1
            row['exit_reference'] = 98. if row['exit_reference'] > 100 else 102.
        after = v2_model.walk_forward_evaluate(changed, folds=5, min_train_events=100)
        self.assertEqual(before['folds'][0]['selection_fingerprint'],
                         after['folds'][0]['selection_fingerprint'])

    def test_nonfinite_feature_is_rejected(self):
        data = [sample(index) for index in range(30)]
        data[0]['x'][0] = float('nan')
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            v2_model.fit_logistic_model(data)

    def test_acceptance_matches_two_strategy_accounts_and_cost_room(self):
        first = sample(10, positive=True)
        first.update(strategy='breakout', fill_ts=1_000, exit_ts=2_000,
                     target=103.)
        second = sample(11, positive=True)
        second.update(strategy='reentry', fill_ts=1_000, exit_ts=2_000,
                      target=103.)
        overlap = sample(12, positive=True)
        overlap.update(strategy='breakout', fill_ts=1_500, exit_ts=2_500,
                       target=103.)
        too_quiet = sample(13, positive=True)
        too_quiet.update(strategy='reentry', fill_ts=3_000, exit_ts=4_000,
                         target=100.4)
        accepted = v2_model._accepted(
            [first, second, overlap, too_quiet], [.9, .9, .9, .9], .6)
        self.assertEqual({item['id'] for item, _ in accepted},
                         {first['id'], second['id']})

    def test_historical_fit_without_prequential_evidence_stays_shadow(self):
        data = [sample(index) for index in range(160)]
        fitted = v2_model.fit_logistic_model(data)
        artifact = v2_model.assemble_model_artifact(
            fitted, {'c': .2, 'threshold': .6, 'cash_selected': False,
                     'reason': 'test'},
            dataset_hash='b' * 64, data_path='test.csv')
        evaluation = v2_model.walk_forward_evaluate(data, folds=5, min_train_events=60)
        promotion = v2_model.evaluate_promotion(artifact, evaluation, [])
        self.assertFalse(promotion['eligible'])
        self.assertIn('true_forward_50', promotion['failed_checks'])

    def test_promotion_uses_actual_execution_return_for_accepted_trades(self):
        data = [sample(index) for index in range(160)]
        fitted = v2_model.fit_logistic_model(data)
        artifact = v2_model.assemble_model_artifact(
            fitted, {'c': .2, 'threshold': .6, 'cash_selected': False,
                     'reason': 'test'},
            dataset_hash='f' * 64, data_path='test.csv')
        evaluation = v2_model.walk_forward_evaluate(data, folds=5, min_train_events=60)
        evidence = {
            'event_id': 'forward-1', 'decision_ts': 1_000,
            'prediction_ts': 1_100, 'label_available_ts': 2_000,
            'probability': .8, 'net_return': .02, 'accepted': True,
            'executed': False, 'execution_net_return': None,
            'execution_label_available_ts': None,
            'model_version': artifact['model_version'],
            'source': 'paper_v2_prequential',
        }
        unexecuted = v2_model.evaluate_promotion(artifact, evaluation, [evidence])
        self.assertEqual(unexecuted['prequential']['model_accepted_count'], 1)
        self.assertEqual(unexecuted['prequential']['accepted_count'], 0)
        executed_record = {
            **evidence, 'executed': True, 'execution_net_return': -.01,
            'execution_label_available_ts': 2_100,
        }
        executed = v2_model.evaluate_promotion(
            artifact, evaluation, [executed_record])
        self.assertEqual(executed['prequential']['accepted_count'], 1)
        self.assertEqual(executed['metrics']['accepted'], 1)
        self.assertEqual(executed['metrics']['profit_factor'], 0.)

    def test_forward_samples_are_canonical_and_order_independent(self):
        history = [sample(index) for index in range(20)]
        digest = 'c' * 64
        cutoff = 10_000_000
        first = forward_sample(1, cutoff + 2 * BAR_MS)
        second = forward_sample(2, cutoff + BAR_MS)
        merged_a, hash_a = v2_model._merge_training_samples(
            history, [first, second], historical_digest=digest,
            historical_last_ts=cutoff)
        merged_b, hash_b = v2_model._merge_training_samples(
            history, [second, first], historical_digest=digest,
            historical_last_ts=cutoff)
        self.assertEqual(hash_a, hash_b)
        self.assertEqual(len(hash_a), 64)
        self.assertNotEqual(hash_a, digest)
        self.assertEqual([row['id'] for row in merged_a],
                         [row['id'] for row in merged_b])
        self.assertEqual([row['id'] for row in merged_a[-2:]], ['f2', 'f1'])

    def test_forward_samples_covered_by_refreshed_history_are_not_counted_twice(self):
        history = [sample(index) for index in range(20)]
        digest = 'e' * 64
        cutoff = 10_000_000
        covered = forward_sample(1, cutoff)
        later = forward_sample(2, cutoff + BAR_MS)
        merged, corpus_hash = v2_model._merge_training_samples(
            history, [covered, later], historical_digest=digest,
            historical_last_ts=cutoff)
        self.assertNotIn('f1', [row['id'] for row in merged])
        self.assertEqual(merged[-1]['id'], 'f2')
        self.assertNotEqual(corpus_hash, digest)
        history_only, history_hash = v2_model._merge_training_samples(
            history, [covered], historical_digest=digest,
            historical_last_ts=cutoff)
        self.assertEqual(history_only, history)
        self.assertEqual(history_hash, digest)

    def test_forward_sample_duplicate_and_contract_mismatch_are_rejected(self):
        cutoff = 10_000_000
        valid = forward_sample(1, cutoff + BAR_MS)
        with self.assertRaisesRegex(ValueError, 'Duplicate training sample id'):
            v2_model._merge_training_samples(
                [sample(0)], [valid, copy.deepcopy(valid)],
                historical_digest='d' * 64, historical_last_ts=cutoff)

        wrong_source = copy.deepcopy(valid)
        wrong_source['metadata']['source'] = 'historical_v2_h8'
        with self.assertRaisesRegex(ValueError, 'source'):
            v2_model._merge_training_samples(
                [sample(0)], [wrong_source], historical_digest='d' * 64,
                historical_last_ts=cutoff)

        wrong_features = copy.deepcopy(valid)
        wrong_features['metadata']['prediction']['feature_version'] = 'old-features'
        with self.assertRaisesRegex(ValueError, 'feature version'):
            v2_model._merge_training_samples(
                [sample(0)], [wrong_features], historical_digest='d' * 64,
                historical_last_ts=cutoff)

    def test_train_report_merges_forward_samples_and_empty_input_is_backward_compatible(self):
        rows = bars(260)
        history = [sample(index) for index in range(40)]
        cutoff = int(rows[-1]['ts'])
        forward = forward_sample(1, cutoff + BAR_MS)
        covered_forward = forward_sample(2, cutoff)
        with tempfile.TemporaryDirectory() as folder, \
                patch('v2_model.build_historical_samples', return_value=history):
            root = Path(folder)
            base = v2_model.train_and_report(
                rows, root / 'data.csv', root / 'base-model.json',
                root / 'base-report.json')
            explicit_empty = v2_model.train_and_report(
                rows, root / 'data.csv', root / 'empty-model.json',
                root / 'empty-report.json', forward_samples=())
            updated = v2_model.train_and_report(
                rows, root / 'data.csv', root / 'updated-model.json',
                root / 'updated-report.json',
                forward_samples=[covered_forward, forward])

            self.assertEqual(base['artifact'], explicit_empty['artifact'])
            self.assertEqual(base['artifact']['dataset_hash'], dataset_digest(rows))
            self.assertNotEqual(updated['artifact']['model_version'],
                                base['artifact']['model_version'])
            contract = updated['report']['label_contract']
            self.assertEqual(contract['historical_events'], 40)
            self.assertEqual(contract['forward_training_events'], 2)
            self.assertEqual(contract['forward_training_events_included'], 1)
            self.assertEqual(contract['forward_training_events_overlap_filtered'], 1)
            self.assertEqual(contract['total_training_events'], 41)
            self.assertEqual(updated['report']['dataset']['training_corpus_hash'],
                             updated['artifact']['dataset_hash'])
            self.assertEqual(len(updated['artifact']['dataset_hash']), 64)

    def test_atomic_json_failure_preserves_previous_report(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'report.json'
            destination.write_text('{"old":true}', encoding='utf-8')
            with patch('v2_model.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaisesRegex(OSError, 'disk failure'):
                    v2_model._write_json(destination, {'new': True})
            self.assertEqual(destination.read_text(encoding='utf-8'), '{"old":true}')
            self.assertEqual(list(destination.parent.glob('.report.json.*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
