"""Integration of few-sample review with immutable, advisory-only learning."""
import json
import unittest

import btc_futures_learning as learner
import test_btc_futures_forward_learning as fixtures


class EarlyLearningTests(unittest.TestCase):
    setUp = fixtures.FuturesForwardLearningTests.setUp
    tearDown = fixtures.FuturesForwardLearningTests.tearDown
    _db = fixtures.FuturesForwardLearningTests._db
    _event = fixtures.FuturesForwardLearningTests._event
    _context = fixtures.FuturesForwardLearningTests._context
    _open = fixtures.FuturesForwardLearningTests._open
    _close = fixtures.FuturesForwardLearningTests._close

    def test_one_label_trains_without_wait_and_each_new_label_retrains(self):
        self._close(self._open(0, self._context(0)))
        original_source = self.source.read_bytes()
        first = learner.sync(self.source, self.learning)
        self.assertEqual(first["trained_sample_count"], 1)
        self.assertEqual(first["pre_entry_trained_sample_count"], 1)
        early = first["early_learning"]
        self.assertTrue(early["usable_for_review"])
        self.assertEqual(early["training_cadence"]["first_training_at_closed_non_neutral_labels"], 1)
        self.assertEqual(self.source.read_bytes(), original_source)
        replay = learner.sync(self.source, self.learning)
        self.assertEqual(replay["training_runs"], first["training_runs"])
        self._close(self._open(1, self._context(1)), "2")
        second = learner.sync(self.source, self.learning)
        self.assertEqual(second["training_runs"], first["training_runs"] + 1)
        self.assertEqual(second["pre_entry_trained_sample_count"], 2)
        self.assertFalse(second["early_learning"]["decision_authority"])
        self.assertFalse(second["readiness"]["ready_for_live"])

    def test_neutral_and_open_trades_do_not_create_training_labels(self):
        self._close(self._open(0, self._context(0)), "0")
        self._open(1, self._context(1))
        report = learner.sync(self.source, self.learning)
        self.assertEqual(report["sample_count"], 1)
        self.assertEqual(report["trained_sample_count"], 0)
        self.assertIsNone(report["pre_entry_model_id"])
        self.assertTrue(report["early_learning"]["usable_for_review"])

    def test_invalid_source_clears_review_and_stale_source_marks_it_unusable(self):
        self._close(self._open(0, self._context(0)))
        learner.sync(self.source, self.learning)
        self.wall += learner.MAX_SOURCE_AGE_SECONDS + 1
        self.assertFalse(learner.status(self.learning)["early_learning"]["usable_for_review"])
        with self._db(self.source) as db:
            db.execute("UPDATE events SET payload=? WHERE kind='configured'", (json.dumps({"changed": True}),))
        invalid = learner.sync(self.source, self.learning)
        self.assertEqual(invalid["status"], "source_conflict")
        self.assertFalse(invalid["early_learning"]["valid"])
        self.assertFalse(invalid["early_learning"]["usable_for_review"])

    def test_prediction_explains_single_sample_without_changing_model_score(self):
        self._close(self._open(0, self._context(0)))
        learner.sync(self.source, self.learning)
        context = self._context(1)
        self.wall = context["captured_at"]
        learner.sync(self.source, self.learning)
        prediction = learner.predict_entry(self.learning, context, now=self.wall)
        self.assertTrue(prediction["available"])
        self.assertEqual(prediction["training_sample_count"], 1)
        self.assertEqual(prediction["training_label_classes"], [1])
        self.assertTrue(prediction["single_label_class"])
        self.assertEqual(prediction["score_interpretation"], "uncalibrated_exploratory_wallet_proxy_score")
        self.assertFalse(prediction["decision_authority"])
        self.assertGreater(prediction["loss_probability"], 0)
        self.assertLess(prediction["loss_probability"], 1)


if __name__ == "__main__":
    unittest.main()
