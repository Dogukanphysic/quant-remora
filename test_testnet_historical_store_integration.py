import csv
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import binance_testnet_learning_seed as seed_builder
import binance_testnet_worker as worker
import testnet_learning_store as store
import testnet_online_learner as learner


DAY_MS = store.DAY_MS
BOUNDARY_MS = 1_700_006_399_999


class HistoricalDevelopmentStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source_path = self.root / "worker.sqlite3"
        self.learning_path = self.root / "learning.sqlite3"
        self.csv_path = self.root / "daily.csv"
        self.db = worker._connect(self.source_path)
        self.policy = worker.POLICY
        self.model_version = worker.POLICY_SPEC_HASH
        registration = self.db.execute(
            """SELECT oos_decision_created_cutoff_ms
               FROM worker_learning_registrations
               WHERE policy=? AND model_version=?""",
            (self.policy, self.model_version),
        ).fetchone()
        self.cutoff = int(registration[0])

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def _manifest(self, sample_count=60, *, days=91):
        first_open = BOUNDARY_MS + 1 - days * DAY_MS
        with self.csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=seed_builder.EXPECTED_FIELDS
            )
            writer.writeheader()
            for index in range(days):
                price = Decimal("100") + index
                writer.writerow(
                    {
                        "ts": first_open + index * DAY_MS,
                        "open": format(price, "f"),
                        "high": format(price, "f"),
                        "low": format(price, "f"),
                        "close": format(price, "f"),
                        "volume": "1",
                        "exchange": "binance_spot",
                        "symbol": "BTCUSDT",
                    }
                )
        return seed_builder.build_seed_manifest(
            self.csv_path,
            interval="1d",
            sample_limit=sample_count,
            now_ms=BOUNDARY_MS + DAY_MS,
        )

    def _insert_boundary_decision(self, close="190", *, created_ms=None):
        if created_ms is None:
            created_ms = self.cutoff + 1
        self.db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                feature_schema, feature_json, target_long, action, client_id,
                created_ms)
               VALUES (?, ?, ?, '100', '0', NULL, NULL, 0, 'hold_cash',
                       NULL, ?)""",
            (BOUNDARY_MS, self.policy, close, created_ms),
        )

    def _insert_later_decision(
        self, candle_close_ms, close, *, created_ms, momentum="0.08"
    ):
        self.db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                feature_schema, feature_json, target_long, action, client_id,
                created_ms)
               VALUES (?, ?, ?, '100', ?, 'test_features_v1', ?, 0,
                       'hold_cash', NULL, ?)""",
            (
                candle_close_ms,
                self.policy,
                str(close),
                momentum,
                '{"momentum":"' + momentum + '"}',
                created_ms,
            ),
        )

    def _viable_seed_manifest(self):
        raw_digest = "ab" * 32
        samples = []
        for index in range(60):
            high = index % 4 < 2
            decision_ts = BOUNDARY_MS - (60 - index) * DAY_MS
            samples.append(
                learner.seal_sample(
                    {
                        "sample_id": f"hist:{raw_digest}:{decision_ts}",
                        "decision_ts": decision_ts,
                        "label_available_ts": decision_ts + DAY_MS,
                        "momentum": learner._format_decimal(
                            Decimal("0.08") if high else Decimal("0")
                        ),
                        "forward_return": learner._format_decimal(
                            Decimal("0.01") if high else Decimal("-0.01")
                        ),
                        "closed": True,
                        "out_of_sample": False,
                        "true_forward_after_freeze": False,
                    }
                )
            )
        payload = {
            "schema": seed_builder.SCHEMA,
            "kind": seed_builder.KIND,
            "symbol": seed_builder.SYMBOL,
            "market": seed_builder.MARKET,
            "source_interval": "1d",
            "daily_horizon_ms": DAY_MS,
            "momentum_lookback_days": seed_builder.MOMENTUM_LOOKBACK_DAYS,
            "evidence_role": seed_builder.EVIDENCE_ROLE,
            "minimum_completed_at_ms": BOUNDARY_MS + 1,
            "raw_candle_count": 91,
            "daily_candle_count": 91,
            "first_candle_close_ms": BOUNDARY_MS - 90 * DAY_MS,
            "last_candle_close_ms": BOUNDARY_MS,
            "last_close": "100",
            "raw_dataset_sha256": raw_digest,
            "provenance": {
                "source_manifest_sha256": None,
                "source_manifest_claims": None,
            },
            "sample_count": 60,
            "first_sample_id": samples[0]["sample_id"],
            "last_sample_id": samples[-1]["sample_id"],
            "first_decision_ts": samples[0]["decision_ts"],
            "last_decision_ts": samples[-1]["decision_ts"],
            "last_label_available_ts": samples[-1]["label_available_ts"],
            "ordered_sample_sha256": seed_builder._sha256(samples),
            "samples": samples,
        }
        manifest = {
            **payload,
            "immutable_sha256": seed_builder._sha256(payload),
        }
        self.assertEqual(seed_builder.verify_seed_manifest(manifest), manifest)
        return manifest

    def _seed(self, sample_count=60):
        manifest = self._manifest(sample_count)
        self._insert_boundary_decision(manifest["last_close"])
        result = store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 100,
        )
        return manifest, result

    def _prepare_legacy_v4_aggregate(self, manifest, *, drop_new_column=False):
        old_version = next(iter(store._SEED_MIGRATABLE_LEARNER_VERSIONS))
        with mock.patch.object(learner, "LEARNER_VERSION", old_version):
            status = store.refresh(
                self.source_path,
                self.learning_path,
                now_ms=self.cutoff + 50,
            )
        self.assertEqual(status["learner_version"], old_version)
        aggregate = sqlite3.connect(self.learning_path)
        aggregate.row_factory = sqlite3.Row
        try:
            state = dict(aggregate.execute(
                "SELECT * FROM online_state WHERE singleton=1"
            ).fetchone())
            payload = {
                key: value
                for key, value in state.items()
                if key not in {
                    "singleton", "updated_ms", "learner_migration_sha256"
                }
            }
            digest = store._sha256(payload)
            aggregate.execute(
                """INSERT OR IGNORE INTO online_state_events
                   (state_version, state_json, state_sha256, created_ms)
                   VALUES (?, ?, ?, ?)""",
                (digest, store._canonical_json(payload), digest, self.cutoff + 51),
            )
            if drop_new_column:
                aggregate.execute(
                    "ALTER TABLE online_state DROP COLUMN learner_migration_sha256"
                )
            aggregate.commit()
        finally:
            aggregate.close()
        return old_version

    def test_seed_trains_without_claiming_live_or_forward_evidence(self):
        manifest, inserted = self._seed(60)
        self.assertFalse(inserted["idempotent"])
        self.assertEqual(inserted["manifest_sha256"], manifest["immutable_sha256"])

        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 200
        )

        self.assertTrue(status["provenance_valid"])
        self.assertEqual(status["evidence"]["historical_development_labels"], 60)
        self.assertEqual(status["evidence"]["development_labels"], 60)
        self.assertEqual(status["evidence"]["finalized_daily_labels"], 0)
        self.assertEqual(status["evidence"]["eligible_oos_daily_labels"], 0)
        self.assertEqual(
            status["evidence"]["true_forward_after_freeze_labels"], 0
        )
        self.assertEqual(status["cadence"]["last_trained_sample_count"], 60)
        self.assertEqual(status["historical_seed"]["sample_count"], 60)
        self.assertEqual(
            status["historical_seed"]["evidence_role"],
            "development_only_not_forward_or_execution_evidence",
        )
        self.assertFalse(status["paper_eligible"])
        self.assertFalse(status["real_money_eligible"])
        self.assertFalse(status["real_orders_enabled"])
        self.assertFalse(status["live_trading_enabled"])

    def test_legacy_learner_migrates_once_and_preserves_old_run(self):
        manifest = self._manifest(2)
        self._insert_boundary_decision(manifest["last_close"])
        old_version = self._prepare_legacy_v4_aggregate(manifest)

        inserted = store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 100,
        )
        status = store.status_snapshot(self.source_path, self.learning_path)

        self.assertEqual(inserted["sample_count"], 2)
        self.assertTrue(status["provenance_valid"])
        self.assertEqual(status["learner_version"], learner.LEARNER_VERSION)
        aggregate = sqlite3.connect(self.learning_path)
        aggregate.row_factory = sqlite3.Row
        try:
            migration = aggregate.execute(
                "SELECT * FROM online_learner_migrations"
            ).fetchall()
            state = aggregate.execute(
                "SELECT * FROM online_state WHERE singleton=1"
            ).fetchone()
            old_runs = aggregate.execute(
                "SELECT COUNT(*) FROM online_learning_runs WHERE learner_version=?",
                (old_version,),
            ).fetchone()[0]
        finally:
            aggregate.close()
        self.assertEqual(len(migration), 1)
        self.assertEqual(old_runs, 1)
        self.assertEqual(
            state["learner_migration_sha256"], migration[0]["record_sha256"]
        )

    def test_interrupted_migration_is_bound_to_original_manifest(self):
        first = self._manifest(2)
        second = self._manifest(1)
        self._insert_boundary_decision(first["last_close"])
        self._prepare_legacy_v4_aggregate(first)
        self.assertTrue(store._migrate_learner_version_for_historical_seed(
            self.source_path,
            self.learning_path,
            first,
            self.cutoff + 100,
        ))

        with self.assertRaisesRegex(
            store.LearningIntegrityError, "does not match the completed"
        ):
            store.seed_historical_development(
                self.source_path,
                second,
                self.learning_path,
                now_ms=self.cutoff + 101,
            )

    def test_migration_survives_prior_v5_probe_adding_nullable_column(self):
        manifest = self._manifest(2)
        self._insert_boundary_decision(manifest["last_close"])
        self._prepare_legacy_v4_aggregate(manifest, drop_new_column=True)
        with self.assertRaisesRegex(
            store.LearningStoreError, "version does not match"
        ):
            with store._connect_learning(self.learning_path):
                pass

        inserted = store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 100,
        )
        self.assertEqual(inserted["sample_count"], 2)
        self.assertTrue(
            store.status_snapshot(
                self.source_path, self.learning_path
            )["provenance_valid"]
        )

    def test_deleted_migration_marker_fails_status_provenance(self):
        manifest = self._manifest(2)
        self._insert_boundary_decision(manifest["last_close"])
        self._prepare_legacy_v4_aggregate(manifest)
        store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 100,
        )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute("DELETE FROM online_learner_migrations")
            aggregate.commit()
        finally:
            aggregate.close()

        status = store.status_snapshot(self.source_path, self.learning_path)
        self.assertFalse(status["provenance_valid"])
        self.assertEqual(status["status"], "learning_status_unavailable")

    def test_same_manifest_is_idempotent_and_replacement_fails_closed(self):
        manifest, _inserted = self._seed(2)
        replay = store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 200,
        )
        self.assertTrue(replay["idempotent"])

        replacement = self._manifest(1)
        with self.assertRaisesRegex(store.LearningIntegrityError, "cannot be replaced"):
            store.seed_historical_development(
                self.source_path,
                replacement,
                self.learning_path,
                now_ms=self.cutoff + 300,
            )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            self.assertEqual(
                aggregate.execute(
                    "SELECT COUNT(*) FROM historical_development_seeds"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                aggregate.execute(
                    "SELECT COUNT(*) FROM historical_development_samples"
                ).fetchone()[0],
                2,
            )
        finally:
            aggregate.close()

    def test_seed_tail_must_exactly_match_latest_worker_decision(self):
        manifest = self._manifest(1)
        self._insert_boundary_decision("190.01")
        with self.assertRaisesRegex(
            store.LearningIntegrityError, "tail does not exactly bridge"
        ):
            store.seed_historical_development(
                self.source_path,
                manifest,
                self.learning_path,
                now_ms=self.cutoff + 100,
            )

    def test_pre_registration_anchor_adds_one_development_only_bridge(self):
        manifest = self._manifest(1)
        self._insert_boundary_decision(
            manifest["last_close"], created_ms=self.cutoff - 1
        )
        store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 100,
        )

        next_close = BOUNDARY_MS + DAY_MS
        bridge = store.capture_daily_label(
            self.db,
            {"candle_close_ms": next_close, "close_latest": Decimal("191")},
            self.policy,
            self.model_version,
            self.cutoff + 200,
        )
        self.assertFalse(bridge["out_of_sample"])
        self.assertFalse(bridge["true_forward_after_freeze"])
        self._insert_later_decision(
            next_close, "191", created_ms=self.cutoff + 201
        )

        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 300
        )
        self.assertTrue(status["provenance_valid"])
        self.assertEqual(status["evidence"]["historical_development_labels"], 1)
        self.assertEqual(status["evidence"]["development_labels"], 2)
        self.assertEqual(status["evidence"]["eligible_oos_daily_labels"], 0)

    def test_frozen_seed_allows_exact_pre_oos_bridge_then_true_forward(self):
        manifest = self._viable_seed_manifest()
        self._insert_boundary_decision("100", created_ms=self.cutoff - 1)
        store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 100,
        )
        frozen = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 200
        )
        self.assertTrue(frozen["cadence"]["candidate_frozen"])

        first_close = BOUNDARY_MS + DAY_MS
        bridge = store.capture_daily_label(
            self.db,
            {"candle_close_ms": first_close, "close_latest": Decimal("101")},
            self.policy,
            self.model_version,
            self.cutoff + 300,
        )
        self.assertFalse(bridge["out_of_sample"])
        self.assertFalse(bridge["true_forward_after_freeze"])
        self._insert_later_decision(
            first_close, "101", created_ms=self.cutoff + 301
        )

        second_close = BOUNDARY_MS + 2 * DAY_MS
        forward = store.capture_daily_label(
            self.db,
            {"candle_close_ms": second_close, "close_latest": Decimal("102")},
            self.policy,
            self.model_version,
            self.cutoff + 400,
        )
        self.assertTrue(forward["out_of_sample"])
        self.assertTrue(forward["true_forward_after_freeze"])
        self._insert_later_decision(
            second_close, "102", created_ms=self.cutoff + 401
        )

        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 500
        )
        self.assertTrue(status["provenance_valid"])
        self.assertTrue(status["cadence"]["candidate_frozen"])
        self.assertEqual(status["evidence"]["historical_development_labels"], 60)
        self.assertEqual(status["evidence"]["eligible_oos_daily_labels"], 1)
        self.assertEqual(status["evidence"]["true_forward_after_freeze_labels"], 1)

    def test_second_non_oos_suffix_after_seed_fails_closed(self):
        manifest = self._manifest(1)
        self._insert_boundary_decision(
            manifest["last_close"], created_ms=self.cutoff - 2
        )
        store.seed_historical_development(
            self.source_path,
            manifest,
            self.learning_path,
            now_ms=self.cutoff + 100,
        )
        first_close = BOUNDARY_MS + DAY_MS
        store.capture_daily_label(
            self.db,
            {"candle_close_ms": first_close, "close_latest": Decimal("191")},
            self.policy,
            self.model_version,
            self.cutoff + 200,
        )
        self._insert_later_decision(
            first_close, "191", created_ms=self.cutoff - 1
        )
        second_close = BOUNDARY_MS + 2 * DAY_MS
        store.capture_daily_label(
            self.db,
            {"candle_close_ms": second_close, "close_latest": Decimal("192")},
            self.policy,
            self.model_version,
            self.cutoff + 300,
        )
        self._insert_later_decision(
            second_close, "192", created_ms=self.cutoff + 301
        )

        with self.assertRaisesRegex(
            store.LearningIntegrityError, "unexpected non-OOS"
        ):
            store.refresh(
                self.source_path,
                self.learning_path,
                now_ms=self.cutoff + 400,
            )

    def test_seed_holds_source_write_reservation_during_commit(self):
        manifest = self._manifest(1)
        self._insert_boundary_decision(manifest["last_close"])
        original = store._historical_seed_record
        concurrent_results = []

        def try_concurrent_start(*args, **kwargs):
            concurrent = sqlite3.connect(
                self.source_path, timeout=0, isolation_level=None
            )
            try:
                concurrent.execute(
                    "UPDATE worker_state SET desired_running=1 WHERE singleton=1"
                )
                concurrent_results.append("write_succeeded")
            except sqlite3.OperationalError as exc:
                concurrent_results.append(str(exc))
            finally:
                concurrent.close()
            return original(*args, **kwargs)

        with mock.patch.object(
            store, "_historical_seed_record", side_effect=try_concurrent_start
        ):
            store.seed_historical_development(
                self.source_path,
                manifest,
                self.learning_path,
                now_ms=self.cutoff + 100,
            )

        self.assertGreaterEqual(len(concurrent_results), 1)
        self.assertTrue(
            all("locked" in result.lower() for result in concurrent_results)
        )
        desired = self.db.execute(
            "SELECT desired_running FROM worker_state WHERE singleton=1"
        ).fetchone()[0]
        self.assertEqual(desired, 0)

    def test_seed_is_rejected_once_live_daily_evidence_exists(self):
        manifest = self._manifest(1)
        previous_close = BOUNDARY_MS - DAY_MS
        self.db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                feature_schema, feature_json, target_long, action, client_id,
                created_ms)
               VALUES (?, ?, '189', '100', '0', NULL, NULL, 0, 'hold_cash',
                       NULL, ?)""",
            (previous_close, self.policy, self.cutoff + 1),
        )
        store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": BOUNDARY_MS,
                "close_latest": Decimal("190"),
            },
            self.policy,
            self.model_version,
            self.cutoff + 2,
        )
        self._insert_boundary_decision("190")

        with self.assertRaisesRegex(store.LearningStoreError, "before live"):
            store.seed_historical_development(
                self.source_path,
                manifest,
                self.learning_path,
                now_ms=self.cutoff + 100,
            )
        aggregate = sqlite3.connect(self.learning_path)
        try:
            self.assertEqual(
                aggregate.execute(
                    "SELECT COUNT(*) FROM historical_development_seeds"
                ).fetchone()[0],
                0,
            )
        finally:
            aggregate.close()

    def test_seed_requires_stopped_worker_and_no_pending_intent(self):
        manifest = self._manifest(1)
        self._insert_boundary_decision(manifest["last_close"])
        self.db.execute(
            "UPDATE worker_state SET desired_running=1 WHERE singleton=1"
        )
        with self.assertRaisesRegex(store.LearningStoreError, "fully stopped"):
            store.seed_historical_development(
                self.source_path,
                manifest,
                self.learning_path,
                now_ms=self.cutoff + 100,
            )

        self.db.execute(
            """UPDATE worker_state SET desired_running=0,
               pending_client_id='pending-test' WHERE singleton=1"""
        )
        with self.assertRaisesRegex(store.LearningStoreError, "fully stopped"):
            store.seed_historical_development(
                self.source_path,
                manifest,
                self.learning_path,
                now_ms=self.cutoff + 101,
            )

    def test_tampered_seed_sample_makes_status_unavailable(self):
        self._seed(2)
        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute(
                """UPDATE historical_development_samples
                   SET sample_json='{}' WHERE ordinal=0"""
            )
            aggregate.commit()
        finally:
            aggregate.close()

        status = store.status_snapshot(self.source_path, self.learning_path)
        self.assertFalse(status["provenance_valid"])
        self.assertEqual(status["status"], "learning_status_unavailable")
        self.assertIn("Historical development sample", status["last_error"])

    def test_read_only_status_accepts_legacy_aggregate_without_seed_tables(self):
        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 100
        )
        self.assertTrue(status["provenance_valid"])
        aggregate = sqlite3.connect(self.learning_path)
        try:
            aggregate.execute("DROP TABLE historical_development_samples")
            aggregate.execute("DROP TABLE historical_development_seeds")
            aggregate.commit()
        finally:
            aggregate.close()

        legacy_status = store.status_snapshot(
            self.source_path, self.learning_path
        )

        self.assertTrue(legacy_status["provenance_valid"])
        self.assertIsNone(legacy_status["historical_seed"])
        self.assertEqual(
            legacy_status["evidence"]["historical_development_labels"], 0
        )

    def test_exact_live_suffix_counts_as_live_oos_but_not_historical(self):
        self._seed(1)
        next_close = BOUNDARY_MS + DAY_MS
        store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": next_close,
                "close_latest": Decimal("191"),
            },
            self.policy,
            self.model_version,
            self.cutoff + 200,
        )
        self.db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                feature_schema, feature_json, target_long, action, client_id,
                created_ms)
               VALUES (?, ?, '191', '100', '0', NULL, NULL, 0, 'hold_cash',
                       NULL, ?)""",
            (next_close, self.policy, self.cutoff + 201),
        )

        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 300
        )

        self.assertEqual(status["evidence"]["historical_development_labels"], 1)
        self.assertEqual(status["evidence"]["development_labels"], 2)
        self.assertEqual(status["evidence"]["finalized_daily_labels"], 1)
        self.assertEqual(status["evidence"]["eligible_oos_daily_labels"], 1)
        self.assertEqual(
            status["evidence"]["true_forward_after_freeze_labels"], 0
        )
        self.assertEqual(status["cadence"]["labels_until_next_training"], 58)

    def test_gap_starts_new_lifecycle_without_reusing_seed(self):
        manifest, _inserted = self._seed(1)
        store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 200
        )
        later_close = BOUNDARY_MS + 2 * DAY_MS
        store.capture_daily_label(
            self.db,
            {
                "candle_close_ms": later_close,
                "close_latest": Decimal("191"),
            },
            self.policy,
            self.model_version,
            self.cutoff + 300,
        )
        self.db.execute(
            """INSERT INTO worker_decisions
               (candle_close_ms, policy, close_latest, close_30d, momentum,
                feature_schema, feature_json, target_long, action, client_id,
                created_ms)
               VALUES (?, ?, '191', '100', '0', NULL, NULL, 0, 'hold_cash',
                       NULL, ?)""",
            (later_close, self.policy, self.cutoff + 301),
        )

        status = store.refresh(
            self.source_path, self.learning_path, now_ms=self.cutoff + 400
        )

        self.assertEqual(status["evidence"]["historical_development_labels"], 1)
        self.assertEqual(status["evidence"]["development_labels"], 0)
        self.assertEqual(status["evidence"]["eligible_oos_daily_labels"], 0)
        self.assertEqual(status["evidence"]["daily_gaps"], 1)
        self.assertEqual(status["cadence"]["labels_until_next_training"], 60)


if __name__ == "__main__":
    unittest.main()
