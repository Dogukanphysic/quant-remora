import math
import unittest
from ada_market_context import augment


class ContextTests(unittest.TestCase):
    def rows(self):
        return [dict(ts=i*900000,close=100+math.sin(i*.1)+i*.01) for i in range(300)]

    def test_no_future_context_and_feature_preservation(self):
        ada,btc = self.rows(),self.rows()
        x = {i:[.001]*10 for i in range(204,300)}
        original = augment(ada,btc,x)
        for i in range(251,300):
            btc[i]['close'] *= 10
        changed = augment(ada,btc,x)
        self.assertEqual(original[250],changed[250])
        self.assertEqual(original[250][:10],x[250])
        self.assertEqual(len(original[250]),18)
        self.assertAlmostEqual(original[250][-2],1.)

    def test_timestamp_mismatch_rejected(self):
        ada,btc = self.rows(),self.rows()
        btc[240]['ts'] += 900000
        with self.assertRaises(ValueError):
            augment(ada,btc,{240:[0.]*10})


if __name__ == '__main__': unittest.main()
