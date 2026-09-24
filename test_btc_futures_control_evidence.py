"""Matching-domain provenance for the operator's experimental adapter."""
import json
import unittest

import btc_futures_learning as learner
import test_btc_futures_forward_learning as fixtures


class ControlEvidenceTests(unittest.TestCase):
    setUp = fixtures.FuturesForwardLearningTests.setUp
    tearDown = fixtures.FuturesForwardLearningTests.tearDown
    _db = fixtures.FuturesForwardLearningTests._db
    _event = fixtures.FuturesForwardLearningTests._event
    _context = fixtures.FuturesForwardLearningTests._context
    _open = fixtures.FuturesForwardLearningTests._open
    _close = fixtures.FuturesForwardLearningTests._close

    def train_one(self, leverage=4):
        context = self._context(0)
        context["leverage"] = leverage
        self._close(self._open(0, context))
        learner.sync(self.source, self.learning)
        candidate = self._context(1)
        candidate["leverage"] = leverage
        self.wall = candidate["captured_at"]
        learner.sync(self.source, self.learning)
        return candidate

    def test_single_matching_sample_can_supply_experimental_control_evidence(self):
        context = self.train_one(10)
        prediction = learner.predict_entry(self.learning, context, now=self.wall)
        self.assertTrue(prediction["available"])
        evidence = prediction["control_evidence"]
        self.assertTrue(evidence["domain_match"])
        self.assertEqual(evidence["domain_sample_count"], 1)
        self.assertEqual(evidence["requested_domain"]["leverage"], "10")
        self.assertFalse(prediction["decision_authority"])
        self.assertFalse(evidence["economic_validation_passed"])

    def test_old_leverage_or_different_profiles_cannot_control_current_domain(self):
        context = self.train_one(4)
        for change in ({"leverage": 10}, {"signal_profile": "responsive"}, {"risk_profile": "aggressive"}):
            with self.subTest(change=change):
                candidate = {**context, **change}
                prediction = learner.predict_entry(self.learning, candidate, now=self.wall)
                self.assertTrue(prediction["available"])  # Shadow score still observable.
                self.assertFalse(prediction["control_evidence"]["domain_match"])
                self.assertEqual(prediction["control_evidence"]["domain_sample_count"], 0)

    def test_old_artifacts_have_no_invented_domain_evidence(self):
        evidence = learner._control_evidence({}, self._context(1))
        self.assertFalse(evidence["domain_match"])
        self.assertEqual(evidence["reason"], "training_domain_metadata_unavailable")

    def test_future_source_refresh_is_not_fresh_evidence(self):
        context = self.train_one()
        with self._db(self.learning) as db:
            report = learner._get(db, "report")
            report["last_success_at"] = self.wall + 1
            learner._put(db, "report", report)
        prediction = learner.predict_entry(self.learning, context, now=self.wall)
        self.assertFalse(prediction["available"])
        self.assertEqual(prediction["reason"], "stale_source_snapshot")

    def test_runtime_authority_is_reported_separately_from_learner_authority(self):
        self.train_one()
        with self._db(self.source) as db:
            state = json.loads(db.execute("SELECT value FROM state WHERE id=1").fetchone()[0])
            state.update(model_decisions_enabled=True,
                         model_control_latest={"authority_applied": True, "reason": "model_defer"})
            db.execute("UPDATE state SET value=? WHERE id=1", (json.dumps(state),))
        report = learner.sync(self.source, self.learning)
        self.assertFalse(report["decision_authority"])
        self.assertTrue(report["execution_adapter"]["enabled_in_source"])
        self.assertTrue(report["execution_adapter"]["last_decision"]["authority_applied"])
        self.assertFalse(report["execution_adapter"]["economic_validation_passed"])


if __name__ == "__main__":
    unittest.main()
