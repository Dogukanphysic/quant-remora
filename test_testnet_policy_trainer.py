import tempfile
import unittest
from pathlib import Path

import testnet_policy_trainer as trainer


def market_result(turnover=80, *, passed=True):
    aggregate = {
        "bars": 400,
        "return": 0.25 if passed else -0.05,
        "annualized_sharpe": 0.8,
        "max_drawdown": 0.20,
        "turnover_units": turnover,
        "profit_factor": 1.30,
    }
    return {
        "aggregate": aggregate,
        "folds": [{"return": 0.02}] * 4 + [{"return": -0.01}],
        "positive_folds": 4,
        "gate_checks": {},
        "gate_pass": passed,
    }


def candidate(threshold, binance_turnover, bitstamp_turnover, *, passed=True):
    markets = {
        "binance_um": market_result(binance_turnover, passed=passed),
        "bitstamp": market_result(bitstamp_turnover, passed=passed),
    }
    return {
        "rule": {
            "family": "momentum", "lookback_days": 30,
            "threshold": threshold, "position": "long_or_cash",
            "execution_lag_days": 1,
        },
        "markets": markets,
        "gate_pass_both_markets": passed,
        "activity": {
            "minimum_market_turnover_units": min(
                binance_turnover, bitstamp_turnover),
            "total_turnover_units": binance_turnover + bitstamp_turnover,
        },
    }


DATA_VERSIONS = {
    "binance_um": {"sha256": "a" * 64, "complete_daily_bars": 1800},
    "bitstamp": {"sha256": "b" * 64, "complete_daily_bars": 2000},
}


class TestnetPolicyTrainerTests(unittest.TestCase):
    def test_selects_highest_cross_market_activity_among_passing_rules(self):
        evaluations = [
            candidate(0.0, 200, 180, passed=False),
            candidate(0.03, 70, 65),
            candidate(0.05, 90, 55),
            candidate(0.10, 91, 103),
        ]
        selected = trainer.select_candidate(evaluations)
        self.assertEqual(selected["rule"]["threshold"], 0.10)

    def test_rejects_when_no_rule_passes_both_markets(self):
        evaluations = [candidate(value, 100, 100, passed=False)
                       for value in trainer.CANDIDATE_THRESHOLDS]
        report, artifact = trainer.build_outputs(evaluations, DATA_VERSIONS)
        self.assertIsNone(report["selected"])
        self.assertEqual(report["status"], "rejected")
        self.assertFalse(artifact["testnet_execution_eligible"])
        self.assertIsNone(artifact["rule"])

    def test_rejects_if_selection_does_not_match_ten_percent_release(self):
        evaluations = [
            candidate(0.05, 120, 110),
            candidate(0.10, 91, 103),
        ]
        report, artifact = trainer.build_outputs(evaluations, DATA_VERSIONS)
        self.assertEqual(report["selected"]["rule"]["threshold"], 0.05)
        self.assertFalse(report["release_compatible"])
        self.assertEqual(artifact["status"], "rejected")
        self.assertFalse(artifact["testnet_execution_eligible"])
        self.assertIsNone(artifact["rule"])

    def test_model_version_is_deterministic_and_data_bound(self):
        evaluations = [candidate(0.10, 91, 103)]
        first = trainer.build_outputs(evaluations, DATA_VERSIONS)[1]
        second = trainer.build_outputs(evaluations, DATA_VERSIONS)[1]
        self.assertEqual(first["model_version"], second["model_version"])
        changed_data = {**DATA_VERSIONS,
                        "bitstamp": {"sha256": "c" * 64,
                                     "complete_daily_bars": 2000}}
        changed = trainer.build_outputs(evaluations, changed_data)[1]
        self.assertNotEqual(first["model_version"], changed["model_version"])

    def test_candidate_artifact_is_testnet_only_and_not_promoted(self):
        evaluations = [candidate(0.10, 91, 103)]
        report, artifact = trainer.build_outputs(evaluations, DATA_VERSIONS)
        self.assertEqual(artifact["policy_id"],
                         "btc_daily_momentum_30d_t10_testnet_v1")
        self.assertEqual(artifact["status"], "testnet_exploration_candidate")
        self.assertEqual(artifact["environment"],
                         "binance_spot_testnet")
        self.assertEqual(artifact["market_data_source"],
                         "binance_public_spot")
        self.assertEqual(artifact["rule"]["threshold"], "0.10")
        self.assertEqual(artifact["order"], {
            "symbol": "BTCUSDT", "quote_usdt": "10", "mode": "long_cash"})
        self.assertIn("selection_contract", artifact)
        self.assertIn("binance_um", artifact["selected_metrics"])
        self.assertTrue(artifact["testnet_only"])
        self.assertFalse(artifact["paper_eligible"])
        self.assertFalse(artifact["real_money_eligible"])
        self.assertFalse(artifact["real_orders_enabled"])
        self.assertFalse(artifact["live_trading_enabled"])
        self.assertEqual(report["one_way_stressed_cost"], 0.002)

    def test_market_gate_requires_every_pre_registered_condition(self):
        good = {
            "return": 0.01, "profit_factor": 1.20,
            "max_drawdown": 0.35, "turnover_units": 50,
        }
        folds = [{"return": 0.01}] * 4 + [{"return": -0.01}]
        passed, checks = trainer._market_gate(good, folds)
        self.assertTrue(passed)
        for key in good:
            broken = dict(good)
            broken[key] = {
                "return": 0.0, "profit_factor": 1.19,
                "max_drawdown": 0.351, "turnover_units": 49,
            }[key]
            self.assertFalse(trainer._market_gate(broken, folds)[0])
        bad_folds = [{"return": 0.01}] * 3 + [{"return": -0.01}] * 2
        self.assertFalse(trainer._market_gate(good, bad_folds)[0])
        self.assertEqual(len(checks), 5)

    def test_writes_only_to_explicit_destinations(self):
        report, artifact = trainer.build_outputs(
            [candidate(0.10, 91, 103)], DATA_VERSIONS)
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            artifact_path = Path(directory) / "config.json"
            trainer.write_outputs(report, artifact, report_path=report_path,
                                  artifact_path=artifact_path)
            self.assertTrue(report_path.exists())
            self.assertTrue(artifact_path.exists())


if __name__ == "__main__":
    unittest.main()
