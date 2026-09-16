import os
import unittest
from unittest.mock import patch

import binance_execution


class BinanceExecutionTests(unittest.TestCase):
    def test_hmac_signature_is_deterministic(self):
        payload, signature = binance_execution.sign(
            {"symbol": "BTCUSDT", "timestamp": 1}, "secret")
        self.assertEqual(payload, "symbol=BTCUSDT&timestamp=1")
        self.assertEqual(len(signature), 64)

    def test_quantity_rounds_down_to_step(self):
        self.assertEqual(str(binance_execution.floor_step("1.239", "0.01")), "1.23")

    def test_production_base_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Testnet"):
            binance_execution.Client(base_url="https://api.binance.com/api")

    def test_order_execution_fails_closed(self):
        client = binance_execution.Client("key", "secret")
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "fail-closed"):
            client.place_market_buy("5")

    def test_order_cap_is_enforced_before_network(self):
        client = binance_execution.Client("key", "secret")
        with patch.dict(os.environ, {"BINANCE_ORDER_EXECUTION_ENABLED": "testnet"}), \
                self.assertRaisesRegex(ValueError, "between 5 and 25"):
            client.place_market_buy("26")


if __name__ == "__main__":
    unittest.main()
