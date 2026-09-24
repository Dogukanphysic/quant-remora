"""Offline evidence contracts for the observational Futures trade journal.

Only temporary SQLite ledgers are used. These tests never import a trading
worker, inspect the real ledger, or connect to an exchange.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest

import btc_futures_learning as learner
import test_btc_futures_learning as fixtures


class FuturesObservationalJournalTests(unittest.TestCase):
    # Reuse fixture construction without inheriting and rerunning unrelated tests.
    setUp = fixtures.FuturesTradeLearningTests.setUp
    tearDown = fixtures.FuturesTradeLearningTests.tearDown
    _connection = fixtures.FuturesTradeLearningTests._connection
    _create_source = fixtures.FuturesTradeLearningTests._create_source
    _event = fixtures.FuturesTradeLearningTests._event
    _state = fixtures.FuturesTradeLearningTests._state
    _trip = fixtures.FuturesTradeLearningTests._trip
    _losses = fixtures.FuturesTradeLearningTests._losses
    _samples = fixtures.FuturesTradeLearningTests._samples
    _assert_shadow = fixtures.FuturesTradeLearningTests._assert_shadow

    def _digest(self):
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def _attempt(self, disposition):
        client_id = "qrf-e-offline-attempt"
        intent_id = self._event("entry_intent", {
            "client_id": client_id, "quantity": "0.001", "margin_usdt": "25",
        }, ts=self.now)
        if disposition == "UNSENT":
            terminal_id = self._event("unsent_entry_recovered", {
                "client_id": client_id,
            }, ts=self.now + 1)
        else:
            terminal_id = self._event("entry_terminal_without_position", {
                "clientOrderId": client_id, "status": disposition,
                "origQty": "0.001", "executedQty": "0",
            }, ts=self.now + 1)
        return intent_id, terminal_id

    def _open(self):
        intent_id = self._event("entry_intent", {
            "client_id": "qrf-e-offline-open", "quantity": "0.001",
            "margin_usdt": "25",
        })
        active_id = self._event("long_activated", {
            "quantity": "0.001", "entry": "85000", "stop": "84500",
            "target": "86000", "risk_profile": "moderate",
        })
        self._state(phase="long", quantity="0.001", entry_price="85000",
                    stop_price="84500", target_price="86000",
                    entry_client_id="qrf-e-offline-open", leverage=10,
                    decision={"rsi": 97, "entry_regime": "latest_not_entry"})
        return intent_id, active_id

    def _table(self, name):
        self.assertIn(name, {"trade_journal", "order_observations"})
        with self._connection(self.learning) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {name} ORDER BY rowid")]

    def _journal(self):
        return self._table("trade_journal")

    def _orders(self):
        return self._table("order_observations")

    def test_five_closed_cycles_are_observed_without_invented_execution_or_pnl(self):
        self._losses(5)
        report = learner.sync(self.source, self.learning)
        journal = report["journal"]
        self.assertEqual(journal["closed_cycle_count"], 5)
        self.assertEqual(journal["evaluated_proxy_count"], 5)
        self.assertEqual(report["sample_count"], 5)
        rows = self._journal()
        self.assertEqual(len(rows), 5)
        self.assertEqual({row["status"] for row in rows}, {"closed"})
        self.assertEqual({row["evaluation_status"] for row in rows}, {"evaluated_wallet_proxy"})
        self.assertEqual({row["wallet_delta_proxy_usdt"] for row in rows}, {"-1"})
        self.assertTrue(all(row["sample_id"] and row["valid"] == 1 for row in rows))
        self.assertTrue(all(json.loads(row["detail"]).get("verified_net_pnl_usdt") is None
                            for row in rows))
        exits = [row for row in self._orders()
                 if row["role"] in {"exit", "stop"} and row["status"] == "FILLED"]
        self.assertEqual(len(exits), 5)
        self.assertTrue(all(row["average_price"] is None for row in exits))
        self.assertEqual(report["verified_count"], 0)
        self._assert_shadow(report)

    def test_open_position_is_visible_but_not_a_completed_example(self):
        intent_id, active_id = self._open()
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertEqual(report["journal"]["open_cycle_count"], 1)
        self.assertEqual(report["journal"]["closed_cycle_count"], 0)
        row, = self._journal()
        self.assertEqual(row["entry_event_id"], intent_id)
        self.assertEqual(row["activated_event_id"], active_id)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["entry_price"], "85000")
        self.assertEqual(row["entry_quantity"], "0.001")
        self.assertEqual(row["entry_fill_status"], "POSITION_CONFIRMED")
        self.assertEqual(row["exit_fill_status"], "UNKNOWN")
        self.assertIsNone(row["wallet_delta_proxy_usdt"])
        self.assertIsNone(row["closed_at"])
        self.assertIsNone(row["sample_id"])
        self.assertEqual(report["journal"]["current_trade"]["entry_event_id"], intent_id)
        self._assert_shadow(report)

    def test_zero_wallet_delta_is_evaluated_without_inventing_a_training_label(self):
        self._trip(wallet="100")
        report = learner.sync(self.source, self.learning)
        row, = self._journal()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["evaluation_status"], "neutral_wallet_proxy")
        self.assertEqual(row["wallet_delta_proxy_usdt"], "0")
        self.assertTrue(row["sample_id"])
        sample, = self._samples()
        self.assertIsNone(sample["label"])
        self.assertIsNone(report["model_id"])
        self.assertIsNone(json.loads(row["detail"]).get("verified_net_pnl_usdt"))

    def test_pending_intent_is_observed_without_claiming_a_fill(self):
        intent_id = self._event("entry_intent", {
            "client_id": "qrf-e-offline-pending", "quantity": "0.001",
        })
        self._state(pending_entry="qrf-e-offline-pending")
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["journal"]["pending_cycle_count"], 1)
        self.assertEqual(report["sample_count"], 0)
        row, = self._journal()
        self.assertEqual(row["entry_event_id"], intent_id)
        self.assertIsNone(row["activated_event_id"])
        self.assertIsNone(row["entry_price"])
        self.assertNotEqual(row["entry_fill_status"], "FILLED")
        self.assertIsNone(row["sample_id"])

    def _assert_terminal_attempt_allows_next_cycle(self, disposition):
        intent_id, terminal_id = self._attempt(disposition)
        self._trip(1, wallet="99")
        # The helper indexes timestamps by cycle; only one actual close exists.
        self._state(completed_round_trips=1)
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["journal"]["terminal_without_position_count"], 1)
        self.assertEqual(report["journal"]["closed_cycle_count"], 1)
        self.assertEqual(report["journal"]["evaluated_proxy_count"], 1)
        attempt = next(row for row in self._journal() if row["entry_event_id"] == intent_id)
        self.assertEqual(attempt["status"], disposition.lower())
        self.assertEqual(attempt["evaluation_status"], "terminal_without_position")
        self.assertIsNone(attempt["sample_id"])
        self.assertIsNone(attempt["wallet_delta_proxy_usdt"])
        self.assertNotEqual(attempt["entry_fill_status"], "FILLED")
        terminal = [row for row in self._orders() if row["event_id"] == terminal_id]
        self.assertTrue(terminal)
        self.assertTrue(all(row["cycle_id"] == attempt["id"] for row in terminal))

    def test_rejected_attempt_does_not_poison_next_completed_cycle(self):
        self._assert_terminal_attempt_allows_next_cycle("REJECTED")

    def test_canceled_attempt_does_not_poison_next_completed_cycle(self):
        self._assert_terminal_attempt_allows_next_cycle("CANCELED")

    def test_expired_attempt_does_not_poison_next_completed_cycle(self):
        self._assert_terminal_attempt_allows_next_cycle("EXPIRED")

    def test_unsent_attempt_does_not_poison_next_completed_cycle(self):
        self._assert_terminal_attempt_allows_next_cycle("UNSENT")

    def test_unmatched_close_is_retained_with_unknown_evaluation(self):
        close_id = self._event("round_trip_closed", {"wallet_usdt": "99"})
        self._state(completed_round_trips=1)
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertEqual(report["journal"]["closed_cycle_count"], 1)
        self.assertEqual(report["journal"]["unknown_evaluation_count"], 1)
        self.assertGreater(report["quarantined_count"], 0)
        row, = self._journal()
        self.assertEqual(row["closed_event_id"], close_id)
        self.assertEqual(row["evaluation_status"], "quarantined_no_training_sample")
        self.assertIsNone(row["entry_price"])
        self.assertIsNone(row["sample_id"])
        self.assertIsNone(row["wallet_delta_proxy_usdt"])
        self.assertEqual(row["exit_fill_status"], "UNKNOWN")

    def test_missing_activation_is_retained_without_inventing_fill_or_learning(self):
        intent_id = self._event("entry_intent", {
            "client_id": "qrf-e-offline-no-activation", "quantity": "0.001",
        })
        self._event("round_trip_closed", {"wallet_usdt": "99"}, ts=self.now + 30)
        self._state(completed_round_trips=1)
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertEqual(report["journal"]["unknown_evaluation_count"], 1)
        row, = self._journal()
        self.assertEqual(row["entry_event_id"], intent_id)
        self.assertIsNone(row["activated_event_id"])
        self.assertIsNone(row["entry_price"])
        self.assertNotEqual(row["entry_fill_status"], "FILLED")
        self.assertEqual(row["evaluation_status"], "quarantined_no_training_sample")

    def test_sync_and_status_preserve_source_and_replay_journal_idempotently(self):
        self._losses(5)
        digest_before = self._digest()
        first = learner.sync(self.source, self.learning)
        cycles, orders = self._journal(), self._orders()
        second = learner.sync(self.source, self.learning)
        current = learner.status(self.learning)
        self.assertEqual(self._digest(), digest_before)
        self.assertEqual(self._journal(), cycles)
        self.assertEqual(self._orders(), orders)
        self.assertEqual(second["training_runs"], first["training_runs"])
        self.assertEqual(second["journal"], first["journal"])
        self.assertEqual(current["journal"], second["journal"])

    def test_conflicting_source_retains_audit_rows_but_invalidates_journal(self):
        self._trip()
        learner.sync(self.source, self.learning)
        before = self._journal()
        with self._connection(self.source) as db:
            db.execute("UPDATE events SET payload=? WHERE kind='round_trip_closed'", (
                json.dumps({"wallet_usdt": "105"}),
            ))
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["status"], "source_conflict")
        self.assertIs(report["journal"]["valid"], False)
        self.assertEqual(report["journal"]["cycle_count"], 0)
        self.assertEqual(report["journal"]["order_observation_count"], 0)
        rows = self._journal()
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in before])
        self.assertTrue(all(row["valid"] == 0 for row in rows + self._orders()))
        self.assertEqual([row["detail"] for row in rows], [row["detail"] for row in before])
        self.assertIs(learner.status(self.learning)["journal"]["valid"], False)
        self._assert_shadow(report)

    def test_ten_x_configuration_change_does_not_change_source_identity(self):
        self._trip()
        first = learner.sync(self.source, self.learning)
        historic = self._journal()
        self._state(leverage=10, contract="btc-usdm-isolated-10x-bollinger-long-v3")
        self._event("open_position_leverage_migrated", {"from": 4, "to": 10},
                    ts=self.now + 1801)
        report = learner.sync(self.source, self.learning)
        self.assertNotEqual(report["status"], "source_conflict")
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["model_id"], first["model_id"])
        self.assertEqual(self._journal()[0]["source_fingerprint"], historic[0]["source_fingerprint"])
        self.assertEqual(self._journal()[0]["leverage"], historic[0]["leverage"])
        self.assertIs(report["journal"]["valid"], True)
        self._assert_shadow(report)

    def test_explicit_order_observation_is_preserved_without_creating_trade_pnl(self):
        intent_id, _ = self._open()
        observed_id = self._event("order_observation", {
            "schema": 1, "role": "entry", "entry_client_id": "qrf-e-offline-open",
            "client_id": "qrf-e-offline-open", "status": "FILLED",
            "quantity": "0.001", "executed_quantity": "0.001",
            "average_price": "85001.20", "order_type": "MARKET", "side": "BUY",
            "observation_source": "lookup_entry",
        }, ts=self.now + 1)
        report = learner.sync(self.source, self.learning)
        row, = self._journal()
        explicit, = [row for row in self._orders() if row["event_id"] == observed_id]
        self.assertEqual(explicit["cycle_id"], row["id"])
        self.assertEqual(explicit["average_price"], "85001.20")
        self.assertEqual(explicit["executed_quantity"], "0.001")
        self.assertEqual(explicit["status"], "FILLED")
        self.assertTrue(explicit["event_hash"])
        self.assertEqual(row["entry_event_id"], intent_id)
        self.assertEqual(row["entry_fill_status"], "FILLED")
        self.assertIsNone(json.loads(row["detail"]).get("verified_net_pnl_usdt"))
        self.assertIsNone(row["wallet_delta_proxy_usdt"])
        self.assertEqual(report["sample_count"], 0)
        self._assert_shadow(report)

    def test_explicit_unknown_status_remains_unknown_without_fabricated_fill_fields(self):
        self._open()
        observed_id = self._event("order_observation", {
            "schema": 1, "role": "stop", "entry_client_id": "qrf-e-offline-open",
            "client_id": "qrf-s-offline-open", "status": "UNKNOWN",
            "observation_source": "lookup_protection", "error_code": -2013,
        }, ts=self.now + 1)
        report = learner.sync(self.source, self.learning)
        observation, = [row for row in self._orders() if row["event_id"] == observed_id]
        self.assertEqual(observation["status"], "UNKNOWN")
        self.assertIsNone(observation["executed_quantity"])
        self.assertIsNone(observation["average_price"])
        self.assertEqual(json.loads(observation["evidence"])["error_code"], "-2013")
        row, = self._journal()
        self.assertEqual(observation["cycle_id"], row["id"])
        self.assertEqual(row["exit_fill_status"], "UNKNOWN")
        self.assertEqual(report["sample_count"], 0)

    def test_partial_entry_order_is_visible_while_position_reconciliation_is_pending(self):
        client = "qrf-e-offline-partial"
        intent_id = self._event("entry_intent", {"client_id": client, "quantity": "0.002"})
        observation_id = self._event("order_observation", {
            "schema": 1, "role": "entry", "entry_client_id": client,
            "client_id": client, "status": "PARTIALLY_FILLED",
            "quantity": "0.002", "executed_quantity": "0.001", "average_price": "85000",
            "observation_source": "lookup_entry",
        }, ts=self.now + 1)
        self._state(pending_entry=client)
        report = learner.sync(self.source, self.learning)
        row, = self._journal()
        self.assertEqual(row["entry_event_id"], intent_id)
        self.assertEqual(row["entry_fill_status"], "PARTIALLY_FILLED")
        self.assertEqual(row["evaluation_status"], "awaiting_position_reconciliation")
        self.assertIsNone(row["activated_event_id"])
        self.assertIsNone(row["closed_event_id"])
        self.assertIsNone(row["sample_id"])
        explicit, = [row for row in self._orders() if row["event_id"] == observation_id]
        self.assertEqual(explicit["executed_quantity"], "0.001")
        self.assertEqual(explicit["quantity"], "0.002")
        self.assertEqual(report["journal"]["pending_cycle_count"], 1)
        self.assertEqual(report["sample_count"], 0)

    def test_current_position_before_activation_is_marked_as_state_evidence_only(self):
        client = "qrf-e-offline-state-only"
        intent_id = self._event("entry_intent", {"client_id": client, "quantity": "0.002"})
        self._state(phase="long", entry_client_id=client, pending_entry=client,
                    quantity="0.001", entry_price="85000", leverage=10,
                    stop_client_id="qrf-s-offline-state", target_client_id="qrf-t-offline-state")
        report = learner.sync(self.source, self.learning)
        row, = self._journal()
        self.assertEqual(row["entry_event_id"], intent_id)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["entry_fill_status"], "POSITION_OBSERVED_IN_STATE")
        self.assertEqual(row["evaluation_status"], "open_position_missing_activation_event")
        self.assertEqual(row["entry_quantity"], "0.001")
        self.assertIsNone(row["activated_event_id"])
        self.assertIsNone(row["sample_id"])
        state_evidence = json.loads(row["detail"])["current_state"]
        self.assertEqual(state_evidence["quantity"], "0.001")
        self.assertEqual(state_evidence["leverage"], "10")
        protections = [row for row in self._orders() if row["role"] in {"stop", "target"}]
        self.assertEqual(len(protections), 2)
        self.assertEqual({row["status"] for row in protections}, {"UNKNOWN"})
        self.assertTrue(all(row["event_id"] is None and row["average_price"] is None
                            for row in protections))
        self.assertEqual(report["sample_count"], 0)

    def test_decimal_zero_quantity_in_current_state_is_not_an_open_position(self):
        self._state(phase="long", quantity="0.00000000", entry_price="0.00000000",
                    entry_client_id=None, pending_entry=None)
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["journal"]["open_cycle_count"], 0)
        current = report["journal"]["current_trade"]
        self.assertEqual(current["status"], "unknown")
        self.assertEqual(current["evaluation_status"], "source_state_event_disagreement")
        self.assertEqual(current["entry_fill_status"], "UNKNOWN")
        self.assertIsNone(current["sample_id"])
        self.assertEqual(report["sample_count"], 0)

    def test_ambiguous_client_id_never_attaches_explicit_fill_to_an_arbitrary_attempt(self):
        client = "qrf-e-offline-duplicate"
        intent_ids = [self._event("entry_intent", {"client_id": client, "quantity": "0.001"},
                                  ts=self.now + offset) for offset in (0, 1)]
        observed_id = self._event("order_observation", {
            "schema": 1, "role": "entry", "entry_client_id": client,
            "client_id": client, "status": "FILLED", "executed_quantity": "0.001",
            "average_price": "85000", "observation_source": "lookup_entry",
        }, ts=self.now + 2)
        report = learner.sync(self.source, self.learning)
        attempts = [row for row in self._journal() if row["entry_event_id"] in intent_ids]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(row["entry_fill_status"] != "FILLED" for row in attempts))
        observation, = [row for row in self._orders() if row["event_id"] == observed_id]
        self.assertNotIn(observation["cycle_id"], {row["id"] for row in attempts})
        self.assertEqual(observation["status"], "FILLED")
        self.assertEqual(report["sample_count"], 0)

    def test_hook_with_other_entry_client_id_does_not_update_open_position(self):
        intent_id, _ = self._open()
        observed_id = self._event("order_observation", {
            "schema": 1, "role": "stop", "entry_client_id": "qrf-e-unrelated",
            "client_id": "qrf-s-unrelated", "status": "FILLED",
            "executed_quantity": "0.003", "average_price": "84000",
        }, ts=self.now + 1)
        report = learner.sync(self.source, self.learning)
        current = next(row for row in self._journal() if row["entry_event_id"] == intent_id)
        observation, = [row for row in self._orders() if row["event_id"] == observed_id]
        self.assertNotEqual(observation["cycle_id"], current["id"])
        self.assertEqual(current["exit_fill_status"], "UNKNOWN")
        self.assertEqual(current["status"], "open")
        self.assertEqual(report["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
