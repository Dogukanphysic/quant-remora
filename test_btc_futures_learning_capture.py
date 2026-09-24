"""Offline tests of durable learning telemetry, independent of real orders."""
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import btc_futures_live as f
from test_btc_futures_live import FakeClient


class LearningCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "futures.sqlite3"
        self.db = f.connect(self.path)
        self.client = FakeClient()
        f.configure(self.db, self.client, Decimal("50"))
        self.client.market_data["bar"] += 900_000
        self.client.market_data["now"] += 900
        self.client.market_data["band"].update(
            enter=True, entry_regime="reclaim", rsi=41,
            trend_confirmed=True, recovery_confirmed=True)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def events(self, kind):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,))]

    def open_fake_position(self):
        with patch.object(f, "ENTRY_VALIDATED", True):
            return f.tick(self.db, self.client, Decimal("50"))

    def test_entry_context_is_durable_before_submission_and_uses_entry_signal(self):
        submit = self.client.submit_entry

        def check_before_submit(quantity, client_id):
            recorded = self.events("entry_intent")[-1]["learning_entry"]
            self.assertEqual(recorded["decision"]["rsi"], 41)
            self.assertEqual(recorded["wallet_before_usdt"], "100")
            self.assertEqual(recorded["quantity"], str(quantity))
            self.assertEqual(f.read(self.db)["learning_entry"], recorded)
            return submit(quantity, client_id)

        with patch.object(self.client, "submit_entry", side_effect=check_before_submit):
            state = self.open_fake_position()
        recorded = self.events("long_activated")[-1]["learning_entry"]
        self.assertEqual(recorded["entry_price"], state["entry_price"])
        self.assertEqual(recorded["stop_price"], state["stop_price"])
        self.assertEqual(recorded["target_price"], state["target_price"])
        state["decision"]["rsi"] = 99
        self.assertEqual(state["learning_entry"]["decision"]["rsi"], 41)
        self.assertEqual([r[0] for r in self.client.submitted],
                         ["ENTRY", "STOP_MARKET", "TAKE_PROFIT_MARKET"])

    def test_close_keeps_entry_context_and_marks_wallet_delta_as_proxy(self):
        state = self.open_fake_position()
        state["wallet_usdt"] = "99"
        self.client.orders.clear()
        market = dict(self.client.market_data, now=self.client.market_data["now"] + 900)
        f.close_epoch(self.db, self.client, state, market, Decimal("97.5"))
        outcome = self.events("round_trip_closed")[-1]["learning_outcome"]
        self.assertEqual(outcome["entry"]["decision"]["rsi"], 41)
        self.assertEqual(outcome["wallet_delta_proxy_usdt"], "-2.5")
        self.assertEqual(outcome["pnl_basis"], "account_wallet_delta_unverified")
        self.assertFalse(outcome["execution_verified"])
        self.assertEqual(outcome["exit_reason"], "unknown_flat_reconciliation")
        self.assertIsNone(f.read(self.db)["learning_entry"])

    def test_legacy_close_does_not_invent_historical_context(self):
        state = self.open_fake_position()
        state.pop("learning_entry")
        self.client.orders.clear()
        f.close_epoch(self.db, self.client, state, self.client.market_data, Decimal("98"))
        outcome = self.events("round_trip_closed")[-1]["learning_outcome"]
        self.assertIsNone(outcome["entry"])
        self.assertIsNone(outcome["wallet_delta_proxy_usdt"])
        self.assertFalse(outcome["execution_verified"])

    def test_rejected_entry_clears_context_without_creating_trade_label(self):
        state = f.read(self.db)
        state.update(pending_entry="qrf-e-fake", learning_entry={"schema": 1})
        with self.db:
            f.write(self.db, state)
        with patch.object(self.client, "lookup", return_value={"status": "REJECTED"}):
            recovered = f.recover_pending_entry(
                self.db, self.client, state, self.client.market_data)
        self.assertIsNone(recovered["learning_entry"])
        self.assertIsNone(recovered["pending_entry"])
        self.assertEqual(self.events("round_trip_closed"), [])

    def test_completed_worker_cycle_reaches_separate_learner_and_public_status(self):
        import btc_futures_learning as learner
        state = self.open_fake_position()
        self.client.orders.clear()
        market = dict(self.client.market_data, now=self.client.market_data["now"] + 900)
        f.close_epoch(self.db, self.client, state, market, Decimal("97.5"))
        before = self.db.execute("SELECT value FROM state WHERE id=1").fetchone()[0]
        learning_path = self.path.parent / "btc-futures-mainnet-learning.sqlite3"
        result = learner.sync(self.path, learning_path)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["trained_sample_count"], 1)
        self.assertEqual(result["verified_count"], 0)
        self.assertEqual(result["recent_outcomes"][0]["quality"],
                         "entry_snapshot_wallet_proxy")
        self.assertFalse(result["decision_authority"])
        self.assertIn("decision.rsi", result["model_feature_names"])
        self.assertEqual(f.public_status(self.path)["learning"]["model_id"],
                         result["model_id"])
        self.assertEqual(self.db.execute("SELECT value FROM state WHERE id=1").fetchone()[0],
                         before)

    def test_shadow_prediction_is_saved_before_submission_without_decision_authority(self):
        advice = {"available": True, "status": "scored", "loss_probability": 0.99,
                  "model_id": "test-shadow-only", "decision_authority": False}
        submit = self.client.submit_entry

        def score(db, context, now):
            self.assertEqual(self.client.submitted, [])
            self.assertNotIn("entry_price", context)
            self.assertNotIn("stop_price", context)
            self.assertEqual(context["reference_ask"], "85001")
            return advice

        def check(quantity, client_id):
            saved = self.events("entry_intent")[-1]["learning_entry"]["shadow_prediction"]
            self.assertEqual(saved, advice)
            return submit(quantity, client_id)

        with patch.object(f, "entry_shadow_prediction", side_effect=score), \
                patch.object(self.client, "submit_entry", side_effect=check):
            state = self.open_fake_position()
        self.assertEqual(state["phase"], "long")
        self.assertTrue(state["decision"]["enter"])

    def test_unavailable_shadow_model_does_not_change_baseline_execution(self):
        with patch("btc_futures_learning.predict_entry", side_effect=OSError("test")):
            state = self.open_fake_position()
        self.assertEqual(state["phase"], "long")
        advice = self.events("entry_intent")[-1]["learning_entry"]["shadow_prediction"]
        self.assertFalse(advice["available"])
        self.assertFalse(advice["decision_authority"])

    def test_responsive_is_explicit_opt_in_and_preserves_position_risk_controls(self):
        self.client.market_data["band"].update(
            enter=False, entry_regime=None, trend_confirmed=False,
            lower_zone_reached=True, recovery_confirmed=True, histogram_rising=True)
        with patch.object(f, "SIGNAL_PROFILE", "responsive"):
            state = self.open_fake_position()
        self.assertEqual(state["phase"], "long")
        self.assertEqual(state["decision"]["entry_regime"], "responsive_reclaim")
        self.assertFalse(state["decision"]["strict_enter"])
        self.assertEqual(state["leverage"], 4)
        self.assertEqual(state["risk_profile"], "moderate")
        self.assertTrue(state["stop_client_id"])
        self.assertTrue(state["target_client_id"])


if __name__ == "__main__":
    unittest.main()
