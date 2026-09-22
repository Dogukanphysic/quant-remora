import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import ada_live as a
from test_ada_live import Fake


class AuthorizationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = a.connect(Path(self.temp.name)/'test.db')
        self.client = Fake()
        self.client.test_order = Mock(return_value={})
        s = a.initialize(self.db,self.client,self.client.market())
        s.update(phase='cash',ada='.3',usdt='172',filled_orders=2,halted='ApiError',
                 halt_reason='API HTTP 401; Binance code -2015; Check API key',
                 use_all_allocated_funds=True,auto_allocate_spot=True)
        with self.db: a.write(self.db,s)
        self.before = a.read(self.db)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_recovery_preserves_budget_and_stays_stopped(self):
        result = a.recover_authorization(self.db,self.client)
        after = a.read(self.db)
        self.assertFalse(result['worker_started'])
        self.assertEqual(result['orders_submitted'],0)
        self.assertIsNone(after['halted'])
        self.assertTrue(after['stopped'])
        for key in ('ada','usdt','filled_orders','use_all_allocated_funds','auto_allocate_spot','last_bar'):
            self.assertEqual(after.get(key),self.before.get(key))
        self.assertEqual(self.client.posts,[])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM authorization_recoveries').fetchone()[0],1)
        with self.assertRaises(ValueError): a.recover_authorization(self.db,self.client)

    def test_other_halt_identity_or_pending_rejected(self):
        for key,value in [('halt_reason','API HTTP 400; Binance code -2013;'),('pending','unknown'),('identity','other')]:
            state = dict(self.before); state[key] = value
            with self.db: a.write(self.db,state)
            with self.assertRaises(ValueError): a.recover_authorization(self.db,self.client)
            self.assertEqual(a.read(self.db),state)
        self.client.test_order.assert_not_called()

    def test_unapplied_order_blocks_even_without_pending(self):
        with self.db:
            self.db.execute("INSERT INTO orders(client_id,request,applied) VALUES ('x','{}',0)")
        with self.assertRaises(ValueError): a.recover_authorization(self.db,self.client)
        self.assertEqual(a.read(self.db),self.before)

    def test_fresh_permission_failure_preserves_halt(self):
        self.client.test_order.side_effect = RuntimeError('API HTTP 401; Binance code -2015;')
        with self.assertRaises(RuntimeError): a.recover_authorization(self.db,self.client)
        self.assertEqual(a.read(self.db),self.before)
        self.assertEqual(self.client.posts,[])

    def test_locked_open_or_changing_balances_block(self):
        first = self.client.account()
        locked = self.client.account(); locked['balances'][0]['locked']='1'
        with patch.object(self.client,'account',return_value=locked):
            with self.assertRaises(ValueError): a.recover_authorization(self.db,self.client)
        changed = self.client.account(); changed['balances'][0]['free']='501'
        with patch.object(self.client,'account',side_effect=[first,changed]):
            with self.assertRaises(ValueError): a.recover_authorization(self.db,self.client)
        with patch.object(self.client,'open_orders',return_value=[{}]):
            with self.assertRaises(ValueError): a.recover_authorization(self.db,self.client)
        self.assertEqual(a.read(self.db),self.before)


if __name__ == '__main__': unittest.main()
