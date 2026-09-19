import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import ada_live as a
from test_ada_live import Fake


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        a.configure_interval('15m')
        self.temp=tempfile.TemporaryDirectory()
        self.db=a.connect(Path(self.temp.name)/'ledger.db')
        self.client=Fake()
        s=a.initialize(self.db,self.client,self.client.market())
        self.created=self.client.book['now']
        intent=dict(client_id='pending-1',side='SELL',created=self.created,qty='293',price='.5')
        s.update(pending='pending-1',halted='RuntimeError',halt_reason='API HTTP 401; no automatic order retry',exit_requested=True)
        with self.db:
            a.write(self.db,s)
            self.db.execute('INSERT INTO orders(client_id,request) VALUES (?,?)',('pending-1',json.dumps(intent)))
        self.before=a.read(self.db)
        self.client.lookup=Mock(side_effect=a.ApiError('not found',400,-2013))
        self.client.request=Mock(side_effect=lambda method,path,*args:
            {'serverTime':int((self.created+120)*1000)} if path=='time' else [])

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()
        a.configure_interval('4h')

    def test_recovery_preserves_funds_and_audits_without_orders(self):
        result=a.recover_unsent(self.db,self.client)
        s=a.read(self.db)
        self.assertFalse(result['worker_started'])
        self.assertEqual(result['orders_submitted'],0)
        self.assertIsNone(s['pending'])
        self.assertIsNone(s['halted'])
        for field in ('ada','usdt','stop','target','initial_mark_usdt','filled_orders'):
            self.assertEqual(s[field],self.before[field])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recoveries').fetchone()[0],1)
        self.assertEqual(self.client.lookup.call_count,2)
        self.assertTrue(all(call.args[0]=='GET' for call in self.client.request.call_args_list))
        self.assertEqual(self.client.posts,[])
        with self.assertRaises(ValueError): a.recover_unsent(self.db,self.client)

    def test_found_or_ambiguous_order_preserves_pending(self):
        for outcome in ({'status':'FILLED'},a.ApiError('no permission',401,-2015),RuntimeError('timeout')):
            self.client.lookup=Mock(return_value=outcome) if isinstance(outcome,dict) else Mock(side_effect=outcome)
            with self.assertRaises((RuntimeError,ValueError)): a.recover_unsent(self.db,self.client)
            self.assertEqual(a.read(self.db),self.before)

    def test_nonempty_history_preserves_pending(self):
        for blocked in ('allOrders','myTrades'):
            self.client.request=Mock(side_effect=lambda method,path,*args:
                {'serverTime':int((self.created+120)*1000)} if path=='time' else [{}] if path==blocked else [])
            with self.assertRaises(ValueError): a.recover_unsent(self.db,self.client)
            self.assertEqual(a.read(self.db),self.before)

    def test_old_intent_cannot_be_cleared(self):
        self.client.request=Mock(return_value={'serverTime':int((self.created+86400)*1000)})
        with self.assertRaises(ValueError): a.recover_unsent(self.db,self.client)
        self.client.lookup.assert_not_called()


if __name__=='__main__':
    unittest.main()
