import copy
from decimal import Decimal, ROUND_DOWN, getcontext, setcontext
import hashlib
import json
import unittest

import testnet_online_learner as learner


START_MS = 1_700_000_000_000


def daily_payload(index, *, forward_return=None, freeze_id=None,
                  out_of_sample=True):
    cycle = index % 4
    return {
        "sample_id": f"daily-{index:04d}",
        "decision_ts": START_MS + index * learner.DAY_MS,
        "label_available_ts": START_MS + (index + 1) * learner.DAY_MS,
        "momentum": "0.08" if cycle < 2 else "0",
        "forward_return": (forward_return if forward_return is not None
                           else ("0.01" if cycle < 2 else "-0.01")),
        "closed": True,
        "out_of_sample": out_of_sample,
        "true_forward_after_freeze": freeze_id is not None,
        **({"freeze_id": freeze_id} if freeze_id is not None else {}),
    }


def development(count=140):
    return [learner.seal_sample(daily_payload(index)) for index in range(count)]


def freeze(dev=None):
    report = learner.learn(development() if dev is None else dev)
    if report["frozen_candidate"] is None:
        raise AssertionError("fixture did not freeze a challenger")
    return report["frozen_candidate"]


def forward_samples(artifact, start=140, count=60, *, losing=False):
    result = []
    for index in range(start, start + count):
        changed = "-0.03" if losing and index % 4 < 2 else None
        result.append(learner.seal_sample(daily_payload(
            index, forward_return=changed,
            freeze_id=artifact["immutable_sha256"])))
    return result


def round_trip_payload(index, artifact, *, winning=True):
    cutoff = artifact["freeze_cutoff_ts"]
    entry = cutoff + (index * 6 + 1) * learner.DAY_MS
    if winning:
        exit_proceeds, pnl = Decimal("101.8"), Decimal("1.8")
    else:
        # -3% exactly on the 100 USDT invested cost. Eight consecutive
        # records also breach the 15% actual-evidence drawdown gate.
        exit_proceeds, pnl = Decimal("97"), Decimal("-3")
    net_return = (pnl / Decimal("100")).quantize(
        Decimal("0.000000000001"))
    return {
        "round_trip_id": f"trip-{index:02d}",
        "entry_ts": entry,
        "exit_ts": entry + 2 * learner.DAY_MS,
        "entry_client_id": f"entry-{index:02d}",
        "exit_client_id": f"exit-{index:02d}",
        "entry_cost_usdt": "100",
        "exit_proceeds_usdt": format(exit_proceeds, "f"),
        "realized_pnl_usdt": format(pnl, "f"),
        "net_return": format(net_return, "f"),
        "environment": "binance_spot_testnet",
        "symbol": "BTCUSDT",
        "freeze_id": artifact["immutable_sha256"],
        "closed": True,
    }


def round_trips(artifact, count=8, *, winning=True):
    return [learner.seal_round_trip(round_trip_payload(
                index, artifact, winning=winning))
            for index in range(count)]


def round_trips_with_costs_and_pnls(artifact, outcomes):
    result = []
    for index, (cost, pnl) in enumerate(outcomes):
        payload = round_trip_payload(index, artifact)
        proceeds = cost + pnl
        payload["entry_cost_usdt"] = learner._format_decimal(cost)
        payload["exit_proceeds_usdt"] = learner._format_decimal(proceeds)
        payload["realized_pnl_usdt"] = learner._format_decimal(pnl)
        payload["net_return"] = learner._metric_decimal(pnl / cost)
        result.append(learner.seal_round_trip(payload))
    return result


def round_trips_with_pnls(artifact, pnls):
    return round_trips_with_costs_and_pnls(
        artifact, [(Decimal("100"), pnl) for pnl in pnls])


class OnlineLearnerTests(unittest.TestCase):
    def test_daily_seal_is_canonical_and_tamper_evident(self):
        payload = daily_payload(0)
        payload["momentum"] = "0.0800"
        sealed = learner.seal_sample(payload)
        self.assertEqual(sealed["momentum"], "0.08")
        with self.assertRaisesRegex(ValueError, "does not match"):
            learner.learn([dict(sealed, forward_return="0.02")])

    def test_daily_horizon_and_continuity_are_exact(self):
        bad = daily_payload(0)
        bad["label_available_ts"] += 1
        with self.assertRaisesRegex(ValueError, "exactly DAY_MS"):
            learner.seal_sample(bad)
        first = learner.seal_sample(daily_payload(0))
        gap = daily_payload(1)
        gap["decision_ts"] += learner.DAY_MS
        gap["label_available_ts"] += learner.DAY_MS
        with self.assertRaisesRegex(ValueError, "exactly contiguous"):
            learner.learn([first, learner.seal_sample(gap)])

    def test_malformed_duplicate_nonfinite_and_open_fail_closed(self):
        sample = daily_payload(0)
        sample["momentum"] = "NaN"
        with self.assertRaisesRegex(ValueError, "finite"):
            learner.seal_sample(sample)
        sample = daily_payload(0)
        sample["closed"] = False
        with self.assertRaisesRegex(ValueError, "closed=true"):
            learner.seal_sample(sample)
        one = learner.seal_sample(daily_payload(0))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            learner.learn([one, one])

    def test_first_phase_freezes_candidate_without_forward_claim(self):
        report = learner.learn(development())
        artifact = report["frozen_candidate"]
        self.assertEqual(report["status"],
                         "candidate_frozen_awaiting_true_forward")
        self.assertEqual(report["development"]["selected_threshold"], "0.05")
        self.assertFalse(report["development"]["selection_uses_future_evidence"])
        self.assertEqual(artifact["challenger_threshold"], "0.05")
        self.assertEqual(artifact["development_sample_count"], 140)
        self.assertFalse(report["proposal_ready_for_review"])
        for key in ("testnet_execution_eligible", "paper_eligible",
                    "real_money_eligible", "real_orders_enabled",
                    "live_trading_enabled", "automatic_activation_enabled",
                    "writes_active_config"):
            self.assertFalse(artifact[key])
            self.assertFalse(report[key])

    def test_safety_violation_refuses_to_freeze_a_challenger(self):
        report = learner.learn(
            development(), safety_violations=("source integrity failure",))
        self.assertEqual(report["status"], "blocked_by_safety_violation")
        self.assertIsNone(report["frozen_candidate"])
        self.assertFalse(report["proposal_ready_for_review"])
        self.assertFalse(report["review_gate_checks"]["challenger_freeze_allowed"])

    def test_frozen_artifact_is_immutable_and_prefix_bound(self):
        dev = development()
        artifact = freeze(dev)
        with self.assertRaisesRegex(ValueError, "does not match"):
            learner.learn(dev, frozen_candidate=dict(
                artifact, challenger_threshold="0.03"))
        changed = copy.deepcopy(dev)
        changed[5] = learner.seal_sample(daily_payload(5, forward_return="0.02"))
        with self.assertRaisesRegex(ValueError, "prefix was changed"):
            learner.learn(changed + forward_samples(artifact),
                          frozen_candidate=artifact)

    def test_forward_samples_must_reference_frozen_artifact(self):
        dev = development()
        artifact = freeze(dev)
        evidence = dev + [learner.seal_sample(daily_payload(
            140, freeze_id="a" * 64))]
        with self.assertRaisesRegex(ValueError, "bound to the artifact"):
            learner.learn(evidence, frozen_candidate=artifact)

    def test_exact_prebind_boundary_sample_is_an_embargo_not_performance(self):
        dev = development()
        artifact = freeze(dev)
        bridge = learner.seal_sample(daily_payload(140))
        forward = forward_samples(artifact, start=141)

        report = learner.learn(
            dev + [bridge] + forward,
            frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact),
        )

        self.assertEqual(report["prebind_embargo"]["sample_count"], 1)
        self.assertEqual(report["prebind_embargo"]["sample_id"], "daily-0140")
        self.assertFalse(
            report["prebind_embargo"]["included_in_performance_metrics"]
        )
        self.assertEqual(report["untouched_true_forward"]["sample_count"], 60)
        self.assertEqual(
            report["matched_holdout_comparison"]["matched_sample_count"], 60
        )

    def test_pre_registration_boundary_requires_store_provenance_handling(self):
        dev = [
            learner.seal_sample(daily_payload(index, out_of_sample=False))
            for index in range(140)
        ]
        artifact = freeze(dev)
        bridge = learner.seal_sample(
            daily_payload(140, out_of_sample=False)
        )
        forward = forward_samples(artifact, start=141)

        with self.assertRaisesRegex(ValueError, "optional pre-bind embargo"):
            learner.learn(
                dev + [bridge] + forward,
                frozen_candidate=artifact,
                actual_round_trips=round_trips(artifact),
            )

        report = learner.learn(
            dev + [bridge] + forward,
            frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact),
            verified_pre_registration_embargo=True,
        )
        self.assertEqual(report["prebind_embargo"]["sample_count"], 1)
        self.assertEqual(report["untouched_true_forward"]["sample_count"], 60)
        self.assertEqual(
            report["matched_holdout_comparison"]["matched_sample_count"], 60
        )
        self.assertEqual(
            report["review_gate_checks"][
                "total_oos_daily_labels_at_least_200"
            ],
            False,
        )

    def test_only_one_exact_prebind_boundary_sample_is_allowed(self):
        dev = development()
        artifact = freeze(dev)
        two_unbound = [
            learner.seal_sample(daily_payload(140)),
            learner.seal_sample(daily_payload(141)),
        ]
        with self.assertRaisesRegex(ValueError, "bound to the artifact"):
            learner.learn(
                dev + two_unbound + forward_samples(
                    artifact, start=142, count=58
                ),
                frozen_candidate=artifact,
            )

    def test_future_returns_never_change_frozen_selection(self):
        dev = development()
        artifact = freeze(dev)
        good = learner.learn(
            dev + forward_samples(artifact), frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact))
        bad = learner.learn(
            dev + forward_samples(artifact, losing=True),
            frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact))
        self.assertEqual(good["development"]["selected_threshold"], "0.05")
        self.assertEqual(bad["development"]["selected_threshold"], "0.05")
        self.assertEqual(bad["status"], "challenger_failed_true_forward")

    def test_round_trip_reconciles_exact_pnl_cost_and_return(self):
        dev = development()
        artifact = freeze(dev)
        sealed = learner.seal_round_trip(round_trip_payload(0, artifact))
        self.assertEqual(sealed["realized_pnl_usdt"], "1.8")
        tampered = dict(sealed, round_trip_id="trip-tampered")
        with self.assertRaisesRegex(ValueError, "does not match"):
            learner.learn(
                dev + forward_samples(artifact), frozen_candidate=artifact,
                actual_round_trips=[tampered])
        at_cutoff = round_trip_payload(0, artifact)
        at_cutoff["entry_ts"] = artifact["freeze_cutoff_ts"]
        at_cutoff["exit_ts"] = artifact["freeze_cutoff_ts"] + learner.DAY_MS
        with self.assertRaisesRegex(ValueError, "strictly after"):
            learner.learn(
                dev + forward_samples(artifact), frozen_candidate=artifact,
                actual_round_trips=[learner.seal_round_trip(at_cutoff)])
        bad = round_trip_payload(0, artifact)
        bad["realized_pnl_usdt"] = "1.9"
        with self.assertRaisesRegex(ValueError, "reconcile exactly"):
            learner.seal_round_trip(bad)
        bad = round_trip_payload(0, artifact)
        bad["net_return"] = "0.1"
        with self.assertRaisesRegex(ValueError, "reconcile to PnL"):
            learner.seal_round_trip(bad)

    def test_count_alone_cannot_satisfy_round_trip_gate(self):
        dev = development()
        artifact = freeze(dev)
        report = learner.learn(
            dev + forward_samples(artifact), frozen_candidate=artifact)
        self.assertEqual(report["actual_round_trip_evidence"]["count"], 0)
        self.assertFalse(report["review_gate_checks"]
                         ["immutable_reconciled_round_trips_at_least_8"])
        with self.assertRaises(TypeError):
            learner.learn(dev, actual_round_trip_count=8)  # type: ignore

    def test_review_requires_frozen_sixty_day_suffix_and_eight_trips(self):
        dev = development()
        artifact = freeze(dev)
        report = learner.learn(
            dev + forward_samples(artifact), frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact))
        self.assertEqual(report["status"], "proposal_ready_for_review")
        self.assertTrue(report["proposal_ready_for_review"])
        self.assertEqual(report["untouched_true_forward"]["sample_count"], 60)
        self.assertEqual(report["actual_round_trip_evidence"]["count"], 8)
        self.assertEqual(report["actual_round_trip_evidence"]["role"],
                         "operational_execution_evidence_not_threshold_selection")
        self.assertEqual(report["actual_round_trip_evidence"]
                         ["total_realized_pnl_usdt"], "14.4")
        self.assertTrue(all(report["review_gate_checks"].values()))

    def test_eight_exact_losing_trips_block_review_readiness(self):
        dev = development()
        artifact = freeze(dev)
        report = learner.learn(
            dev + forward_samples(artifact), frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact, winning=False))
        self.assertEqual(report["actual_round_trip_evidence"]["count"], 8)
        self.assertEqual(report["status"], "actual_execution_evidence_failed")
        self.assertFalse(report["proposal_ready_for_review"])
        self.assertFalse(report["review_gate_checks"]
                         ["actual_round_trips_net_positive"])
        self.assertFalse(report["review_gate_checks"]
                         ["actual_round_trips_realized_pnl_positive"])
        self.assertFalse(report["review_gate_checks"]
                         ["actual_round_trips_profit_factor_at_least_1_15"])
        self.assertFalse(report["review_gate_checks"]
                         ["actual_round_trips_max_drawdown_at_most_15_percent"])

    def test_59_forward_labels_or_7_trips_remains_collecting(self):
        dev = development()
        artifact = freeze(dev)
        cases = (
            (forward_samples(artifact, count=59), round_trips(artifact),
             "true_forward_labels_at_least_60"),
            (forward_samples(artifact), round_trips(artifact, 7),
             "immutable_reconciled_round_trips_at_least_8"),
        )
        for suffix, trips, gate in cases:
            with self.subTest(gate=gate):
                report = learner.learn(
                    dev + suffix, frozen_candidate=artifact,
                    actual_round_trips=trips)
                self.assertEqual(report["status"], "collecting_review_evidence")
                self.assertFalse(report["review_gate_checks"][gate])

    def test_safety_violation_blocks_review(self):
        dev = development()
        artifact = freeze(dev)
        report = learner.learn(
            dev + forward_samples(artifact), frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact),
            safety_violations=("manual reconciliation occurred",))
        self.assertEqual(report["status"], "blocked_by_safety_violation")
        self.assertFalse(report["proposal_ready_for_review"])

    def test_report_is_deterministic_strict_json_and_contract_is_isolated(self):
        dev = development()
        artifact = freeze(dev)
        evidence, trips = dev + forward_samples(artifact), round_trips(artifact)
        first = learner.learn(evidence, frozen_candidate=artifact,
                              actual_round_trips=trips)
        second = learner.learn(copy.deepcopy(evidence),
                               frozen_candidate=copy.deepcopy(artifact),
                               actual_round_trips=copy.deepcopy(trips))
        self.assertEqual(first, second)
        json.dumps(first, sort_keys=True, allow_nan=False)
        first["contract"]["review_gates"][
            "minimum_immutable_actual_round_trips"] = 0
        third = learner.learn(evidence, frozen_candidate=artifact,
                              actual_round_trips=trips)
        self.assertEqual(third["contract"]["review_gates"]
                         ["minimum_immutable_actual_round_trips"], 8)

    def test_decimal_global_context_cannot_change_reports_or_versions(self):
        dev = development()
        baseline_fit = learner.learn(dev)
        artifact = baseline_fit["frozen_candidate"]
        evidence = dev + forward_samples(artifact)
        trips = round_trips(artifact)
        baseline_evaluation = learner.learn(
            evidence, frozen_candidate=artifact, actual_round_trips=trips)
        original = getcontext().copy()
        try:
            getcontext().prec = 6
            getcontext().rounding = ROUND_DOWN
            changed_fit = learner.learn(dev)
            changed_evaluation = learner.learn(
                evidence, frozen_candidate=artifact, actual_round_trips=trips)
        finally:
            setcontext(original)
        self.assertEqual(baseline_fit, changed_fit)
        self.assertEqual(baseline_evaluation, changed_evaluation)
        self.assertEqual(
            baseline_fit["learner_version"], changed_fit["learner_version"])

    def test_profit_factor_uses_compounded_equity_pnl_not_raw_returns(self):
        # A +13% first period followed by -10% has a raw-return ratio above
        # 1.15.  The loss is applied to the larger post-gain equity, so the
        # economically correct P&L profit factor (including exit cost) is lower.
        first_forward = (
            Decimal("1.13") / (Decimal("1") - learner.ONE_WAY_TRANSITION_COST)
            - Decimal("1")
        )
        first = daily_payload(0, forward_return=format(first_forward, "f"))
        first["momentum"] = "0.20"
        second = daily_payload(1, forward_return="-0.10")
        second["momentum"] = "0.20"
        samples = learner._validate_samples([
            learner.seal_sample(first),
            learner.seal_sample(second),
        ])

        metrics = learner._metrics(samples, Decimal("0.10"))

        self.assertEqual(metrics["profit_factor_basis"], "compounded_equity_pnl")
        self.assertGreater(Decimal("0.13") / Decimal("0.102"), Decimal("1.15"))
        self.assertLess(Decimal(str(metrics["profit_factor"])), Decimal("1.15"))

    def test_profit_factor_gate_uses_exact_value_below_display_boundary(self):
        dev = development()
        artifact = freeze(dev)
        evidence = dev + forward_samples(artifact)
        # Exact PF is 1.1499999999996, which the report displays as 1.15.
        pnls = ([Decimal("28.74999999999")] * 4
                + [Decimal("-25")] * 4)

        report = learner.learn(
            evidence,
            frozen_candidate=artifact,
            actual_round_trips=round_trips_with_pnls(artifact, pnls),
        )

        self.assertEqual(
            report["actual_round_trip_evidence"]["profit_factor"], "1.15")
        self.assertFalse(report["review_gate_checks"][
            "actual_round_trips_profit_factor_at_least_1_15"])

    def test_drawdown_gate_uses_exact_value_above_display_boundary(self):
        dev = development()
        artifact = freeze(dev)
        evidence = dev + forward_samples(artifact)
        # Exact drawdown is 0.1500000000004, displayed as 0.15.
        pnls = [Decimal("-15.00000000004")] + [Decimal("3")] * 7

        report = learner.learn(
            evidence,
            frozen_candidate=artifact,
            actual_round_trips=round_trips_with_pnls(artifact, pnls),
        )

        self.assertEqual(
            report["actual_round_trip_evidence"]["max_drawdown"], "0.15")
        self.assertFalse(report["review_gate_checks"][
            "actual_round_trips_max_drawdown_at_most_15_percent"])

    def test_compounded_net_gate_derives_exact_returns_from_cash_evidence(self):
        dev = development()
        artifact = freeze(dev)
        evidence = dev + forward_samples(artifact)
        outcomes = [
            (Decimal("200"), Decimal("20")),
            (Decimal("100"), Decimal("-9.09090909092")),
            *[(Decimal("100"), Decimal("0")) for _ in range(6)],
        ]
        trips = round_trips_with_costs_and_pnls(artifact, outcomes)
        rounded_equity = Decimal("1")
        for trip in trips:
            rounded_equity *= Decimal("1") + Decimal(trip["net_return"])

        report = learner.learn(
            evidence,
            frozen_candidate=artifact,
            actual_round_trips=trips,
        )

        # The sealed 12-decimal return fields compound to +1e-13, but the exact
        # PnL/cost ratios compound to -1.2e-13. The review gate must use the
        # exact cash evidence even though both values display as zero.
        self.assertEqual(rounded_equity - Decimal("1"), Decimal("1E-13"))
        self.assertEqual(
            report["actual_round_trip_evidence"]["compounded_net_return"], "0")
        self.assertTrue(report["review_gate_checks"][
            "actual_round_trips_realized_pnl_positive"])
        self.assertTrue(report["review_gate_checks"][
            "actual_round_trips_profit_factor_at_least_1_15"])
        self.assertTrue(report["review_gate_checks"][
            "actual_round_trips_max_drawdown_at_most_15_percent"])
        self.assertFalse(report["review_gate_checks"][
            "actual_round_trips_net_positive"])

    def test_report_version_hashes_canonical_final_report_content(self):
        report = learner.learn(development())
        content = copy.deepcopy(report)
        version = content.pop("report_version")
        encoded = json.dumps(
            content, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(version, hashlib.sha256(encoded).hexdigest())
        content["status"] = "tampered"
        tampered = json.dumps(
            content, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("utf-8")
        self.assertNotEqual(version, hashlib.sha256(tampered).hexdigest())

    def test_semantic_implementation_revision_has_golden_reports(self):
        fit = learner.learn(development())
        artifact = fit["frozen_candidate"]
        evaluation = learner.learn(
            development() + forward_samples(artifact),
            frozen_candidate=artifact,
            actual_round_trips=round_trips(artifact),
        )

        self.assertEqual(learner.IMPLEMENTATION_REVISION, 5)
        self.assertEqual(
            learner.LEARNER_VERSION,
            "bc524e74e7458c07ac76285ad3e60bdbabd3e5fc8b06c69d6677a29dbaeee305",
        )
        self.assertEqual(
            fit["report_version"],
            "738165ba88c5e709ef9f5cdd44f9666f74954c2cb5f8cf70dbf26240314e298a",
        )
        self.assertEqual(
            evaluation["report_version"],
            "a151b187811b204c01ebe580259b7b55838252c51d2a15aaa6da50e9f32aca9a",
        )


if __name__ == "__main__":
    unittest.main()
