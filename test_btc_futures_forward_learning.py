"""Offline evidence contracts for genuine pre-entry Futures shadow forecasts.

All ledgers and artifacts are temporary. No worker, exchange client, credential,
or production database is imported or read by these tests.
"""
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import btc_futures_learning as learner


class FuturesForwardLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite3"
        self.learning = self.root / "learning.sqlite3"
        self.epoch = 1_800_000_000
        self.wall = self.epoch
        self.completed = 0
        self.wallet = Decimal("100")
        self.clock = patch.object(learner.time, "time", side_effect=lambda: self.wall)
        self.clock.start()
        with self._db(self.source) as db:
            db.execute("CREATE TABLE state(id INTEGER PRIMARY KEY,value TEXT)")
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,ts REAL,kind TEXT,payload TEXT)")
            state = {"schema": 1, "identity": "isolated-forward-test-account",
                     "initial_wallet_usdt": "100", "wallet_usdt": "100",
                     "completed_round_trips": 0, "phase": "cash"}
            db.execute("INSERT INTO state VALUES (1,?)", (json.dumps(state),))
            db.execute("INSERT INTO events(ts,kind,payload) VALUES (?,?,?)",
                       (self.epoch - 60, "configured", "{}"))

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    @contextmanager
    def _db(self, path):
        db = sqlite3.connect(path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(self, kind, payload, stamp):
        with self._db(self.source) as db:
            return db.execute("INSERT INTO events(ts,kind,payload) VALUES (?,?,?)",
                              (stamp, kind, json.dumps(payload))).lastrowid

    def _context(self, index):
        stamp = self.epoch + index * 7200
        return {"schema": 1, "bar": (stamp - 900) * 1000,
                "captured_at": stamp, "wallet_before_usdt": str(self.wallet),
                "quantity": "0.001", "risk_profile": "moderate", "leverage": 4,
                "atr": "200", "reference_ask": str(85000 + 100 * index),
                "decision": {"rsi": 34 + index, "lower": 82000,
                             "upper": 87000, "entry_limit": 83000,
                             "entry_regime": "trend_reclaim", "raw_enter": True,
                             "enter": True, "recovery_confirmed": True,
                             "trend_confirmed": True, "histogram_rising": True}}

    def _open(self, index, context=None):
        stamp = self.epoch + index * 7200
        intent = {"client_id": f"qrf-e-forward-{index}", "quantity": "0.001",
                  "margin_usdt": "25"}
        activation = {"quantity": "0.001", "entry": str(85000 + 100 * index),
                      "stop": str(84500 + 100 * index),
                      "target": str(86000 + 100 * index),
                      "risk_profile": "moderate", "stop_atr": "2.5", "target_atr": "5"}
        if context is not None:
            intent["learning_entry"] = deepcopy(context)
            activation["learning_entry"] = dict(deepcopy(context),
                entry_price=activation["entry"], stop_price=activation["stop"],
                target_price=activation["target"], filled_at=stamp + 1)
        intent_id = self._event("entry_intent", intent, stamp)
        self._event("long_activated", activation, stamp + 1)
        return {"index": index, "stamp": stamp, "entry_event_id": intent_id,
                "entry": activation.get("learning_entry")}

    def _close(self, opened, delta="-1"):
        stamp = opened["stamp"] + 1800
        self.wallet += Decimal(delta)
        close = {"wallet_usdt": str(self.wallet)}
        if opened["entry"] is not None:
            close["learning_outcome"] = {
                "schema": 1, "entry": deepcopy(opened["entry"]),
                "closed_at": stamp, "wallet_after_usdt": str(self.wallet),
                "wallet_delta_proxy_usdt": delta,
                "pnl_basis": "account_wallet_delta_unverified",
                "execution_verified": False, "exit_reason": "test_closed"}
        self._event("round_trip_closed", close, stamp)
        self.completed += 1
        with self._db(self.source) as db:
            state = json.loads(db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])
            state.update(wallet_usdt=str(self.wallet), completed_round_trips=self.completed)
            db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(state),))
        self.wall = stamp + 1

    def _training(self, count=4, *, enriched=True):
        for index in range(count):
            opened = self._open(index, self._context(index) if enriched else None)
            self._close(opened, "-1" if index % 2 == 0 else "2")
        self.wall = self.epoch + count * 7200 - 1
        return learner.sync(self.source, self.learning)

    def _predict(self, context):
        self.wall = context["captured_at"]
        return learner.predict_entry(self.learning, context, now=self.wall)

    def _assert_no_authority(self, report):
        for key in ("automatic_activation", "decision_authority", "real_orders_enabled",
                    "mainnet_candidate_active"):
            self.assertIs(report[key], False, key)

    def _selected_artifact(self):
        report = learner.status(self.learning)
        with self._db(self.learning) as db:
            row = db.execute("SELECT artifact FROM pre_entry_models WHERE id=?",
                             (report["pre_entry_model_id"],)).fetchone()
        self.assertIsNotNone(row)
        return report["pre_entry_model_id"], json.loads(row[0])

    def test_five_legacy_samples_cannot_seed_a_pre_entry_model(self):
        report = self._training(5, enriched=False)
        self.assertEqual(report["sample_count"], 5)
        self.assertEqual(report["compatible_sample_count"], 0)
        self.assertIsNone(report["pre_entry_model_id"])
        self.assertFalse(self._predict(self._context(5))["available"])
        self.assertEqual(report["true_forward_proxy_count"], 0)
        self._assert_no_authority(report)

    def test_scoring_is_read_only_and_uses_only_compatible_samples(self):
        report = self._training()
        self.assertEqual(report["compatible_sample_count"], 4)
        self.assertIsNotNone(report["pre_entry_model_id"])
        before_source = self.source.read_bytes()
        before_learning = self.learning.read_bytes()
        context = self._context(4)
        original = deepcopy(context)
        prediction = self._predict(context)
        self.assertTrue(prediction["available"])
        self.assertEqual(prediction["feature_schema"], "pre_entry_v1")
        self.assertEqual(prediction["model_id"], report["pre_entry_model_id"])
        self.assertLessEqual(prediction["trained_through_at"], prediction["predicted_at"])
        self.assertLessEqual(prediction["model_created_at"], prediction["predicted_at"])
        self.assertGreaterEqual(prediction["loss_probability"], 0)
        self.assertLessEqual(prediction["loss_probability"], 1)
        self.assertEqual(context, original)
        self.assertEqual(self.source.read_bytes(), before_source)
        self.assertEqual(self.learning.read_bytes(), before_learning)
        self._assert_no_authority(prediction)

    def test_pre_entry_training_does_not_read_actual_fill_or_outcome_features(self):
        self._training()
        _, artifact = self._selected_artifact()
        names = artifact["model"]["feature_names"]
        for forbidden in ("entry_price", "stop_distance_fraction", "target_distance_fraction",
                          "reward_risk_ratio", "filled_at", "exit_reason", "net_pnl_usdt"):
            self.assertNotIn(forbidden, names)
        base = self._context(4)
        original = self._predict(base)
        with_later_data = dict(base, entry_price="1", stop_price="0.5", target_price="2",
                               filled_at=base["captured_at"] + 1, net_pnl_usdt="999",
                               exit_reason="future_result")
        changed = self._predict(with_later_data)
        self.assertTrue(changed["available"])
        self.assertEqual(original["feature_digest"], changed["feature_digest"])
        self.assertEqual(original["loss_probability"], changed["loss_probability"])

    def test_future_created_artifact_is_not_scored(self):
        self._training()
        context = self._context(4)
        context["captured_at"] -= 2
        self.assertFalse(self._predict(context)["available"])

    def test_future_trained_through_artifact_is_not_scored(self):
        for index in range(4):
            self._close(self._open(index, self._context(index)), "-1" if index % 2 == 0 else "2")
        # Simulate a source containing a close timestamp later than the observer clock.
        future_close = self.epoch + 3 * 7200 + 1800
        self.wall = future_close - 2
        learner.sync(self.source, self.learning)
        context = self._context(4)
        context.update(captured_at=future_close - 1, bar=(future_close - 901) * 1000)
        self.assertFalse(self._predict(context)["available"])

    def test_stale_sync_is_not_scored(self):
        self._training()
        context = self._context(4)
        context["captured_at"] += learner.MAX_SOURCE_AGE_SECONDS + 1
        self.assertFalse(self._predict(context)["available"])

    def test_old_training_evidence_is_not_refreshed_by_repeated_sync(self):
        self._training()
        context = self._context(4)
        context["captured_at"] += learner.MAX_MODEL_AGE_SECONDS + 1
        context["bar"] = (context["captured_at"] - 900) * 1000
        self.wall = context["captured_at"] - 1
        learner.sync(self.source, self.learning)
        self.assertFalse(self._predict(context)["available"])

    def test_tampered_model_artifact_is_not_scored(self):
        self._training()
        model_id, artifact = self._selected_artifact()
        artifact["model"]["weights"][0] += 5
        with self._db(self.learning) as db:
            db.execute("UPDATE pre_entry_models SET artifact=? WHERE id=?",
                       (json.dumps(artifact), model_id))
        before = self.learning.read_bytes()
        self.assertFalse(self._predict(self._context(4))["available"])
        report = learner.status(self.learning)
        self.assertIsNone(report["pre_entry_model_id"])
        self.assertFalse(report["readiness"]["technical_ready"])
        self.assertIn("invalid_or_stale_pre_entry_artifact", report["readiness"]["blockers"])
        self.assertEqual(self.learning.read_bytes(), before)

    def test_matching_durable_original_prediction_resolves_exactly_once(self):
        self._training()
        context = self._context(4)
        prediction = self._predict(context)
        self.assertTrue(prediction["available"])
        context["shadow_prediction"] = deepcopy(prediction)
        opened = self._open(4, context)
        pending = learner.sync(self.source, self.learning)
        self.assertEqual(pending["true_forward_proxy_count"], 0)
        self._close(opened, "-1")
        first = learner.sync(self.source, self.learning)
        second = learner.sync(self.source, self.learning)
        self.assertEqual(first["true_forward_proxy_count"], 1)
        self.assertEqual(second["true_forward_proxy_count"], 1)
        self.assertEqual(second["true_forward_verified_count"], 0)
        with self._db(self.learning) as db:
            rows = db.execute("SELECT entry_event_id,model_id,prediction,label "
                              "FROM forward_predictions WHERE closed_event_id IS NOT NULL").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], opened["entry_event_id"])
        self.assertEqual(rows[0][1], prediction["model_id"])
        self.assertEqual(json.loads(rows[0][2]), prediction)
        self.assertEqual(rows[0][3], 1)
        self._assert_no_authority(second)

    def test_scoring_without_durable_original_does_not_fabricate_forward_evidence(self):
        self._training()
        context = self._context(4)
        self.assertTrue(self._predict(context)["available"])
        self._close(self._open(4, context), "2")
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["compatible_sample_count"], 5)
        self.assertEqual(report["true_forward_proxy_count"], 0)
        with self._db(self.learning) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM forward_predictions").fetchone()[0], 0)

    def test_prediction_for_different_context_is_not_forward_evidence(self):
        self._training()
        context = self._context(4)
        prediction = self._predict(context)
        context["decision"]["rsi"] += 10
        context["shadow_prediction"] = prediction
        self._close(self._open(4, context))
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["true_forward_proxy_count"], 0)
        self._assert_no_authority(report)

    def test_prediction_created_after_intent_cannot_become_forward_evidence(self):
        self._training()
        context = self._context(4)
        prediction = self._predict(context)
        prediction["predicted_at"] = context["captured_at"] + 60
        context["shadow_prediction"] = prediction
        self._close(self._open(4, context))
        self.assertEqual(learner.sync(self.source, self.learning)["true_forward_proxy_count"], 0)

    def test_readonly_source_verification_detects_corruption_before_next_sync(self):
        self._training()
        context = self._context(4)
        self.wall = context["captured_at"]
        good = learner.predict_entry(self.learning, context, now=self.wall,
                                     source_path=self.source)
        self.assertTrue(good["available"])
        with self._db(self.source) as db:
            db.execute("UPDATE events SET payload='{}' WHERE kind='entry_intent' AND id="
                       "(SELECT MIN(id) FROM events WHERE kind='entry_intent')")
        before = self.learning.read_bytes()
        invalid = learner.predict_entry(self.learning, context, now=self.wall,
                                        source_path=self.source)
        self.assertFalse(invalid["available"])
        self.assertEqual(self.learning.read_bytes(), before)

    def test_source_corruption_invalidates_forward_evidence_and_readiness(self):
        self._training()
        context = self._context(4)
        context["shadow_prediction"] = self._predict(context)
        self._close(self._open(4, context))
        self.assertEqual(learner.sync(self.source, self.learning)["true_forward_proxy_count"], 1)
        with self._db(self.source) as db:
            db.execute("UPDATE events SET payload='{}' WHERE kind='entry_intent' AND id="
                       "(SELECT MIN(id) FROM events WHERE kind='entry_intent')")
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["status"], "source_conflict")
        self.assertEqual(report["true_forward_proxy_count"], 0)
        self.assertFalse(report["readiness"]["technical_ready"])
        self.assertFalse(report["readiness"]["economic_evidence_ready"])
        self.assertFalse(self._predict(self._context(5))["available"])
        self._assert_no_authority(report)

    def test_more_proxy_samples_never_grant_live_authority(self):
        report = self._training(32)
        self.assertEqual(report["compatible_sample_count"], 32)
        self.assertEqual(report["verified_count"], 0)
        self.assertEqual(report["true_forward_verified_count"], 0)
        self.assertFalse(report["readiness"]["economic_evidence_ready"])
        self._assert_no_authority(report)
        self._assert_no_authority(self._predict(self._context(32)))

    def test_missing_learning_database_is_unavailable_without_creating_file(self):
        context = self._context(0)
        self.assertFalse(self._predict(context)["available"])
        self.assertFalse(self.learning.exists())


if __name__ == "__main__":
    unittest.main()
