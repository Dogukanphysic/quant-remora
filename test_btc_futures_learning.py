"""Offline contracts for the mainnet Futures trade-learning sidecar.

Every source ledger is an isolated temporary SQLite file.  These tests never
import the trading worker, read the real ledger, or contact an exchange.
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import btc_futures_learning as learner


class FuturesTradeLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.sqlite3"
        self.learning = self.root / "learning.sqlite3"
        self.initial_wallet = Decimal("100")
        self.now = 1_800_000_000
        self._create_source(self.source)

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def _connection(self, path):
        db = sqlite3.connect(path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _create_source(self, path, *, identity="offline-test-account"):
        state = {
            "schema": 1,
            "identity": identity,
            "contract": "btc-usdm-isolated-4x-bollinger-long-v2",
            "symbol": "BTCUSDT",
            "interval": "15m",
            "initial_wallet_usdt": str(self.initial_wallet),
            "wallet_usdt": str(self.initial_wallet),
            "completed_round_trips": 0,
            "phase": "cash",
        }
        with self._connection(path) as db:
            db.execute("CREATE TABLE state(id INTEGER PRIMARY KEY,value TEXT)")
            db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,ts REAL,kind TEXT,payload TEXT)")
            db.execute("INSERT INTO state VALUES (1,?)", (json.dumps(state),))
            db.execute("INSERT INTO events(ts,kind,payload) VALUES (?,?,?)", (
                self.now - 60, "configured", json.dumps({
                    "margin_usdt": "25", "leverage": 4,
                    "risk_profile": "moderate", "entry_mode": "exploratory",
                }),
            ))

    def _event(self, kind, payload, *, ts=None, path=None):
        with self._connection(path or self.source) as db:
            cursor = db.execute("INSERT INTO events(ts,kind,payload) VALUES (?,?,?)", (
                self.now if ts is None else ts, kind, json.dumps(payload),
            ))
            return cursor.lastrowid

    def _state(self, *, path=None, **updates):
        with self._connection(path or self.source) as db:
            state = json.loads(db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])
            state.update(updates)
            db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(state),))

    def _trip(self, index=0, *, wallet="99", path=None, intent_extra=None,
              close_extra=None, include_intent=True, duplicate_activation=False):
        start = self.now + index * 7200
        intent = {"client_id": f"qrf-e-offline-{index}", "quantity": "0.001",
                  "margin_usdt": "25"}
        intent.update(intent_extra or {})
        if include_intent:
            self._event("entry_intent", intent, ts=start, path=path)
        activation = {
            "quantity": "0.001", "entry": str(85000 + index * 100),
            "stop": str(84500 + index * 100), "target": str(86000 + index * 100),
            "risk_profile": "moderate", "stop_atr": "2.5", "target_atr": "5",
        }
        learning_entry = intent.get("learning_entry")
        if learning_entry is not None:
            learning_entry = dict(learning_entry, entry_price=activation["entry"],
                                  stop_price=activation["stop"], target_price=activation["target"],
                                  filled_at=start)
            activation["learning_entry"] = learning_entry
        self._event("long_activated", activation, ts=start, path=path)
        if duplicate_activation:
            self._event("long_activated", activation, ts=start + 1, path=path)
        close = {"wallet_usdt": str(wallet), "exit_order": {
            "client_id": f"qrf-s-offline-{index}", "type": "STOP_MARKET", "status": "FILLED",
        }, "cooldown_bars": 4}
        if learning_entry is not None:
            close["learning_outcome"] = {
                "schema": 1, "entry": learning_entry, "closed_at": start + 1800,
                "wallet_after_usdt": str(wallet),
                "wallet_delta_proxy_usdt": str(Decimal(wallet) - Decimal(learning_entry["wallet_before_usdt"])),
                "pnl_basis": "account_wallet_delta_unverified", "execution_verified": False,
                "exit_reason": "STOP_MARKET", "exit_order": close["exit_order"],
            }
        close.update(close_extra or {})
        self._event("round_trip_closed", close, ts=start + 1800, path=path)
        self._state(path=path, wallet_usdt=str(wallet), completed_round_trips=index + 1)

    def _losses(self, count=5):
        for index in range(count):
            self._trip(index, wallet=str(self.initial_wallet - index - 1))

    def _samples(self, path=None):
        with self._connection(path or self.learning) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM samples ORDER BY closed_event_id")]

    def _assert_shadow(self, report):
        self.assertIs(report["decision_authority"], False)
        self.assertIs(report["automatic_activation"], False)
        self.assertIs(report["real_orders_enabled"], False)

    def test_five_legacy_trades_are_unverified_wallet_proxies(self):
        self._losses()
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 5)
        self.assertEqual(report["proxy_count"], 5)
        self.assertEqual(report["verified_count"], 0)
        self.assertEqual(report["quarantined_count"], 0)
        samples = self._samples()
        self.assertEqual(len(samples), 5)
        self.assertEqual([Decimal(row["wallet_delta_proxy_usdt"]) for row in samples],
                         [Decimal("-1")] * 5)
        self.assertTrue(all(row["label"] == 1 for row in samples))
        self.assertTrue(all("proxy" in row["quality"].lower() for row in samples))
        self._assert_shadow(report)

    def test_sync_and_status_do_not_modify_source_bytes_or_content(self):
        self._losses()
        before_bytes = self.source.read_bytes()
        with self._connection(self.source) as db:
            before_dump = list(db.iterdump())
        learner.sync(self.source, self.learning)
        learner.status(self.learning)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(),
                         hashlib.sha256(before_bytes).digest())
        with self._connection(self.source) as db:
            self.assertEqual(list(db.iterdump()), before_dump)

    def test_replay_is_idempotent_and_does_not_retrain_same_dataset(self):
        self._losses()
        first = learner.sync(self.source, self.learning)
        rows = self._samples()
        second = learner.sync(str(self.source), str(self.learning))
        self.assertEqual(second["sample_count"], first["sample_count"])
        self.assertEqual(second["model_id"], first["model_id"])
        self.assertEqual(second["training_runs"], first["training_runs"])
        self.assertEqual(self._samples(), rows)
        self._assert_shadow(second)

    def test_new_completed_trade_adds_one_model_version(self):
        self._losses(4)
        first = learner.sync(self.source, self.learning)
        self._trip(4, wallet="95")
        second = learner.sync(self.source, self.learning)
        self.assertEqual(second["sample_count"], 5)
        self.assertEqual(second["trained_sample_count"], 5)
        self.assertEqual(second["training_runs"], first["training_runs"] + 1)
        self.assertNotEqual(second["model_id"], first["model_id"])
        self.assertEqual([prediction["trained_sample_count"]
                          for prediction in second["validation"]["predictions"]], [1, 2, 3, 4])
        self._assert_shadow(second)

    def test_single_class_losses_are_not_reported_as_validated_model(self):
        self._losses()
        report = learner.sync(self.source, self.learning)
        self.assertIn("insufficient", report["status"])
        self.assertIs(report["single_class"], True)
        self.assertEqual(report["label_classes"], [1])
        self.assertEqual(report["verified_count"], 0)
        self._assert_shadow(report)
        self.assertEqual(learner.status(self.learning)["model_id"], report["model_id"])

    def test_historical_features_ignore_later_state_decision_and_exit_metadata(self):
        self._trip(wallet="99")
        learner.sync(self.source, self.learning)
        baseline = json.loads(self._samples()[0]["features"])
        alternative = self.root / "alternative-source.sqlite3"
        alternative_learning = self.root / "alternative-learning.sqlite3"
        self._create_source(alternative)
        self._trip(wallet="110", path=alternative, close_extra={
            "rsi": 99, "atr": 100000, "entry_regime": "future_winner",
            "net_pnl_usdt": "10", "features": {"rsi": 99, "atr": 100000},
        })
        self._state(path=alternative, decision={"rsi": 99, "entry_regime": "future_winner"},
                    last_bar=self.now * 1000 + 99_000_000)
        learner.sync(alternative, alternative_learning)
        actual = json.loads(self._samples(alternative_learning)[0]["features"])
        self.assertEqual(actual, baseline)

    def test_entry_snapshot_features_stay_causal_when_exit_changes(self):
        entry = {
            "schema": 1, "bar": (self.now - 900) * 1000, "captured_at": self.now,
            "wallet_before_usdt": "100", "quantity": "0.001", "risk_profile": "moderate",
            "leverage": 4, "atr": "200", "reference_ask": "85000",
            "decision": {"rsi": 34, "lower": 82000, "upper": 87000,
                         "entry_limit": 83000, "entry_regime": "trend_reclaim",
                         "recovery_confirmed": True, "trend_confirmed": True,
                         "histogram_rising": True, "raw_enter": True, "enter": True},
        }
        self._trip(intent_extra={"learning_entry": entry})
        learner.sync(self.source, self.learning)
        baseline = json.loads(self._samples()[0]["features"])
        alternative = self.root / "enriched-alternative.sqlite3"
        alternate_learning = self.root / "enriched-learning.sqlite3"
        self._create_source(alternative)
        self._trip(wallet="105", path=alternative, intent_extra={"learning_entry": entry},
                   close_extra={"rsi": 99, "decision": {"rsi": 99}, "net_pnl_usdt": "5"})
        self._state(path=alternative, decision={"rsi": 99, "entry_regime": "future"})
        learner.sync(alternative, alternate_learning)
        self.assertEqual(json.loads(self._samples(alternate_learning)[0]["features"]), baseline)

    def test_snapshot_captured_after_trade_close_is_quarantined(self):
        entry = {
            "schema": 1, "bar": (self.now - 900) * 1000,
            "captured_at": self.now + 3600,
            "wallet_before_usdt": "100", "quantity": "0.001", "risk_profile": "moderate",
            "leverage": 4, "decision": {"rsi": 34, "entry_regime": "trend_reclaim"},
        }
        self._trip(intent_extra={"learning_entry": entry})
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertGreater(report["quarantined_count"], 0)
        self.assertIsNone(report["model_id"])

    def test_snapshot_with_future_candle_is_quarantined(self):
        entry = {
            "schema": 1, "bar": (self.now + 900) * 1000, "captured_at": self.now,
            "wallet_before_usdt": "100", "quantity": "0.001", "risk_profile": "moderate",
            "leverage": 4, "decision": {"rsi": 34, "entry_regime": "trend_reclaim"},
        }
        self._trip(intent_extra={"learning_entry": entry})
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertGreater(report["quarantined_count"], 0)
        self.assertIsNone(report["model_id"])

    def test_legacy_position_closed_by_updated_worker_retains_proxy_provenance(self):
        self._trip(close_extra={"learning_outcome": {
            "schema": 1, "entry": None, "closed_at": self.now + 1800,
            "wallet_after_usdt": "99", "wallet_delta_proxy_usdt": None,
            "pnl_basis": "account_wallet_delta_unverified", "execution_verified": False,
            "exit_reason": "STOP_MARKET",
        }})
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["proxy_count"], 1)
        self.assertEqual(report["verified_count"], 0)
        self.assertEqual(report["quarantined_count"], 0)
        self.assertEqual(Decimal(self._samples()[0]["wallet_delta_proxy_usdt"]), Decimal("-1"))
        self._assert_shadow(report)

    def test_missing_entry_intent_is_quarantined(self):
        self._trip(include_intent=False)
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertGreater(report["quarantined_count"], 0)
        self.assertIsNone(report["model_id"])
        self._assert_shadow(report)

    def test_ambiguous_duplicate_activation_is_quarantined(self):
        self._trip(duplicate_activation=True)
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertGreater(report["quarantined_count"], 0)
        self.assertIsNone(report["model_id"])

    def test_orphan_close_is_quarantined(self):
        self._event("round_trip_closed", {"wallet_usdt": "99"})
        self._state(completed_round_trips=1, wallet_usdt="99")
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertGreater(report["quarantined_count"], 0)

    def test_open_trade_is_not_a_completed_training_example(self):
        self._event("entry_intent", {"client_id": "qrf-e-open", "quantity": "0.001", "margin_usdt": "25"})
        self._event("long_activated", {"quantity": "0.001", "entry": "85000", "stop": "84500", "target": "86000"})
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 0)
        self.assertIsNone(report["model_id"])

    def test_source_and_learning_path_must_differ(self):
        before = self.source.read_bytes()
        with self.assertRaises(ValueError):
            learner.sync(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), before)

    def test_mutated_prior_event_invalidates_model_and_marks_source_conflict(self):
        self._losses()
        learner.sync(self.source, self.learning)
        with self._connection(self.source) as db:
            row = db.execute("SELECT id,payload FROM events WHERE kind='round_trip_closed' ORDER BY id LIMIT 1").fetchone()
            payload = json.loads(row[1])
            payload["wallet_usdt"] = "150"
            db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["status"], "source_conflict")
        self.assertIsNone(report["model_id"])
        self.assertEqual(report["sample_count"], 0)
        self.assertIsNotNone(report["last_error_at"])
        self._assert_shadow(report)

    def test_source_event_reset_invalidates_prior_evidence(self):
        self._losses()
        learner.sync(self.source, self.learning)
        with self._connection(self.source) as db:
            db.execute("DELETE FROM events WHERE kind<>'configured'")
        self._state(completed_round_trips=0, wallet_usdt="100")
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["status"], "source_conflict")
        self.assertIsNone(report["model_id"])
        self.assertEqual(report["sample_count"], 0)
        self._assert_shadow(report)

    def test_source_account_identity_change_is_rejected(self):
        self._losses()
        learner.sync(self.source, self.learning)
        self._state(identity="different-offline-account")
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["status"], "source_conflict")
        self.assertIsNone(report["model_id"])

    def test_routine_state_updates_do_not_rewrite_historical_features(self):
        self._losses()
        first = learner.sync(self.source, self.learning)
        samples = self._samples()
        self._state(last_poll=self.now + 100000, decision={"rsi": 98},
                    wallet_usdt="96", unrealized_pnl_usdt="1")
        second = learner.sync(self.source, self.learning)
        self.assertNotEqual(second["status"], "source_error")
        self.assertEqual(second["model_id"], first["model_id"])
        self.assertEqual(second["training_runs"], first["training_runs"])
        self.assertEqual(self._samples(), samples)

    def test_source_read_failure_is_visible_and_recovery_restores_current_model(self):
        self._losses()
        first = learner.sync(self.source, self.learning)
        source_bytes = self.source.read_bytes()
        self.source.write_bytes(b"temporarily unreadable sqlite source")
        failed = learner.sync(self.source, self.learning)
        self.assertEqual(failed["status"], "source_error")
        self.assertIsNone(failed["model_id"])
        self.assertIsNotNone(failed["last_error_at"])
        self.assertEqual(failed["last_success_at"], first["last_success_at"])
        self.assertEqual(learner.status(self.learning)["status"], "source_error")
        self._assert_shadow(failed)
        self.source.write_bytes(source_bytes)
        recovered = learner.sync(self.source, self.learning)
        self.assertNotIn(recovered["status"], {"source_error", "source_conflict"})
        self.assertEqual(recovered["model_id"], first["model_id"])
        self.assertEqual(recovered["training_runs"], first["training_runs"])
        self.assertIsNotNone(recovered["last_success_at"])


if __name__ == "__main__":
    unittest.main()
