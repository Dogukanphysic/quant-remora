import copy
import math
import unittest

from v2_engine import (
    BAR_MS,
    BarrierSpec,
    CandidateEvent,
    CostModel,
    LabelNotAvailableError,
    MAIN_SPEC,
    POLICY,
    candidate_signals,
    dataset_digest,
    indicators,
    label_candidate,
    label_candidates,
    promotion_gate,
    purged_expanding_walk_forward,
    round_trip_net_return,
    select_compatible_samples,
)


def bars(count=260, base=100.0):
    result = []
    for index in range(count):
        price = base + index * 0.03
        result.append(
            {
                "ts": index * BAR_MS,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 10.0,
            }
        )
    return result


def event(rows, *, fill_index=220, strategy="breakout", spec=MAIN_SPEC, event_id="event"):
    return CandidateEvent(
        event_id=event_id,
        strategy=strategy,
        decision_index=fill_index - 1,
        decision_ts=rows[fill_index - 1]["ts"],
        fill_index=fill_index,
        fill_ts=rows[fill_index]["ts"],
        atr=1.0,
        spec_id=spec.spec_id,
        policy=spec.policy,
        dataset_hash=dataset_digest(rows),
    )


class V2EngineTests(unittest.TestCase):
    def test_future_mutation_does_not_change_past_signal_or_indicators(self):
        rows = bars(280)
        decision = 220
        before_features = indicators(rows[: decision + 1])
        before = candidate_signals(rows, decision, features=indicators(rows))
        changed = copy.deepcopy(rows)
        for row in changed[decision + 1 :]:
            row.update(open=500.0, high=700.0, low=400.0, close=600.0, volume=99999.0)
        after_features = indicators(changed[: decision + 1])
        after = candidate_signals(changed, decision, features=indicators(changed))
        self.assertEqual(before, after)
        self.assertEqual(before_features, after_features)

    def test_same_bar_stop_is_conservative_when_both_barriers_touch(self):
        rows = bars()
        candidate = event(rows)
        fill = candidate.fill_index
        entry = rows[fill]["open"]
        stop = entry - MAIN_SPEC.stop_atr
        target = entry + MAIN_SPEC.target_atr
        rows[fill].update(high=target + 1, low=stop - 1)
        label = label_candidate(rows, candidate)
        self.assertEqual(label["exit_reason"], "stop")
        self.assertEqual(label["exit_reference"], stop)
        self.assertLess(label["net_return"], 0)

    def test_same_bar_stop_wins_even_when_open_gaps_above_target(self):
        rows = bars()
        candidate = event(rows)
        fill = candidate.fill_index
        entry = rows[fill]["open"]
        stop = entry - MAIN_SPEC.stop_atr
        target = entry + MAIN_SPEC.target_atr
        rows[fill].update(open=target + 0.5, high=target + 1, low=stop - 1)
        candidate = CandidateEvent(**{**candidate.__dict__, "fill_ts": rows[fill]["ts"]})
        label = label_candidate(rows, candidate)
        self.assertEqual(label["exit_reason"], "stop")

    def test_unfinished_vertical_horizon_is_not_mislabeled(self):
        rows = bars(225)
        candidate = event(rows, fill_index=220)
        with self.assertRaises(LabelNotAvailableError):
            label_candidate(rows, candidate)

    def test_per_strategy_labels_never_overlap(self):
        rows = bars()
        first = event(rows, fill_index=220, event_id="first")
        overlapping = event(rows, fill_index=221, event_id="second")
        independent = event(rows, fill_index=221, strategy="reentry", event_id="third")
        labels = label_candidates(rows, [first, overlapping, independent])
        self.assertEqual({label["id"] for label in labels}, {"first", "third"})
        self.assertEqual(len(labels), 2)

    def test_execution_cost_is_applied_once_per_side(self):
        cost = CostModel(fee_each_side=0.01, slippage_each_side=0.02, spread=0.04)
        actual = round_trip_net_return(100.0, 110.0, cost)
        expected = (110 * (1 - 0.02 - 0.02) * (1 - 0.01)) / (
            100 * (1 + 0.02 + 0.02) * (1 + 0.01)
        ) - 1
        self.assertAlmostEqual(actual, expected)

    def test_metadata_contains_complete_forward_label_provenance(self):
        rows = bars()
        candidate = event(rows)
        label = label_candidate(rows, candidate)
        metadata = label["metadata"]
        self.assertEqual(
            set(("spec_id", "policy", "cost_version", "fill_ts", "label_available_ts", "dataset_hash"))
            - metadata.keys(),
            set(),
        )
        self.assertEqual(metadata["fill_ts"], rows[candidate.fill_index]["ts"])
        self.assertGreater(metadata["label_available_ts"], metadata["fill_ts"])

    def test_old_spec_and_cost_samples_are_isolated(self):
        wanted = {
            "metadata": {
                "spec_id": MAIN_SPEC.spec_id,
                "policy": POLICY,
                "cost_version": CostModel().version,
                "dataset_hash": "new-data",
            }
        }
        old_spec = copy.deepcopy(wanted)
        old_spec["metadata"]["spec_id"] = "bollinger_15m_v1"
        old_cost = copy.deepcopy(wanted)
        old_cost["metadata"]["cost_version"] = "free-fill"
        old_data = copy.deepcopy(wanted)
        old_data["metadata"]["dataset_hash"] = "old-data"
        selected = select_compatible_samples(
            [old_spec, wanted, old_cost, old_data],
            spec_id=MAIN_SPEC.spec_id,
            policy=POLICY,
            cost_version=CostModel().version,
            dataset_hash="new-data",
        )
        self.assertEqual(selected, [wanted])

    def test_purged_expanding_folds_exclude_unavailable_labels(self):
        samples = []
        for index in range(20):
            fill = index * 100
            samples.append(
                {
                    "id": str(index),
                    "fill_ts": fill,
                    "label_available_ts": fill + (450 if index == 4 else 50),
                }
            )
        folds = purged_expanding_walk_forward(samples, folds=5, min_train_events=5)
        self.assertEqual(len(folds), 5)
        first = folds[0]
        self.assertEqual(first["validation"][0]["fill_ts"], 500)
        self.assertNotIn(samples[4], first["train"])
        self.assertTrue(all(s["label_available_ts"] <= first["validation_start_ts"] for s in first["train"]))
        self.assertLess(len(first["train"]), len(folds[-1]["train"]))

    def test_promotion_gate_fails_without_all_required_evidence(self):
        result = promotion_gate(
            total_events=199,
            accepted_returns=[0.01] * 29,
            fold_net_returns=[1.0, 1.0, 1.0, -1.0, -1.0],
            brier=0.26,
            baseline_brier=0.25,
            true_forward_count=49,
            bootstrap_iterations=200,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "shadow")
        self.assertIn("events_200", result["failed_checks"])
        self.assertIn("accepted_30", result["failed_checks"])
        self.assertIn("positive_folds_4_of_5", result["failed_checks"])
        self.assertIn("brier_improves_baseline", result["failed_checks"])
        self.assertIn("true_forward_50", result["failed_checks"])

    def test_promotion_gate_succeeds_only_with_full_contract(self):
        accepted = [0.012] * 45 + [-0.006] * 5
        result = promotion_gate(
            total_events=240,
            accepted_returns=accepted,
            fold_net_returns=[0.04, 0.02, 0.03, 0.01, -0.001],
            brier=0.19,
            baseline_brier=0.24,
            true_forward_count=55,
            bootstrap_iterations=500,
        )
        self.assertTrue(result["eligible"])
        self.assertEqual(result["status"], "paper_eligible")
        self.assertGreater(result["metrics"]["profit_factor"], 1.2)
        self.assertGreater(result["metrics"]["bootstrap_mean_lower_95"], 0)
        self.assertTrue(all(result["checks"].values()))

    def test_h8_and_h16_have_distinct_spec_ids(self):
        self.assertNotEqual(BarrierSpec(horizon_bars=8).spec_id, BarrierSpec(horizon_bars=16).spec_id)


if __name__ == "__main__":
    unittest.main()
