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
    def test_probe_crossing_is_broader_than_capital_crossing(self):
        self.assertIsNone(remora_signal._cross_side(0.25, 0.31, 0.20, 0.80))
        self.assertEqual(
            remora_signal._cross_side(0.25, 0.31, 0.30, 0.70), "long")
        self.assertIsNone(remora_signal._cross_side(0.75, 0.69, 0.20, 0.80))
        self.assertEqual(
            remora_signal._cross_side(0.75, 0.69, 0.30, 0.70), "short")

    def test_every_closed_bar_gets_one_versioned_probe(self):
        self.assertEqual(
            remora_signal._probe_candidate(0.40, 0.48, 100.0, 99.0),
            ("long", "closed_15m_stoch_direction_h8_v2"),
        )
        self.assertEqual(
            remora_signal._probe_candidate(0.60, 0.52, 100.0, 101.0),
            ("short", "closed_15m_stoch_direction_h8_v2"),
        )
        self.assertEqual(
            remora_signal._probe_candidate(0.50, 0.50, 100.0, 99.0),
            ("short", "closed_15m_price_direction_h8_v2"),
        )
        self.assertEqual(
            remora_signal._probe_candidate(0.50, 0.50, 100.0, 100.0),
            ("long", "closed_15m_price_direction_h8_v2"),
        )

    def test_crossings_have_priority_over_fallback_direction(self):
        self.assertEqual(
            remora_signal._probe_candidate(0.25, 0.35, 100.0, 99.0),
            ("long", "stoch_rsi_cross_30_70_h8_v2"),
        )
        self.assertEqual(
            remora_signal._probe_candidate(0.10, 0.35, 100.0, 99.0),
            ("long", "capital_stoch_rsi_cross_20_80_h8_v2"),
        )
        self.assertEqual(
            remora_signal._probe_candidate(0.90, 0.65, 100.0, 101.0),
            ("short", "capital_stoch_rsi_cross_20_80_h8_v2"),
        )

    def test_context_contains_1h_regime_trigger_and_explicit_missing_futures_data(self):
        context = remora_signal.evaluate(rows())
        self.assertEqual(context["profile"], remora_signal.PROFILE)
        self.assertIn(context["trend"], {"bullish", "bearish", "uncertain"})
        self.assertIn(context["regime"], {"trending", "ranging", "low_volatility", "extreme"})
        self.assertTrue(0 <= context["stoch_rsi"] <= 1)
        self.assertEqual(context["components"]["funding"], "UNAVAILABLE")
        self.assertEqual(context["components"]["order_book_depth"], "UNAVAILABLE")
        self.assertFalse(context["full_sdd_data_available"])
        self.assertIn(context["probe_side"], {"long", "short"})
        self.assertTrue(context["probe_profile"].endswith("_v2"))

    def test_each_consecutive_closed_bar_has_exactly_one_probe_direction(self):
        history = rows(825)
        for length in range(820, 826):
            context = remora_signal.evaluate(history[:length])
            self.assertIn(context["probe_side"], {"long", "short"})
            self.assertTrue(context["probe_profile"].endswith("_v2"))

    def test_future_candles_do_not_change_current_context(self):
        base = rows()
        future = rows(4, start=base[-1]["ts"] + BAR_MS)
        mutated_future = [
            {**row, "open": 10_000.0, "high": 20_000.0, "low": 1.0,
             "close": 15_000.0, "volume": 1_000_000.0}
            for row in future
        ]
        cutoff = len(base)
        first = remora_signal.evaluate((base + future)[:cutoff])
        again = remora_signal.evaluate((base + mutated_future)[:cutoff])
        second = remora_signal.evaluate(base + future[:-1])
        self.assertEqual(first["decision_ts"], base[-1]["ts"])
        self.assertGreater(second["decision_ts"], first["decision_ts"])
        self.assertEqual(first, again)

    def test_rows_older_than_fixed_window_do_not_change_context(self):
        history = rows(900)
        changed = [dict(row) for row in history]
        for row in changed[:-remora_signal.MIN_BARS]:
            row.update(open=500.0, high=501.0, low=499.0, close=500.0, volume=1.0)
        self.assertEqual(
            remora_signal.evaluate(history),
            remora_signal.evaluate(changed),
        )

    def test_missing_1h_history_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "at least 820"):
            remora_signal.evaluate(rows(819))


if __name__ == "__main__":
    unittest.main()
