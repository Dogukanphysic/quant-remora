import copy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import binance_testnet_worker as worker
import testnet_sizing


class TestnetSizingTests(unittest.TestCase):
    def test_only_exact_override_allowed(self):
        base = copy.deepcopy(worker.POLICY_CONFIG)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.json'
            for amount in ('15', '25', 'NaN', '-15'):
                base['testnet_sizing_override'] = {
                    'quote_usdt': amount, 'scope': 'next_entries_only',
                    'authorization': 'user_requested_testnet_risk_increase'}
                path.write_text(json.dumps(base), encoding='utf-8')
                if amount == '15':
                    loaded = worker._load_policy_config(path)
                    self.assertEqual(loaded['order']['quote_usdt'], '10')
                    self.assertFalse(loaded['real_orders_enabled'])
                else:
                    with self.assertRaises(ValueError):
                        worker._load_policy_config(path)

    def test_running_unknown_pending_or_account_lock_refused(self):
        for field in ('running', 'desired_running', 'pending_client_id',
                      'status_available', 'execution_state_known', 'account_lock'):
            state = {'status_available': True, 'execution_state_known': True}
            state[field] = False if field.endswith(('available', 'known')) else True
            with self.subTest(field=field), \
                    patch.object(worker, '_control_mutex', return_value=nullcontext()), \
                    patch.object(worker, 'status_snapshot', return_value=state), \
                    patch.object(worker, '_lock_active', return_value=field == 'account_lock'):
                with self.assertRaises(ValueError):
                    testnet_sizing.configure('15')

    def test_stopped_override_preserves_policy_and_rollback(self):
        original = copy.deepcopy(worker.POLICY_CONFIG)
        original.pop('testnet_sizing_override', None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'policy.json'
            path.write_text(json.dumps(original), encoding='utf-8')
            loader = worker._load_policy_config
            with patch.object(worker, '_control_mutex', return_value=nullcontext()), \
                    patch.object(worker, 'status_snapshot', return_value={
                        'status_available': True, 'execution_state_known': True}), \
                    patch.object(worker, '_lock_active', return_value=False), \
                    patch.object(worker, 'POLICY_CONFIG_PATH', path), \
                    patch.object(worker, '_load_policy_config', side_effect=lambda p=None: loader(p or path)):
                result = testnet_sizing.configure('15')
                changed = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(result['orders_submitted'], 0)
                self.assertEqual(changed.pop('testnet_sizing_override')['quote_usdt'], '15')
                self.assertEqual(changed, original)
                testnet_sizing.configure('10')
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), original)


if __name__ == '__main__':
    unittest.main()
