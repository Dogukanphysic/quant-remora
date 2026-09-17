from decimal import Decimal, ROUND_DOWN, getcontext, setcontext
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

import binance_testnet_worker as worker
import testnet_learning_store as store


DAY_MS = store.DAY_MS
BASE_MS = 1_700_006_399_999


class TestnetLearningStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source_path = root / "worker.sqlite3"
        self.learning_path = root / "online.sqlite3"
        self.db = worker._connect(self.source_path)
        self.policy = worker.POLICY
        self.model = worker.POLICY_SPEC_HASH
        registration = self.db.execute(
            """SELECT * FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model),
        ).fetchone()
        self.cutoff = int(registration["oos_decision_created_cutoff_ms"])

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def _insert_decision(
        self,
        index,
        close,
        momentum,
        *,
        created_ms=None,
        feature=True,
        action="hold_cash",
    ):
        candle = BASE_MS + index * DAY_MS
        self.db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                feature_schema, feature_json, target_long, action, client_id,
                created_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                candle,
                self.policy,
                store._decimal_text(close, "close"),
                "100",
                store._decimal_text(momentum, "momentum"),
                "test_features_v1" if feature else None,
                json.dumps({"momentum": str(momentum)}, separators=(",", ":"))
                if feature
                else None,
                int(Decimal(str(momentum)) > Decimal("0.10")),
                action,
                self.cutoff + index + 1 if created_ms is None else created_ms,
            ),
        )
        return candle

    def _capture_and_insert(
        self,
        index,
        close,
        momentum,
        *,
        feature=True,
        now_ms=None,
        created_ms=None,
    ):
        signal = {"candle_close_ms": BASE_MS + index * DAY_MS,
                  "close_latest": Decimal(str(close))}
        result = store.capture_daily_label(
            self.db,
            signal,
            self.policy,
            self.model,
            self.cutoff + index + 100 if now_ms is None else now_ms,
        )
        self._insert_decision(
            index,
            close,
            momentum,
            feature=feature,
            created_ms=created_ms,
        )
        return result

    @staticmethod
    def _pattern(index):
        high = index % 4 < 2
        return Decimal("0.08") if high else Decimal("0"), (
            Decimal("0.01") if high else Decimal("-0.01")
        )

    def _append_pattern_labels(
        self, first_sample, count, close, *, append_after_ms=None
    ):
        current = Decimal(str(close))
        for sample_index in range(first_sample, first_sample + count):
            _momentum, forward = self._pattern(sample_index)
            next_momentum, _unused = self._pattern(sample_index + 1)
            current *= Decimal("1") + forward
            append_ms = (
                None
                if append_after_ms is None
                else append_after_ms + sample_index - first_sample + 1
            )
            self._capture_and_insert(
                sample_index + 1,
                current,
                next_momentum,
                now_ms=append_ms,
                created_ms=append_ms,
            )
        return current

    def _seed_pattern(self, count):
        momentum, _forward = self._pattern(0)
        close = Decimal("100")
        self._insert_decision(0, close, momentum)
        return self._append_pattern_labels(0, count, close)

    def _seed_flat(self, count):
        close = Decimal("100")
        self._insert_decision(0, close, Decimal("0"))
        for index in range(count):
            self._capture_and_insert(index + 1, close, Decimal("0"))
        return close

    def _seed_review_ready(self, *, bind_offset=20_000):
        close = self._seed_pattern(60)
        bind_ms = self.cutoff + bind_offset
        frozen = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms
        )
        self.assertIsNotNone(frozen["latest"]["frozen_candidate_sha256"])
        close = self._append_pattern_labels(
            60, 140, close, append_after_ms=bind_ms
        )
        for entry_index in range(64, 96, 4):
            self._open_at(f"ready-entry-{entry_index}", entry_index)
            self._close_at(f"ready-exit-{entry_index}", entry_index + 2)
        ready = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms + 1000
        )
        self.assertEqual(ready["status"], "proposal_ready_for_review")
        self.assertTrue(ready["proposal_ready_for_review"])
        return close, bind_ms, ready

    def _insert_intent(
        self,
        client_id,
        side,
        decision_index,
        quote,
        *,
        realized=None,
        commission=None,
    ):
        candle = BASE_MS + decision_index * DAY_MS
        self.db.execute(
            """INSERT INTO worker_order_intents
               (client_id, policy, decision_ms, candle_close_ms, symbol, side,
                state, executed_qty, net_base_qty, cumulative_quote_qty,
                realized_pnl_usdt, commission_by_asset, created_ms, updated_ms)
               VALUES (?, ?, ?, ?, 'BTCUSDT', ?, 'filled', '0.001', ?, ?, ?, ?, ?, ?)""",
            (
                client_id,
                self.policy,
                candle,
                candle,
                side,
                "0.001" if side == "BUY" else None,
                str(quote),
                None if realized is None else str(realized),
                json.dumps(commission or {}, sort_keys=True, separators=(",", ":")),
                self.cutoff + decision_index + 500,
                self.cutoff + decision_index + 500,
            ),
        )

    def _open_at(self, client_id, decision_index, *, cost="100"):
        self._insert_intent(client_id, "BUY", decision_index, cost)
        self.db.execute(
            """UPDATE worker_state SET position_qty='0.001',
               position_quote_cost=?, pnl_complete=1,
               pnl_incomplete_reason=NULL WHERE singleton=1""",
            (cost,),
        )
        return store.open_round_trip(
            self.db, client_id, self.policy, self.model, self.cutoff + 1000
        )

    def _close_at(
        self,
        client_id,
        decision_index,
        *,
        proceeds="102",
        realized="2",
        complete=True,
    ):
        self._insert_intent(
            client_id,
            "SELL",
            decision_index,
            proceeds,
            realized=realized if complete else None,
        )
        self.db.execute(
            """UPDATE worker_state SET position_qty='0', position_quote_cost='0',
               pnl_complete=?, pnl_incomplete_reason=? WHERE singleton=1""",
            (int(complete), None if complete else "incomplete fixture"),
        )
        return store.close_round_trip(
            self.db,
            client_id,
            self.policy,
            self.model,
            complete,
            self.cutoff + 1100,
        )

    def test_exact_daily_label_is_idempotent_and_flags_come_from_cutoff(self):
        self._insert_decision(
            0, "100", "0.08", created_ms=self.cutoff - 1
        )
        result = store.capture_daily_label(
            self.db,
            {"candle_close_ms": BASE_MS + DAY_MS,
             "close_latest": Decimal("101")},
            self.policy,
            self.model,
            self.cutoff + 10,
        )
        duplicate = store.capture_daily_label(
            self.db,
            {"candle_close_ms": BASE_MS + DAY_MS,
             "close_latest": Decimal("101")},
            self.policy,
            self.model,
            self.cutoff + 11,
        )
        row = self.db.execute(
            "SELECT * FROM worker_learning_daily_labels"
        ).fetchone()
        sample = json.loads(row["sample_json"])
        self.assertEqual(result["record_id"], duplicate["record_id"])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM worker_learning_daily_labels"
            ).fetchone()[0],
            1,
        )
        self.assertFalse(sample["out_of_sample"])
        self.assertFalse(sample["true_forward_after_freeze"])
        self.assertNotIn("freeze_id", sample)
        self.assertEqual(sample["forward_return"], "0.01")

    def test_causal_backfill_seals_older_exact_horizons_without_gap(self):
        self._insert_decision(0, "100", "0.08")
        self._insert_decision(1, "101", "0.08")
        self._insert_decision(2, "102", "0")

        first = store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": BASE_MS + DAY_MS,
                "close_latest": Decimal("101"),
            },
            self.policy,
            self.model,
            self.cutoff + 200,
        )
        second = store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": BASE_MS + 2 * DAY_MS,
                "close_latest": Decimal("102"),
            },
            self.policy,
            self.model,
            self.cutoff + 201,
        )
        self.assertEqual(first["status"], "label_sealed")
        self.assertEqual(second["status"], "label_sealed")
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM worker_learning_daily_labels"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM worker_learning_daily_gaps"
            ).fetchone()[0],
            0,
        )

    def test_source_revision_bumps_once_for_each_eligibility_transition(self):
        def revision():
            return self.db.execute(
                """SELECT learning_revision FROM worker_learning_meta
                   WHERE singleton=1"""
            ).fetchone()[0]

        self.assertEqual(revision(), 0)
        self._seed_pattern(1)
        self.assertEqual(revision(), 1)
        duplicate = store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": BASE_MS + DAY_MS,
                "close_latest": Decimal("101"),
            },
            self.policy,
            self.model,
            self.cutoff + 300,
        )
        self.assertEqual(duplicate["status"], "label_sealed")
        self.assertEqual(revision(), 1)

        store.record_source_error(self.db, "revision fixture", self.cutoff + 301)
        store.record_source_error(self.db, "revision fixture", self.cutoff + 301)
        self.assertEqual(revision(), 2)

        self._open_at("revision-entry", 0)
        self._close_at("revision-exit", 1)
        self.assertEqual(revision(), 4)

        gap_signal = {
            "candle_close_ms": BASE_MS + 3 * DAY_MS,
            "close_latest": Decimal("102"),
        }
        store.capture_daily_label(
            self.db,
            gap_signal,
            self.policy,
            self.model,
            self.cutoff + 302,
        )
        store.capture_daily_label(
            self.db,
            gap_signal,
            self.policy,
            self.model,
            self.cutoff + 303,
        )
        self.assertEqual(revision(), 5)

        payload = store._canonical_json(
            {
                "schema": 1,
                "operation": "capture_daily_label",
                "reference": {
                    "candle_close_ms": BASE_MS + 3 * DAY_MS,
                    "policy": self.policy,
                    "model_version": self.model,
                },
            }
        )
        digest = store._sha256(json.loads(payload))
        event_id = f"learning-outbox:{digest}"
        self.db.execute(
            """INSERT INTO worker_learning_outbox
               (event_id, operation, payload_json, payload_sha256,
                created_ms, attempt_count)
               VALUES (?, 'capture_daily_label', ?, ?, ?, 1)""",
            (event_id, payload, digest, self.cutoff + 304),
        )
        self.assertEqual(revision(), 6)
        self.db.execute(
            """UPDATE worker_learning_outbox SET last_error='retry'
               WHERE event_id=?""",
            (event_id,),
        )
        self.assertEqual(revision(), 6)
        self.db.execute(
            """UPDATE worker_learning_outbox SET resolved_ms=?
               WHERE event_id=?""",
            (self.cutoff + 305, event_id),
        )
        self.assertEqual(revision(), 7)

    def test_gap_is_recorded_and_never_becomes_a_label(self):
        self._insert_decision(0, "100", "0.08")
        result = store.capture_daily_label(
            self.db,
            {"candle_close_ms": BASE_MS + 2 * DAY_MS,
             "close_latest": Decimal("102")},
            self.policy,
            self.model,
            self.cutoff + 20,
        )
        self.assertEqual(result["status"], "gap_recorded")
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM worker_learning_daily_labels"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT reason FROM worker_learning_daily_gaps"
            ).fetchone()[0],
            "non_contiguous_daily_horizon",
        )

    def test_exact_round_trip_closes_once_and_incomplete_one_is_quarantined(self):
        self._insert_decision(0, "100", "0.20", action="buy")
        self._insert_decision(1, "102", "0", action="sell")
        opened = self._open_at("entry-1", 0)
        closed = self._close_at("exit-1", 1)
        duplicate = store.close_round_trip(
            self.db,
            "exit-1",
            self.policy,
            self.model,
            True,
            self.cutoff + 1200,
        )
        self.assertEqual(opened["status"], "open")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["record_id"], duplicate["record_id"])
        row = self.db.execute(
            """SELECT * FROM worker_learning_round_trips
               WHERE status='closed'"""
        ).fetchone()
        self.assertEqual(row["realized_pnl_usdt"], "2")
        self.assertEqual(row["net_return"], "0.02")
        self.assertEqual(row["entry_momentum"], "0.2")
        self.assertEqual(row["exit_momentum"], "0")

        self._open_at("entry-2", 0)
        quarantined = self._close_at(
            "exit-2", 1, proceeds="99", realized="-1", complete=False
        )
        self.assertEqual(quarantined["status"], "quarantined")
        self.assertFalse(quarantined["exact_pnl"])

    def test_legacy_round_trip_binding_columns_migrate_neutral_and_stay_excluded(self):
        self._insert_decision(0, "100", "0.20", action="buy")
        self._insert_decision(1, "102", "0", action="sell")
        self._open_at("legacy-bind-buy", 0)
        self._close_at("legacy-bind-sell", 1)
        rows = self.db.execute(
            """SELECT record_id, status, record_json
               FROM worker_learning_round_trips"""
        ).fetchall()
        for row in rows:
            record = json.loads(row["record_json"])
            for field in (
                "entry_freeze_id",
                "entry_freeze_bound_ms",
                "entry_decision_created_ms",
                "entry_candidate_bound",
            ):
                record.pop(field)
            encoded = store._canonical_json(record)
            digest = store._sha256(record)
            self.db.execute(
                """UPDATE worker_learning_round_trips SET
                   record_id=?, record_json=?, record_sha256=?,
                   entry_freeze_id=NULL, entry_freeze_bound_ms=NULL,
                   entry_decision_created_ms=NULL, entry_candidate_bound=0
                   WHERE record_id=?""",
                (
                    f"legacy:{row['status']}:{digest[:32]}",
                    encoded,
                    digest,
                    row["record_id"],
                ),
            )
        for column in (
            "entry_candidate_bound",
            "entry_decision_created_ms",
            "entry_freeze_bound_ms",
            "entry_freeze_id",
        ):
            self.db.execute(
                f"ALTER TABLE worker_learning_round_trips DROP COLUMN {column}"
            )
        store.ensure_source_schema(
            self.db, self.policy, self.model, self.cutoff + 15
        )

        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 16
        )
        self.assertEqual(
            status["evidence"]["exact_closed_testnet_round_trips"], 1
        )
        self.assertEqual(
            status["evidence"]["candidate_matched_exact_round_trips"], 0
        )
        migrated = self.db.execute(
            """SELECT entry_freeze_id, entry_freeze_bound_ms,
                      entry_decision_created_ms, entry_candidate_bound
               FROM worker_learning_round_trips WHERE status='closed'"""
        ).fetchone()
        self.assertEqual(tuple(migrated), (None, None, None, 0))

    def test_source_seals_and_refresh_are_decimal_context_independent(self):
        root = self.source_path.parent

        def build(name):
            source_path = root / f"{name}-worker.sqlite3"
            learning_path = root / f"{name}-online.sqlite3"
            database = worker._connect(source_path)
            saved_db, saved_cutoff = self.db, self.cutoff
            try:
                self.db = database
                self.cutoff = 9_000_000_000_000
                self._insert_decision(0, "100", "0.2", action="buy")
                self._capture_and_insert(
                    1,
                    "102",
                    "0",
                    now_ms=self.cutoff + 10,
                    created_ms=self.cutoff + 2,
                )
                self._open_at("decimal-entry", 0)
                self._close_at("decimal-exit", 1)
                label_hash = database.execute(
                    """SELECT record_sha256
                       FROM worker_learning_daily_labels"""
                ).fetchone()[0]
                trip_hash = database.execute(
                    """SELECT record_sha256 FROM worker_learning_round_trips
                       WHERE status='closed'"""
                ).fetchone()[0]
                status = store.refresh(
                    source_path,
                    learning_path,
                    now_ms=self.cutoff + 20,
                )
                return label_hash, trip_hash, status["latest"]["report_version"]
            finally:
                self.db, self.cutoff = saved_db, saved_cutoff
                database.close()

        original_context = getcontext().copy()
        try:
            baseline = build("decimal-default")
            getcontext().prec = 6
            getcontext().rounding = ROUND_DOWN
            changed = build("decimal-changed")
        finally:
            setcontext(original_context)
        self.assertEqual(changed, baseline)

    def test_legacy_open_position_is_adopted_without_invented_features(self):
        self._insert_decision(0, "100", "0.20", feature=False, action="buy")
        self._insert_intent("legacy-entry", "BUY", 0, "100")
        self.db.execute(
            """UPDATE worker_state SET position_qty='0.001',
               position_quote_cost='100' WHERE singleton=1"""
        )
        adopted = store.adopt_open_round_trip(
            self.db, self.policy, self.model, self.cutoff + 30
        )
        row = self.db.execute(
            """SELECT entry_feature_schema, entry_feature_json,
                      entry_feature_status, source_kind
               FROM worker_learning_round_trips WHERE record_id=?""",
            (adopted["record_id"],),
        ).fetchone()
        self.assertEqual(row["entry_feature_schema"],
                         "legacy_schema_v0_missing")
        self.assertIsNone(row["entry_feature_json"])
        self.assertIn("diagnostic", row["entry_feature_status"])
        self.assertEqual(row["source_kind"], "legacy_open_position_adopted")

    def test_epoch_reset_quarantines_open_record_without_closing_it(self):
        self._insert_decision(0, "100", "0.20", action="buy")
        self._open_at("entry-reset", 0)
        results = store.quarantine_open_round_trips(
            self.db, "explicit test epoch reset", self.cutoff + 40
        )
        self.assertEqual(len(results), 1)
        statuses = [
            row[0]
            for row in self.db.execute(
                """SELECT status FROM worker_learning_round_trips
                   WHERE entry_client_id='entry-reset' ORDER BY status"""
            )
        ]
        self.assertEqual(statuses, ["open", "quarantined"])

    def test_refresh_trains_at_sixty_then_freezes_and_labels_future(self):
        close = self._seed_pattern(59)
        before = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 2000
        )
        self.assertEqual(before["cadence"]["last_trained_sample_count"], 0)
        self.assertEqual(before["cadence"]["next_training_sample_count"], 60)

        close = self._append_pattern_labels(59, 1, close)
        frozen = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 2100
        )
        freeze_id = frozen["latest"]["frozen_candidate_sha256"]
        self.assertIsNotNone(freeze_id)
        self.assertEqual(frozen["cadence"]["last_trained_sample_count"], 60)
        self.assertEqual(frozen["cadence"]["phase"],
                         "frozen_candidate_evaluation")
        self.assertIsNone(frozen["cadence"]["next_training_sample_count"])

        close = self._append_pattern_labels(
            60, 2, close, append_after_ms=self.cutoff + 2100
        )
        after = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 2200
        )
        sample = json.loads(
            self.db.execute(
                """SELECT sample_json FROM worker_learning_daily_labels
                   ORDER BY decision_candle_close_ms DESC LIMIT 1"""
            ).fetchone()[0]
        )
        self.assertTrue(sample["true_forward_after_freeze"])
        self.assertEqual(sample["freeze_id"], freeze_id)
        bridge = json.loads(
            self.db.execute(
                """SELECT sample_json FROM worker_learning_daily_labels
                   WHERE decision_candle_close_ms=?""",
                (BASE_MS + 60 * DAY_MS,),
            ).fetchone()[0]
        )
        self.assertFalse(bridge["true_forward_after_freeze"])
        registration = self.db.execute(
            """SELECT true_forward_decision_created_cutoff_ms
               FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model),
        ).fetchone()
        decision_times = self.db.execute(
            """SELECT candle_close_ms, created_ms FROM worker_decisions
               WHERE candle_close_ms IN (?, ?) ORDER BY candle_close_ms""",
            (BASE_MS + 60 * DAY_MS, BASE_MS + 61 * DAY_MS),
        ).fetchall()
        self.assertLessEqual(decision_times[0][1], registration[0])
        self.assertGreater(decision_times[1][1], registration[0])
        self.assertEqual(after["cadence"]["last_trained_sample_count"], 60)
        self.assertFalse(after["proposal_ready_for_review"])
        for flag in (
            "testnet_execution_eligible",
            "paper_eligible",
            "real_money_eligible",
            "real_orders_enabled",
            "live_trading_enabled",
            "automatic_activation_enabled",
            "writes_active_config",
        ):
            self.assertFalse(after[flag])

    def test_backfilled_label_at_freeze_append_boundary_is_not_true_forward(self):
        close = self._seed_pattern(60)
        bind_ms = self.cutoff + 7000
        status = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms
        )
        self.assertIsNotNone(status["latest"]["frozen_candidate_sha256"])
        momentum, forward = self._pattern(60)
        next_momentum, _ = self._pattern(61)
        close *= Decimal("1") + forward
        result = self._capture_and_insert(
            61, close, next_momentum, now_ms=bind_ms
        )
        sample = json.loads(
            self.db.execute(
                """SELECT sample_json FROM worker_learning_daily_labels
                   WHERE record_id=?""",
                (result["record_id"],),
            ).fetchone()[0]
        )
        self.assertFalse(sample["true_forward_after_freeze"])
        self.assertNotIn("freeze_id", sample)

    def test_post_freeze_gap_retires_candidate_and_restarts_lifecycle(self):
        close = self._seed_pattern(60)
        bind_ms = self.cutoff + 8000
        first = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms
        )
        old_freeze_id = first["latest"]["frozen_candidate_sha256"]
        self.assertIsNotNone(old_freeze_id)
        close = self._append_pattern_labels(
            60, 2, close, append_after_ms=bind_ms
        )

        momentum, _ = self._pattern(64)
        close *= Decimal("1.01")
        gap = self._capture_and_insert(
            64, close, momentum, now_ms=bind_ms + 100
        )
        self.assertEqual(gap["status"], "gap_recorded")
        self.assertTrue(gap["candidate_retired"])
        registration = self.db.execute(
            """SELECT frozen_candidate_sha256
               FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model),
        ).fetchone()
        self.assertIsNone(registration[0])
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM worker_learning_candidate_retirements"
            ).fetchone()[0],
            1,
        )

        retired = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms + 200
        )
        self.assertFalse(retired["cadence"]["candidate_frozen"])
        self.assertEqual(retired["evidence"]["eligible_oos_daily_labels"], 0)
        self.assertEqual(
            retired["evidence"]["true_forward_after_freeze_labels"], 0
        )
        self.assertEqual(
            retired["evidence"]["total_true_forward_after_freeze_labels"], 1
        )
        self.assertEqual(retired["evidence"]["retired_candidates"], 1)

        close = self._append_pattern_labels(
            64, 60, close, append_after_ms=bind_ms + 200
        )
        post_gap_rows = self.db.execute(
            """SELECT sample_json FROM worker_learning_daily_labels
               WHERE decision_candle_close_ms>=?
               ORDER BY decision_candle_close_ms""",
            (BASE_MS + 64 * DAY_MS,),
        ).fetchall()
        self.assertEqual(len(post_gap_rows), 60)
        self.assertTrue(
            all(
                not json.loads(row[0])["true_forward_after_freeze"]
                for row in post_gap_rows
            )
        )

        restarted = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms + 400
        )
        new_freeze_id = restarted["latest"]["frozen_candidate_sha256"]
        self.assertIsNotNone(new_freeze_id)
        self.assertNotEqual(new_freeze_id, old_freeze_id)
        self.assertEqual(
            restarted["cadence"]["last_trained_sample_count"], 60
        )
        self.assertEqual(
            restarted["evidence"]["eligible_oos_daily_labels"], 60
        )
        registration = self.db.execute(
            """SELECT frozen_candidate_sha256
               FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model),
        ).fetchone()
        self.assertEqual(registration[0], new_freeze_id)
        aggregate = sqlite3.connect(self.learning_path)
        try:
            retirement = aggregate.execute(
                """SELECT frozen_candidate_sha256
                   FROM online_candidate_retirements"""
            ).fetchone()
            run_count = aggregate.execute(
                "SELECT COUNT(*) FROM online_learning_runs"
            ).fetchone()[0]
        finally:
            aggregate.close()
        self.assertEqual(retirement[0], old_freeze_id)
        self.assertGreaterEqual(run_count, 3)

    def test_missing_decisions_after_epoch_reset_create_retirement_gap(self):
        close = self._seed_pattern(60)
        bind_ms = self.cutoff + 8200
        frozen = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms
        )
        old_freeze_id = frozen["latest"]["frozen_candidate_sha256"]
        self.db.execute("DELETE FROM worker_decisions")

        momentum, _ = self._pattern(62)
        gap = self._capture_and_insert(
            62,
            close,
            momentum,
            now_ms=bind_ms + 1,
            created_ms=bind_ms + 1,
        )
        self.assertEqual(gap["status"], "gap_recorded")
        self.assertEqual(gap["reason"], "missing_decision_epoch_boundary")
        self.assertTrue(gap["candidate_retired"])
        next_momentum, _ = self._pattern(63)
        close *= Decimal("1.01")
        self._capture_and_insert(
            63,
            close,
            next_momentum,
            now_ms=bind_ms + 2,
            created_ms=bind_ms + 2,
        )

        recovered = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms + 3
        )
        self.assertFalse(recovered["cadence"]["candidate_frozen"])
        self.assertEqual(recovered["evidence"]["retired_candidates"], 1)
        self.assertEqual(
            recovered["evidence"]["eligible_oos_daily_labels"], 1
        )
        self.assertNotEqual(
            recovered["latest"]["frozen_candidate_sha256"], old_freeze_id
        )

    def test_only_candidate_transition_round_trips_feed_review_evidence(self):
        close = self._seed_pattern(60)
        first = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 3000
        )
        self.assertEqual(first["latest"]["status"],
                         "candidate_frozen_awaiting_true_forward")
        close = self._append_pattern_labels(
            60, 11, close, append_after_ms=self.cutoff + 3000
        )

        # Candidate is 5%.  An otherwise matching entry exactly on the freeze
        # candle is not post-freeze operational evidence.
        self._open_at("boundary-entry", 60)
        self._close_at("boundary-exit", 62)
        # At index 61 the lower-threshold candidate was already long on the
        # previous decision, so this is not its cash-to-long transition.
        self._open_at("prelong-entry", 61)
        self._close_at("prelong-exit", 62)
        # The complete candidate path is cash at 63, long at 64/65, cash at 66.
        self._open_at("match-entry", 64)
        self._close_at("match-exit", 66)
        # Matching endpoints are insufficient: the candidate already emitted
        # an exit at 66, before this recorded exit at 70.
        self._open_at("late-entry", 64)
        self._close_at("late-exit", 70)
        bindings = {
            row[0]: row[1:]
            for row in self.db.execute(
                """SELECT entry_client_id, entry_freeze_id,
                          entry_freeze_bound_ms, entry_decision_created_ms,
                          entry_candidate_bound
                   FROM worker_learning_round_trips WHERE status='open'"""
            )
        }
        self.assertEqual(bindings["match-entry"][0], first["latest"]["frozen_candidate_sha256"])
        self.assertGreater(bindings["match-entry"][2], bindings["match-entry"][1])
        self.assertEqual(bindings["match-entry"][3], 1)
        self.assertEqual(bindings["boundary-entry"][3], 0)
        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 3100
        )
        self.assertEqual(status["evidence"]["exact_closed_testnet_round_trips"], 4)
        self.assertEqual(status["evidence"]["candidate_matched_exact_round_trips"], 1)

    def test_gap_keeps_training_on_latest_contiguous_segment_and_is_reported(self):
        close = self._seed_pattern(5)
        # Skip one decision day.  The skipped horizon is recorded; following
        # labels form a new strict segment rather than spanning the gap.
        momentum, _ = self._pattern(7)
        close *= Decimal("1.01")
        gap = self._capture_and_insert(7, close, momentum)
        self.assertEqual(gap["status"], "gap_recorded")
        close = self._append_pattern_labels(7, 60, close)
        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4000
        )
        self.assertEqual(status["cadence"]["last_trained_sample_count"], 60)
        self.assertEqual(status["evidence"]["daily_gaps"], 1)

    def test_failed_fit_cadence_resets_for_new_segment(self):
        close = self._seed_flat(300)
        failed = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4500
        )
        self.assertFalse(failed["cadence"]["candidate_frozen"])
        self.assertEqual(failed["cadence"]["last_trained_sample_count"], 300)
        self.assertEqual(failed["cadence"]["next_training_sample_count"], 330)
        old_segment_id = failed["cadence"]["active_segment_id"]

        momentum, _ = self._pattern(302)
        gap = self._capture_and_insert(302, close, momentum)
        self.assertEqual(gap["status"], "gap_recorded")
        close = self._append_pattern_labels(302, 60, close)
        restarted = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4600
        )
        self.assertNotEqual(
            restarted["cadence"]["active_segment_id"], old_segment_id
        )
        self.assertEqual(
            restarted["cadence"]["last_trained_sample_count"], 60
        )
        self.assertTrue(restarted["cadence"]["candidate_frozen"])

    def test_new_safety_violation_invalidates_cached_readiness(self):
        self._seed_pattern(60)
        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4700
        )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_state SET proposal_ready_for_review=1,
                   latest_status='paper_eligible' WHERE singleton=1"""
            )
            aggregate.commit()
            before_counts = aggregate.execute(
                """SELECT (SELECT COUNT(*) FROM online_samples),
                          (SELECT COUNT(*) FROM online_round_trips),
                          (SELECT COUNT(*) FROM online_learning_runs)"""
            ).fetchone()
        finally:
            aggregate.close()
        store.record_source_error(
            self.db, "synthetic isolated learning failure", self.cutoff + 4701
        )

        blocked = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4702
        )
        self.assertFalse(blocked["proposal_ready_for_review"])
        self.assertEqual(blocked["status"], "blocked_by_safety_violation")
        self.assertEqual(blocked["evidence"]["safety_violations"], 1)
        aggregate = sqlite3.connect(self.learning_path)
        try:
            after_counts = aggregate.execute(
                """SELECT (SELECT COUNT(*) FROM online_samples),
                          (SELECT COUNT(*) FROM online_round_trips),
                          (SELECT COUNT(*) FROM online_learning_runs)"""
            ).fetchone()
            state = aggregate.execute(
                """SELECT safety_violation_count, safety_violation_digest
                   FROM online_state WHERE singleton=1"""
            ).fetchone()
        finally:
            aggregate.close()
        self.assertEqual(after_counts[0:2], before_counts[0:2])
        self.assertEqual(after_counts[2], before_counts[2] + 1)
        self.assertEqual(state[0], 1)
        self.assertEqual(len(state[1]), 64)

    def test_refresh_health_overrides_stale_ready_until_success(self):
        self._seed_review_ready(bind_offset=21_000)

        ready = store.status_snapshot(self.source_path, self.learning_path)
        self.assertTrue(ready["proposal_ready_for_review"])
        failed_health = store.mark_refresh_failure(
            self.db,
            "  transport\nfailed\x00temporarily  ",
            self.cutoff + 22_001,
        )
        self.assertTrue(failed_health["last_attempt_failed"])
        self.assertEqual(
            failed_health["sanitized_error"],
            "transport failed temporarily",
        )

        unhealthy = store.status_snapshot(
            self.source_path, self.learning_path
        )
        self.assertEqual(unhealthy["status"], "learning_refresh_unhealthy")
        self.assertEqual(
            unhealthy["latest"]["status"], "learning_refresh_unhealthy"
        )
        self.assertEqual(
            unhealthy["latest"]["aggregate_status"],
            "proposal_ready_for_review",
        )
        self.assertFalse(unhealthy["proposal_ready_for_review"])
        self.assertFalse(
            unhealthy["latest"]["proposal_ready_for_review"]
        )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            persisted = aggregate.execute(
                """SELECT proposal_ready_for_review, latest_status
                   FROM online_state WHERE singleton=1"""
            ).fetchone()
        finally:
            aggregate.close()
        self.assertEqual(persisted, (1, "proposal_ready_for_review"))

        cleared_health = store.mark_refresh_success(
            self.db, self.cutoff + 22_002
        )
        self.assertFalse(cleared_health["last_attempt_failed"])
        self.assertEqual(cleared_health["last_success_ms"], self.cutoff + 22_002)
        recovered = store.status_snapshot(
            self.source_path, self.learning_path
        )
        self.assertEqual(recovered["status"], "proposal_ready_for_review")
        self.assertTrue(recovered["proposal_ready_for_review"])

    def test_status_fails_closed_on_persisted_provenance_mismatch(self):
        self._seed_review_ready(bind_offset=23_000)
        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_state SET learner_version='obsolete-version',
                   proposal_ready_for_review=1,
                   latest_status='paper_eligible' WHERE singleton=1"""
            )
            aggregate.commit()
        finally:
            aggregate.close()

        unavailable = store.status_snapshot(
            self.source_path, self.learning_path
        )
        self.assertEqual(unavailable["status"], "learning_status_unavailable")
        self.assertFalse(unavailable["proposal_ready_for_review"])
        self.assertFalse(unavailable["provenance_valid"])
        self.assertEqual(unavailable["learner_version"], "obsolete-version")
        self.assertEqual(
            unavailable["expected_learner_version"],
            store.learner.LEARNER_VERSION,
        )

        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_state SET learner_version=?
                   WHERE singleton=1""",
                (store.learner.LEARNER_VERSION,),
            )
            aggregate.commit()
        finally:
            aggregate.close()

        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_state SET latest_status='tampered_ready'
                   WHERE singleton=1"""
            )
            aggregate.commit()
        finally:
            aggregate.close()
        state_tampered = store.status_snapshot(
            self.source_path, self.learning_path
        )
        self.assertEqual(
            state_tampered["status"], "learning_status_unavailable"
        )
        self.assertFalse(state_tampered["proposal_ready_for_review"])

        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_state
                   SET latest_status='proposal_ready_for_review'
                   WHERE singleton=1"""
            )
            original_artifact = aggregate.execute(
                """SELECT artifact_json FROM online_learning_runs
                   WHERE run_id=(SELECT latest_run_id FROM online_state
                                 WHERE singleton=1)"""
            ).fetchone()[0]
            aggregate.execute(
                """UPDATE online_learning_runs SET artifact_json='{}'
                   WHERE run_id=(SELECT latest_run_id FROM online_state
                                 WHERE singleton=1)"""
            )
            aggregate.commit()
        finally:
            aggregate.close()
        run_tampered = store.status_snapshot(
            self.source_path, self.learning_path
        )
        self.assertEqual(
            run_tampered["status"], "learning_status_unavailable"
        )
        self.assertFalse(run_tampered["proposal_ready_for_review"])

        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_learning_runs SET artifact_json=?
                   WHERE run_id=(SELECT latest_run_id FROM online_state
                                 WHERE singleton=1)""",
                (original_artifact,),
            )
            aggregate.commit()
        finally:
            aggregate.close()
        self.db.execute(
            "UPDATE worker_learning_meta SET schema_version=999 WHERE singleton=1"
        )
        source_unavailable = store.status_snapshot(
            self.source_path, self.learning_path
        )
        self.assertEqual(
            source_unavailable["status"], "learning_status_unavailable"
        )
        self.assertFalse(source_unavailable["proposal_ready_for_review"])
        self.assertIsNone(source_unavailable["learner_version"])

    def test_source_revision_blocks_stale_ready_until_refresh_catches_up(self):
        close, bind_ms, _ready = self._seed_review_ready(bind_offset=24_000)
        before = store.status_snapshot(self.source_path, self.learning_path)
        self.assertTrue(before["proposal_ready_for_review"])
        before_revision = before["evidence"]["source_learning_revision"]

        close = self._append_pattern_labels(
            200, 1, close, append_after_ms=bind_ms + 500
        )
        stale = store.status_snapshot(self.source_path, self.learning_path)
        self.assertEqual(stale["status"], "stale_source_evidence")
        self.assertFalse(stale["proposal_ready_for_review"])
        self.assertEqual(
            stale["evidence"]["source_learning_revision"],
            before_revision + 1,
        )
        self.assertEqual(
            stale["evidence"]["ingested_source_revision"], before_revision
        )

        caught_up = store.refresh(
            self.source_path, self.learning_path, now_ms=bind_ms + 100
        )
        self.assertNotEqual(caught_up["status"], "stale_source_evidence")
        self.assertEqual(
            caught_up["evidence"]["source_learning_revision"],
            caught_up["evidence"]["ingested_source_revision"],
        )

    def test_concurrent_refreshes_read_lifecycle_under_writer_lock(self):
        self._seed_pattern(60)
        initialized = store._connect_learning(self.learning_path)
        initialized.close()
        barrier = threading.Barrier(2)
        gate_lock = threading.Lock()
        gate_count = 0
        real_source_read = store._source_read_connection
        source_resolved = self.source_path.resolve()

        def gated_source_read(path):
            nonlocal gate_count
            connection = real_source_read(path)
            should_wait = False
            if Path(path).resolve() == source_resolved:
                with gate_lock:
                    if gate_count < 2:
                        gate_count += 1
                        should_wait = True
            if should_wait:
                try:
                    barrier.wait(timeout=10)
                except BaseException:
                    connection.close()
                    raise
            return connection

        with mock.patch.object(
            store, "_source_read_connection", side_effect=gated_source_read
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        store.refresh,
                        self.source_path,
                        self.learning_path,
                        self.cutoff + 4717 + index,
                    )
                    for index in range(2)
                ]
                results = [future.result(timeout=20) for future in futures]

        freeze_ids = {
            result["latest"]["frozen_candidate_sha256"] for result in results
        }
        self.assertEqual(len(freeze_ids), 1)
        self.assertNotIn(None, freeze_ids)
        aggregate = sqlite3.connect(self.learning_path)
        try:
            run_count = aggregate.execute(
                "SELECT COUNT(*) FROM online_learning_runs"
            ).fetchone()[0]
        finally:
            aggregate.close()
        self.assertEqual(run_count, 1)

    def test_unresolved_learning_outbox_is_a_reversible_readiness_blocker(self):
        self._seed_review_ready(bind_offset=25_000)

        def insert_outbox(client_id, created_ms):
            payload = store._canonical_json(
                {
                    "schema": 1,
                    "operation": "open_round_trip",
                    "reference": {
                        "client_id": client_id,
                        "policy": self.policy,
                        "model_version": self.model,
                    },
                }
            )
            digest = store._sha256(json.loads(payload))
            event_id = f"learning-outbox:{digest}"
            self.db.execute(
                """INSERT INTO worker_learning_outbox
                   (event_id, operation, payload_json, payload_sha256,
                    created_ms, attempt_count)
                   VALUES (?, 'open_round_trip', ?, ?, ?, 1)""",
                (event_id, payload, digest, created_ms),
            )
            return event_id

        first_event = insert_outbox("first-client", self.cutoff + 4721)
        blocked = store.status_snapshot(self.source_path, self.learning_path)
        self.assertEqual(
            blocked["status"], "blocked_by_unresolved_learning_outbox"
        )
        self.assertFalse(blocked["proposal_ready_for_review"])
        self.assertEqual(blocked["evidence"]["unresolved_learning_outbox"], 1)

        refreshed_block = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4722
        )
        self.assertEqual(
            refreshed_block["status"],
            "blocked_by_unresolved_learning_outbox",
        )
        self.assertFalse(refreshed_block["proposal_ready_for_review"])
        aggregate = sqlite3.connect(self.learning_path)
        try:
            self.assertEqual(
                aggregate.execute(
                    """SELECT unresolved_learning_outbox_count
                       FROM online_state WHERE singleton=1"""
                ).fetchone()[0],
                1,
            )
        finally:
            aggregate.close()

        self.db.execute(
            """UPDATE worker_learning_outbox SET resolved_ms=?
               WHERE event_id=?""",
            (self.cutoff + 4723, first_event),
        )
        stale = store.status_snapshot(self.source_path, self.learning_path)
        self.assertEqual(stale["status"], "stale_source_evidence")
        self.assertFalse(stale["proposal_ready_for_review"])
        refreshed_clear = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4724
        )
        self.assertNotIn("blocked", refreshed_clear["status"])
        self.assertNotEqual(refreshed_clear["status"], "stale_source_evidence")
        self.assertEqual(
            refreshed_clear["evidence"]["unresolved_learning_outbox"], 0
        )

    def test_capture_failure_gap_retires_candidate_and_seals_next_horizon(self):
        close = self._seed_pattern(60)
        freeze_ms = self.cutoff + 4740
        frozen = store.refresh(
            self.source_path, self.learning_path, now_ms=freeze_ms
        )
        old_freeze_id = frozen["latest"]["frozen_candidate_sha256"]
        self.assertIsNotNone(old_freeze_id)

        # Simulate a sidecar rollback while the core D+1 decision commits.
        _momentum, forward = self._pattern(60)
        close *= Decimal("1") + forward
        next_momentum, _unused = self._pattern(61)
        self._insert_decision(
            61,
            close,
            next_momentum,
            created_ms=freeze_ms + 1,
        )

        _momentum, forward = self._pattern(61)
        close *= Decimal("1") + forward
        next_momentum, _unused = self._pattern(62)
        recovered = self._capture_and_insert(
            62,
            close,
            next_momentum,
            now_ms=freeze_ms + 2,
            created_ms=freeze_ms + 2,
        )
        self.assertEqual(recovered["status"], "label_sealed")
        self.assertEqual(
            recovered["recovered_gap"]["reason"],
            "missing_learning_label_boundary",
        )
        self.assertTrue(recovered["recovered_gap"]["candidate_retired"])
        self.assertFalse(recovered["true_forward_after_freeze"])
        self.assertEqual(
            self.db.execute(
                """SELECT COUNT(*) FROM worker_learning_daily_gaps
                   WHERE reason='missing_learning_label_boundary'"""
            ).fetchone()[0],
            1,
        )
        registration = self.db.execute(
            """SELECT frozen_candidate_sha256
               FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model),
        ).fetchone()
        self.assertIsNone(registration[0])

        refreshed = store.refresh(
            self.source_path, self.learning_path, now_ms=freeze_ms + 3
        )
        self.assertFalse(refreshed["cadence"]["candidate_frozen"])
        self.assertEqual(refreshed["evidence"]["retired_candidates"], 1)
        self.assertEqual(refreshed["evidence"]["eligible_oos_daily_labels"], 1)

    def test_unbound_post_freeze_label_retires_candidate_and_recovers(self):
        close = self._seed_pattern(60)
        freeze_ms = self.cutoff + 4750
        with mock.patch.object(
            store,
            "_transition_candidate_on_source",
            side_effect=RuntimeError("simulated bind crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated bind crash"):
                store.refresh(
                    self.source_path, self.learning_path, now_ms=freeze_ms
                )

        aggregate = sqlite3.connect(self.learning_path)
        try:
            frozen = aggregate.execute(
                """SELECT frozen_candidate_sha256
                   FROM online_state WHERE singleton=1"""
            ).fetchone()[0]
        finally:
            aggregate.close()
        self.assertIsNotNone(frozen)
        self.assertIsNone(
            self.db.execute(
                """SELECT frozen_candidate_sha256
                   FROM worker_learning_registrations
                   WHERE policy=? AND model_version=?""",
                (self.policy, self.model),
            ).fetchone()[0]
        )

        # Emulate an aggregate-first crash from the legacy one-phase build,
        # which had no durable source-side transition blocker.  New refreshes
        # retain the aggregate pending intent and recover this historical row.
        self.db.execute(
            "DELETE FROM worker_learning_pending_candidate_transition WHERE singleton=1"
        )

        close = self._append_pattern_labels(
            60, 2, close, append_after_ms=freeze_ms
        )
        sample = json.loads(
            self.db.execute(
                """SELECT sample_json FROM worker_learning_daily_labels
                   ORDER BY decision_candle_close_ms DESC LIMIT 1"""
            ).fetchone()[0]
        )
        self.assertFalse(sample["true_forward_after_freeze"])

        recovered = store.refresh(
            self.source_path, self.learning_path, now_ms=freeze_ms + 100
        )
        self.assertFalse(recovered["proposal_ready_for_review"])
        self.assertFalse(recovered["cadence"]["candidate_frozen"])
        self.assertEqual(recovered["evidence"]["retired_candidates"], 1)
        self.assertEqual(
            recovered["evidence"]["eligible_oos_daily_labels"], 1
        )
        self.assertEqual(
            recovered["cadence"]["last_trained_sample_count"], 0
        )
        self.assertEqual(
            recovered["cadence"]["next_training_sample_count"], 60
        )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            retirement = aggregate.execute(
                """SELECT reason, boundary_sample_sha256
                   FROM online_candidate_retirements"""
            ).fetchone()
            run_count = aggregate.execute(
                "SELECT COUNT(*) FROM online_learning_runs"
            ).fetchone()[0]
        finally:
            aggregate.close()
        self.assertEqual(retirement[0], "post_freeze_source_binding_missing")
        self.assertEqual(retirement[1], sample["immutable_sha256"])
        self.assertGreaterEqual(run_count, 2)

        close = self._append_pattern_labels(
            62, 59, close, append_after_ms=freeze_ms + 100
        )
        retrained = store.refresh(
            self.source_path, self.learning_path, now_ms=freeze_ms + 200
        )
        self.assertTrue(retrained["cadence"]["candidate_frozen"])
        self.assertEqual(
            retrained["cadence"]["last_trained_sample_count"], 60
        )
        self.assertNotEqual(
            retrained["latest"]["frozen_candidate_sha256"], frozen
        )

    def test_two_phase_candidate_replacement_blocks_capture_and_recovers(self):
        close = self._seed_pattern(60)
        first_freeze_ms = self.cutoff + 47_600
        first = store.refresh(
            self.source_path, self.learning_path, now_ms=first_freeze_ms
        )
        first_candidate = first["latest"]["frozen_candidate_sha256"]
        self.assertIsNotNone(first_candidate)

        # A missing daily decision retires A at the source.  Sixty subsequent
        # labels are a fresh contiguous development cohort for replacement B.
        next_momentum, _unused = self._pattern(62)
        gap = self._capture_and_insert(
            62,
            close,
            next_momentum,
            now_ms=first_freeze_ms + 1,
            created_ms=first_freeze_ms + 1,
        )
        self.assertEqual(gap["status"], "gap_recorded")
        close = self._append_pattern_labels(
            62, 60, close, append_after_ms=first_freeze_ms + 10
        )

        replacement_ms = first_freeze_ms + 1_000
        with mock.patch.object(
            store,
            "_transition_candidate_on_source",
            side_effect=RuntimeError("simulated source transition failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "simulated source transition failure"
            ):
                store.refresh(
                    self.source_path,
                    self.learning_path,
                    now_ms=replacement_ms,
                )

        aggregate = sqlite3.connect(self.learning_path)
        aggregate.row_factory = sqlite3.Row
        try:
            pending = aggregate.execute(
                """SELECT frozen_candidate_sha256,
                          pending_candidate_transition_json,
                          pending_candidate_transition_sha256
                   FROM online_state WHERE singleton=1"""
            ).fetchone()
            retired = aggregate.execute(
                """SELECT COUNT(*) FROM online_candidate_retirements
                   WHERE frozen_candidate_sha256=?""",
                (first_candidate,),
            ).fetchone()[0]
        finally:
            aggregate.close()
        replacement_candidate = pending["frozen_candidate_sha256"]
        self.assertNotEqual(replacement_candidate, first_candidate)
        self.assertIsNotNone(pending["pending_candidate_transition_json"])
        self.assertIsNotNone(pending["pending_candidate_transition_sha256"])
        self.assertEqual(retired, 1)
        self.assertEqual(
            self.db.execute(
                """SELECT COUNT(*)
                   FROM worker_learning_pending_candidate_transition"""
            ).fetchone()[0],
            1,
        )

        blocked = store.status_snapshot(self.source_path, self.learning_path)
        self.assertEqual(blocked["status"], "candidate_transition_pending")
        self.assertFalse(blocked["proposal_ready_for_review"])
        _momentum, forward = self._pattern(122)
        next_close = close * (Decimal("1") + forward)
        with self.assertRaisesRegex(
            store.LearningStoreError, "transition is pending"
        ):
            store.capture_daily_label(
                self.db,
                {
                    "candle_close_ms": BASE_MS + 123 * DAY_MS,
                    "close_latest": next_close,
                },
                self.policy,
                self.model,
                replacement_ms + 1,
            )

        recovered = store.refresh(
            self.source_path, self.learning_path, now_ms=replacement_ms + 2
        )
        self.assertNotEqual(recovered["status"], "candidate_transition_pending")
        self.assertEqual(
            recovered["latest"]["frozen_candidate_sha256"],
            replacement_candidate,
        )
        registration = self.db.execute(
            """SELECT frozen_candidate_sha256
               FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model),
        ).fetchone()
        self.assertEqual(registration[0], replacement_candidate)
        self.assertEqual(
            self.db.execute(
                """SELECT COUNT(*)
                   FROM worker_learning_pending_candidate_transition"""
            ).fetchone()[0],
            0,
        )
        bridge = store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": BASE_MS + 123 * DAY_MS,
                "close_latest": next_close,
            },
            self.policy,
            self.model,
            replacement_ms + 3,
        )
        self.assertEqual(bridge["status"], "label_sealed")
        self.assertFalse(bridge["true_forward_after_freeze"])

    def test_aggregate_mirror_tamper_fails_closed(self):
        self._seed_pattern(1)
        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 47_700
        )
        aggregate = sqlite3.connect(self.learning_path)
        aggregate.row_factory = sqlite3.Row
        try:
            sample = aggregate.execute(
                """SELECT source_ledger_id, source_record_id, out_of_sample
                   FROM online_samples LIMIT 1"""
            ).fetchone()
            aggregate.execute(
                """UPDATE online_samples SET out_of_sample=?
                   WHERE source_ledger_id=? AND source_record_id=?""",
                (
                    1 - int(sample["out_of_sample"]),
                    sample["source_ledger_id"],
                    sample["source_record_id"],
                ),
            )
            aggregate.commit()
            with self.assertRaisesRegex(
                store.LearningIntegrityError, "sample seal"
            ):
                store.refresh(
                    self.source_path,
                    self.learning_path,
                    now_ms=self.cutoff + 47_701,
                )
            unavailable = store.status_snapshot(
                self.source_path, self.learning_path
            )
            self.assertEqual(unavailable["status"], "learning_status_unavailable")
            self.assertFalse(unavailable["proposal_ready_for_review"])
            aggregate.execute(
                """UPDATE online_samples SET out_of_sample=?
                   WHERE source_ledger_id=? AND source_record_id=?""",
                (
                    int(sample["out_of_sample"]),
                    sample["source_ledger_id"],
                    sample["source_record_id"],
                ),
            )
            aggregate.commit()
        finally:
            aggregate.close()

        self._open_at("mirror-entry", 0)
        self._close_at("mirror-exit", 1)
        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 47_702
        )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_round_trips SET net_return='999'
                   WHERE status='closed'"""
            )
            aggregate.commit()
        finally:
            aggregate.close()
        with self.assertRaisesRegex(
            store.LearningIntegrityError, "round-trip seal"
        ):
            store.refresh(
                self.source_path,
                self.learning_path,
                now_ms=self.cutoff + 47_703,
            )
        unavailable = store.status_snapshot(self.source_path, self.learning_path)
        self.assertEqual(unavailable["status"], "learning_status_unavailable")
        self.assertFalse(unavailable["proposal_ready_for_review"])

    def test_aggregate_rejects_a_second_source_without_state_change(self):
        self._seed_pattern(60)
        first = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4770
        )
        a_candidate = first["latest"]["frozen_candidate_sha256"]
        aggregate = sqlite3.connect(self.learning_path)
        try:
            before = aggregate.execute(
                """SELECT finalized_daily_label_count,
                          eligible_oos_daily_label_count, latest_run_id,
                          frozen_candidate_sha256, updated_ms
                   FROM online_state WHERE singleton=1"""
            ).fetchone()
            before_runs = aggregate.execute(
                "SELECT COUNT(*) FROM online_learning_runs"
            ).fetchone()[0]
        finally:
            aggregate.close()

        second_path = self.source_path.with_name("worker-b.sqlite3")
        second = worker._connect(second_path)
        try:
            registration = second.execute(
                """SELECT * FROM worker_learning_registrations
                   WHERE policy=? AND model_version=?""",
                (self.policy, self.model),
            ).fetchone()
            second_cutoff = int(
                registration["oos_decision_created_cutoff_ms"]
            )
            second.execute(
                """INSERT INTO worker_decisions
                   (candle_close_ms, policy, close_latest, close_30d,
                    momentum, feature_schema, feature_json, target_long,
                    action, client_id, created_ms)
                   VALUES (?, ?, '100', '100', '0.2', 'test_features_v1',
                           '{"momentum":"0.2"}', 1, 'buy', NULL, ?)""",
                (BASE_MS, self.policy, second_cutoff + 1),
            )
            store.capture_daily_label(
                second,
                {
                    "candle_close_ms": BASE_MS + DAY_MS,
                    "close_latest": Decimal("99"),
                },
                self.policy,
                self.model,
                second_cutoff + 2,
            )
            second.execute(
                """INSERT INTO worker_decisions
                   (candle_close_ms, policy, close_latest, close_30d,
                    momentum, feature_schema, feature_json, target_long,
                    action, client_id, created_ms)
                   VALUES (?, ?, '99', '100', '0', 'test_features_v1',
                           '{"momentum":"0"}', 0, 'hold_cash', NULL, ?)""",
                (BASE_MS + DAY_MS, self.policy, second_cutoff + 3),
            )
            with self.assertRaisesRegex(
                store.LearningIntegrityError, "another source ledger"
            ):
                store.refresh(
                    second_path,
                    self.learning_path,
                    now_ms=self.cutoff + 4771,
                )
            self.assertIsNone(
                second.execute(
                    """SELECT frozen_candidate_sha256
                       FROM worker_learning_registrations
                       WHERE policy=? AND model_version=?""",
                    (self.policy, self.model),
                ).fetchone()[0]
            )
        finally:
            second.close()

        aggregate = sqlite3.connect(self.learning_path)
        try:
            after = aggregate.execute(
                """SELECT finalized_daily_label_count,
                          eligible_oos_daily_label_count, latest_run_id,
                          frozen_candidate_sha256, updated_ms
                   FROM online_state WHERE singleton=1"""
            ).fetchone()
            after_runs = aggregate.execute(
                "SELECT COUNT(*) FROM online_learning_runs"
            ).fetchone()[0]
            source_count = aggregate.execute(
                "SELECT COUNT(*) FROM online_sources"
            ).fetchone()[0]
        finally:
            aggregate.close()
        self.assertEqual(after, before)
        self.assertEqual(after_runs, before_runs)
        self.assertEqual(source_count, 1)
        self.assertEqual(after[3], a_candidate)

    def test_source_rejects_cross_policy_registration_and_rebind(self):
        with self.assertRaisesRegex(
            store.LearningIntegrityError, "another policy/model identity"
        ):
            store.ensure_source_schema(
                self.db, "cross_policy", "cross_model", self.cutoff + 1
            )
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM worker_learning_registrations"
            ).fetchone()[0],
            1,
        )

        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4_775
        )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            before = aggregate.execute(
                """SELECT source_ledger_id, policy, model_version
                   FROM online_aggregate_identity WHERE singleton=1"""
            ).fetchone()
        finally:
            aggregate.close()

        rogue_policy = "cross_policy"
        rogue_model = "cross_model"
        self.db.execute(
            """UPDATE worker_learning_registrations SET
               policy=?, model_version=?, identity_prefix=?
               WHERE policy=? AND model_version=?""",
            (
                rogue_policy,
                rogue_model,
                store._model_prefix(rogue_policy, rogue_model),
                self.policy,
                self.model,
            ),
        )
        with self.assertRaisesRegex(
            store.LearningIntegrityError, "another policy/model identity"
        ):
            store.refresh(
                self.source_path,
                self.learning_path,
                now_ms=self.cutoff + 4_776,
            )
        unavailable = store.status_snapshot(self.source_path, self.learning_path)
        self.assertEqual(unavailable["status"], "learning_status_unavailable")
        self.assertFalse(unavailable["proposal_ready_for_review"])
        aggregate = sqlite3.connect(self.learning_path)
        try:
            after = aggregate.execute(
                """SELECT source_ledger_id, policy, model_version
                   FROM online_aggregate_identity WHERE singleton=1"""
            ).fetchone()
        finally:
            aggregate.close()
        self.assertEqual(before, after)

    def test_refresh_closes_aggregate_on_pretransaction_and_bind_errors(self):
        original_connect = store._connect_learning

        class TrackingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.closed = False

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def close(self):
                self.closed = True
                self.connection.close()

        def run_with_tracking(extra_patch, expected_exception):
            opened = []

            def connect(path):
                tracked = TrackingConnection(original_connect(path))
                opened.append(tracked)
                return tracked

            with mock.patch.object(store, "_connect_learning", side_effect=connect):
                with extra_patch:
                    with self.assertRaises(expected_exception):
                        store.refresh(
                            self.source_path,
                            self.learning_path,
                            now_ms=self.cutoff + 4800,
                        )
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)

        aggregate = original_connect(self.learning_path)
        aggregate.close()
        run_with_tracking(
            mock.patch.object(
                store,
                "_source_read_connection",
                side_effect=RuntimeError("source open failed"),
            ),
            RuntimeError,
        )

        self._seed_pattern(60)
        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 4801
        )
        self.db.execute(
            """UPDATE worker_learning_registrations SET
               freeze_cutoff_candle_ms=NULL, frozen_candidate_json=NULL,
               frozen_candidate_sha256=NULL
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model),
        )
        run_with_tracking(
            mock.patch.object(
                store,
                "_transition_candidate_on_source",
                side_effect=RuntimeError("bind failed"),
            ),
            RuntimeError,
        )

        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE online_state SET frozen_candidate_json='{'
                   WHERE singleton=1"""
            )
            aggregate.commit()
        finally:
            aggregate.close()
        run_with_tracking(mock.patch.object(store, "_now_ms", return_value=1),
                          store.LearningIntegrityError)

    def test_refresh_ingest_is_idempotent_and_never_writes_active_config(self):
        self._seed_pattern(1)
        config_before = worker.POLICY_CONFIG_PATH.read_bytes()
        first = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 5000
        )
        second = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 5001
        )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            sample_count = aggregate.execute(
                "SELECT COUNT(*) FROM online_samples"
            ).fetchone()[0]
            run_count = aggregate.execute(
                "SELECT COUNT(*) FROM online_learning_runs"
            ).fetchone()[0]
        finally:
            aggregate.close()
        self.assertEqual(sample_count, 1)
        self.assertEqual(run_count, 1)
        self.assertEqual(first["latest"]["run_id"], second["latest"]["run_id"])
        self.assertEqual(worker.POLICY_CONFIG_PATH.read_bytes(), config_before)
        self.assertFalse(second["writes_active_config"])

    def test_tampered_source_record_fails_closed_without_replacing_aggregate(self):
        self._seed_pattern(1)
        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 6000
        )
        self.db.execute(
            """UPDATE worker_learning_daily_labels
               SET record_json='{}'"""
        )
        with self.assertRaises(store.LearningIntegrityError):
            store.refresh(
                self.source_path, self.learning_path, now_ms=self.cutoff + 6001
            )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            self.assertEqual(
                aggregate.execute(
                    "SELECT COUNT(*) FROM online_samples"
                ).fetchone()[0],
                1,
            )
        finally:
            aggregate.close()

    def test_previously_ingested_gap_tamper_is_rejected(self):
        self._insert_decision(0, "100", "0.08")
        store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": BASE_MS + 2 * DAY_MS,
                "close_latest": Decimal("101"),
            },
            self.policy,
            self.model,
            self.cutoff + 6100,
        )
        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 6101
        )
        row = self.db.execute(
            "SELECT * FROM worker_learning_daily_gaps"
        ).fetchone()
        changed = json.loads(row["record_json"])
        changed["reason"] = "tampered_gap_reason"
        changed_json = store._canonical_json(changed)
        changed_digest = store._sha256(changed)
        self.db.execute(
            """UPDATE worker_learning_daily_gaps
               SET reason=?, record_json=?, record_sha256=? WHERE gap_id=?""",
            (
                changed["reason"],
                changed_json,
                changed_digest,
                row["gap_id"],
            ),
        )
        with self.assertRaisesRegex(
            store.LearningIntegrityError,
            "Previously ingested daily gap evidence changed",
        ):
            store.refresh(
                self.source_path,
                self.learning_path,
                now_ms=self.cutoff + 6102,
            )

    def test_source_error_collision_and_ingested_tamper_are_rejected(self):
        now_ms = self.cutoff + 6200
        recorded = store.record_source_error(
            self.db, "sealed source error", now_ms
        )
        original = self.db.execute(
            """SELECT * FROM worker_learning_source_errors
               WHERE error_id=?""",
            (recorded["error_id"],),
        ).fetchone()
        self.db.execute(
            """UPDATE worker_learning_source_errors SET message='collision'
               WHERE error_id=?""",
            (recorded["error_id"],),
        )
        with self.assertRaisesRegex(
            store.LearningIntegrityError, "identity collision"
        ):
            store.record_source_error(self.db, "sealed source error", now_ms)
        self.db.execute(
            """UPDATE worker_learning_source_errors
               SET message=?, record_json=?, record_sha256=? WHERE error_id=?""",
            (
                original["message"],
                original["record_json"],
                original["record_sha256"],
                recorded["error_id"],
            ),
        )
        store.refresh(
            self.source_path, self.learning_path, now_ms=now_ms + 1
        )

        changed = json.loads(original["record_json"])
        changed["message"] = "tampered source error"
        changed_json = store._canonical_json(changed)
        changed_digest = store._sha256(changed)
        self.db.execute(
            """UPDATE worker_learning_source_errors
               SET message=?, record_json=?, record_sha256=? WHERE error_id=?""",
            (
                changed["message"],
                changed_json,
                changed_digest,
                recorded["error_id"],
            ),
        )
        with self.assertRaisesRegex(
            store.LearningIntegrityError,
            "Previously ingested source error evidence changed",
        ):
            store.refresh(
                self.source_path,
                self.learning_path,
                now_ms=now_ms + 2,
            )


if __name__ == "__main__":
    unittest.main()
