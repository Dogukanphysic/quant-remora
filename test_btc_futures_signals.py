import copy
import itertools
import unittest

from btc_futures_signals import apply_profile
from btc_live import bollinger_touch


def blocked_band(**changes):
    band = dict(
        enter=False, entry_regime=None, lower_zone_reached=True,
        recovery_confirmed=True, histogram_rising=True, trend_confirmed=False,
        lower=102.0, upper=105.0, rsi=30.0,
    )
    band.update(changes)
    return band


def declining_recovery_rows():
    rows = []
    for index in range(220):
        close = 120 - index * .08 + (index % 2) * .2
        rows.append(dict(ts=index * 900000, open=close, high=close + .1,
                         low=close - .1, close=close, volume=10.0))
    baseline = bollinger_touch(rows)
    previous_close = rows[-2]["close"]
    rows[-1].update(open=previous_close - .05, close=previous_close + .60,
                    high=previous_close + .70,
                    low=baseline["entry_limit"] - .01)
    return rows


class SignalProfileTests(unittest.TestCase):
    def test_trend_default_preserves_existing_decisions_exactly(self):
        cases = [blocked_band(), bollinger_touch(declining_recovery_rows())]
        for regime in ("trend_reclaim", "trend_squeeze_breakout"):
            cases.append(blocked_band(enter=True, entry_regime=regime,
                                      trend_confirmed=True))
        for band in cases:
            with self.subTest(regime=band["entry_regime"]):
                expected = dict(band, signal_profile="trend")
                self.assertEqual(apply_profile(band), expected)
                self.assertEqual(apply_profile(band, "trend"), expected)

    def test_responsive_keeps_valid_strict_entries_and_their_regimes(self):
        for regime in ("trend_reclaim", "trend_squeeze_breakout"):
            # A strict breakout need not meet the extra reclaim condition.
            band = blocked_band(enter=True, entry_regime=regime,
                                lower_zone_reached=False, trend_confirmed=True)
            with self.subTest(regime=regime):
                self.assertEqual(apply_profile(band, "responsive"),
                                 dict(band, signal_profile="responsive"))

    def test_responsive_reclaim_requires_all_three_flags(self):
        for flags in itertools.product((False, True), repeat=3):
            with self.subTest(flags=flags):
                band = blocked_band(**dict(zip(
                    ("lower_zone_reached", "recovery_confirmed", "histogram_rising"),
                    flags)))
                result = apply_profile(band, "responsive")
                self.assertIs(result["enter"], all(flags))
                self.assertEqual(result["entry_regime"],
                                 "responsive_reclaim" if all(flags) else None)

    def test_low_rsi_and_failed_recovery_do_not_enable_entry(self):
        band = blocked_band(rsi=0.0, recovery_confirmed=False)
        self.assertFalse(apply_profile(band, "responsive")["enter"])

    def test_missing_reclaim_diagnostic_cannot_create_entry(self):
        for field in ("lower_zone_reached", "recovery_confirmed", "histogram_rising"):
            band = blocked_band()
            del band[field]
            with self.subTest(field=field):
                result = apply_profile(band, "responsive")
                self.assertFalse(result["enter"])
                self.assertIsNone(result["entry_regime"])
        self.assertFalse(apply_profile(dict(enter=False, entry_regime=None),
                                       "responsive")["enter"])

    def test_missing_base_decision_is_rejected(self):
        for band in ({}, {"entry_regime": None}, {"enter": False}):
            with self.subTest(band=band), self.assertRaises(ValueError):
                apply_profile(band, "responsive")

    def test_malformed_flags_are_rejected_even_for_existing_entry(self):
        fields = ("enter", "lower_zone_reached", "lower_touched", "upper_touched",
                  "volume_confirmed", "histogram_rising", "recovery_confirmed",
                  "trend_confirmed", "squeeze", "breakout", "reclaim_exit",
                  "breakout_exit")
        for profile, field, value in itertools.product(
                ("trend", "responsive"), fields, (None, 0, 1, "false", "true", [], {})):
            band = blocked_band(enter=True, entry_regime="trend_reclaim")
            band[field] = value
            with self.subTest(profile=profile, field=field, value=value):
                with self.assertRaises(ValueError):
                    apply_profile(band, profile)

    def test_inconsistent_or_malformed_regime_is_rejected(self):
        for enter, regime in ((True, None), (False, "trend_reclaim"),
                              (True, True), (True, 1), (True, ""), (True, " ")):
            with self.subTest(enter=enter, regime=regime), self.assertRaises(ValueError):
                apply_profile(blocked_band(enter=enter, entry_regime=regime), "responsive")

    def test_bad_profile_and_non_dict_diagnostics_are_rejected(self):
        for profile in ("", "aggressive", "Responsive", None, [], {}):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                apply_profile(blocked_band(), profile)
        for band in (None, [], "band"):
            with self.subTest(band=band), self.assertRaises(ValueError):
                apply_profile(band, "trend")

    def test_input_is_unchanged_and_all_other_diagnostics_are_preserved(self):
        band = blocked_band(extra_diagnostic={"nested": [1, 2]}, reclaim_exit=True,
                            breakout_exit=False)
        before = copy.deepcopy(band)
        result = apply_profile(band, "responsive")
        self.assertIsNot(result, band)
        self.assertEqual(band, before)
        self.assertEqual(result, dict(before, signal_profile="responsive", enter=True,
                                      entry_regime="responsive_reclaim"))

    def test_real_trend_blocked_recovery_becomes_responsive_candidate(self):
        rows = declining_recovery_rows()
        original_rows = copy.deepcopy(rows)
        band = bollinger_touch(rows)
        self.assertTrue(band["lower_zone_reached"])
        self.assertTrue(band["recovery_confirmed"])
        self.assertTrue(band["histogram_rising"])
        self.assertFalse(band["trend_confirmed"])
        self.assertFalse(band["enter"])

        self.assertFalse(apply_profile(band, "trend")["enter"])
        responsive = apply_profile(band, "responsive")
        self.assertTrue(responsive["enter"])
        self.assertEqual(responsive["entry_regime"], "responsive_reclaim")
        self.assertFalse(responsive["trend_confirmed"])
        self.assertEqual(rows, original_rows)
        self.assertEqual(bollinger_touch(rows), band)


if __name__ == "__main__":
    unittest.main()
