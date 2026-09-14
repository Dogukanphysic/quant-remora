import copy
import json
import math
import sqlite3
import unittest
from unittest.mock import patch

from v2_engine import BAR_MS, BarrierSpec, CostModel, MAIN_SPEC
from v2_store import (
    SOURCE,
    STREAM_ID,
    ensure_tables,
    get_execution,
    record_decisions,
    record_execution,
    replay_labels,
    required_fetch_count,
    stable_event_id,
    status,
)


def bars(count=260, base=100.0):
    result = []
    for index in range(count):
        price = base + index * 0.02
        result.append(
            {
                "ts": index * BAR_MS,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": 10.0,
            }
        )
    return result


class V2StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        ensure_tables(self.db)
        self.rows = bars()

    def tearDown(self):
        self.db.close()

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_first_anchor_records_only_latest_closed_bar_without_backfill(self, mocked):
        now = self.rows[221]["ts"]
        ids = record_decisions(self.db, self.rows, None, now)
        self.assertEqual(len(ids), 1)
        prediction = self.db.execute(
            "SELECT decision_ts,fill_ts,score,accepted FROM v2_predictions"
        ).fetchone()
        self.assertEqual(prediction, (self.rows[220]["ts"], self.rows[221]["ts"], None, 0))
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args.args[1], 220)
        current = status(self.db)
        self.assertEqual(current["anchor_decision_ts"], self.rows[220]["ts"])

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_record_and_replay_are_idempotent(self, _mocked):
        now = self.rows[221]["ts"]
        first = record_decisions(self.db, self.rows, None, now)
        second = record_decisions(self.db, self.rows, None, now)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        resolved_first = replay_labels(self.db, self.rows, self.rows[229]["ts"])
        resolved_second = replay_labels(self.db, self.rows, self.rows[229]["ts"])
        self.assertEqual(resolved_first, first)
        self.assertEqual(resolved_second, [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v2_predictions").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v2_samples").fetchone()[0], 1)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_after_insert_failure_rolls_back_prediction_and_shadow_cursor(self, _mocked):
        observed = {}

        def fail(**payload):
            observed.update(payload)
            raise RuntimeError("challenger unavailable")

        now = self.rows[221]["ts"]
        with self.assertRaisesRegex(RuntimeError, "challenger unavailable"):
            record_decisions(self.db, self.rows, None, now, after_insert=fail)
        self.assertEqual(
            set(observed),
            {"db", "event_id", "strategy", "decision_ts", "created_ts", "frozen"},
        )
        self.assertIs(observed["db"], self.db)
        self.assertEqual(observed["strategy"], "breakout")
        self.assertEqual(observed["created_ts"], now)
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM v2_predictions").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM v2_shadow_state").fetchone()[0], 0
        )
        self.assertEqual(len(record_decisions(self.db, self.rows, None, now)), 1)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_prediction_score_and_model_version_are_frozen_at_entry(self, _mocked):
        model = {
            "breakout": {
                "score": 0.71,
                "threshold": 0.65,
                "model_version": "model-a",
                "feature_version": "features-a",
                "features": [0.1, 0.2, 0.3],
                "eligible": True,
            }
        }
        record_decisions(self.db, self.rows, model, self.rows[221]["ts"])
        model["breakout"].update(score=0.05, threshold=0.9, model_version="model-b", eligible=False)
        replay_labels(self.db, self.rows, self.rows[229]["ts"])
        detail = json.loads(self.db.execute("SELECT detail FROM v2_samples").fetchone()[0])
        frozen = detail["metadata"]["prediction"]
        self.assertEqual(frozen["score"], 0.71)
        self.assertEqual(frozen["threshold"], 0.65)
        self.assertEqual(frozen["model_version"], "model-a")
        self.assertEqual(frozen["feature_version"], "features-a")
        self.assertTrue(frozen["accepted"])
        self.assertEqual(detail["x"], [0.1, 0.2, 0.3])
        self.assertEqual(detail["metadata"]["source"], SOURCE)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_unresolved_prediction_locks_same_strategy_overlap(self, _mocked):
        first = record_decisions(self.db, self.rows, None, self.rows[221]["ts"])
        second = record_decisions(self.db, self.rows, None, self.rows[222]["ts"])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v2_predictions").fetchone()[0], 1)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_label_is_written_only_after_its_source_bar_closes(self, _mocked):
        ids = record_decisions(self.db, self.rows, None, self.rows[221]["ts"])
        self.assertEqual(replay_labels(self.db, self.rows, self.rows[229]["ts"] - 1), [])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v2_samples").fetchone()[0], 0)
        self.assertEqual(replay_labels(self.db, self.rows, self.rows[229]["ts"]), ids)
        sample = self.db.execute(
            "SELECT event_id,label_available_ts,source FROM v2_samples"
        ).fetchone()
        self.assertEqual(sample, (ids[0], self.rows[229]["ts"], SOURCE))

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_sleep_gap_replays_exact_event_from_200_bar_window(self, _mocked):
        ids = record_decisions(self.db, self.rows, None, self.rows[71]["ts"])
        replay_window = self.rows[50:250]
        self.assertEqual(replay_labels(self.db, replay_window, self.rows[249]["ts"] + BAR_MS), ids)
        detail = json.loads(self.db.execute("SELECT detail FROM v2_samples").fetchone()[0])
        self.assertEqual(detail["fill_ts"], self.rows[71]["ts"])
        self.assertEqual(detail["holding_bars"], MAIN_SPEC.horizon_bars)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_fetch_window_expands_for_prediction_older_than_default_window(self, _mocked):
        record_decisions(self.db, self.rows, None, self.rows[221]["ts"])
        count = required_fetch_count(self.db, 500 * BAR_MS)
        self.assertEqual(count, 280)
        self.assertEqual(
            self.db.execute("SELECT resolved FROM v2_predictions").fetchone()[0], 0)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_unrecoverable_multi_month_gap_releases_prediction_lock(self, _mocked):
        record_decisions(self.db, self.rows, None, self.rows[221]["ts"])
        count = required_fetch_count(
            self.db, 1_000 * BAR_MS, base_count=240, max_count=300)
        self.assertEqual(count, 240)
        self.assertEqual(
            self.db.execute(
                "SELECT resolved,resolution FROM v2_predictions"
            ).fetchone(),
            (1, "history_retention_exceeded_no_label"),
        )

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_zero_volume_fill_releases_lock_without_fake_sample(self, _mocked):
        ids = record_decisions(self.db, self.rows, None, self.rows[221]["ts"])
        self.rows[221]["volume"] = 0.0
        self.assertEqual(replay_labels(self.db, self.rows, self.rows[222]["ts"]), [])
        prediction = self.db.execute(
            "SELECT resolved,resolution FROM v2_predictions WHERE event_id=?", (ids[0],)
        ).fetchone()
        self.assertEqual(prediction, (1, "no_fill_zero_volume"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v2_samples").fetchone()[0], 0)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_replay_window_exclusion_censors_and_releases_lock(self, _mocked):
        first = record_decisions(self.db, self.rows, None, self.rows[221]["ts"])
        self.assertEqual(len(first), 1)
        late_window = self.rows[225:250]
        self.assertEqual(
            replay_labels(self.db, late_window, self.rows[250]["ts"]), [])
        self.assertEqual(
            self.db.execute(
                "SELECT resolved,resolution FROM v2_predictions WHERE event_id=?", first
            ).fetchone(),
            (1, "replay_window_excluded_prediction_no_label"),
        )
        second = record_decisions(self.db, self.rows, None, self.rows[231]["ts"])
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_old_spec_and_cost_rows_do_not_block_or_resolve_current_scope(self, _mocked):
        old_spec = BarrierSpec(horizon_bars=16)
        old_cost = CostModel(spread=0.001)
        old_id = stable_event_id("breakout", self.rows[210]["ts"], spec=old_spec)
        self.db.execute(
            "INSERT INTO v2_predictions "
            "(event_id,stream_id,spec_id,policy,cost_version,strategy,decision_ts,fill_ts,atr,"
            "model_version,score,threshold,accepted,created_ts,resolved,resolved_ts,resolution) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL)",
            (
                old_id,
                STREAM_ID,
                old_spec.spec_id,
                old_spec.policy,
                old_cost.version,
                "breakout",
                self.rows[210]["ts"],
                self.rows[211]["ts"],
                1.0,
                "old",
                0.9,
                0.5,
                1,
                self.rows[211]["ts"],
            ),
        )
        self.db.commit()
        current_ids = record_decisions(self.db, self.rows, None, self.rows[221]["ts"])
        self.assertEqual(len(current_ids), 1)
        replay_labels(self.db, self.rows, self.rows[229]["ts"])
        old_resolved = self.db.execute(
            "SELECT resolved FROM v2_predictions WHERE event_id=?", (old_id,)
        ).fetchone()[0]
        self.assertEqual(old_resolved, 0)
        current = status(self.db)
        self.assertEqual((current["predictions"], current["resolved"], current["true_forward"]), (1, 1, 1))

    def test_event_id_includes_complete_store_contract(self):
        decision_ts = self.rows[220]["ts"]
        default_cost = CostModel()
        expected_payload = (
            f"{MAIN_SPEC.spec_id}|{default_cost.version}|{STREAM_ID}|breakout|{decision_ts}"
        )
        import hashlib

        expected = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()[:24]
        self.assertEqual(stable_event_id("breakout", decision_ts), expected)

    def test_cost_versions_cannot_collide_on_primary_event_id(self):
        decision_ts = self.rows[220]["ts"]
        first = stable_event_id("breakout", decision_ts, cost=CostModel())
        second = stable_event_id(
            "breakout", decision_ts, cost=CostModel(spread=0.001))
        self.assertNotEqual(first, second)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_execution_is_linked_queryable_and_idempotent(self, _mocked):
        model = {
            "breakout": {
                "score": 0.8,
                "threshold": 0.7,
                "model_version": "model-a",
                "eligible": True,
            }
        }
        event_id = record_decisions(
            self.db, self.rows, model, self.rows[221]["ts"]
        )[0]
        args = (
            self.db,
            event_id,
            "model-a",
            self.rows[221]["ts"],
            self.rows[224]["ts"],
            "micro_probe",
            0.004,
            0.02,
            5.0,
            "take_profit",
        )
        self.assertTrue(record_execution(*args))
        self.assertFalse(self.db.in_transaction)
        self.assertFalse(record_execution(*args))
        self.assertEqual(
            get_execution(self.db, event_id),
            {
                "event_id": event_id,
                "model_version": "model-a",
                "entry_ts_ms": self.rows[221]["ts"],
                "exit_ts_ms": self.rows[224]["ts"],
                "entry_mode": "micro_probe",
                "net_return": 0.004,
                "pnl_usd": 0.02,
                "cost_usd": 5.0,
                "exit_reason": "take_profit",
            },
        )
        current = status(self.db)
        self.assertEqual(current["executions"], 1)
        self.assertAlmostEqual(current["execution_pnl_usd"], 0.02)
        self.assertAlmostEqual(current["execution_cost_usd"], 5.0)

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_execution_rejects_missing_mismatch_and_overwrite(self, _mocked):
        model = {
            "breakout": {
                "score": 0.8,
                "threshold": 0.7,
                "model_version": "model-a",
                "eligible": True,
            }
        }
        event_id = record_decisions(
            self.db, self.rows, model, self.rows[221]["ts"]
        )[0]
        valid = dict(
            db=self.db,
            event_id=event_id,
            model_version="model-a",
            entry_ts_ms=self.rows[221]["ts"],
            exit_ts_ms=self.rows[224]["ts"],
            entry_mode="normal",
            net_return=-0.003,
            pnl_usd=-0.15,
            cost_usd=50.0,
            exit_reason="stop",
        )
        with self.assertRaisesRegex(ValueError, "matching v2 prediction"):
            record_execution(**{**valid, "event_id": "missing"})
        with self.assertRaisesRegex(ValueError, "does not match"):
            record_execution(**{**valid, "model_version": "model-b"})
        self.assertTrue(record_execution(**valid))
        with self.assertRaisesRegex(ValueError, "already frozen"):
            record_execution(**{**valid, "pnl_usd": -0.16,
                                "net_return": -0.0032})

    @patch("v2_store.candidate_signals", return_value=("breakout",))
    def test_execution_validates_numbers_and_timestamps(self, _mocked):
        event_id = record_decisions(
            self.db,
            self.rows,
            {"breakout": {"model_version": "model-a"}},
            self.rows[221]["ts"],
        )[0]
        valid = dict(
            db=self.db,
            event_id=event_id,
            model_version="model-a",
            entry_ts_ms=self.rows[221]["ts"],
            exit_ts_ms=self.rows[224]["ts"],
            entry_mode="micro_probe",
            net_return=0.0,
            pnl_usd=0.0,
            cost_usd=1.0,
            exit_reason="horizon",
        )
        invalid = (
            {"entry_ts_ms": self.rows[220]["ts"]},
            {"exit_ts_ms": self.rows[220]["ts"]},
            {"net_return": math.nan},
            {"pnl_usd": math.inf},
            {"cost_usd": -0.01},
            {"net_return": 0.1},
            {"entry_mode": " "},
            {"exit_reason": ""},
            {"manage_transaction": "yes"},
        )
        for replacement in invalid:
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                record_execution(**{**valid, **replacement})


if __name__ == "__main__":
    unittest.main()
