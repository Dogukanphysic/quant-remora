"""Offline regressions for explicit 10x selection and protected 4x migration."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import copy
from decimal import Decimal
import os
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import btc_futures_live as f
from test_btc_futures_live import FakeClient, FILTERS


CONTRACT_2 = "btc-usdm-isolated-2x-bollinger-long-v1"
CONTRACT_4 = "btc-usdm-isolated-4x-bollinger-long-v2"
CONTRACT_10 = "btc-usdm-isolated-10x-bollinger-long-v3"


def bracket_payload(cap="50000", maximum=20):
    return [{"symbol": "BTCUSDT", "brackets": [{
        "bracket": 1, "initialLeverage": maximum,
        "notionalFloor": "0", "notionalCap": cap,
        "maintMarginRatio": "0.004", "cum": "0",
    }]}]


@contextmanager
def selected_leverage(leverage):
    contract = CONTRACT_10 if leverage == 10 else CONTRACT_4
    legacy = {CONTRACT_2, CONTRACT_4} if leverage == 10 else {CONTRACT_2}
    with ExitStack() as stack:
        stack.enter_context(patch.object(f, "LEVERAGE", leverage))
        stack.enter_context(patch.object(f, "CONTRACT", contract))
        stack.enter_context(patch.object(f, "LEGACY_CONTRACTS", legacy))
        yield


class LeverageClient(FakeClient):
    """Only in-memory responses; no HTTP or exchange credentials."""

    def __init__(self):
        super().__init__()
        self.brackets = bracket_payload()
        self.leverage_cap = "50000"
        self.liquidation = "40000"

    def leverage_bracket(self):
        return copy.deepcopy(self.brackets)

    def positions(self):
        rows = super().positions()
        rows[0]["liquidationPrice"] = self.liquidation
        return rows

    def request(self, method, path, params=None, signed=False):
        result = super().request(method, path, params, signed)
        if (method, path) == ("POST", "/fapi/v1/leverage"):
            result["maxNotionalValue"] = self.leverage_cap
        return result


class LeverageSelectionTests(unittest.TestCase):
    def test_import_defaults_to_four_and_ten_requires_explicit_selection(self):
        for setting, expected in ((None, 4), ("4", 4), ("10", 10)):
            with self.subTest(setting=setting), patch.dict(os.environ):
                os.environ.pop("BTC_FUTURES_LEVERAGE", None)
                if setting is not None:
                    os.environ["BTC_FUTURES_LEVERAGE"] = setting
                module = runpy.run_path(str(Path(f.__file__)), run_name="_offline_import")
                self.assertEqual(module["LEVERAGE"], expected)
                self.assertEqual(module["CONTRACT"],
                                 CONTRACT_10 if expected == 10 else CONTRACT_4)
                self.assertTrue(module["CONFIRM"].startswith(f"{expected}X "))

    def test_unsupported_or_malformed_environment_fails_before_execution(self):
        for setting in ("2", "5", "20", "10.0", "10.5", "true", "garbage"):
            with self.subTest(setting=setting), patch.dict(
                    os.environ, {"BTC_FUTURES_LEVERAGE": setting}):
                with self.assertRaises(ValueError):
                    runpy.run_path(str(Path(f.__file__)), run_name="_offline_import")

    def test_quantity_default_tracks_selected_leverage_and_rounds_down(self):
        with selected_leverage(4):
            self.assertEqual(f.order_quantity("50", "85000", FILTERS),
                             Decimal("0.002"))
        with selected_leverage(10):
            self.assertEqual(f.order_quantity("50", "85000", FILTERS),
                             Decimal("0.005"))
            with self.assertRaises(ValueError):
                f.order_quantity("50", "85000", FILTERS, leverage=4)
            with self.assertRaises(ValueError):
                f.order_quantity("0.01", "85000", FILTERS)
            with self.assertRaises(ValueError):
                f.order_quantity("10000000", "85000", FILTERS)

    def test_client_requires_matching_symbol_leverage_and_positive_cap(self):
        client = object.__new__(f.Client)
        good = {"symbol": f.SYMBOL, "leverage": 10, "maxNotionalValue": "50000"}
        bad = ({"symbol": "ETHUSDT"}, {"leverage": 4},
               {"maxNotionalValue": "0"}, {"maxNotionalValue": "NaN"})
        with selected_leverage(10):
            for change in bad:
                with self.subTest(change=change), patch.object(
                        client, "request", return_value=dict(good, **change)):
                    with self.assertRaises(ValueError):
                        client.configure_leverage()
            with patch.object(client, "request", return_value=good) as request:
                client.configure_leverage()
                request.assert_called_once_with("POST", "/fapi/v1/leverage",
                                                {"symbol": f.SYMBOL, "leverage": 10}, True)


class LeverageBracketTests(unittest.TestCase):
    def test_symbol_object_and_list_are_accepted(self):
        payload = bracket_payload()
        for shape in (payload, payload[0]):
            with self.subTest(shape=type(shape).__name__):
                f.validate_leverage_bracket(shape, Decimal("1000"), leverage=10)

    def test_target_leverage_must_fit_the_actual_notional_tier(self):
        payload = bracket_payload(cap="1000", maximum=20)
        payload[0]["brackets"].append({
            "bracket": 2, "initialLeverage": 5,
            "notionalFloor": "1000", "notionalCap": "5000",
            "maintMarginRatio": "0.01", "cum": "0",
        })
        f.validate_leverage_bracket(payload, Decimal("999.99"), leverage=10)
        for notional in ("1000", "1200", "5001"):
            with self.subTest(notional=notional), self.assertRaises(ValueError):
                f.validate_leverage_bracket(payload, Decimal(notional), leverage=10)

    def test_returned_notional_caps_are_not_multiplied_twice(self):
        payload = bracket_payload(cap="1000", maximum=20)
        payload[0]["notionalCoef"] = 2
        with self.assertRaises(ValueError):
            f.validate_leverage_bracket(payload, Decimal("1500"), leverage=10)

    def test_missing_ambiguous_or_invalid_bracket_fails_closed(self):
        for payload in ([], {}, [{"symbol": "ETHUSDT", "brackets": []}],
                        bracket_payload() + bracket_payload(),
                        [{"symbol": f.SYMBOL, "brackets": []}],
                        bracket_payload(cap="NaN"), bracket_payload(cap="-1"),
                        bracket_payload(maximum=4)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                f.validate_leverage_bracket(payload, Decimal("500"), leverage=10)


class ProtectedTenXMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "futures.sqlite3"
        self.db = f.connect(self.path)
        self.addCleanup(self.db.close)
        with selected_leverage(4):
            self.client = LeverageClient()
            self.state = f.configure(self.db, self.client, Decimal("50"))
        self.client.requests.clear()

    def long(self):
        self.state.update(
            phase="long", quantity="0.007", entry_price="84343.20",
            stop_price="83618.00", target_price="85793.50",
            entry_client_id="qrf-e-before-migration", stop_client_id="qrf-s-before-migration",
            target_client_id="qrf-t-before-migration", filled_orders=11,
            completed_round_trips=5,
            learning_entry={"schema": 1, "leverage": 4, "quantity": "0.007",
                            "captured_at": 1_799_999_900,
                            "decision": {"entry_regime": "responsive_reclaim"},
                            "shadow_prediction": {"status": "unavailable"}})
        self.client._position = Decimal("0.007")
        self.client._entry = Decimal("84343.20")
        self.client.orders = [
            {"clientOrderId": self.state["stop_client_id"], "type": "STOP_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.007",
             "status": "NEW", "stopPrice": self.state["stop_price"]},
            {"clientOrderId": self.state["target_client_id"], "type": "TAKE_PROFIT_MARKET",
             "side": "SELL", "reduceOnly": True, "origQty": "0.007",
             "status": "NEW", "stopPrice": self.state["target_price"]},
        ]
        with self.db:
            f.write(self.db, self.state)
        return copy.deepcopy(self.state)

    def tick(self):
        with selected_leverage(10):
            return f.tick(self.db, self.client, Decimal("50"))

    def assert_no_mutations(self):
        self.assertEqual([r for r in self.client.requests if r[0] != "GET"], [])
        self.assertEqual(self.client.submitted, [])

    def test_open_four_to_ten_keeps_existing_exposure_and_entry_learning(self):
        original = self.long()
        orders = copy.deepcopy(self.client.orders)
        migrated = self.tick()
        self.assertEqual(migrated["contract"], CONTRACT_10)
        self.assertEqual(migrated["leverage"], 10)
        for key in ("quantity", "entry_price", "stop_price", "target_price",
                    "entry_client_id", "stop_client_id", "target_client_id",
                    "learning_entry", "filled_orders", "completed_round_trips"):
            self.assertEqual(migrated[key], original[key], key)
        self.assertEqual(self.client.orders, orders)
        self.assertEqual(self.client.submitted, [])
        mutations = [(r[0], r[1]) for r in self.client.requests if r[0] != "GET"]
        self.assertEqual(mutations, [("POST", "/fapi/v1/leverage")])

    def test_flat_four_to_ten_does_not_open_an_order(self):
        migrated = self.tick()
        self.assertEqual((migrated["contract"], migrated["leverage"], migrated["phase"]),
                         (CONTRACT_10, 10, "cash"))
        self.assertEqual(self.client.submitted, [])

    def test_already_applied_exchange_transition_is_readback_only(self):
        self.long()
        self.client.leverage = 10
        migrated = self.tick()
        self.assertEqual(migrated["leverage"], 10)
        self.assert_no_mutations()

    def test_source_contract_cannot_adopt_an_unrelated_exchange_leverage(self):
        original = self.long()
        self.client.leverage = 2
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(f.read(self.db), original)
        self.assert_no_mutations()

    def test_inadequate_tier_cap_refuses_before_leverage_post(self):
        original = self.long()
        self.client.brackets = bracket_payload(cap="100")
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(f.read(self.db), original)
        self.assert_no_mutations()

    def test_inadequate_exchange_returned_cap_cannot_commit_migration(self):
        original = self.long()
        self.client.leverage_cap = "100"
        with self.assertRaises(ValueError):
            self.tick()
        self.assertEqual(f.read(self.db), original)
        self.assertEqual(self.client.submitted, [])

    def test_stop_below_or_at_liquidation_refuses_migration(self):
        original = self.long()
        for liquidation in ("83618.00", "84000"):
            with self.subTest(liquidation=liquidation):
                self.client.liquidation = liquidation
                with self.assertRaises(ValueError):
                    self.tick()
                self.assertEqual(f.read(self.db), original)
                self.assert_no_mutations()

    def test_post_change_liquidation_check_does_not_rewrite_entry_context(self):
        original = self.long()
        configure = self.client.configure_leverage

        def changed_liquidation():
            result = configure()
            self.client.liquidation = "84000"
            return result

        with patch.object(self.client, "configure_leverage", side_effect=changed_liquidation):
            with self.assertRaises(ValueError):
                self.tick()
        self.assertEqual(f.read(self.db), original)
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(len(self.client.orders), 2)


if __name__ == "__main__":
    unittest.main()
