import csv
import hashlib
from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import binance_archive


def zipped(rows, name="BTCUSDT-15m-2026-08.csv"):
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr(name, "\n".join(",".join(map(str, row)) for row in rows))
    return stream.getvalue()


class BinanceArchiveTests(unittest.TestCase):
    def test_official_um_url_and_month_range(self):
        self.assertEqual(
            binance_archive.archive_url("um", "btcusdt", "2026-08"),
            "https://data.binance.vision/data/futures/um/monthly/klines/"
            "BTCUSDT/15m/BTCUSDT-15m-2026-08.zip",
        )
        self.assertEqual(
            binance_archive.months_between("2025-12", "2026-02"),
            ["2025-12", "2026-01", "2026-02"],
        )

    def test_verified_download_normalizes_microseconds(self):
        first = 1_785_715_200_000_000
        blob = zipped([
            [first, 100, 102, 99, 101, 12, 0, 0, 0, 0, 0, 0],
            [first + 900_000_000, 101, 103, 100, 102, 13, 0, 0, 0, 0, 0, 0],
        ])
        filename = "BTCUSDT-15m-2026-08.zip"
        checksum = f"{hashlib.sha256(blob).hexdigest()}  {filename}\n".encode()
        with tempfile.TemporaryDirectory() as folder, patch(
                "binance_archive._download", side_effect=[blob, checksum]):
            out = Path(folder) / "data.csv"
            result = binance_archive.download_monthly(
                "um", "BTCUSDT", "2026-08", "2026-08", out)
            self.assertEqual(result["candles"], 2)
            rows = binance_archive.read_dataset(out)
            self.assertEqual(rows[0]["ts"], first // 1000)
            self.assertTrue(out.with_suffix(".csv.manifest.json").is_file())

    def test_checksum_mismatch_fails_without_output(self):
        blob = zipped([[1_785_715_200_000, 100, 102, 99, 101, 12]])
        checksum = ("0" * 64 + "  BTCUSDT-15m-2026-08.zip\n").encode()
        with tempfile.TemporaryDirectory() as folder, patch(
                "binance_archive._download", side_effect=[blob, checksum]):
            out = Path(folder) / "data.csv"
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                binance_archive.download_monthly(
                    "um", "BTCUSDT", "2026-08", "2026-08", out)
            self.assertFalse(out.exists())

    def test_gap_is_rejected(self):
        rows = [
            {"ts": 1_785_715_200_000, "open": 100., "high": 102., "low": 99.,
             "close": 101., "volume": 1., "exchange": "binance_um", "symbol": "BTCUSDT"},
            {"ts": 1_785_717_000_000, "open": 101., "high": 103., "low": 100.,
             "close": 102., "volume": 1., "exchange": "binance_um", "symbol": "BTCUSDT"},
        ]
        with self.assertRaisesRegex(ValueError, "eksik"):
            binance_archive.validate_dataset(rows, "um", "BTCUSDT")

    def test_spot_rest_payload_is_validated(self):
        ts = 1_785_715_200_000
        payload = [[
            ts, "100", "102", "99", "101", "12", ts + 899_999,
            "0", 4, "0", "0", "0",
        ]]
        rows = binance_archive._rest_rows(payload, "BTCUSDT")
        self.assertEqual(rows[0]["exchange"], "binance_spot")
        self.assertEqual(rows[0]["ts"], ts)
        payload[0][6] += 1
        with self.assertRaisesRegex(ValueError, "kapanış"):
            binance_archive._rest_rows(payload, "BTCUSDT")

    def test_basis_features_are_timestamp_aligned_and_causal(self):
        def rows(exchange, closes):
            return [{
                "ts": 1_785_715_200_000 + index * 900_000,
                "open": close, "high": close, "low": close, "close": close,
                "volume": 1., "exchange": exchange, "symbol": "BTCUSDT",
            } for index, close in enumerate(closes)]
        spot = rows("binance_spot", [100.] * 5)
        futures = rows("binance_um", [101., 101., 101., 101., 102.])
        features = binance_archive._basis_features(spot, futures)
        last = features[futures[-1]["ts"]]
        self.assertAlmostEqual(last[0], .02)
        self.assertAlmostEqual(last[2], .01)
        futures[-1]["ts"] += 900_000
        with self.assertRaisesRegex(ValueError, "zaman"):
            binance_archive._basis_features(spot, futures)


if __name__ == "__main__":
    unittest.main()
