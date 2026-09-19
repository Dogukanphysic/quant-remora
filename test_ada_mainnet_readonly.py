import unittest
from ada_mainnet_readonly import public,estimate


class ReadOnlyTests(unittest.TestCase):
    def test_order_and_account_endpoints_rejected(self):
        for endpoint in ('order','account','https://api.binance.com/api/v3/order'):
            with self.assertRaises(ValueError):
                public(endpoint)

    def test_declared_balance_not_claimed_verified(self):
        result = estimate(dict(symbol='ADAUSDT',bidPrice='.5',askPrice='.51'),'294')
        self.assertEqual(result['indicative_bid_value_usdt'],'147.0')
        self.assertFalse(result['account_balance_verified'])

    def test_bad_book_rejected(self):
        with self.assertRaises(ValueError):
            estimate(dict(symbol='ADAUSDT',bidPrice='NaN',askPrice='.51'),'294')


if __name__ == '__main__':
    unittest.main()
