import unittest

import low_frequency


class LowFrequencyTests(unittest.TestCase):
    def test_resample_daily_keeps_only_complete_days(self):
        rows = []
        for index in range(96 + 20):
            price = 100 + index
            rows.append({"ts": index * 900_000, "open": price, "high": price + 1,
                         "low": price - 1, "close": price + 0.5, "volume": 2})
        bars = low_frequency.resample_daily(rows)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["open"], 100)
        self.assertEqual(bars[0]["close"], 195.5)

    def test_metrics_apply_signal_on_following_day(self):
        result = low_frequency.metrics([100.0, 110.0, 121.0], [0, 1, 1], 0, 3)
        self.assertAlmostEqual(
            result["return"], 0.1 - low_frequency.ONE_WAY_STRESSED_COST)
        self.assertEqual(result["turnover_units"], 1)

    def test_search_space_is_fixed(self):
        closes = [100 + index * 0.1 for index in range(500)]
        self.assertEqual(len(low_frequency.configurations(closes)), 281)


if __name__ == "__main__":
    unittest.main()
