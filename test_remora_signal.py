import unittest

import remora_signal
from strategies import BAR_MS


def rows(count=840, start=0):
    result = []
    price = 100.0
    for index in range(count):
        # Persistent trend with cyclic pullbacks creates finite RSI/StochRSI.
        change = 0.11 if index % 9 else -0.35
        price += change
        result.append({
            "ts": start + index * BAR_MS,
            "open": price - change / 2,
            "high": price + 0.18,
            "low": price - 0.18,
            "close": price,
            "volume": 100.0 + index % 7,
        })
    return result


class RemoraSignalTests(unittest.TestCase):
    def test_context_contains_1h_regime_trigger_and_explicit_missing_futures_data(self):
        context = remora_signal.evaluate(rows())
        self.assertEqual(context["profile"], remora_signal.PROFILE)
        self.assertIn(context["trend"], {"bullish", "bearish", "uncertain"})
        self.assertIn(context["regime"], {"trending", "ranging", "low_volatility", "extreme"})
        self.assertTrue(0 <= context["stoch_rsi"] <= 1)
        self.assertEqual(context["components"]["funding"], "UNAVAILABLE")
        self.assertEqual(context["components"]["order_book_depth"], "UNAVAILABLE")
        self.assertFalse(context["full_sdd_data_available"])

    def test_future_candles_do_not_change_current_context(self):
        base = rows()
        first = remora_signal.evaluate(base)
        future = rows(4, start=base[-1]["ts"] + BAR_MS)
        second = remora_signal.evaluate(base + future[:-1])
        self.assertEqual(first["decision_ts"], base[-1]["ts"])
        self.assertGreater(second["decision_ts"], first["decision_ts"])
        again = remora_signal.evaluate(base)
        self.assertEqual(first, again)

    def test_missing_1h_history_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "at least 820"):
            remora_signal.evaluate(rows(819))


if __name__ == "__main__":
    unittest.main()
