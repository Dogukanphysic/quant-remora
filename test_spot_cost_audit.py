import unittest
from spot_cost_audit import buy_hold, depth_fill, rounded_quantity


class CostAuditTests(unittest.TestCase):
    def test_book_depth_weighted_price_and_insufficient_depth(self):
        self.assertAlmostEqual(depth_fill([['100', '1'], ['200', '1']], 200), 200/1.5)
        self.assertIsNone(depth_fill([['100', '1']], 101))

    def test_quantity_rounds_down_and_disabled_step(self):
        rules = [dict(filterType='LOT_SIZE', stepSize='.01', minQty='.01', maxQty='100'),
                 dict(filterType='MARKET_LOT_SIZE', stepSize='0', minQty='0', maxQty='100')]
        result = rounded_quantity(15, 101, rules)
        self.assertEqual(result['quantity'], '0.14')
        self.assertTrue(result['quantity_valid'])
        self.assertFalse(result['exchange_acceptance_verified'])
        self.assertFalse(rounded_quantity(.1, 101, rules)['quantity_valid'])

    def test_buy_hold_same_price_loses_round_trip_cost(self):
        rows = [dict(open=100, close=100, low=100)]*3
        self.assertAlmostEqual(buy_hold(rows, 0, 3, 0)['pnl_usd'], 0)
        self.assertAlmostEqual(buy_hold(rows, 0, 3, .01)['pnl_usd'], 200*.99/1.01-200)

    def test_buy_hold_uses_start_open_and_end_close(self):
        rows = [dict(open=1, close=1, low=1), dict(open=100, close=150, low=100),
                dict(open=150, close=200, low=150)]
        self.assertAlmostEqual(buy_hold(rows, 1, 3, 0)['pnl_usd'], 200)


if __name__ == '__main__':
    unittest.main()
