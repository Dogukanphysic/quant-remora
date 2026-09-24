import copy
import io
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import ada_live as a


class Fake:
    identity = 'fake-account'
    def __init__(self):
        self.bal={'ADA':Decimal('500'),'USDT':Decimal('10000')}
        self.posts=[]; self.orders={}; self.ambiguous=False; self.partial=False
        self.book=dict(bid='.5',ask='.501',bar=100*14400000,now=101*14400+30,atr='.01',enter=False,leave=False,
            filters=[dict(filterType='LOT_SIZE',stepSize='.1',minQty='.1',maxQty='100000'),
                     dict(filterType='PRICE_FILTER',tickSize='.0001',minPrice='.0001',maxPrice='1000'),
                     dict(filterType='NOTIONAL',minNotional='5',maxNotional='9000000')])
    def market(self): return copy.deepcopy(self.book)
    def account(self): return dict(canTrade=True,balances=[dict(asset=k,free=str(v)) for k,v in self.bal.items()])
    def open_orders(self): return []
    def commission(self):
        row=dict(taker='0',maker='0',buyer='0',seller='0')
        return dict(symbol=a.SYMBOL,discount={},standardCommission=dict(row,taker='.001'),
                    specialCommission=row,taxCommission=row)
    def submit(self,intent):
        self.posts.append(dict(intent))
        qty=a.dec(intent['qty'])
        if self.partial: qty=a.grid(qty/2,Decimal('.1'))
        price=a.dec(intent['price']); quote=qty*price
        fee=quote*Decimal('.001')
        side=intent['side']
        self.bal['ADA'] += qty if side=='BUY' else -qty
        self.bal['USDT'] += -quote-fee if side=='BUY' else quote-fee
        self.orders[intent['client_id']]=dict(symbol=a.SYMBOL,clientOrderId=intent['client_id'],side=side,
            executedQty=str(qty),cummulativeQuoteQty=str(quote),status='EXPIRED' if self.partial else 'FILLED',
            fills=[dict(qty=str(qty),price=str(price),commission=str(fee),commissionAsset='USDT')])
        if self.ambiguous: raise RuntimeError('Lost POST response')
        return self.orders[intent['client_id']]
    def lookup(self,intent): return copy.deepcopy(self.orders[intent['client_id']])


class LiveTests(unittest.TestCase):
    def test_order_check_does_not_change_pending_or_submit(self):
        s=a.initialize(self.db,self.client,self.client.market())
        s.update(pending='uncertain',halted='RuntimeError')
        with self.db: a.write(self.db,s)
        self.client.test_order=Mock(return_value={})
        report=a.check_trade_permission(self.client,Path(self.temp.name)/'ledger.db')
        self.assertTrue(report['ok'])
        self.assertEqual(a.read(self.db),s)
        self.assertEqual(self.client.posts,[])
        self.assertEqual(self.client.test_order.call_count,1)
        self.client.test_order.side_effect=RuntimeError('API HTTP 401; Binance code -2015')
        report=a.check_trade_permission(self.client,Path(self.temp.name)/'ledger.db')
        self.assertFalse(report['ok'])
        self.assertIn('-2015',report['reason'])
        self.assertEqual(a.read(self.db),s)
    def test_order_check_requires_bound_key(self):
        a.initialize(self.db,self.client,self.client.market())
        self.client.identity='other'
        self.client.test_order=Mock()
        report=a.check_trade_permission(self.client,Path(self.temp.name)/'ledger.db')
        self.assertFalse(report['ok'])
        self.client.test_order.assert_not_called()
    def test_test_order_routes_only_to_test_endpoint(self):
        client=object.__new__(a.Client)
        client.request=Mock(return_value={})
        client.test_order(dict(side='SELL',qty='1',price='.5'))
        self.assertEqual(client.request.call_args.args[:2],('POST','order/test'))
    def test_http_error_exposes_code_not_secrets(self):
        client = object.__new__(a.Client)
        client.opener = Mock()
        error = a.HTTPError('https://example.invalid/?signature=PRIVATE',401,'PRIVATE',{},
                            io.BytesIO(b'{"code":-2015,"msg":"PRIVATE"}'))
        client.opener.open.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            client.request('GET','time')
        self.assertIn('Binance code -2015',str(caught.exception))
        self.assertNotIn('PRIVATE',str(caught.exception))
        self.assertEqual(client.opener.open.call_count,1)
    def test_http_non_json_error_is_sanitized(self):
        client = object.__new__(a.Client)
        client.opener = Mock()
        client.opener.open.side_effect = a.HTTPError('https://example.invalid/',401,'PRIVATE',{},
                                                    io.BytesIO(b'<html>PRIVATE</html>'))
        with self.assertRaises(RuntimeError) as caught:
            client.request('GET','time')
        self.assertIn('API HTTP 401',str(caught.exception))
        self.assertNotIn('PRIVATE',str(caught.exception))
    def test_new_key_preserves_allocation_and_audits_old_state(self):
        a.initialize(self.db,self.client,self.client.market())
        old=a.read(self.db)
        old['halted']='ValueError'
        with self.db: a.write(self.db,old)
        self.client.identity='new-key'
        with patch.object(a,'ROOT',Path(self.temp.name)):
            (Path(self.temp.name)/'reports').mkdir()
            a.prepare_new_key(self.db,self.client)
        current=a.read(self.db)
        self.assertEqual(current['ada'],'294')
        self.assertEqual(current['identity'],'new-key')
        self.assertIsNone(current['halted'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM key_changes').fetchone()[0],1)
        self.assertEqual(self.client.posts,[])

    def test_new_key_rejects_used_ledger(self):
        self.client.book['leave']=True
        a.tick(self.db,self.client)
        with self.assertRaises(ValueError): a.prepare_new_key(self.db,self.client)

    def test_diagnosis_reports_reason_without_orders_or_ledger(self):
        with patch.object(self.client,'commission',return_value={}) :
            result=a.diagnose(self.client)
        self.assertFalse(result['checks']['commission']['ok'])
        self.assertEqual(result['orders_submitted'],0)
        self.assertEqual(self.client.posts,[])
        self.assertIsNone(a.read(self.db))
    def setUp(self):
        a.configure_interval('4h')
        self.temp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(a,'DB',Path(self.temp.name)/'ledger.db')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.db=a.connect(Path(self.temp.name)/'ledger.db')
        self.client=Fake()
    def tearDown(self):
        a.configure_interval('4h')
        self.db.close(); self.temp.cleanup()
    def test_interval_migration_preserves_position_and_history(self):
        old = a.tick(self.db,self.client)
        a.configure_interval('15m')
        a.migrate_interval(self.db,self.client)
        new = a.read(self.db)
        for key in ('ada','usdt','stop','target','opened','initial_mark_usdt','filled_orders','identity'):
            self.assertEqual(old[key],new[key])
        self.assertEqual(new['interval'],'15m')
        self.assertIsNone(new['last_bar'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM interval_changes').fetchone()[0],1)
        a.migrate_interval(self.db,self.client)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM interval_changes').fetchone()[0],1)
        self.assertEqual(self.client.posts,[])
    def test_interval_migration_blocks_pending_halt_and_wrong_key(self):
        old = a.tick(self.db,self.client)
        a.configure_interval('15m')
        for key,value in [('pending','uncertain'),('halted','error'),('identity','other')]:
            bad = dict(old); bad[key]=value
            with self.db: a.write(self.db,bad)
            with self.assertRaises(ValueError): a.migrate_interval(self.db,self.client)
            self.assertEqual(a.read(self.db),bad)
        self.assertEqual(self.client.posts,[])
    def test_15m_entry_uses_closed_15m_window(self):
        a.configure_interval('15m')
        self.client.book.update(bar=100*900000,now=101*900+30,leave=True)
        a.tick(self.db,self.client)
        a.tick(self.db,self.client)
        self.client.book.update(bar=101*900000,now=102*900+30,leave=False,enter=True)
        a.tick(self.db,self.client)
        self.assertEqual(self.client.posts[-1]['side'],'BUY')
        self.assertEqual(a.read(self.db)['contract'],a.CONTRACT)
    def test_bollinger_touch_uses_prior_band_and_closed_candle_extremes(self):
        rows=[dict(close=1.0,low=1.0,high=1.0) for _ in range(20)]
        rows.append(dict(close=1.01,low=.99,high=1.01))
        result=a.bollinger_touch(rows)
        self.assertEqual((result['lower'],result['upper']),(1.0,1.0))
        self.assertTrue(result['lower_zone_reached'])
        self.assertTrue(result['lower_touched'])
        self.assertTrue(result['upper_touched'])
    def test_bollinger_near_lower_zone_is_narrow_and_not_a_touch(self):
        rows=[dict(close=float(i % 2),low=0.0,high=1.0) for i in range(20)]
        band=a.bollinger_touch(rows+[dict(close=.5,low=-.4,high=.5)])
        self.assertAlmostEqual(band['lower'],-.5)
        self.assertAlmostEqual(band['entry_limit'],-.3)
        self.assertTrue(band['lower_zone_reached'])
        self.assertFalse(band['lower_touched'])
        outside=a.bollinger_touch(rows+[dict(close=.5,low=-.29,high=.5)])
        self.assertFalse(outside['lower_zone_reached'])
    def test_bollinger_switch_audited_and_next_candles_trade(self):
        a.configure_interval('15m')
        self.client.book.update(bar=100*900000,now=101*900+30,
                                bollinger_touch=dict(lower=.49,upper=.52,entry_limit=.493,
                                                     lower_zone_reached=True,lower_touched=False,upper_touched=False))
        s=a.initialize(self.db,self.client,self.client.market())
        s.update(ada='0',usdt='100',phase='cash')
        with self.db: a.write(self.db,s)
        with patch.object(a,'BOLLINGER_TOUCH',True):
            a.tick(self.db,self.client)
            self.assertEqual(self.client.posts,[])
            self.assertEqual(a.read(self.db)['strategy_mode'],a.BB_STRATEGY)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM strategy_changes').fetchone()[0],1)
            self.client.book.update(bar=101*900000,now=102*900+30)
            a.tick(self.db,self.client)
            self.assertEqual(self.client.posts[-1]['side'],'BUY')
            self.client.book.update(bar=102*900000,now=103*900+30,
                                    bollinger_touch=dict(lower=.49,upper=.52,entry_limit=.493,
                                                         lower_zone_reached=False,lower_touched=False,upper_touched=True))
            a.tick(self.db,self.client)
            self.assertEqual(self.client.posts[-1]['side'],'SELL')
        with self.assertRaisesRegex(ValueError,'--bollinger-touch'):
            a.tick(self.db,self.client)
    def test_existing_bollinger_touch_v1_migrates_without_first_candle_order(self):
        a.configure_interval('15m')
        self.client.book.update(bar=100*900000,now=101*900+30,
                                bollinger_touch=dict(lower=.49,upper=.52,entry_limit=.493,
                                                     lower_zone_reached=True,lower_touched=False,upper_touched=False))
        s=a.initialize(self.db,self.client,self.client.market())
        s.update(ada='0',usdt='100',phase='cash',strategy_mode='bollinger_touch_15m_v1')
        with self.db: a.write(self.db,s)
        with patch.object(a,'BOLLINGER_TOUCH',True):
            a.tick(self.db,self.client)
        migrated=a.read(self.db)
        self.assertEqual(migrated['strategy_mode'],a.BB_STRATEGY)
        self.assertEqual(migrated['last_bar'],100*900000)
        self.assertEqual(self.client.posts,[])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM strategy_changes').fetchone()[0],1)
    def test_15m_learner_does_not_change_shared_learner(self):
        import trend4h_learning as shared
        before = (shared.STEP,shared.FRAME,shared.VERSION)
        a.configure_interval('15m')
        learner = a.ada_learner()
        self.assertEqual(learner.STEP,900000)
        self.assertEqual(learner.FRAME,'15m')
        self.assertEqual((shared.STEP,shared.FRAME,shared.VERSION),before)
    def test_negative_model_prediction_can_exit(self):
        a.configure_interval('15m')
        self.client.book.update(bar=100*900000,now=101*900+30)
        with patch.object(a,'MODEL_DECISIONS',True), patch('ada_model_decisions.decision',return_value={
                'owner':'learned_model','target_long':False}):
            s = a.tick(self.db,self.client)
        self.assertEqual(self.client.posts[-1]['side'],'SELL')
        self.assertEqual(s['latest_candle_decision']['owner'],'learned_model')
    def test_positive_model_prediction_cannot_override_stop(self):
        a.configure_interval('15m')
        self.client.book.update(bar=100*900000,now=101*900+30)
        a.tick(self.db,self.client)
        self.client.book.update(bid='.45',ask='.451',bar=101*900000,now=102*900+30)
        with patch.object(a,'MODEL_DECISIONS',True), patch('ada_model_decisions.decision',return_value={
                'owner':'learned_model','target_long':True}):
            a.tick(self.db,self.client)
        self.assertEqual(self.client.posts[-1]['side'],'SELL')
    def test_live_gate_has_no_network(self):
        with patch.dict(a.os.environ,{},clear=True), patch.object(a,'build_opener') as opener:
            with self.assertRaises(ValueError): a.Client(live=True)
            with self.assertRaises(ValueError): a.Client(live=False)
            opener.assert_not_called()
    def test_all_allocated_funds_can_buy_more_than_initial_ada(self):
        s=dict(ada='0.3',usdt='200')
        with patch.object(a,'USE_ALL_ALLOCATED_FUNDS',True):
            order=a.size(s,self.client.market(),'BUY',Decimal('.001'))
        self.assertGreater(a.dec(order['qty']),a.CAP)
        self.assertLessEqual(a.dec(order['qty'])*a.dec(order['price'])*Decimal('1.001'),Decimal('200'))
        self.assertEqual(self.client.posts,[])
    def test_allocation_diagnostic_reports_shortfall_without_mutation(self):
        s=a.initialize(self.db,self.client,self.client.market())
        self.client.bal['ADA']=Decimal('290')
        report=a.diagnose(self.client)
        self.assertEqual(report['allocation']['assets']['ADA']['shortfall'],'4')
        self.assertFalse(report['checks']['allocated_balance']['ok'])
        self.assertEqual(a.read(self.db),s)
        self.assertEqual(self.client.posts,[])
    def test_only_294_allocated_and_no_quote_import(self):
        s=a.tick(self.db,self.client)
        self.assertEqual(s['ada'],'294'); self.assertEqual(s['usdt'],'0')
        self.assertEqual(len(self.client.posts),0)
    def test_sell_then_buy_uses_only_proceeds(self):
        self.client.book['leave']=True
        sold=a.tick(self.db,self.client)
        proceeds=a.dec(sold['usdt'])
        self.assertLessEqual(a.dec(self.client.posts[0]['qty']),Decimal('294'))
        a.tick(self.db,self.client)  # residual below minimum becomes cash
        self.client.book.update(leave=False,enter=True,bar=101*14400000,now=102*14400+30)
        s=a.tick(self.db,self.client)
        order=self.client.posts[-1]
        self.assertEqual(order['side'],'BUY')
        self.assertLessEqual(a.dec(order['qty'])*a.dec(order['price'])*Decimal('1.001'),proceeds)
        self.assertGreaterEqual(a.dec(s['usdt']),0)
        self.assertLessEqual(a.dec(s['ada']),Decimal('294'))
    def test_ambiguous_post_reconciles_without_resubmitting(self):
        self.client.book['leave']=True; self.client.ambiguous=True
        with self.assertRaises(RuntimeError): a.tick(self.db,self.client)
        self.assertIsNotNone(a.read(self.db)['pending'])
        a.tick(self.db,self.client)
        self.assertEqual(len(self.client.posts),1)
        self.assertIsNone(a.read(self.db)['pending'])
    def test_duplicate_settlement_does_not_double_count(self):
        self.client.book['leave']=True
        s=a.tick(self.db,self.client)
        self.assertEqual(a.settle(self.db,self.client),s)
    def test_partial_expired_fill_accounted(self):
        self.client.book['leave']=True; self.client.partial=True
        s=a.tick(self.db,self.client)
        self.assertTrue(s['exit_requested']); self.assertIsNone(s['pending'])
        self.assertTrue(Decimal(0)<a.dec(s['ada'])<Decimal(294))
    def test_external_withdrawal_blocks(self):
        a.tick(self.db,self.client); self.client.bal['ADA']=Decimal('200')
        with self.assertRaises(ValueError): a.tick(self.db,self.client)
        self.assertEqual(len(self.client.posts),0)
    def test_wrong_identity_refused(self):
        a.tick(self.db,self.client); self.client.identity='other'
        with self.assertRaises(ValueError): a.tick(self.db,self.client)
        with self.assertRaises(ValueError): a.settle(self.db,self.client)
    def test_bnb_fee_mode_refused(self):
        data=self.client.commission(); data['discount']={'enabledForAccount':True,'enabledForSymbol':True}
        with self.assertRaises(ValueError): a.fee_rates(data)
    def test_stop_uses_observed_price(self):
        a.tick(self.db,self.client)
        self.client.book.update(bid='.45',ask='.451',now=self.client.book['now']+60)
        a.tick(self.db,self.client)
        self.assertEqual(self.client.posts[0]['side'],'SELL')
        self.assertLess(a.dec(self.client.posts[0]['price']),Decimal('.45'))
    def test_fill_mismatch_keeps_pending(self):
        self.client.book['leave']=True; self.client.ambiguous=True
        with self.assertRaises(RuntimeError): a.tick(self.db,self.client)
        self.client.orders[self.client.posts[0]['client_id']]['fills']=[]
        with self.assertRaises(ValueError): a.tick(self.db,self.client)
        self.assertIsNotNone(a.read(self.db)['pending'])


if __name__=='__main__': unittest.main()
