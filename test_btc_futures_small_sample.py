import copy
import json
import unittest

from btc_futures_small_sample import summarize, ASSUMPTION


def sample(index, delta="-1", *, legacy=False, regime="reclaim", profile="responsive", leverage=4):
    amount = float(delta)
    return {"id": "sample-" + str(index), "label": 1 if amount < 0 else 0 if amount > 0 else None,
            "wallet_delta_proxy_usdt": delta, "opened_at": index * 100, "closed_at": index * 100 + 10,
            "closed_event_id": index * 3, "quality": "legacy_wallet_proxy" if legacy else "entry_snapshot_wallet_proxy",
            "entry_context": None if legacy else {"captured_at": index * 100 - 1,
                "decision": {"entry_regime": regime}, "signal_profile": profile, "leverage": leverage}}


class SmallSampleTests(unittest.TestCase):
    def test_empty_waits_for_one_closed_sample(self):
        result = summarize([])
        self.assertEqual(result["status"], "collecting_closed_samples")
        self.assertEqual(result["cadence"]["minimum_closed_samples"], 1)
        self.assertIsNone(result["negative_label_rate"]["wilson_95_descriptive_interval"])
        self.assertTrue(result["negative_label_rate"]["prior_only"])

    def test_one_loss_is_initial_observation_not_forecast(self):
        result = summarize([sample(1)])
        self.assertEqual(result["status"], "initial_observation")
        self.assertAlmostEqual(result["negative_label_rate"]["shrunk_mean"], 3 / 5)
        self.assertFalse(result["decision_authority"])
        self.assertFalse(result["execution_verified"])
        self.assertTrue(all(group["review_flag"] is None for group in result["groups"]))

    def test_five_losses_have_wide_uncertainty_and_shrinkage(self):
        result = summarize([sample(i) for i in range(1, 6)])
        self.assertEqual(result["negative_count"], 5)
        self.assertEqual(result["chronological_trailing_negative_streak"], 5)
        self.assertEqual(result["total_negative_wallet_proxy_usdt"], "-5")
        rate = result["negative_label_rate"]
        self.assertAlmostEqual(rate["shrunk_mean"], 7 / 9)
        self.assertAlmostEqual(rate["wilson_95_descriptive_interval"]["lower"], 0.5655175352)
        self.assertAlmostEqual(rate["wilson_95_descriptive_interval"]["upper"], 1)
        self.assertEqual(rate["interval_assumption"], ASSUMPTION)

    def test_neutral_is_not_a_loss_and_breaks_trailing_streak(self):
        result = summarize([sample(4, "-3"), sample(1, "-1"), sample(3, "0"), sample(2, "2")])
        self.assertEqual((result["negative_count"], result["positive_count"], result["neutral_count"]), (2, 1, 1))
        self.assertEqual(result["negative_label_rate"]["non_neutral_label_count"], 3)
        self.assertEqual(result["chronological_trailing_negative_streak"], 1)
        self.assertEqual(result["closed_sample_ids"], ["sample-1", "sample-2", "sample-3", "sample-4"])
        self.assertEqual(summarize([sample(1), sample(2, "0")])["chronological_trailing_negative_streak"], 0)

    def test_repeated_condition_retains_wins_and_is_only_a_review_flag(self):
        result = summarize([sample(i) for i in range(1, 4)] + [sample(4, "5")])
        group = next(group for group in result["groups"] if group["condition_field"] == "entry_regime")
        self.assertEqual((group["negative_count"], group["positive_count"]), (3, 1))
        self.assertEqual(group["review_flag"], "repeated_negative_proxy_for_review")
        self.assertEqual(group["net_wallet_proxy_usdt"], "2")
        self.assertIn("not_cause", group["interpretation"])
        self.assertFalse(result["automatic_activation_enabled"])

    def test_duplicate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_closed_sample_id"):
            summarize([sample(1), sample(1)])

    def test_legacy_has_no_invented_conditions(self):
        result = summarize([sample(1, legacy=True), sample(2, "3", legacy=True)])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["legacy_closed_sample_count"], 2)
        self.assertEqual(result["missing_entry_context_count"], 2)
        self.assertEqual(result["missing_group_field_counts"]["entry_regime"], 2)

    def test_missing_category_does_not_use_strategy_defaults(self):
        result = summarize([sample(1, regime=None, profile=None, leverage=None)])
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["missing_entry_context_count"], 0)
        self.assertEqual(result["missing_group_field_counts"]["signal_profile"], 1)

    def test_input_unchanged_and_report_json_safe(self):
        source = [sample(1), sample(2, "2")]
        before = copy.deepcopy(source)
        json.dumps(summarize(source), allow_nan=False)
        self.assertEqual(source, before)

    def test_groups_capped_without_preferential_loss_selection(self):
        result = summarize([sample(i, "1", regime="r" + str(i), profile="p" + str(i), leverage=i) for i in range(1, 5)])
        self.assertEqual(len(result["groups"]), 8)
        self.assertEqual(result["omitted_group_count"], 4)
        self.assertTrue(all(group["positive_count"] == 1 for group in result["groups"]))

    def test_invalid_evidence_fails_closed(self):
        cases = [("label", True), ("label", 1.0), ("label", 3), ("label", None),
                 ("wallet_delta_proxy_usdt", "NaN"), ("wallet_delta_proxy_usdt", "Infinity"),
                 ("wallet_delta_proxy_usdt", "1"), ("closed_at", float("inf")),
                 ("closed_at", 50), ("opened_at", -1), ("closed_event_id", 0),
                 ("id", ""), ("quality", "fabricated")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                row = sample(1)
                row[field] = value
                with self.assertRaises(ValueError):
                    summarize([row])
        for value in ("NaN", -1, True):
            with self.subTest(leverage=value), self.assertRaises(ValueError):
                summarize([sample(1, leverage=value)])
        row = sample(1)
        row["entry_context"]["captured_at"] = 101
        with self.assertRaisesRegex(ValueError, "after_intent"):
            summarize([row])


if __name__ == "__main__":
    unittest.main()
