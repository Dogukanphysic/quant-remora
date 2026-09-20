import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import ada_live as a
from test_ada_live import Fake


class RebaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=a.connect(Path(self.temp.name)/'ledger.db')
        self.client=Fake()
        a.configure_interval('15m')
        s=a.initialize(self.db,self.client,self.client.market())
        s.update(ada='0.3',usdt='67.39542711',phase='cash',filled_orders=1,
                 halted='ValueError',halt_reason='Allocated capital unavailable; external account change')
        with self.db: a.write(self.db,s)
        self.before=a.read(self.db)
        self.client.bal={'ADA':Decimal('296.90639847'),'USDT':Decimal('0'),'TRY':Decimal('133.14')}
        self.mode=patch.object(a,'USE_ALL_ALLOCATED_FUNDS',True)
        self.mode.start()

    def tearDown(self):
        self.mode.stop()
        a.configure_interval('4h')
        self.db.close()
        self.temp.cleanup()

    def test_external_conversion_is_new_epoch_not_profit(self):
        result=a.adopt_spot_balance(self.db,self.client)
        s=a.read(self.db)
        self.assertTrue(result['ok'])
        self.assertFalse(result['worker_started'])
        self.assertEqual(s['ada'],'296.90639847')
        self.assertEqual(s['usdt'],'0')
        self.assertEqual(s['filled_orders'],1)
        self.assertEqual(s['marked_pnl_from_activation_usdt'],'0')
        self.assertIsNone(s['halted'])
        self.assertTrue(s['stopped'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM capital_rebases').fetchone()[0],1)
        self.assertEqual(self.client.posts,[])
        with self.assertRaises(ValueError): a.adopt_spot_balance(self.db,self.client)

    def test_pending_or_wrong_halt_blocks_rebase(self):
        for field,value in [('pending','unknown'),('halt_reason','API HTTP 401;'),('identity','other')]:
            s=dict(self.before); s[field]=value
            with self.db: a.write(self.db,s)
            with self.assertRaises(ValueError): a.adopt_spot_balance(self.db,self.client)
            self.assertEqual(a.read(self.db),s)

    def test_locked_or_changing_funds_block_rebase(self):
        account=self.client.account()
        account['balances'][0]['locked']='1'
        with patch.object(self.client,'account',return_value=account):
            with self.assertRaises(ValueError): a.adopt_spot_balance(self.db,self.client)
        first=self.client.account(); second=self.client.account()
        second['balances'][0]['free']='295'
        with patch.object(self.client,'account',side_effect=[first,second]):
            with self.assertRaises(ValueError): a.adopt_spot_balance(self.db,self.client)
        self.assertEqual(a.read(self.db),self.before)

    def test_full_balance_rebuy_remains_limited_by_quote_and_fees(self):
        a.adopt_spot_balance(self.db,self.client)
        self.client.book.update(leave=True,bar=100*900000,now=101*900+30)
        a.tick(self.db,self.client)
        a.tick(self.db,self.client)
        cash=a.read(self.db)['usdt']
        self.client.book.update(leave=False,enter=True,bid='.4',ask='.401',bar=101*900000,now=102*900+30)
        a.tick(self.db,self.client)
        s=a.read(self.db)
        self.assertGreater(a.dec(s['ada']),a.CAP)
        self.assertGreaterEqual(a.dec(s['usdt']),0)
        self.assertLess(a.dec(s['usdt']),a.dec(cash))


if __name__=='__main__': unittest.main()
