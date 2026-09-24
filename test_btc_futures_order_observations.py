"""Offline worker-hook contracts using fake clients and temporary ledgers only."""
from contextlib import closing
from decimal import Decimal
import json
import sqlite3
import unittest
from unittest.mock import patch

import btc_futures_learning as learner
import btc_futures_live as worker
import test_btc_futures_learning_capture as fixtures


class FuturesOrderObservationTests(unittest.TestCase):
    setUp = fixtures.LearningCaptureTests.setUp
    tearDown = fixtures.LearningCaptureTests.tearDown
    events = fixtures.LearningCaptureTests.events
    open_fake_position = fixtures.LearningCaptureTests.open_fake_position

    def _sync(self):
        learning_path = self.path.parent / "learning.sqlite3"
        report = learner.sync(self.path, learning_path)
        with closing(sqlite3.connect(learning_path)) as db:
            db.row_factory = sqlite3.Row
            cycles = [dict(row) for row in db.execute("SELECT * FROM trade_journal WHERE valid=1")]
            observations = [dict(row) for row in db.execute("SELECT * FROM order_observations WHERE valid=1")]
        return report, cycles, observations

    def _pending(self):
        state = worker.read(self.db)
        client_id = "qrf-e-offline-hook"
        state.update(pending_entry=client_id)
        with self.db:
            worker.event(self.db, "entry_intent", {
                "client_id": client_id, "quantity": "0.002", "margin_usdt": "50",
            }, self.client.market_data["now"])
            worker.write(self.db, state)
        return state, client_id

    def test_fake_open_close_lifecycle_reaches_journal_with_observed_fill_and_cancel(self):
        submit = self.client.submit_entry

        def submit_with_fill_fields(quantity, client_id):
            result = submit(quantity, client_id)
            return dict(result, origQty=str(quantity), executedQty=str(quantity),
                        avgPrice="85001", type="MARKET", side="BUY")

        with patch.object(self.client, "submit_entry", side_effect=submit_with_fill_fields):
            state = self.open_fake_position()
        observations = self.events("order_observation")
        self.assertEqual({row["role"] for row in observations}, {"entry", "stop", "target"})
        entry, = [row for row in observations if row["role"] == "entry"]
        self.assertEqual(entry["status"], "FILLED")
        self.assertEqual(entry["executed_quantity"], state["quantity"])
        lookup = self.client.lookup_protection

        def filled_stop(client_id):
            if client_id == state["stop_client_id"]:
                self.client.orders = [row for row in self.client.orders
                                      if row["clientOrderId"] != client_id]
                return {"clientOrderId": client_id, "type": "STOP_MARKET", "status": "FILLED",
                        "origQty": state["quantity"], "executedQty": state["quantity"],
                        "avgPrice": "83750", "side": "SELL"}
            return lookup(client_id)

        self.client._position = Decimal("0")
        market = dict(self.client.market_data, now=self.client.market_data["now"] + 30)
        with patch.object(self.client, "lookup_protection", side_effect=filled_stop):
            worker.close_epoch(self.db, self.client, state, market, Decimal("97.5"))
        report, cycles, observations = self._sync()
        cycle, = cycles
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(cycle["status"], "closed")
        self.assertEqual(cycle["entry_fill_status"], "FILLED")
        self.assertEqual(cycle["exit_fill_status"], "FILLED")
        self.assertEqual(cycle["wallet_delta_proxy_usdt"], "-2.5")
        stop_fills = [row for row in observations if row["role"] == "stop" and row["status"] == "FILLED"]
        self.assertEqual(len(stop_fills), 1)
        self.assertEqual(stop_fills[0]["average_price"], "83750")
        self.assertTrue(any(row["role"] == "target" and row["status"] == "CANCELED"
                            for row in observations))
        self.assertIsNone(json.loads(cycle["detail"])["verified_net_pnl_usdt"])
        self.assertEqual(report["verified_count"], 0)
        self.assertFalse(report["real_orders_enabled"])
        self.assertFalse(report["decision_authority"])

    def test_submit_exception_records_unknown_then_propagates_without_claiming_a_fill(self):
        failure = worker.ApiError("offline sensitive remote text", 400, -2010)
        with patch.object(self.client, "submit_entry", side_effect=failure):
            with self.assertRaises(worker.ApiError) as caught:
                self.open_fake_position()
        self.assertIs(caught.exception, failure)
        observation, = self.events("order_observation")
        self.assertEqual(observation["status"], "UNKNOWN")
        self.assertEqual(observation["observation_source"], "submit_error")
        self.assertEqual(observation["error_code"], -2010)
        self.assertNotIn("offline sensitive remote text", json.dumps(observation))
        self.assertNotIn("average_price", observation)
        self.assertNotIn("executed_quantity", observation)
        self.assertEqual(self.events("long_activated"), [])
        self.assertEqual(self.events("round_trip_closed"), [])
        report, cycles, observations = self._sync()
        cycle, = cycles
        self.assertEqual(cycle["entry_fill_status"], "UNKNOWN")
        self.assertIsNone(cycle["sample_id"])
        self.assertEqual(report["sample_count"], 0)
        self.assertFalse(report["decision_authority"])

    def test_identical_client_observation_is_deduplicated_but_status_change_is_recorded(self):
        state, client_id = self._pending()
        result = {"status": "NEW", "origQty": "0.002", "executedQty": "0"}
        for offset in (0, 10, 20):
            worker.record_order_observation(self.db, state, "entry", client_id, "lookup", result,
                                            now=self.client.market_data["now"] + offset)
        self.assertEqual(len(self.events("order_observation")), 1)
        partial = dict(result, status="PARTIALLY_FILLED", executedQty="0.001", avgPrice="85000")
        for offset in (30, 40):
            worker.record_order_observation(self.db, state, "entry", client_id, "lookup", partial,
                                            now=self.client.market_data["now"] + offset)
        observations = self.events("order_observation")
        self.assertEqual([row["status"] for row in observations], ["NEW", "PARTIALLY_FILLED"])
        report, cycles, _ = self._sync()
        self.assertEqual(cycles[0]["entry_fill_status"], "PARTIALLY_FILLED")
        self.assertEqual(report["sample_count"], 0)

    def test_partial_lookup_keeps_requested_and_executed_quantities_distinct(self):
        state, client_id = self._pending()
        partial = {"status": "PARTIALLY_FILLED", "origQty": "0.002",
                   "executedQty": "0.001", "avgPrice": "85000"}
        with patch.object(self.client, "lookup", return_value=partial):
            result = worker.observe_order_call(
                self.db, state, "entry", client_id, "lookup", lambda: self.client.lookup(client_id),
                self.client.market_data["now"] + 1)
        self.assertIs(result, partial)
        observation, = self.events("order_observation")
        self.assertEqual(observation["status"], "PARTIALLY_FILLED")
        self.assertEqual(observation["quantity"], "0.002")
        self.assertEqual(observation["executed_quantity"], "0.001")
        report, cycles, observations = self._sync()
        self.assertEqual(cycles[0]["entry_fill_status"], "PARTIALLY_FILLED")
        self.assertIsNone(cycles[0]["activated_event_id"])
        self.assertIsNone(cycles[0]["sample_id"])
        self.assertEqual(report["journal"]["open_cycle_count"], 0)
        self.assertEqual(report["sample_count"], 0)

    def test_success_acknowledgement_without_status_does_not_imply_filled(self):
        state, client_id = self._pending()
        acknowledgement = {"orderId": 12345, "clientOrderId": client_id, "code": 200}
        worker.record_order_observation(self.db, state, "entry", client_id, "submit_response",
                                        acknowledgement, now=self.client.market_data["now"])
        observed, = self.events("order_observation")
        self.assertEqual(observed["status"], "UNKNOWN")
        self.assertNotIn("executed_quantity", observed)
        self.assertNotIn("average_price", observed)
        report, cycles, _ = self._sync()
        self.assertEqual(cycles[0]["entry_fill_status"], "UNKNOWN")
        self.assertIsNone(cycles[0]["sample_id"])
        self.assertEqual(report["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
