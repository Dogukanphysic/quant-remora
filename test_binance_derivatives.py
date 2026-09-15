import math
from pathlib import Path
import tempfile
import unittest

import binance_derivatives as derivatives


class BinanceDerivativesTests(unittest.TestCase):
    def test_official_archive_urls_and_dates(self):
        self.assertEqual(
            derivatives.metrics_url("btcusdt", "2026-08-31"),
            "https://data.binance.vision/data/futures/um/daily/metrics/"
            "BTCUSDT/BTCUSDT-metrics-2026-08-31.zip",
        )
        self.assertEqual(
            derivatives.funding_url("BTCUSDT", "2026-08"),
            "https://data.binance.vision/data/futures/um/monthly/fundingRate/"
            "BTCUSDT/BTCUSDT-fundingRate-2026-08.zip",
        )
        self.assertEqual(
            derivatives.dates_between("2026-08-30", "2026-09-01"),
            ["2026-08-30", "2026-08-31", "2026-09-01"],
        )

    def test_metrics_parser_sorts_and_requires_continuity(self):
        header = (
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        )
        content = header + (
            "2026-08-31 00:05:00,BTCUSDT,101,1001,1.1,1.2,1.3,1.4\n"
            "2026-08-31 00:00:00,BTCUSDT,100,1000,1,1,1,1\n"
        )
        rows = derivatives._parse_metrics(content, "BTCUSDT")
        self.assertLess(rows[0]["ts"], rows[1]["ts"])
        broken = content.replace("00:05:00", "00:10:00")
        with self.assertRaisesRegex(ValueError, "boşluk"):
            derivatives._parse_metrics(broken, "BTCUSDT")

    def test_funding_parser_accepts_boundary_plus_one_ms(self):
        content = (
            "calc_time,funding_interval_hours,last_funding_rate\n"
            "1785542400001,8,0.00004123\n"
            "1785571200000,8,-0.00001000\n"
        )
        rows = derivatives._parse_funding(content, "BTCUSDT")
        self.assertEqual(rows[0]["ts"], 1785542400001)
        self.assertAlmostEqual(rows[1]["funding_rate"], -0.00001)

    def test_derivative_features_never_use_future_observation(self):
        base = 1_785_542_400_000
        metrics = []
        for index in range(13):
            metrics.append({
                "ts": base - 3_600_000 + index * 300_000,
                "open_interest": 100 + index,
                "open_interest_value": 1000 + index,
                "top_account_ratio": 1.1,
                "top_position_ratio": 1.2,
                "global_account_ratio": 1.3,
                "taker_buy_sell_ratio": 1.4,
            })
        funding = [
            {"ts": base - 1, "funding_rate": 0.0001},
            {"ts": base + 1, "funding_rate": 0.9},
        ]
        features = derivatives.derivative_features(metrics, funding, [base])
        self.assertAlmostEqual(features[base][0], 0.0001)
        self.assertAlmostEqual(features[base][2], math.log(112 / 100))

    def test_metric_staleness_rejects_forward_fill(self):
        base = 1_785_542_400_000
        metrics = [{
            "ts": base, "open_interest": 100, "open_interest_value": 1000,
            "top_account_ratio": 1.1, "top_position_ratio": 1.2,
            "global_account_ratio": 1.3, "taker_buy_sell_ratio": 1.4,
        }]
        funding = [{"ts": base - 1, "funding_rate": 0.0001}]
        self.assertEqual(
            derivatives.derivative_features(metrics, funding, [base + 900_000]), {})

    def test_fixed_return_model_uses_purged_holdout(self):
        samples = [{
            "entry_ts": index * 10,
            "exit_ts": index * 10 + 1,
            "x": [float(index % 2)],
            "net_return": 0.01 if index % 2 else -0.005,
        } for index in range(100)]
        result = derivatives._return_model_evaluation(samples)
        self.assertEqual(result["train_count"], 70)
        self.assertEqual(result["validation_count"], 30)
        self.assertGreaterEqual(result["accepted"], 10)
        self.assertTrue(result["quality_pass"])

    def test_exit_policy_uses_next_open_and_future_bar(self):
        rows = [
            {"ts": 900_000, "open": 100, "high": 100, "low": 100,
             "close": 100, "volume": 1},
            {"ts": 1_800_000, "open": 100, "high": 103, "low": 99.5,
             "close": 102, "volume": 1},
        ]
        sample = {
            "x": [0, 0.01],
            "metadata": {"decision_ts": 900_000, "side": "long"},
        }
        result = derivatives._policy_return(
            sample, rows, {900_000: 0, 1_800_000: 1}, 1.0, 2.0, 1)
        self.assertGreater(result, 0)

    def test_local_metrics_csv_rejects_wrong_symbol(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metrics.csv"
            path.write_text(
                ",".join(derivatives.METRIC_FIELDS) + "\n" +
                "1785542400000,ETHUSDT,1,1,1,1,1,1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "BTCUSDT"):
                derivatives._read_csv(path, derivatives.METRIC_FIELDS, "metrics")


if __name__ == "__main__":
    unittest.main()
