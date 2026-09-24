import copy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import btc_live as b


class FakeClient:
    identity = 'btc-test-account'

    def __init__(self):
        self.book = dict(bid='85000', ask='85001', atr='500', now=1_800_000_000,
                         bar=1_799_999_100_000, filters=[])

    def account(self):
        return {'canTrade': True, 'balances': [
            {'asset': 'BTC', 'free': '0', 'locked': '0'},
            {'asset': 'USDT', 'free': '172.50', 'locked': '0'},
        ]}

    def open_orders(self):
        return []


class BtcLiveTests(unittest.TestCase):
    def setUp(self):
        b.configure_interval('15m')
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / 'btc.sqlite3'
        self.patch = patch.object(b, 'DB', self.db_path)
        self.patch.start()
        self.db = b.connect(self.db_path)

    def tearDown(self):
        self.db.close()
        self.patch.stop()
        self.temp.cleanup()

    def test_first_ledger_allocates_all_free_usdt_in_cash(self):
        state = b.initialize(self.db, FakeClient(), FakeClient().book)
        self.assertEqual(state['btc'], '0')
        self.assertEqual(state['usdt'], '172.50')
        self.assertEqual(state['phase'], 'cash')
        self.assertTrue(state['use_all_allocated_funds'])
        self.assertEqual(state['strategy_mode'], b.BB_STRATEGY)

    def test_reclaim_rejects_oversold_without_real_reversal(self):
        rows = []
        for index in range(220):
            close = 100 + (index % 2) * .2
            rows.append({'ts': index * 900000, 'open': close, 'high': close + .1,
                         'low': close - .1, 'close': close, 'volume': 10.0})
        latest = copy.deepcopy(rows[-1])
        baseline = b.bollinger_touch(rows)
        latest.update(low=baseline['entry_limit'] - .01,
                      open=rows[-2]['close'] + .10,
                      close=rows[-2]['close'] - .05)
        rows[-1] = latest
        decision = b.bollinger_touch(rows)
        self.assertTrue(decision['lower_zone_reached'])
        self.assertFalse(decision['recovery_confirmed'])
        self.assertIsNone(decision['entry_regime'])
        self.assertFalse(decision['enter'])

    def test_buy_size_uses_all_tracked_usdt_but_reserves_fee(self):
        state = {'btc': '0', 'usdt': '172.50'}
        market = {'ask': '85001', 'bid': '85000', 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '.00001', 'minQty': '.00001', 'maxQty': '100'},
            {'filterType': 'PRICE_FILTER', 'tickSize': '.01', 'minPrice': '.01', 'maxPrice': '1000000'},
            {'filterType': 'NOTIONAL', 'minNotional': '5', 'maxNotional': '10000000'},
        ]}
        with patch.object(b, 'USE_ALL_ALLOCATED_FUNDS', True):
            order = b.size(state, market, 'BUY', Decimal('.001'))
        self.assertIsNotNone(order)
        self.assertLessEqual(Decimal(order['qty']) * Decimal(order['price']) * Decimal('1.001'),
                             Decimal('172.50'))


if __name__ == '__main__':
    unittest.main()
