"""Offline integration checks for explicit experimental entry authority.

Every exchange operation uses FakeClient; all execution and learning databases
are temporary. Nothing starts a worker or accesses a live account.
"""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import btc_futures_learning as learner
import btc_futures_live as f
from test_btc_futures_live import FakeClient


class FuturesModelControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "futures.sqlite3"
        self.learning_path = self.path.parent / "btc-futures-mainnet-learning.sqlite3"
        self.db = f.connect(self.path)
        self.addCleanup(self.db.close)
        for name, value in (("ENTRY_VALIDATED", True), ("MODEL_DECISIONS", False),
                            ("SIGNAL_PROFILE", "trend"), ("RISK_PROFILE", "moderate")):
            override = patch.object(f, name, value)
            override.start()
            self.addCleanup(override.stop)
        self.client = FakeClient()
        self.wallet = Decimal("100")
        self.client.account = lambda: {
            "canTrade": True, "multiAssetsMargin": False, "assets": [{
                "asset": "USDT", "availableBalance": str(self.wallet),
                "walletBalance": str(self.wallet)}]}
        self.wall = self.client.market_data["now"]
        clock = patch.object(learner.time, "time", side_effect=lambda: self.wall)
        clock.start()
        self.addCleanup(clock.stop)
        f.configure(self.db, self.client, Decimal("50"))
        self.advance()
        self.client.market_data["band"].update(
            enter=True, entry_regime="reclaim", rsi=41,
            trend_confirmed=True, recovery_confirmed=True)

    def advance(self, bars=1):
        self.client.market_data["bar"] += bars * 900_000
        self.client.market_data["now"] += bars * 900
        self.wall = self.client.market_data["now"]

    def tick(self):
        return f.tick(self.db, self.client, Decimal("50"))

    def events(self, kind):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM events WHERE kind=? ORDER BY id", (kind,))]

    def write_state(self, **updates):
        state = f.read(self.db)
        state.update(updates)
        with self.db:
            f.write(self.db, state)
        return state

    @staticmethod
    def advice(allow, reason="model_loss_score", *, applied=True):
        return {"allow": allow, "authority_applied": applied,
                "reason": reason, "mode": "experimental"}

    def close_fake_position(self, delta="-1"):
        state = f.read(self.db)
        self.advance()
        self.wallet += Decimal(delta)
        self.client._position = Decimal("0")
        self.client._entry = Decimal("0")
        self.client.orders.clear()
        return f.close_epoch(self.db, self.client, state,
                             self.client.market_data, self.wallet)

    def test_disabled_adapter_leaves_baseline_entry_and_protections_unchanged(self):
        with patch.object(f, "model_entry_decision") as control:
            state = self.tick()
        control.assert_not_called()
        self.assertFalse(state["model_decisions_enabled"])
        self.assertEqual(state["phase"], "long")
        self.assertTrue(state["decision"]["enter"])
        self.assertEqual([row[0] for row in self.client.submitted],
                         ["ENTRY", "STOP_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertEqual(self.events("model_entry_decision"), [])

    def test_model_defer_consumes_bar_without_order_intent_or_learning_label(self):
        prediction = {"available": True, "status": "scored",
                      "loss_probability": 0.99, "decision_authority": False}
        captured = []

        def score(db, context, now):
            captured.append(deepcopy(context))
            return prediction

        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "entry_shadow_prediction", side_effect=score) as scorer, \
                patch.object(f, "model_entry_decision", return_value=self.advice(False)) as control:
            state = self.tick()
            again = self.tick()
        scorer.assert_called_once()
        control.assert_called_once()
        self.assertTrue(captured[0]["decision"]["enter"])
        self.assertFalse(state["decision"]["enter"])
        self.assertEqual(state["phase"], "cash")
        self.assertEqual(again["last_bar"], self.client.market_data["bar"])
        self.assertFalse(state["model_control_latest"]["allow"])
        self.assertTrue(state["model_decisions_enabled"])
        self.assertIsNone(state["pending_entry"])
        self.assertIsNone(state["learning_entry"])
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.events("entry_intent"), [])
        self.assertEqual(self.events("round_trip_closed"), [])
        self.assertEqual(len(self.events("model_entry_decision")), 1)
        report = learner.sync(self.path, self.learning_path)
        self.assertEqual(report["sample_count"], 0)
        self.assertEqual(report["true_forward_proxy_count"], 0)

    def test_unavailable_model_fallback_records_reason_then_uses_eligible_baseline(self):
        prediction = {"available": False, "status": "unavailable",
                      "reason": "stale_source_snapshot", "decision_authority": False}
        fallback = self.advice(True, "stale_source_snapshot", applied=False)
        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "entry_shadow_prediction", return_value=prediction), \
                patch.object(f, "model_entry_decision", return_value=fallback):
            state = self.tick()
        self.assertEqual(state["phase"], "long")
        self.assertFalse(state["model_control_latest"]["authority_applied"])
        self.assertEqual(state["model_control_latest"]["reason"], "stale_source_snapshot")
        recorded = self.events("entry_intent")[0]["learning_entry"]["shadow_prediction"]
        self.assertEqual(recorded, prediction)
        self.assertNotIn("loss_probability", recorded)
        self.assertEqual(len(self.events("model_entry_decision")), 1)

    def test_halt_prevents_model_callback_and_entry(self):
        self.write_state(halted="ProtectionMissing")
        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "model_entry_decision") as control, \
                patch.object(f, "entry_shadow_prediction") as scorer:
            state = self.tick()
        control.assert_not_called()
        scorer.assert_not_called()
        self.assertEqual(state["halted"], "ProtectionMissing")
        self.assertEqual(self.client.submitted, [])

    def test_cooldown_prevents_model_callback_and_entry(self):
        self.write_state(cooldown_until_bar=self.client.market_data["bar"] + 900_000)
        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "model_entry_decision") as control, \
                patch.object(f, "entry_shadow_prediction") as scorer:
            state = self.tick()
        control.assert_not_called()
        scorer.assert_not_called()
        self.assertFalse(state["decision"]["enter"])
        self.assertEqual(state["decision"]["entry_gate_reason"], "cooldown_after_exit")
        self.assertEqual(self.client.submitted, [])

    def test_raw_false_signal_cannot_be_manufactured_by_model(self):
        self.client.market_data["band"].update(enter=False, entry_regime=None)
        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "model_entry_decision") as control, \
                patch.object(f, "entry_shadow_prediction") as scorer:
            state = self.tick()
        control.assert_not_called()
        scorer.assert_not_called()
        self.assertFalse(state["decision"]["raw_enter"])
        self.assertEqual(self.client.submitted, [])

    def test_validated_entry_gate_cannot_be_overridden_by_model(self):
        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "ENTRY_VALIDATED", False), \
                patch.object(f, "model_entry_decision") as control, \
                patch.object(f, "entry_shadow_prediction") as scorer:
            state = self.tick()
        control.assert_not_called()
        scorer.assert_not_called()
        self.assertTrue(state["decision"]["raw_enter"])
        self.assertFalse(state["decision"]["enter"])
        self.assertEqual(self.client.submitted, [])

    def test_allocation_loss_limit_cannot_be_overridden_by_model(self):
        self.wallet = Decimal("80")
        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "model_entry_decision") as control, \
                patch.object(f, "entry_shadow_prediction") as scorer:
            state = self.tick()
        control.assert_not_called()
        scorer.assert_not_called()
        self.assertEqual(state["halted"], "AllocationLossLimit")
        self.assertEqual(self.client.submitted, [])

    def test_open_position_and_existing_protections_are_not_changed_by_adapter(self):
        before = self.tick()
        orders = deepcopy(self.client.orders)
        submissions = deepcopy(self.client.submitted)
        self.advance()
        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "model_entry_decision") as control, \
                patch.object(f, "entry_shadow_prediction") as scorer:
            after = self.tick()
        control.assert_not_called()
        scorer.assert_not_called()
        for key in ("quantity", "entry_price", "stop_price", "target_price",
                    "stop_client_id", "target_client_id", "leverage"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.client.orders, orders)
        self.assertEqual(self.client.submitted, submissions)

    def test_actual_adapter_accepts_authenticated_same_domain_score(self):
        self.tick()
        self.close_fake_position()
        self.advance(bars=f.COOLDOWN_BARS)
        self.wall = self.client.market_data["now"] - 1
        learner.sync(self.path, self.learning_path)
        self.wall = self.client.market_data["now"]
        with patch.object(f, "MODEL_DECISIONS", True):
            state = self.tick()
        control = state["model_control_latest"]
        self.assertTrue(control["authority_applied"], control)
        self.assertTrue(control["baseline_allowed"])
        self.assertEqual(control["training_sample_count"], 1)
        self.assertEqual(control["domain_sample_count"], 1)
        self.assertIn(control["reason"], ("model_defer", "model_explore"))
        self.assertEqual(state["phase"], "long" if control["allow"] else "cash")
        self.assertEqual(len(self.events("entry_intent")), 2 if control["allow"] else 1)
        self.assertEqual(len(self.events("round_trip_closed")), 1)
        self.assertFalse(control["economic_validation"])

    def test_allowed_prediction_is_durable_before_submit_and_resolves_as_true_forward(self):
        self.tick()
        self.close_fake_position()
        self.advance(bars=f.COOLDOWN_BARS)
        # Train strictly before the next candidate, with a fresh source report.
        self.wall = self.client.market_data["now"] - 1
        trained = learner.sync(self.path, self.learning_path)
        self.assertEqual(trained["compatible_sample_count"], 1)
        self.assertIsNotNone(trained["pre_entry_model_id"])
        self.wall = self.client.market_data["now"]
        captured = []
        original_scorer = f.entry_shadow_prediction
        original_submit = self.client.submit_entry

        def score(db, context, now):
            captured.append(deepcopy(context))
            result = original_scorer(db, context, now)
            self.assertTrue(result["available"], result)
            return result

        def submit(quantity, client_id):
            entry = self.events("entry_intent")[-1]["learning_entry"]
            prediction = entry["shadow_prediction"]
            self.assertEqual({key: value for key, value in entry.items()
                              if key != "shadow_prediction"}, captured[0])
            self.assertEqual(prediction["context_digest"], learner._context_digest(entry))
            self.assertFalse(prediction["decision_authority"])
            self.assertTrue(f.read(self.db)["model_control_latest"]["allow"])
            kinds = [row[0] for row in self.db.execute("SELECT kind FROM events ORDER BY id")]
            self.assertLess(max(i for i, kind in enumerate(kinds) if kind == "model_entry_decision"),
                            max(i for i, kind in enumerate(kinds) if kind == "entry_intent"))
            return original_submit(quantity, client_id)

        with patch.object(f, "MODEL_DECISIONS", True), \
                patch.object(f, "entry_shadow_prediction", side_effect=score) as scorer, \
                patch.object(f, "model_entry_decision", return_value=self.advice(True)), \
                patch.object(self.client, "submit_entry", side_effect=submit):
            state = self.tick()
        scorer.assert_called_once()
        self.assertEqual(state["phase"], "long")
        self.close_fake_position(delta="2")
        report = learner.sync(self.path, self.learning_path)
        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["true_forward_proxy_count"], 1)
        self.assertEqual(report["true_forward_verified_count"], 0)
        self.assertFalse(report["decision_authority"])
        self.assertFalse(report["readiness"]["economic_evidence_ready"])


if __name__ == "__main__":
    unittest.main()
