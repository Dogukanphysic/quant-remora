import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import ada_live as a
from test_ada_live import Fake


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=a.connect(Path(self.temp.name)/'ledger.db')
        a.configure_interval('15m')
        self.client=Fake()
        self.client.book.update(bar=100*900000,now=101*900+30)
        s=a.initialize(self.db,self.client,self.client.market())
        s.update(ada='0.3',usdt='72.5',phase='cash',filled_orders=1,use_all_allocated_funds=True)
        with self.db: a.write(self.db,s)
        self.client.bal={'ADA':Decimal('.3'),'USDT':Decimal('172.5'),'TRY':Decimal('133.14')}
        self.flags=patch.multiple(a,USE_ALL_ALLOCATED_FUNDS=True,AUTO_ALLOCATE_SPOT=True)
        self.flags.start()

    def tearDown(self):
        self.flags.stop(); a.configure_interval('4h')
        self.db.close(); self.temp.cleanup()

    def test_deposit_recorded_once_and_not_profit(self):
        before=a.read(self.db)
        expected=Decimal('72.5')+Decimal('.3')*Decimal('.5')-a.dec(before['initial_mark_usdt'])
        s=a.tick(self.db,self.client)
        self.assertEqual(s['usdt'],'172.5')
        self.assertEqual(a.dec(s['external_capital_inflows_usdt']),Decimal('100'))
        self.assertEqual(Decimal(s['marked_pnl_from_activation_usdt']),expected)
        a.tick(self.db,self.client)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM capital_flows').fetchone()[0],1)
        self.assertEqual(self.client.posts,[])

    def test_disabled_option_does_not_allocate(self):
        with patch.object(a,'AUTO_ALLOCATE_SPOT',False): s=a.tick(self.db,self.client)
        self.assertEqual(s['usdt'],'72.5')
        self.assertNotIn('external_capital_inflows_usdt',s)

    def test_ada_deposit_is_valued_and_managed_without_resetting_history(self):
        self.client.bal.update(ADA=Decimal('300.3'),USDT=Decimal('72.5'))
        s=a.tick(self.db,self.client)
        self.assertEqual(s['phase'],'long')
        self.assertEqual(s['filled_orders'],1)
        self.assertEqual(a.dec(s['external_capital_inflows_usdt']),Decimal('150'))
        self.assertEqual(self.client.posts,[])

    def test_shortfall_or_locked_funds_not_imported(self):
        before=a.read(self.db)
        self.client.bal['ADA']=Decimal('0')
        with self.assertRaises(ValueError): a.tick(self.db,self.client)
        self.assertEqual(a.read(self.db),before)
        self.client.bal['ADA']=Decimal('.3')
        account=self.client.account(); account['balances'][1]['locked']='1'
        with patch.object(self.client,'account',return_value=account):
            with self.assertRaises(ValueError): a.tick(self.db,self.client)
        self.assertEqual(a.read(self.db),before)

    def test_unapplied_order_blocks_allocation(self):
        with self.db: self.db.execute('INSERT INTO orders(client_id,request) VALUES (?,?)',('unknown','{}'))
        before=a.read(self.db)
        with self.assertRaises(ValueError): a.tick(self.db,self.client)
        self.assertEqual(a.read(self.db),before)

    def test_pending_order_is_reconciled_before_allocation(self):
        s=a.read(self.db); s['pending']='pending'
        with self.db: a.write(self.db,s)
        with patch.object(a,'settle',return_value=s) as settle, patch.object(self.client,'account') as account:
            a.tick(self.db,self.client)
            settle.assert_called_once(); account.assert_not_called()

    def test_account_change_between_reads_blocks_allocation(self):
        before=a.read(self.db); first=self.client.account(); second=self.client.account()
        second['balances'][1]['free']='173.5'
        with patch.object(self.client,'account',side_effect=[first,first,second]):
            with self.assertRaises(ValueError): a.tick(self.db,self.client)
        self.assertEqual(a.read(self.db),before)


if __name__=='__main__': unittest.main()
