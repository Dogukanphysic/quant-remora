from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import btc_futures_live as f


FILTERS = [
    {"filterType": "PRICE_FILTER", "tickSize": "0.10", "minPrice": "0.10", "maxPrice": "1000000"},
    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100"},
    {"filterType": "MIN_NOTIONAL", "notional": "5"},
]


class FakeClient:
    identity = "futures-test-account"

    def __init__(self):
        self._position = Decimal("0")
        self._entry = Decimal("0")
        self.orders = []
        self.submitted = []
        self.multi_assets = False
        self.single_asset_configurations = 0
        self.leverage = f.LEVERAGE
        self.leverage_configurations = 0
        self.requests = []
        self.market_data = {
            "now": 1_800_000_000, "bar": 1_799_999_100_000,
            "bid": "85000", "ask": "85001", "mark": "85000", "atr": "500",
            "filters": FILTERS,
            "band": {"enter": False, "entry_regime": None, "lower": 84000.0,
                     "upper": 86000.0, "entry_limit": 84400.0, "rsi": 50.0},
        }

    def position_mode(self):
        return {"dualSidePosition": False}

    def multi_assets_mode(self):
        return {"multiAssetsMargin": self.multi_assets}

    def configure_single_asset_mode(self):
        self.multi_assets = False
        self.single_asset_configurations += 1
        return {"code": 200}

    def account(self):
        return {"canTrade": True, "multiAssetsMargin": False, "assets": [{
            "asset": "USDT", "availableBalance": "100", "walletBalance": "100"
        }]}

    def positions(self):
        return [{"symbol": f.SYMBOL, "positionAmt": str(self._position),
                 "positionSide": "BOTH", "marginType": "isolated",
                 "leverage": str(self.leverage),
                 "entryPrice": str(self._entry), "markPrice": "85000",
                 "unRealizedProfit": "0", "liquidationPrice": "43000"}]

    def open_orders(self):
        return list(self.orders)

    def configure_isolated_leverage(self):
        return f.Client.configure_isolated_leverage(self)

    def configure_leverage(self):
        return f.Client.configure_leverage(self)

    def leverage_bracket(self):
        return {"symbol": f.SYMBOL, "brackets": [{
            "notionalFloor": 0, "notionalCap": 1000000,
            "initialLeverage": 20, "maintMarginRatio": 0.005,
        }]}

    def request(self, method, path, params=None, signed=False):
        self.requests.append((method, path, params, signed))
        if (method, path) == ("POST", "/fapi/v1/marginType"):
            return {"code": 200}
        if (method, path) == ("POST", "/fapi/v1/leverage"):
            self.leverage_configurations += 1
            self.leverage = int(params["leverage"])
            return {"symbol": f.SYMBOL, "leverage": self.leverage,
                    "maxNotionalValue": "1000000"}
        raise AssertionError(f"Unexpected fake request: {method} {path}")

    def market(self):
        return dict(self.market_data)

    def submit_entry(self, quantity, client_id):
        self.submitted.append(("ENTRY", client_id, quantity))
        self._position = Decimal(quantity)
        self._entry = Decimal("85001")
        return {"status": "FILLED"}

    def lookup(self, client_id):
        for order in self.orders:
            if order["clientOrderId"] == client_id:
                return order
        raise f.ApiError("missing", 400, -2013)

    def lookup_protection(self, client_id):
        return self.lookup(client_id)

    def submit_protection(self, order_type, quantity, stop_price, client_id):
        self.submitted.append((order_type, client_id, quantity))
        self.orders.append({"clientOrderId": client_id, "type": order_type,
                            "side": "SELL", "reduceOnly": True,
                            "origQty": str(quantity), "status": "NEW"})
        return self.orders[-1]

    def cancel(self, client_id):
        self.orders = [row for row in self.orders if row["clientOrderId"] != client_id]
        return {"status": "CANCELED"}

    def cancel_protection(self, client_id):
        return self.cancel(client_id)


class FuturesLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "futures.sqlite3"
        self.patch = patch.object(f, "DB", self.path)
        self.patch.start()
        self.db = f.connect(self.path)

    def tearDown(self):
        self.db.close()
        self.patch.stop()
        self.temp.cleanup()

    def legacy_long(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"))
        state.update(contract="btc-usdm-isolated-2x-bollinger-long-v1",
                     leverage=2, phase="long", quantity="0.001",
                     entry_price="85000", stop_price="84000",
                     target_price="87000", stop_client_id="qrf-s-legacy",
                     target_client_id="qrf-t-legacy", filled_orders=1)
        client.leverage = 2
        client._position = Decimal("0.001")
        client._entry = Decimal("85000")
        client.orders = [
            {"clientOrderId": "qrf-s-legacy", "type": "STOP_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.001",
             "status": "NEW", "stopPrice": "84000"},
            {"clientOrderId": "qrf-t-legacy", "type": "TAKE_PROFIT_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.001",
             "status": "NEW", "stopPrice": "87000"},
        ]
        with self.db:
            f.write(self.db, state)
        client.requests.clear()
        return client, state

    def test_leverage_only_client_does_not_change_margin_or_position_mode(self):
        client = FakeClient()
        result = client.configure_leverage()
        self.assertEqual(result["leverage"], 4)
        self.assertEqual(client.requests, [
            ("POST", "/fapi/v1/leverage", {"symbol": f.SYMBOL, "leverage": 4}, True)
        ])

    def test_http_error_identifies_route_without_leaking_request_or_remote_body(self):
        client = object.__new__(f.Client)
        client.key = "dummy-api-key-do-not-leak"
        client.secret = "dummy-api-secret-do-not-leak"
        client.opener = Mock()
        payload = json.dumps({"code": -4067,
                              "msg": "untrusted-remote-body signature=private"}).encode()
        failure = HTTPError("https://example.invalid/?signature=private", 400,
                            "bad request", {}, io.BytesIO(payload))
        client.opener.open.side_effect = [
            io.BytesIO(b'{"serverTime": 1800000000000}'), failure]
        with self.assertRaises(f.ApiError) as caught:
            client.request("POST", "/fapi/v1/leverage",
                           {"symbol": f.SYMBOL, "leverage": 4}, True)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, -4067)
        self.assertIn("POST /fapi/v1/leverage", message)
        for forbidden in (client.key, client.secret, "signature=", "timestamp=",
                          "untrusted-remote-body", "example.invalid"):
            self.assertNotIn(forbidden, message)

    def test_transport_error_identifies_sanitized_route_and_retry_safety(self):
        client = object.__new__(f.Client)
        client.opener = Mock()
        for method, path, retry_safe in (
                ("GET", "/fapi/v1/time", True),
                ("POST", "/fapi/v1/leverage", False)):
            with self.subTest(method=method):
                client.opener.open.side_effect = URLError("signature=do-not-leak")
                with self.assertRaises(f.TransportError) as caught:
                    client.request(method, path)
                self.assertEqual(caught.exception.retry_safe, retry_safe)
                self.assertIn(f"{method} {path}", str(caught.exception))
                self.assertNotIn("do-not-leak", str(caught.exception))

    def test_quantity_uses_explicit_margin_and_fixed_4x(self):
        qty = f.order_quantity("50", "85000", FILTERS)
        self.assertEqual(qty, Decimal("0.002"))
        with self.assertRaisesRegex(ValueError, "selected 4x"):
            f.order_quantity("50", "85000", FILTERS, leverage=3)

    def test_account_uses_v2_for_can_trade_and_asset_mode_fields(self):
        client = object.__new__(f.Client)
        with patch.object(client, "request", return_value={"canTrade": True}) as request:
            self.assertTrue(client.account()["canTrade"])
        request.assert_called_once_with("GET", "/fapi/v2/account", signed=True)

    def test_protection_uses_current_algo_order_endpoint(self):
        client = object.__new__(f.Client)
        with patch.object(client, "request", return_value={"algoId": 1}) as request:
            result = client.submit_protection(
                "STOP_MARKET", Decimal("0.003"), Decimal("85000"), "qrf-s-test")
        self.assertEqual(result["algoId"], 1)
        method, path, params, signed = request.call_args.args
        self.assertEqual((method, path, signed),
                         ("POST", "/fapi/v1/algoOrder", True))
        self.assertEqual(params["algoType"], "CONDITIONAL")
        self.assertEqual(params["triggerPrice"], "85000")
        self.assertEqual(params["clientAlgoId"], "qrf-s-test")

    def test_protection_prices_are_outside_entry(self):
        stop, target = f.protection_prices(
            "85001", "500", FILTERS, "standard")
        self.assertEqual(stop, Decimal("84001.0"))
        self.assertEqual(target, Decimal("87001.0"))

    def test_moderate_profile_widens_stop_and_target_without_changing_rr(self):
        stop, target = f.protection_prices(
            "85001", "500", FILTERS, "moderate")
        self.assertEqual(stop, Decimal("83751.0"))
        self.assertEqual(target, Decimal("87501.0"))
        self.assertEqual(
            (Decimal("87501.0") - Decimal("85001")) /
            (Decimal("85001") - Decimal("83751.0")), Decimal("2"))

    def test_aggressive_profile_widens_stop_and_target_without_changing_rr(self):
        stop, target = f.protection_prices(
            "85001", "500", FILTERS, "aggressive")
        self.assertEqual(stop, Decimal("83501.0"))
        self.assertEqual(target, Decimal("88001.0"))
        self.assertEqual(
            (Decimal("88001.0") - Decimal("85001")) /
            (Decimal("85001") - Decimal("83501.0")), Decimal("2"))

    def test_configure_requires_flat_and_creates_separate_ledger(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"))
        self.assertEqual(state["contract"], f.CONTRACT)
        self.assertEqual(state["phase"], "cash")
        self.assertEqual(state["margin_usdt"], "50")
        self.assertEqual(state["risk_profile"], "moderate")
        self.assertEqual(state["stop_atr"], "2.5")
        self.assertEqual(state["target_atr"], "5")

    def test_all_available_mode_tracks_mode_and_current_balance(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"), True)
        self.assertEqual(state["allocation_mode"], "all_available")
        self.assertEqual(state["margin_usdt"], "100")

        client.market_data["bar"] += 900_000
        client.market_data["band"] = dict(client.market_data["band"],
                                          enter=True, entry_regime="reclaim")
        with patch.object(f, "ENTRY_VALIDATED", True):
            state = f.tick(self.db, client, Decimal("50"), True)
        self.assertEqual(state["last_entry_margin_usdt"], "100")
        self.assertEqual(state["quantity"], "0.004")

    def test_legacy_2x_ledger_migrates_to_4x_before_reconciliation(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"))
        state["contract"] = "btc-usdm-isolated-2x-bollinger-long-v1"
        state["leverage"] = 2
        client.leverage = 2
        with self.db:
            f.write(self.db, state)
        migrated = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(migrated["contract"], f.CONTRACT)
        self.assertEqual(migrated["leverage"], 4)
        self.assertEqual(client.leverage, 4)

    def test_open_legacy_2x_position_migrates_with_protections_intact(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"))
        state.update(contract="btc-usdm-isolated-2x-bollinger-long-v1",
                     leverage=2, phase="long", quantity="0.001",
                     entry_price="85000", stop_client_id="qrf-s-legacy",
                     target_client_id="qrf-t-legacy", filled_orders=1)
        client.leverage = 2
        client._position = Decimal("0.001")
        client._entry = Decimal("85000")
        client.orders = [
            {"clientOrderId": "qrf-s-legacy", "type": "STOP_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.001",
             "status": "NEW"},
            {"clientOrderId": "qrf-t-legacy", "type": "TAKE_PROFIT_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.001",
             "status": "NEW"},
        ]
        with self.db:
            f.write(self.db, state)
        reconciled = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(reconciled["contract"], f.CONTRACT)
        self.assertEqual(reconciled["leverage"], 4)
        self.assertIsNone(reconciled["leverage_migration_pending"])
        self.assertEqual(client.leverage, 4)
        self.assertEqual(client.leverage_configurations, 2)
        self.assertEqual(len(client.orders), 2)

    def test_open_legacy_position_refuses_migration_without_both_protections(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"))
        state.update(contract="btc-usdm-isolated-2x-bollinger-long-v1",
                     leverage=2, phase="long", quantity="0.001",
                     entry_price="85000", stop_client_id="qrf-s-legacy",
                     target_client_id="qrf-t-legacy", filled_orders=1)
        client.leverage = 2
        client._position = Decimal("0.001")
        client._entry = Decimal("85000")
        client.orders = [
            {"clientOrderId": "qrf-s-legacy", "type": "STOP_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.001",
             "status": "NEW"},
        ]
        with self.db:
            f.write(self.db, state)
        with self.assertRaises(ValueError):
            f.tick(self.db, client, Decimal("50"))
        unchanged = f.read(self.db)
        self.assertEqual(unchanged["leverage"], 2)
        self.assertEqual(unchanged["contract"], state["contract"])
        self.assertEqual(client.leverage_configurations, 1)

    def test_open_migration_calls_only_leverage_and_preserves_protection_orders(self):
        client, previous = self.legacy_long()
        original_orders = [dict(row) for row in client.orders]
        migrated = f.tick(self.db, client, Decimal("50"))
        self.assertEqual([(method, path) for method, path, *_ in client.requests],
                         [("POST", "/fapi/v1/leverage")])
        self.assertEqual(migrated["contract"], f.CONTRACT)
        self.assertEqual(migrated["leverage"], 4)
        self.assertEqual(migrated["quantity"], previous["quantity"])
        self.assertEqual(migrated["entry_price"], previous["entry_price"])
        self.assertEqual(client.orders, original_orders)
        self.assertEqual(client.submitted, [])

    def test_already_migrated_exchange_is_reconciled_without_repeating_post(self):
        client, _ = self.legacy_long()
        client.leverage = 4
        migrated = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(migrated["contract"], f.CONTRACT)
        self.assertEqual(migrated["leverage"], 4)
        self.assertEqual(client.requests, [])
        self.assertEqual(client.submitted, [])

    def test_rejected_migration_preserves_legacy_ledger_and_both_protections(self):
        client, previous = self.legacy_long()
        original_orders = [dict(row) for row in client.orders]
        with patch.object(client, "configure_leverage", side_effect=f.ApiError(
                "POST /fapi/v1/leverage rejected", 400, -4067)):
            with self.assertRaises(f.ApiError):
                f.tick(self.db, client, Decimal("50"))
        self.assertEqual(f.read(self.db), previous)
        self.assertEqual(client.leverage, 2)
        self.assertEqual(client.orders, original_orders)

    def test_success_response_without_4x_readback_does_not_migrate_ledger(self):
        client, previous = self.legacy_long()
        original_orders = [dict(row) for row in client.orders]
        with patch.object(client, "configure_leverage", return_value={
                "symbol": f.SYMBOL, "leverage": 4, "maxNotionalValue": "1000000"}):
            with self.assertRaises(ValueError):
                f.tick(self.db, client, Decimal("50"))
        self.assertEqual(f.read(self.db), previous)
        self.assertEqual(client.orders, original_orders)

    def test_failed_post_migration_read_is_reconciled_without_repeating_mutation(self):
        client, previous = self.legacy_long()
        original_positions = client.positions
        calls = 0

        def positions():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise f.TransportError("GET position read failed", retry_safe=True)
            return original_positions()

        with patch.object(client, "positions", side_effect=positions):
            with self.assertRaises(f.TransportError):
                f.tick(self.db, client, Decimal("50"))
        self.assertEqual(f.read(self.db), previous)
        self.assertEqual(client.leverage, 4)
        self.assertEqual(len(client.requests), 1)
        reconciled = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(reconciled["contract"], f.CONTRACT)
        self.assertEqual(len(client.requests), 1)

    def test_applied_leverage_with_lost_post_response_is_not_retried(self):
        client, previous = self.legacy_long()
        original_orders = [dict(row) for row in client.orders]
        configure_leverage = client.configure_leverage

        def apply_then_lose_response():
            configure_leverage()
            raise f.TransportError("POST leverage outcome unknown", retry_safe=False)

        with patch.object(client, "configure_leverage", side_effect=apply_then_lose_response):
            with self.assertRaises(f.TransportError) as caught:
                f.worker_step(self.db, client, Decimal("50"))
        self.assertFalse(caught.exception.retry_safe)
        self.assertEqual(client.leverage, 4)
        self.assertEqual(f.read(self.db), previous)
        self.assertEqual(client.orders, original_orders)
        self.assertEqual(len(client.requests), 1)

        reconciled, retrying = f.worker_step(self.db, client, Decimal("50"))
        self.assertFalse(retrying)
        self.assertEqual(reconciled["contract"], f.CONTRACT)
        self.assertEqual(reconciled["leverage"], 4)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.orders, original_orders)

    def test_migration_obeys_halt_allocation_and_account_mode_before_mutation(self):
        client, previous = self.legacy_long()
        for condition in ("halt", "allocation", "account_mode"):
            with self.subTest(condition=condition):
                state = dict(previous)
                if condition == "halt":
                    state["halted"] = "OperatorReviewRequired"
                with self.db:
                    f.write(self.db, state)
                if condition == "halt":
                    result = f.tick(self.db, client, Decimal("50"))
                    self.assertEqual(result["halted"], state["halted"])
                elif condition == "allocation":
                    with self.assertRaises(ValueError):
                        f.tick(self.db, client, Decimal("49"))
                else:
                    with patch.object(client, "position_mode", return_value={
                            "dualSidePosition": True}):
                        with self.assertRaises(ValueError):
                            f.tick(self.db, client, Decimal("50"))
                self.assertEqual(client.requests, [])
                self.assertEqual(f.read(self.db), state)

    def test_inconsistent_open_position_cannot_trigger_leverage_migration(self):
        client, previous = self.legacy_long()
        original_position = client.positions()[0]
        mutations = (
            {"positionAmt": "0.002"}, {"entryPrice": "84000"},
            {"positionSide": "LONG"}, {"marginType": "cross"},
            {"leverage": "3"},
        )
        for change in mutations:
            with self.subTest(change=change):
                with patch.object(client, "positions", return_value=[
                        dict(original_position, **change)]):
                    with self.assertRaises(ValueError):
                        f.tick(self.db, client, Decimal("50"))
                self.assertEqual(client.requests, [])
                self.assertEqual(f.read(self.db), previous)

    def test_pending_or_extra_orders_block_migration_before_mutation(self):
        client, previous = self.legacy_long()
        original_orders = list(client.orders)
        for condition in ("pending", "external_order", "extra_owned_order"):
            with self.subTest(condition=condition):
                state = dict(previous)
                client.orders = list(original_orders)
                if condition == "pending":
                    state["pending_entry"] = "qrf-e-pending"
                else:
                    client.orders.append({
                        "clientOrderId": ("qrf-s-extra" if condition == "extra_owned_order"
                                          else "external-order"),
                        "type": "STOP_MARKET", "side": "SELL", "reduceOnly": True,
                        "origQty": "0.001", "status": "NEW"})
                with self.db:
                    f.write(self.db, state)
                with self.assertRaises(ValueError):
                    f.tick(self.db, client, Decimal("50"))
                self.assertEqual(client.requests, [])
                self.assertEqual(f.read(self.db), state)

    def test_protection_disappearing_during_migration_blocks_ledger_commit(self):
        client, previous = self.legacy_long()
        configure_leverage = client.configure_leverage

        def migrate_with_protection_removed():
            result = configure_leverage()
            client.orders.pop()
            return result

        with patch.object(client, "configure_leverage",
                          side_effect=migrate_with_protection_removed):
            with self.assertRaises(ValueError):
                f.tick(self.db, client, Decimal("50"))
        self.assertEqual(f.read(self.db), previous)
        self.assertEqual(client.leverage, 4)
        self.assertEqual(client.submitted, [])

    def test_protection_fill_during_migration_reconciles_closed_position(self):
        client, previous = self.legacy_long()
        configure_leverage = client.configure_leverage
        lookup_protection = client.lookup_protection

        def migrate_then_stop_fills():
            result = configure_leverage()
            client._position = Decimal("0")
            client.orders = [row for row in client.orders
                             if row["clientOrderId"] != previous["stop_client_id"]]
            return result

        def lookup(client_id):
            if client_id == previous["stop_client_id"]:
                return {"type": "STOP_MARKET", "status": "FILLED"}
            return lookup_protection(client_id)

        with patch.object(client, "configure_leverage", side_effect=migrate_then_stop_fills), \
                patch.object(client, "lookup_protection", side_effect=lookup):
            reconciled = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(reconciled["contract"], f.CONTRACT)
        self.assertEqual(reconciled["phase"], "cash")
        self.assertEqual(reconciled["quantity"], "0")
        self.assertEqual(reconciled["completed_round_trips"], 1)
        self.assertEqual(reconciled["filled_orders"], 2)
        self.assertEqual(client.orders, [])
        self.assertEqual(client.submitted, [])

    def test_read_transport_failure_is_recorded_and_can_recover(self):
        client = FakeClient()
        f.configure(self.db, client, Decimal("50"))
        original_market = client.market
        calls = {"count": 0}

        def flaky_market():
            calls["count"] += 1
            if calls["count"] == 1:
                raise f.TransportError("read failed", retry_safe=True)
            return original_market()

        client.market = flaky_market
        state, retrying = f.worker_step(self.db, client, Decimal("50"))
        self.assertTrue(retrying)
        self.assertEqual(state["transient_failures"], 1)
        self.assertEqual(state["last_error"],
                         "retryable_get_transport_failure")

        state, retrying = f.worker_step(self.db, client, Decimal("50"))
        self.assertFalse(retrying)
        self.assertEqual(state["transient_failures"], 0)
        self.assertIsNone(state["last_error"])

    def test_mutating_transport_failure_is_never_retried(self):
        client = FakeClient()
        f.configure(self.db, client, Decimal("50"))
        with patch.object(f, "tick", side_effect=f.TransportError(
                "unknown order outcome", retry_safe=False)):
            with self.assertRaises(f.TransportError):
                f.worker_step(self.db, client, Decimal("50"))

    def test_all_available_diagnose_reports_dynamic_allocation(self):
        client = FakeClient()
        client.commission = lambda: {"symbol": f.SYMBOL}
        report = f.diagnose(client, Decimal("50"), True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["allocation_mode"], "all_available")
        self.assertEqual(report["checks"]["account_balance"]["allocation_usdt"],
                         "100")

    def test_configure_switches_flat_account_to_single_asset_mode(self):
        client = FakeClient()
        client.multi_assets = True
        state = f.configure(self.db, client, Decimal("50"), True)
        self.assertEqual(client.single_asset_configurations, 1)
        self.assertEqual(state["allocation_mode"], "all_available")

    def test_first_run_can_configure_an_empty_ledger(self):
        client = FakeClient()
        self.assertIsNone(f.read(self.db))
        state = f.configure(self.db, client, Decimal("50"))
        state = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(state["phase"], "cash")
        self.assertEqual(state["contract"], f.CONTRACT)

    def test_status_reports_empty_existing_ledger_as_not_configured(self):
        status = f.public_status(self.path)
        self.assertEqual(status["status"], "not_configured")
        self.assertTrue(status["ledger_present"])

    def test_diagnose_has_aggregate_ok(self):
        client = FakeClient()
        client.commission = lambda: {"symbol": f.SYMBOL}
        report = f.diagnose(client, Decimal("50"))
        self.assertTrue(report["ok"])

    def test_signal_opens_long_and_confirms_two_reduce_only_protections(self):
        client = FakeClient()
        f.configure(self.db, client, Decimal("50"))
        client.market_data["bar"] += 900_000
        client.market_data["band"] = dict(client.market_data["band"],
                                          enter=True, entry_regime="reclaim")
        with patch.object(f, "ENTRY_VALIDATED", True):
            state = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(state["phase"], "long")
        self.assertGreater(Decimal(state["quantity"]), 0)
        self.assertEqual({row["type"] for row in client.orders},
                         {"STOP_MARKET", "TAKE_PROFIT_MARKET"})
        self.assertTrue(all(row["reduceOnly"] for row in client.orders))

    def test_unvalidated_signal_is_recorded_but_cannot_open_mainnet_long(self):
        client = FakeClient()
        f.configure(self.db, client, Decimal("50"))
        client.market_data["bar"] += 900_000
        client.market_data["band"] = dict(client.market_data["band"],
                                          enter=True, entry_regime="trend_reclaim")
        state = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(state["phase"], "cash")
        self.assertTrue(state["decision"]["raw_enter"])
        self.assertFalse(state["decision"]["enter"])
        self.assertEqual(state["entry_gate_reason"], f.ENTRY_GATE_REASON)
        self.assertEqual(client.submitted, [])

    def test_exploratory_mode_can_open_only_on_raw_signal(self):
        client = FakeClient()
        f.configure(self.db, client, Decimal("50"))
        client.market_data["bar"] += 900_000
        client.market_data["band"] = dict(client.market_data["band"],
                                          enter=True,
                                          entry_regime="trend_reclaim")
        with patch.object(f, "ENTRY_VALIDATED", True), \
             patch.object(f, "ENTRY_GATE_REASON", None), \
             patch.object(f, "ENTRY_MODE", "exploratory"), \
             patch.object(f, "LOSS_LIMIT_RATE", Decimal("0.10")):
            state = f.tick(self.db, client, Decimal("50"))
        self.assertEqual(state["phase"], "long")
        self.assertEqual(state["entry_mode"], "exploratory")
        self.assertEqual(state["loss_limit_rate"], "0.10")
        self.assertTrue(state["decision"]["enter"])

    def test_new_worker_recovers_entry_gate_migration_without_opening(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"))
        state["halted"] = f.ENTRY_GATE_MIGRATION_HALT
        with self.db:
            f.write(self.db, state)
        client.market_data["bar"] += 900_000
        client.market_data["band"] = dict(client.market_data["band"],
                                          enter=True, entry_regime="trend_reclaim")
        migrated = f.tick(self.db, client, Decimal("50"))
        self.assertIsNone(migrated["halted"])
        self.assertFalse(migrated["entry_validation_passed"])
        self.assertEqual(migrated["entry_gate_reason"], f.ENTRY_GATE_REASON)
        self.assertEqual(migrated["phase"], "cash")
        self.assertEqual(client.submitted, [])

    def test_closed_trade_starts_four_bar_entry_cooldown(self):
        client = FakeClient()
        state = f.configure(self.db, client, Decimal("50"))
        state.update(phase="long", quantity="0.001", entry_price="85000",
                     stop_client_id="qrf-s-test", target_client_id="qrf-t-test")
        client.orders = [
            {"clientOrderId": "qrf-s-test", "type": "STOP_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.001",
             "status": "NEW"},
            {"clientOrderId": "qrf-t-test", "type": "TAKE_PROFIT_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.001",
             "status": "NEW"},
        ]
        with self.db:
            f.write(self.db, state)
        closed = f.close_epoch(self.db, client, state, client.market_data,
                               Decimal("99"))
        self.assertEqual(closed["last_exit_bar"], client.market_data["bar"])
        self.assertEqual(closed["cooldown_until_bar"],
                         client.market_data["bar"] + 4 * 900_000)

    def test_untracked_position_is_rejected(self):
        client = FakeClient()
        f.configure(self.db, client, Decimal("50"))
        client._position = Decimal("0.001")
        client._entry = Decimal("85000")
        with self.assertRaisesRegex(ValueError, "Untracked"):
            f.tick(self.db, client, Decimal("50"))

    def test_pending_entry_can_close_if_protection_filled_during_restart(self):
        client = FakeClient()
        f.configure(self.db, client, Decimal("50"))
        client.market_data["bar"] += 900_000
        client.market_data["band"] = dict(client.market_data["band"],
                                          enter=True, entry_regime="reclaim")
        with patch.object(f, "ENTRY_VALIDATED", True):
            state = f.tick(self.db, client, Decimal("50"))
        # Recreate the durable pre-finalization state and simulate a stop fill.
        state["pending_entry"] = state["entry_client_id"]
        client.orders.append({"clientOrderId": state["entry_client_id"],
                              "type": "MARKET", "status": "FILLED"})
        client._position = Decimal("0")
        with self.db:
            f.write(self.db, state)
        recovered = f.recover_pending_entry(self.db, client, state,
                                            client.market_data)
        self.assertEqual(recovered["phase"], "cash")
        self.assertEqual(recovered["completed_round_trips"], 1)
        self.assertEqual(recovered["unrealized_pnl_usdt"], "0")


if __name__ == "__main__":
    unittest.main()
