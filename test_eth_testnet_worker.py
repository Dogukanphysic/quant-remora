import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class EthTestnetWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {
            'BINANCE_TESTNET_POLICY_MODE': 'hourly',
            'BINANCE_TESTNET_DECISION_INTERVAL': '15m',
        })
        cls.env.start()
        cls.worker = importlib.import_module('eth_testnet_worker')

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_policy_is_eth_testnet_only(self):
        w = self.worker
        self.assertEqual(w.SYMBOL, 'ETHUSDT')
        self.assertEqual(w.BASE_ASSET, 'ETH')
        self.assertEqual(w.INTERVAL, '15m')
        self.assertEqual(w.ENTRY_QUOTE_USDT, w.Decimal('15'))
        self.assertTrue(w.POLICY_CONFIG['testnet_only'])
        self.assertFalse(w.POLICY_CONFIG['real_money_eligible'])
        self.assertFalse(w.POLICY_CONFIG['real_orders_enabled'])

    def test_new_ledger_schema_rejects_btc_symbol(self):
        w = self.worker
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'eth.sqlite3'
            db = w._connect(path)
            try:
                w._ensure_schema(db)
                definition = db.execute(
                    "SELECT sql FROM sqlite_master WHERE name='worker_order_intents'"
                ).fetchone()[0]
                self.assertIn("symbol = 'ETHUSDT'", definition)
                self.assertNotIn("symbol = 'BTCUSDT'", definition)
            finally:
                db.close()

    def test_status_does_not_create_or_submit_orders(self):
        w = self.worker
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'missing.sqlite3'
            lock_path = Path(directory) / 'missing.lock'
            with patch.object(w, 'STATE_DIR', Path(directory)):
                status = w.status_snapshot(db_path, lock_path)
            self.assertFalse(status['running'])
            self.assertEqual(status['symbol'], 'ETHUSDT')
            self.assertFalse(db_path.exists())
            self.assertFalse(status['learning']['active_metrics_available'])
            self.assertIsNone(status['learning']['active_total_labels'])
            self.assertFalse((Path(directory) / 'eth-testnet15m-learning.sqlite3').exists())

    def _learning_database(self, directory, *, populated=True):
        path = Path(directory) / 'eth-testnet15m-learning.sqlite3'
        db = sqlite3.connect(path)
        try:
            with db:
                db.executescript('''
                    CREATE TABLE samples(ts INTEGER PRIMARY KEY, x TEXT,
                        first_seen REAL, origin TEXT, y REAL, label_end INTEGER);
                    CREATE TABLE models(id INTEGER PRIMARY KEY, created REAL,
                        last_label INTEGER, value TEXT);
                    CREATE TABLE predictions(ts INTEGER PRIMARY KEY,
                        model_id INTEGER, prediction REAL, created REAL);
                ''')
                if populated:
                    db.executemany('INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)', [
                        (0, '[]', 0, 'historical', .01, 900_000),
                        (900_000, '[]', 0, 'observed', .02, 1_800_000),
                        (1_800_000, '[]', 0, 'observed', None, None),
                    ])
                    db.executemany('INSERT INTO models VALUES (?, ?, ?, ?)', [
                        (1, 100, 900_000, '{}'), (2, 1900, 1_800_000, '{}'),
                    ])
                    db.executemany('INSERT INTO predictions VALUES (?, ?, ?, ?)', [
                        (0, 1, .01, 100),
                        # A prediction at label completion is not forward evidence.
                        (900_000, 1, .01, 1800),
                        (1_800_000, 2, .01, 1900),
                    ])
        finally:
            db.close()
        return path

    def test_status_preserves_durable_metrics_during_refresh_outage(self):
        w = self.worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            learning_path = self._learning_database(root)
            status_path = root / 'eth-testnet15m-learning-status.json'
            refresh_error = {'status': 'error', 'error': 'BinanceTransportError'}
            status_path.write_text(json.dumps(refresh_error), encoding='utf-8')
            ledger_path = root / 'worker.sqlite3'
            db = w._connect(ledger_path)
            try:
                db.execute('''UPDATE worker_state SET desired_running=1,
                    transient_failures=1, last_error='Open-order read outage: timeout'
                    WHERE singleton=1''')
            finally:
                db.close()
            originals = {path: path.read_bytes() for path in (learning_path, status_path, ledger_path)}
            with patch.object(w, 'STATE_DIR', root), \
                    patch.object(w, '_learning_status_nonfatal', return_value={}), \
                    patch.object(w, '_lock_active', return_value=True), \
                    patch.object(w, '_now_ms', return_value=2_000_000), \
                    patch.object(w.execution, 'Client', side_effect=AssertionError('No API calls')):
                status = w.status_snapshot(ledger_path, root / 'worker.lock')
            self.assertTrue(status['running'])
            self.assertTrue(status['desired_running'])
            self.assertFalse(status['halted'])
            self.assertEqual(status['transient_failures'], 1)
            self.assertEqual(status['last_error'], 'Open-order read outage: timeout')
            learning = status['learning']
            self.assertEqual(learning['active_cadence_learning'], refresh_error)
            self.assertTrue(learning['active_metrics_available'])
            self.assertEqual(learning['active_metrics_source'], 'learner_database')
            self.assertEqual(learning['active_metrics_observed_ms'], 2_000_000)
            self.assertEqual(learning['active_label_counts'], {'historical': 1, 'observed': 1})
            self.assertEqual(learning['active_total_labels'], 2)
            self.assertEqual(learning['active_model_id'], 2)
            self.assertEqual(learning['active_last_training'], 1900)
            self.assertEqual(learning['active_last_label_end_ms'], 1_800_000)
            self.assertEqual(learning['active_forward_scored_predictions'], 1)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)

    def test_current_database_metrics_override_stale_successful_json(self):
        w = self.worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._learning_database(root)
            (root / 'eth-testnet15m-learning-status.json').write_text(json.dumps({
                'status': 'trained_challenger', 'label_counts': {'historical': 999},
                'model_id': 99, 'forward_scored_predictions': 999,
            }), encoding='utf-8')
            with patch.object(w, 'STATE_DIR', root), \
                    patch.object(w, '_learning_status_nonfatal', return_value={}):
                learning = w._public_learning_status(root / 'worker.sqlite3')
            self.assertEqual(learning['active_total_labels'], 2)
            self.assertEqual(learning['active_model_id'], 2)
            self.assertEqual(learning['active_forward_scored_predictions'], 1)

    def test_empty_database_reports_actual_zero_counts(self):
        w = self.worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._learning_database(root, populated=False)
            with patch.object(w, 'STATE_DIR', root), \
                    patch.object(w, '_learning_status_nonfatal', return_value={}):
                learning = w._public_learning_status(root / 'worker.sqlite3')
            self.assertTrue(learning['active_metrics_available'])
            self.assertEqual(learning['active_label_counts'], {})
            self.assertEqual(learning['active_total_labels'], 0)
            self.assertEqual(learning['active_forward_scored_predictions'], 0)
            self.assertIsNone(learning['active_model_id'])
            self.assertIsNone(learning['active_last_training'])
            self.assertIsNone(learning['active_last_label_end_ms'])

    def test_unavailable_database_does_not_reuse_cached_metrics(self):
        w = self.worker
        for content in (None, b'not a sqlite database'):
            with self.subTest(database=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'eth-testnet15m-learning.sqlite3'
                if content is not None:
                    path.write_bytes(content)
                (root / 'eth-testnet15m-learning-status.json').write_text(json.dumps({
                    'status': 'trained_challenger', 'label_counts': {'historical': 999},
                    'model_id': 99, 'forward_scored_predictions': 999,
                }), encoding='utf-8')
                with patch.object(w, 'STATE_DIR', root), \
                        patch.object(w, '_learning_status_nonfatal', return_value={}):
                    learning = w._public_learning_status(root / 'worker.sqlite3')
                self.assertFalse(learning['active_metrics_available'])
                self.assertTrue(learning['active_metrics_error'])
                for field in ('active_label_counts', 'active_total_labels', 'active_model_id',
                              'active_last_training', 'active_last_label_end_ms',
                              'active_forward_scored_predictions', 'active_metrics_observed_ms'):
                    self.assertIsNone(learning[field], field)
                if content is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_bytes(), content)

    def test_invalid_refresh_json_does_not_hide_database_metrics(self):
        w = self.worker
        for content in ('{', 'null', '[]'):
            with self.subTest(json=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._learning_database(root)
                (root / 'eth-testnet15m-learning-status.json').write_text(content, encoding='utf-8')
                with patch.object(w, 'STATE_DIR', root), \
                        patch.object(w, '_learning_status_nonfatal', return_value={}):
                    learning = w._public_learning_status(root / 'worker.sqlite3')
                self.assertEqual(learning['active_cadence_learning']['status'], 'unavailable')
                self.assertTrue(learning['active_metrics_available'])
                self.assertEqual(learning['active_total_labels'], 2)

    def test_policy_file_has_distinct_identity(self):
        config = json.loads((Path(__file__).parent/'config/binance-testnet-eth-hourly-policy.json').read_text())
        self.assertEqual(config['policy_id'], 'eth_hourly_momentum_24h_t002_testnet_v1')
        self.assertEqual(config['order']['symbol'], 'ETHUSDT')
        self.assertEqual(len(config['model_version']), 64)


if __name__ == '__main__':
    unittest.main()
