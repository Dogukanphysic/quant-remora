import unittest

import long_horizon


class LongHorizonTests(unittest.TestCase):
    def test_resample_uses_complete_aligned_4h_blocks(self):
        rows = []
        for index in range(32):
            price = 100 + index
            rows.append({
                "ts": index * 900_000, "open": price, "high": price + 1,
                "low": price - 1, "close": price + 0.5, "volume": 2,
            })
        bars = long_horizon.resample_4h(rows)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["open"], 100)
        self.assertEqual(bars[0]["close"], 115.5)
        self.assertEqual(bars[0]["volume"], 32)

    def test_position_is_applied_one_bar_after_signal(self):
        closes = [100.0, 110.0, 121.0]
        result = long_horizon._metrics(closes, [0, 1, 1], 0, 3)
        expected = 0.1 - long_horizon.ONE_WAY_STRESSED_COST
        self.assertAlmostEqual(result["return"], expected)
        self.assertEqual(result["turnover_units"], 1)

    def test_resample_rejects_misaligned_input(self):
        rows = [{
            "ts": 1 + index * 900_000, "open": 100, "high": 101,
            "low": 99, "close": 100, "volume": 1,
        } for index in range(16)]
        with self.assertRaisesRegex(ValueError, "hizalı"):
            long_horizon.resample_4h(rows)


if __name__ == "__main__":
    unittest.main()
