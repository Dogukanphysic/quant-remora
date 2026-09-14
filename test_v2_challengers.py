import copy
import json
import sqlite3
import unittest

import v2_challengers
import v2_model
import v2_store
from v2_engine import BAR_MS, MAIN_SPEC, POLICY, round_trip_net_return


def historical_sample(index, positive=None):
    positive = bool(index % 2) if positive is None else bool(positive)
    decision_ts = index * 10 * BAR_MS
    fill_ts = decision_ts + BAR_MS
    exit_ts = fill_ts + 2 * BAR_MS
    entry = 100.0
    exit_reference = 101.0 if positive else 99.0
    vector = [
        float((index + feature) % 7) / 7
        for feature in range(len(v2_model.FEATURE_NAMES))
    ]
    vector[-1] = 1.0 if index % 3 else 0.0
    return {
        "id": f"history-{index}",
        "strategy": "breakout" if vector[-1] else "reentry",
        "decision_ts": decision_ts,
        "fill_ts": fill_ts,
        "exit_ts": exit_ts,
        "label_available_ts": exit_ts + BAR_MS,
        "entry_reference": entry,
        "exit_reference": exit_reference,
        "target": entry * 1.02,
        "net_return": round_trip_net_return(
            entry, exit_reference, v2_model.COST_SCENARIOS["30bp"]
        ),
        "x": vector,
        "source": "historical_v2_h8",
    }


class V2ChallengerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.samples = [historical_sample(index) for index in range(40)]
        cls.cutoff = max(sample["label_available_ts"] for sample in cls.samples)
        cls.artifact = v2_challengers.train_frozen_cohort(
            cls.samples,
            cohort_id="test-cohort-a",
            control_model_version="control-model",
            training_cutoff_label_ts=cls.cutoff,
            threshold=0.01,
            created_ts=cls.cutoff,
        )

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        v2_store.ensure_tables(self.db)
        v2_challengers.register_model(self.db, self.artifact)

    def tearDown(self):
        self.db.close()

    def add_prediction(self, suffix="a", *, decision_ts=None, resolved=0):
        decision_ts = (
            self.cutoff + BAR_MS if decision_ts is None else int(decision_ts)
        )
        created_ts = decision_ts + BAR_MS
        event_id = f"event-{suffix}"
        vector = list(self.samples[-1]["x"])
        self.db.execute(
            "INSERT INTO v2_predictions "
            "(event_id,stream_id,spec_id,policy,cost_version,strategy,"
            "decision_ts,fill_ts,atr,model_version,score,threshold,"
            "feature_version,features,accepted,created_ts,resolved,resolved_ts,resolution) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                v2_store.STREAM_ID,
                MAIN_SPEC.spec_id,
                POLICY,
                v2_model.COST_SCENARIOS["30bp"].version,
                "breakout",
                decision_ts,
                decision_ts + BAR_MS,
                1.0,
                "control-model",
                0.4,
                0.6,
                v2_model.FEATURE_VERSION,
                json.dumps(vector, separators=(",", ":")),
                0,
                created_ts,
                resolved,
                created_ts if resolved else None,
                "labeled" if resolved else None,
            ),
        )
        self.db.commit()
        return event_id, created_ts

    def test_schema_supports_multiple_frozen_models_on_same_event(self):
        second = v2_challengers.train_frozen_cohort(
            self.samples,
            cohort_id="test-cohort-b",
            control_model_version="control-model",
            training_cutoff_label_ts=self.cutoff,
            c_value=0.2,
            threshold=0.99,
            created_ts=self.cutoff,
        )
        self.assertTrue(v2_challengers.register_model(self.db, second))
        event_id, created_ts = self.add_prediction()
        self.assertTrue(
            v2_challengers.record_event_score(
                self.db,
                event_id,
                self.artifact["model_version"],
                execution_gate=True,
                created_ts=created_ts,
            )
        )
        self.assertTrue(
            v2_challengers.record_event_score(
                self.db,
                event_id,
                second["model_version"],
                execution_gate=True,
                created_ts=created_ts,
            )
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM v2_challenger_scores WHERE event_id=?",
                (event_id,),
            ).fetchone()[0],
            2,
        )

    def test_score_retry_is_idempotent_and_changed_row_cannot_be_overwritten(self):
        event_id, created_ts = self.add_prediction()
        args = dict(
            db=self.db,
            event_id=event_id,
            model_version=self.artifact["model_version"],
            execution_gate=True,
            created_ts=created_ts,
        )
        self.assertTrue(v2_challengers.record_event_score(**args))
        self.assertFalse(v2_challengers.record_event_score(**args))
        row = self.db.execute(
            "SELECT threshold,would_accept,execution_gate,accepted "
            "FROM v2_challenger_scores"
        ).fetchone()
        self.assertEqual(row, (0.01, 1, 1, 1))
        self.db.execute(
            "UPDATE v2_challenger_scores SET execution_gate=0,accepted=0"
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
            v2_challengers.record_event_score(**args)

    def test_cutoff_late_score_and_wrong_timestamp_fail_closed(self):
        old_event, old_created = self.add_prediction(
            "old", decision_ts=self.cutoff
        )
        with self.assertRaisesRegex(ValueError, "at or before"):
            v2_challengers.record_event_score(
                self.db,
                old_event,
                self.artifact["model_version"],
                execution_gate=True,
                created_ts=old_created,
            )
        event_id, created_ts = self.add_prediction("future")
        with self.assertRaisesRegex(ValueError, "creation timestamp"):
            v2_challengers.record_event_score(
                self.db,
                event_id,
                self.artifact["model_version"],
                execution_gate=True,
                created_ts=created_ts + 1,
            )
        self.db.execute(
            "UPDATE v2_predictions SET resolved=1,resolved_ts=?,resolution='labeled' "
            "WHERE event_id=?",
            (created_ts + BAR_MS, event_id),
        )
        self.db.execute(
            "INSERT INTO v2_samples "
            "(event_id,stream_id,spec_id,policy,cost_version,strategy,fill_ts,"
            "exit_ts,label_available_ts,source,net_return,detail,created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                v2_store.STREAM_ID,
                MAIN_SPEC.spec_id,
                POLICY,
                v2_model.COST_SCENARIOS["30bp"].version,
                "breakout",
                self.cutoff + 2 * BAR_MS,
                self.cutoff + 3 * BAR_MS,
                self.cutoff + 4 * BAR_MS,
                v2_store.SOURCE,
                0.001,
                json.dumps({"entry_reference": 100.0, "exit_reference": 100.5}),
                self.cutoff + 4 * BAR_MS,
            ),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "after its label"):
            v2_challengers.record_event_score(
                self.db,
                event_id,
                self.artifact["model_version"],
                execution_gate=True,
                created_ts=created_ts,
            )

    def test_score_rejects_event_from_a_different_control_model(self):
        event_id, created_ts = self.add_prediction("wrong-control")
        self.db.execute(
            "UPDATE v2_predictions SET model_version='rotated-control' "
            "WHERE event_id=?",
            (event_id,),
        )
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "frozen challenger contract"):
            v2_challengers.record_event_score(
                self.db,
                event_id,
                self.artifact["model_version"],
                execution_gate=True,
                created_ts=created_ts,
            )

    def test_corrupted_registered_artifact_fails_closed(self):
        self.db.execute(
            "UPDATE v2_challenger_models SET artifact_payload='{}'"
        )
        self.db.commit()
        with self.assertRaises(ValueError):
            v2_challengers.load_model(
                self.db, self.artifact["model_version"]
            )

    def test_training_and_stdlib_inference_are_deterministic(self):
        repeated = v2_challengers.train_frozen_cohort(
            list(reversed(self.samples)),
            cohort_id="test-cohort-a",
            control_model_version="control-model",
            training_cutoff_label_ts=self.cutoff,
            threshold=0.01,
            created_ts=self.cutoff,
        )
        self.assertEqual(self.artifact, repeated)
        encoded = json.dumps(self.artifact, sort_keys=True, allow_nan=False)
        loaded = json.loads(encoded)
        first = v2_challengers.predict_probability(
            self.artifact, self.samples[-1]["x"]
        )
        second = v2_challengers.predict_probability(
            loaded, self.samples[-1]["x"]
        )
        self.assertEqual(first, second)
        self.assertTrue(0 <= first <= 1)

    def test_score_core_cannot_change_capital_or_create_execution(self):
        self.db.execute(
            "CREATE TABLE state (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        before = json.dumps({"cash_usd": 1000.0, "position": None})
        self.db.execute("INSERT INTO state VALUES ('portfolio',?)", (before,))
        self.db.commit()
        event_id, created_ts = self.add_prediction()
        v2_challengers.record_event_score(
            self.db,
            event_id,
            self.artifact["model_version"],
            execution_gate=True,
            created_ts=created_ts,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT value FROM state WHERE key='portfolio'"
            ).fetchone()[0],
            before,
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM v2_executions").fetchone()[0],
            0,
        )

    def test_isolated_paper_trade_records_entry_and_exit_only_in_challenger_book(self):
        event_id, created_ts = self.add_prediction("paper")
        v2_challengers.record_event_score(
            self.db, event_id, self.artifact["model_version"],
            execution_gate=True, created_ts=created_ts,
        )
        plan = {
            "quantity": 0.05,
            "entry_reference": 100.0,
            "entry": 100.05,
            "cost": 5.0075025,
            "stop": 99.0,
            "target": 101.0,
            "fee": 0.001,
            "slippage": 0.0005,
        }
        self.assertTrue(v2_challengers.open_paper_position(
            self.db, event_id, self.artifact["model_version"],
            strategy="breakout", plan=plan, bid=99.99,
            quote_ts_ms=created_ts, decision_close_ts_ms=created_ts,
            now_ms=created_ts,
        ))
        opened = v2_challengers.paper_portfolio_snapshot(
            self.db, self.artifact["model_version"]
        )
        self.assertEqual(opened["initial_usd"], 100.0)
        self.assertEqual(opened["open_trades"], 1)
        self.assertIsNotNone(opened["position"])
        self.assertFalse(opened["included_in_main_1000_usd"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM v2_executions").fetchone()[0], 0
        )

        closed = v2_challengers.paper_tick(
            self.db, {"bid": 102.0}, created_ts + BAR_MS
        )
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["exit_reason"], "target")
        self.assertGreater(closed[0]["pnl_usd"], 0)
        final = v2_challengers.paper_portfolio_snapshot(
            self.db, self.artifact["model_version"]
        )
        self.assertEqual(final["open_trades"], 0)
        self.assertEqual(final["closed_trades"], 1)
        self.assertEqual(final["completed_trades"], 1)
        self.assertGreater(final["equity_usd"], 100.0)
        execution = self.db.execute(
            "SELECT status,exit_reason,pnl_usd FROM v2_challenger_executions "
            "WHERE event_id=? AND model_version=?",
            (event_id, self.artifact["model_version"]),
        ).fetchone()
        self.assertEqual(execution[0:2], ("closed", "target"))
        self.assertGreater(execution[2], 0)

    def test_isolated_paper_rejects_non_executable_score(self):
        event_id, created_ts = self.add_prediction("paper-rejected")
        v2_challengers.record_event_score(
            self.db, event_id, self.artifact["model_version"],
            execution_gate=False, created_ts=created_ts,
        )
        plan = {
            "quantity": 0.05, "entry_reference": 100.0, "entry": 100.05,
            "cost": 5.0075025, "stop": 99.0, "target": 101.0,
            "fee": 0.001, "slippage": 0.0005,
        }
        self.assertFalse(v2_challengers.open_paper_position(
            self.db, event_id, self.artifact["model_version"],
            strategy="breakout", plan=plan, bid=99.99,
            quote_ts_ms=created_ts, decision_close_ts_ms=created_ts,
            now_ms=created_ts,
        ))
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM v2_challenger_executions"
            ).fetchone()[0], 0
        )

    def test_zero_labeled_events_reports_collecting_only(self):
        report = v2_challengers.evaluate_model(
            self.db, self.artifact["model_version"]
        )
        self.assertEqual(report["status"], "collecting")
        self.assertEqual(report["matched_future_events"], 0)
        self.assertFalse(report["micro_probe_candidate"])
        self.assertFalse(report["capital_mutation"])

    def test_artifact_contract_fields_are_hashed_and_validated(self):
        for field in (
            "policy",
            "spec_id",
            "signal_version",
            "cost_version",
            "execution_policy_version",
        ):
            broken = copy.deepcopy(self.artifact)
            broken[field] = "wrong-contract"
            with self.subTest(field=field), self.assertRaises(ValueError):
                v2_challengers.validate_artifact(broken)


if __name__ == "__main__":
    unittest.main()
