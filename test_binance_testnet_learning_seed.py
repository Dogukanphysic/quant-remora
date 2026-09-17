import copy
import csv
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import binance_testnet_learning_seed as seed
import testnet_online_learner as learner


DAY_MS = learner.DAY_MS
FIFTEEN_MINUTES_MS = 15 * 60 * 1000


class HistoricalLearningSeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now_ms = 200 * DAY_MS

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _price(day: int) -> Decimal:
        return Decimal("100") + Decimal(day)

    def _rows(self, days=35, *, interval="1d", symbol="BTCUSDT"):
        step = DAY_MS if interval == "1d" else FIFTEEN_MINUTES_MS
        count = days if interval == "1d" else days * 96
        rows = []
        for index in range(count):
            day = index if interval == "1d" else index // 96
            price = self._price(day)
            rows.append(
                {
                    "ts": DAY_MS + index * step,
                    "open": format(price, "f"),
                    "high": format(price, "f"),
                    "low": format(price, "f"),
                    "close": format(price, "f") + ".0000",
                    "volume": "1.2500",
                    "exchange": "binance_spot",
                    "symbol": symbol,
                }
            )
        return rows

    def _write(self, rows, *, interval="1d", manifest=True):
        path = self.root / f"candles-{interval}.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=seed.EXPECTED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        if manifest:
            raw_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            source = {
                "source": "https://data-api.binance.vision",
                "endpoint": "/api/v3/klines",
                "security": "NONE",
                "market": "spot",
                "symbol": "BTCUSDT",
                "interval": interval,
                "candles": len(rows),
                "first_ts": int(rows[0]["ts"]),
                "last_ts": int(rows[-1]["ts"]),
                "dataset_sha256": raw_digest,
                "output": "machine-specific-path-is-not-part-of-sealed-claims",
            }
            path.with_suffix(path.suffix + ".manifest.json").write_text(
                json.dumps(source, indent=2), encoding="utf-8"
            )
        return path

    def test_builds_latest_exact_daily_samples_with_decimal_arithmetic(self):
        path = self._write(self._rows(days=35))

        manifest = seed.build_seed_manifest(
            path, sample_limit=2, now_ms=self.now_ms
        )

        self.assertEqual(manifest["sample_count"], 2)
        self.assertEqual(manifest["raw_candle_count"], 35)
        self.assertEqual(manifest["daily_candle_count"], 35)
        self.assertEqual(manifest["first_candle_close_ms"], 2 * DAY_MS - 1)
        self.assertEqual(manifest["last_candle_close_ms"], 36 * DAY_MS - 1)
        self.assertEqual(manifest["minimum_completed_at_ms"], 36 * DAY_MS)
        self.assertEqual(manifest["last_label_available_ts"], 36 * DAY_MS - 1)
        self.assertEqual(manifest["last_close"], "134")
        first, last = manifest["samples"]
        self.assertEqual(first["decision_ts"], 34 * DAY_MS - 1)
        self.assertEqual(last["decision_ts"], 35 * DAY_MS - 1)
        with localcontext(seed.DECIMAL_CONTEXT):
            self.assertEqual(
                first["momentum"], learner._format_decimal(
                    Decimal("132") / Decimal("102") - Decimal("1")
                )
            )
            self.assertEqual(
                first["forward_return"], learner._format_decimal(
                    Decimal("133") / Decimal("132") - Decimal("1")
                )
            )
        self.assertTrue(all(item["closed"] for item in manifest["samples"]))
        self.assertTrue(all(
            item["out_of_sample"] is False
            and item["true_forward_after_freeze"] is False
            for item in manifest["samples"]
        ))
        self.assertEqual(seed.verify_seed_manifest(manifest), manifest)
        later_retry = seed.build_seed_manifest(
            path, sample_limit=2, now_ms=self.now_ms + DAY_MS
        )
        self.assertEqual(later_retry, manifest)

    def test_aggregates_complete_15m_utc_days_and_keeps_last_daily_close(self):
        path = self._write(self._rows(days=32, interval="15m"), interval="15m")

        manifest = seed.build_seed_manifest(path, now_ms=self.now_ms)

        self.assertEqual(manifest["source_interval"], "15m")
        self.assertEqual(manifest["raw_candle_count"], 32 * 96)
        self.assertEqual(manifest["daily_candle_count"], 32)
        self.assertEqual(manifest["sample_count"], 1)
        sample = manifest["samples"][0]
        self.assertEqual(sample["decision_ts"], 32 * DAY_MS - 1)
        self.assertEqual(sample["label_available_ts"], 33 * DAY_MS - 1)
        self.assertEqual(sample["momentum"], "0.3")
        with localcontext(seed.DECIMAL_CONTEXT):
            self.assertEqual(
                sample["forward_return"], learner._format_decimal(
                    Decimal("131") / Decimal("130") - Decimal("1")
                )
            )
        self.assertEqual(manifest["last_close"], "131")
        seed.verify_seed_manifest(manifest)

    def test_requires_exact_sample_limit_and_uses_manifest_provenance(self):
        path = self._write(self._rows(days=32))
        with self.assertRaisesRegex(ValueError, "Requested 2 samples.*only 1"):
            seed.build_seed_manifest(path, sample_limit=2, now_ms=self.now_ms)
        with self.assertRaisesRegex(ValueError, "does not match the source manifest"):
            seed.build_seed_manifest(
                path, interval="15m", now_ms=self.now_ms
            )
        manifest = seed.build_seed_manifest(path, now_ms=self.now_ms)
        provenance = manifest["provenance"]
        self.assertRegex(provenance["source_manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("output", provenance["source_manifest_claims"])

    def test_gap_duplicate_and_out_of_order_inputs_fail_closed(self):
        base = self._rows(days=32)
        variants = {
            "gap": base[:5] + base[6:],
            "duplicate": base[:6] + [dict(base[5])] + base[6:],
            "out_of_order": base[:5] + [base[6], base[5]] + base[7:],
        }
        for name, rows in variants.items():
            with self.subTest(name=name):
                path = self._write(rows, manifest=False)
                with self.assertRaisesRegex(
                    ValueError, "gap, duplicate, or out-of-order"
                ):
                    seed.build_seed_manifest(
                        path, interval="1d", now_ms=self.now_ms
                    )

    def test_partial_or_open_utc_days_fail_closed(self):
        incomplete = self._rows(days=32, interval="15m")[:-1]
        path = self._write(incomplete, interval="15m", manifest=False)
        with self.assertRaisesRegex(ValueError, "complete UTC days"):
            seed.build_seed_manifest(path, interval="15m", now_ms=self.now_ms)

        complete = self._write(
            self._rows(days=32, interval="15m"), interval="15m", manifest=False
        )
        last_close = 33 * DAY_MS - 1
        with self.assertRaisesRegex(ValueError, "open candle or incomplete day"):
            seed.build_seed_manifest(
                complete, interval="15m", now_ms=last_close
            )

    def test_wrong_source_symbol_interval_and_invalid_decimal_fail_closed(self):
        wrong_symbol = self._write(
            self._rows(days=32, symbol="ETHUSDT"), manifest=False
        )
        with self.assertRaisesRegex(ValueError, "wrong symbol"):
            seed.build_seed_manifest(
                wrong_symbol, interval="1d", now_ms=self.now_ms
            )

        wrong_interval = self._write(self._rows(days=32), manifest=False)
        with self.assertRaisesRegex(ValueError, "gap, duplicate, or out-of-order"):
            seed.build_seed_manifest(
                wrong_interval, interval="15m", now_ms=self.now_ms
            )

        invalid = self._rows(days=32)
        invalid[7]["close"] = "NaN"
        invalid_path = self._write(invalid, manifest=False)
        with self.assertRaisesRegex(ValueError, "finite decimal"):
            seed.build_seed_manifest(
                invalid_path, interval="1d", now_ms=self.now_ms
            )

    def test_source_manifest_mismatch_and_seed_tampering_fail_closed(self):
        path = self._write(self._rows(days=32))
        source_path = path.with_suffix(path.suffix + ".manifest.json")
        source = json.loads(source_path.read_text(encoding="utf-8"))
        source["dataset_sha256"] = "0" * 64
        source_path.write_text(json.dumps(source), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match the raw CSV"):
            seed.build_seed_manifest(path, now_ms=self.now_ms)

        path = self._write(self._rows(days=33))
        manifest = seed.build_seed_manifest(path, now_ms=self.now_ms)
        tampered = copy.deepcopy(manifest)
        tampered["samples"][0]["forward_return"] = "0.5"
        with self.assertRaisesRegex(ValueError, "immutable_sha256 does not match"):
            seed.verify_seed_manifest(tampered)


if __name__ == "__main__":
    unittest.main()
