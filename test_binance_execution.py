from io import BytesIO
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import binance_execution


def exchange_info(*, market_step="0.00001000", min_notional="5.00000000"):
    return {
        "symbols": [{
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.00001000",
                 "maxQty": "9000.00000000", "stepSize": "0.00001000"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000",
                 "maxQty": "100.00000000", "stepSize": market_step},
                {"filterType": "NOTIONAL", "minNotional": min_notional},
            ],
        }]
    }


class BinanceExecutionTests(unittest.TestCase):
    def test_hmac_signature_is_deterministic(self):
        payload, signature = binance_execution.sign(
            {"symbol": "BTCUSDT", "timestamp": 1}, "secret")
        self.assertEqual(payload, "symbol=BTCUSDT&timestamp=1")
        self.assertEqual(len(signature), 64)

    def test_quantity_rounds_down_to_step(self):
        self.assertEqual(str(binance_execution.floor_step("1.239", "0.01")), "1.23")

    def test_non_finite_quantity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            binance_execution.floor_step("NaN", "0.01")

    def test_symbol_rules_parse_market_step_and_notional(self):
        rules = binance_execution.parse_symbol_rules(exchange_info())
        self.assertEqual(rules.base_asset, "BTC")
        self.assertEqual(rules.step_size, binance_execution.Decimal("0.00001000"))
        self.assertEqual(rules.min_notional, binance_execution.Decimal("5.00000000"))
        self.assertEqual(rules.floor_quantity("0.00001999"),
                         binance_execution.Decimal("0.00001000"))

    def test_zero_market_step_safely_falls_back_to_lot_size(self):
        rules = binance_execution.parse_symbol_rules(exchange_info(market_step="0"))
        self.assertEqual(rules.step_size, binance_execution.Decimal("0.00001000"))

    def test_production_base_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Testnet"):
            binance_execution.Client(base_url="https://api.binance.com/api")

    def test_public_market_client_has_no_production_execution_surface(self):
        client = binance_execution.PublicMarketDataClient()
        for attribute in (
            "api_key", "secret", "account", "open_orders", "order_by_client_id",
            "test_market_buy", "place_market_buy", "place_market_sell",
        ):
            with self.subTest(attribute=attribute):
                self.assertFalse(hasattr(client, attribute))
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            binance_execution.PublicMarketDataClient(("https://example.com/api",))

    def test_public_market_klines_are_get_only_validated_and_closed(self):
        client = binance_execution.PublicMarketDataClient()
        closed = [0, "100", "110", "90", "105", "12", 999_999]
        current = [1_000_000, "105", "115", "100", "110", "8", 1_059_999]
        response = BytesIO(json.dumps([closed, current]).encode("utf-8"))
        with patch.object(binance_execution, "urlopen", return_value=response) as opener, \
                patch.object(binance_execution.time, "time", return_value=1000):
            result = client.klines(interval="1m", limit=1, symbol="BTCUSDT")
        self.assertEqual(result, [closed])
        request = opener.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.startswith(
            "https://data-api.binance.vision/api/v3/klines?"
        ))
        self.assertIn("symbol=BTCUSDT&interval=1m&limit=2", request.full_url)
        self.assertNotIn("signature=", request.full_url)
        self.assertIsNone(request.get_header("X-MBX-APIKEY"))

    def test_public_market_klines_fall_back_after_rate_limit(self):
        client = binance_execution.PublicMarketDataClient()
        closed = [0, "100", "110", "90", "105", "12", 999_999]
        rate_limited = HTTPError(
            "https://data-api.binance.vision/api/v3/klines", 429,
            "rate limit", {}, BytesIO(b'{"code":-1003}'),
        )
        response = BytesIO(json.dumps([closed]).encode("utf-8"))
        with patch.object(
            binance_execution, "urlopen", side_effect=[rate_limited, response]
        ) as opener, patch.object(binance_execution.time, "time", return_value=1000):
            self.assertEqual(client.klines(limit=1), [closed])
        self.assertEqual(opener.call_count, 2)
        self.assertTrue(opener.call_args_list[0].args[0].full_url.startswith(
            "https://data-api.binance.vision/api/"
        ))
        self.assertTrue(opener.call_args_list[1].args[0].full_url.startswith(
            "https://api.binance.com/api/"
        ))

    def test_public_market_klines_reject_invalid_ohlcv(self):
        client = binance_execution.PublicMarketDataClient()
        invalid = [0, "100", "101", "90", "105", "12", 999_999]
        with patch.object(client, "_fetch_klines", return_value=[invalid]), \
                patch.object(binance_execution.time, "time", return_value=1000), \
                self.assertRaisesRegex(ValueError, "OHLCV"):
            client.klines(limit=1)

    def test_order_execution_fails_closed(self):
        client = binance_execution.Client("key", "secret")
        with patch.object(client, "_request") as request, \
                patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(ValueError, "fail-closed"):
            client.place_market_buy("5", client_order_id="remora-buy-1")
        request.assert_not_called()

    def test_sell_execution_fails_closed_before_public_requests(self):
        client = binance_execution.Client("key", "secret")
        with patch.object(client, "_request") as request, \
                patch.dict(os.environ, {}, clear=True), \
                self.assertRaisesRegex(ValueError, "fail-closed"):
            client.place_market_sell("0.001", client_order_id="remora-sell-1")
        request.assert_not_called()

    def test_order_cap_is_enforced_before_network(self):
        client = binance_execution.Client("key", "secret")
        with patch.dict(os.environ, {"BINANCE_ORDER_EXECUTION_ENABLED": "testnet"}), \
                self.assertRaisesRegex(ValueError, "between 5 and 25"):
            client.place_market_buy("26", client_order_id="remora-buy-cap")

    def test_actual_orders_require_explicit_client_order_id(self):
        client = binance_execution.Client("key", "secret")
        with self.assertRaises(TypeError):
            client.place_market_buy("5")
        with self.assertRaises(TypeError):
            client.place_market_sell("0.001")

    def test_closed_klines_omit_current_candle(self):
        client = binance_execution.Client()
        closed = [0, "1", "2", "0", "1", "3", 999_999]
        current = [1_000_000, "1", "2", "0", "1", "3", 1_059_999]
        with patch.object(client, "_request", return_value=[closed, current]) as request, \
                patch.object(binance_execution.time, "time", return_value=1000):
            result = client.klines("1m", 1)
        self.assertEqual(result, [closed])
        request.assert_called_once_with(
            "GET", "/v3/klines",
            {"symbol": "BTCUSDT", "interval": "1m", "limit": 2},
        )

    def test_book_ticker_uses_public_endpoint(self):
        client = binance_execution.Client()
        ticker = {"symbol": "BTCUSDT", "bidPrice": "59999.9", "askPrice": "60000.1"}
        with patch.object(client, "_request", return_value=ticker) as request:
            self.assertIs(client.book_ticker(), ticker)
        request.assert_called_once_with(
            "GET", "/v3/ticker/bookTicker", {"symbol": "BTCUSDT"}
        )

    def test_order_query_uses_orig_client_order_id(self):
        client = binance_execution.Client("key", "secret")
        order = {"orderId": 42, "clientOrderId": "remora-buy-42"}
        with patch.object(client, "_request", return_value=order) as request:
            self.assertIs(client.order_by_client_id("remora-buy-42"), order)
        request.assert_called_once_with(
            "GET", "/v3/order",
            {"symbol": "BTCUSDT", "origClientOrderId": "remora-buy-42"}, True,
        )

    def test_filled_order_recovery_normalizes_my_trades_fills(self):
        client = binance_execution.Client("key", "secret")
        order = {
            "symbol": "BTCUSDT", "orderId": 42,
            "clientOrderId": "remora-buy-42", "status": "FILLED",
            "side": "BUY", "executedQty": "0.00150000",
        }
        trades = [
            {
                "symbol": "BTCUSDT", "id": 501, "orderId": 42,
                "price": "60000.00", "qty": "0.00100000",
                "commission": "0.00000100", "commissionAsset": "BTC",
            },
            {
                "symbol": "BTCUSDT", "id": 502, "orderId": 42,
                "price": "60001.00", "qty": "0.00050000",
                "commission": "0.03", "commissionAsset": "USDT",
            },
        ]
        with patch.object(client, "_request", side_effect=[order, trades]) as request:
            recovered = client.reconcile_filled_order("remora-buy-42")

        self.assertNotIn("fills", order)
        self.assertEqual(recovered["fills"], [
            {
                "price": "60000.00", "qty": "0.00100000",
                "commission": "0.00000100", "commissionAsset": "BTC",
                "tradeId": 501,
            },
            {
                "price": "60001.00", "qty": "0.00050000",
                "commission": "0.03", "commissionAsset": "USDT",
                "tradeId": 502,
            },
        ])
        self.assertEqual(
            binance_execution.net_base_quantity(recovered),
            binance_execution.Decimal("0.00149900"),
        )
        self.assertEqual(request.call_args_list[1].args, (
            "GET", "/v3/myTrades", {"symbol": "BTCUSDT", "orderId": 42}, True,
        ))

    def test_filled_order_recovery_rejects_quantity_mismatch(self):
        client = binance_execution.Client("key", "secret")
        order = {
            "symbol": "BTCUSDT", "orderId": 42,
            "clientOrderId": "remora-sell-42", "status": "FILLED",
            "side": "SELL", "executedQty": "0.00150000",
        }
        trades = [{
            "symbol": "BTCUSDT", "id": 503, "orderId": 42,
            "price": "60000", "qty": "0.00100000",
            "commission": "0.06", "commissionAsset": "USDT",
        }]
        with patch.object(client, "_request", side_effect=[order, trades]), \
                self.assertRaisesRegex(ValueError, "do not match"):
            client.reconcile_filled_order("remora-sell-42")

    def test_trades_for_order_rejects_trade_from_another_order(self):
        client = binance_execution.Client("key", "secret")
        trades = [{
            "symbol": "BTCUSDT", "id": 504, "orderId": 43,
            "price": "60000", "qty": "0.00100000",
            "commission": "0", "commissionAsset": "USDT",
        }]
        with patch.object(client, "_request", return_value=trades), \
                self.assertRaisesRegex(ValueError, "different order"):
            client.trades_for_order(42)

    def test_market_buy_preserves_deterministic_client_id(self):
        client = binance_execution.Client("key", "secret")
        order = {"orderId": 43, "clientOrderId": "remora-buy-43", "fills": []}
        with patch.object(client, "_request", return_value=order) as request, \
                patch.dict(os.environ, {"BINANCE_ORDER_EXECUTION_ENABLED": "testnet"}):
            self.assertIs(
                client.place_market_buy("5", client_order_id="remora-buy-43"), order
            )
        method, path, params, signed = request.call_args.args
        self.assertEqual((method, path, signed), ("POST", "/v3/order", True))
        self.assertEqual(params["newClientOrderId"], "remora-buy-43")
        self.assertEqual(params["quoteOrderQty"], "5")
        self.assertEqual(params["newOrderRespType"], "FULL")

    def test_market_sell_floors_quantity_and_checks_min_notional(self):
        client = binance_execution.Client("key", "secret")
        ticker = {"symbol": "BTCUSDT", "bidPrice": "60000", "askPrice": "60001"}
        order = {"orderId": 44, "clientOrderId": "remora-sell-44", "fills": []}
        with patch.object(client, "_request",
                          side_effect=[exchange_info(), ticker, order]) as request, \
                patch.dict(os.environ, {"BINANCE_ORDER_EXECUTION_ENABLED": "testnet"}):
            self.assertIs(
                client.place_market_sell("0.00009999", client_order_id="remora-sell-44"),
                order,
            )
        method, path, params, signed = request.call_args_list[-1].args
        self.assertEqual((method, path, signed), ("POST", "/v3/order", True))
        self.assertEqual(params["quantity"], "0.00009000")
        self.assertEqual(params["newClientOrderId"], "remora-sell-44")

    def test_market_sell_rejects_below_min_notional_without_post(self):
        client = binance_execution.Client("key", "secret")
        ticker = {"symbol": "BTCUSDT", "bidPrice": "60000", "askPrice": "60001"}
        with patch.object(client, "_request",
                          side_effect=[exchange_info(), ticker]) as request, \
                patch.dict(os.environ, {"BINANCE_ORDER_EXECUTION_ENABLED": "testnet"}), \
                self.assertRaisesRegex(ValueError, "notional"):
            client.place_market_sell("0.00005", client_order_id="remora-sell-small")
        self.assertEqual(request.call_count, 2)

    def test_net_base_quantity_subtracts_only_base_asset_commission(self):
        order = {
            "side": "BUY",
            "executedQty": "0.00150000",
            "fills": [
                {"qty": "0.00100000", "commission": "0.00000100",
                 "commissionAsset": "BTC"},
                {"qty": "0.00050000", "commission": "0.02",
                 "commissionAsset": "USDT"},
            ],
        }
        self.assertEqual(
            binance_execution.net_base_quantity(order),
            binance_execution.Decimal("0.00149900"),
        )

    def test_post_url_error_is_ambiguous_and_never_retried(self):
        client = binance_execution.Client()
        with patch.object(binance_execution, "urlopen",
                          side_effect=URLError("connection reset")) as opener, \
                self.assertRaisesRegex(binance_execution.AmbiguousOrderError,
                                       "reconcile"):
            client._request("POST", "/v3/order", {"symbol": "BTCUSDT"})
        self.assertEqual(opener.call_count, 1)

    def test_post_http_5xx_is_ambiguous_and_never_retried(self):
        client = binance_execution.Client()
        error = HTTPError(
            "https://testnet.binance.vision/api/v3/order", 503, "unavailable", {},
            BytesIO(b'{"code":-1000,"msg":"unknown"}'),
        )
        with patch.object(binance_execution, "urlopen", side_effect=error) as opener, \
                self.assertRaisesRegex(binance_execution.AmbiguousOrderError,
                                       "reconcile"):
            client._request("POST", "/v3/order", {"symbol": "BTCUSDT"})
        self.assertEqual(opener.call_count, 1)

    def test_get_http_418_429_and_5xx_are_transport_errors(self):
        client = binance_execution.Client()
        for status in (418, 429, 503):
            with self.subTest(status=status):
                error = HTTPError(
                    "https://testnet.binance.vision/api/v3/time", status,
                    "transient", {}, BytesIO(b'{"code":-1003,"msg":"retry later"}'),
                )
                with patch.object(binance_execution, "urlopen",
                                  side_effect=error) as opener, \
                        self.assertRaisesRegex(
                            binance_execution.BinanceTransportError,
                            f"Binance HTTP {status}",
                        ):
                    client._request("GET", "/v3/time")
                self.assertEqual(opener.call_count, 1)

    def test_explicit_http_4xx_remains_value_error(self):
        client = binance_execution.Client()
        for method, status in (("GET", 401), ("POST", 400)):
            with self.subTest(method=method, status=status):
                error = HTTPError(
                    "https://testnet.binance.vision/api/v3/order", status,
                    "invalid", {}, BytesIO(b'{"code":-2015,"msg":"invalid"}'),
                )
                with patch.object(binance_execution, "urlopen", side_effect=error), \
                        self.assertRaises(ValueError) as caught:
                    client._request(method, "/v3/order")
                self.assertNotIsInstance(
                    caught.exception,
                    (binance_execution.BinanceTransportError,
                     binance_execution.AmbiguousOrderError),
                )


if __name__ == "__main__":
    unittest.main()
