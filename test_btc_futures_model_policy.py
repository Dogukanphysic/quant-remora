import copy
import hashlib
import json
import unittest

import btc_futures_model_policy as policy


class ModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.prediction = {
            "available": True, "status": "scored", "model_id": "pre-entry-1234567890abcdef1234",
            "artifact_digest": "a" * 64, "source_fingerprint": "b" * 64,
            "context_digest": "c" * 64, "loss_probability": 0.4,
            "predicted_at": 1000, "model_created_at": 950,
            "trained_through_at": 900, "training_sample_count": 1,
            "control_evidence": {"domain_match": True, "domain_sample_count": 1},
            "decision_authority": False, "automatic_activation": False,
            "real_orders_enabled": False, "mainnet_candidate_active": False,
        }
        self.options = dict(enabled=True, baseline_allowed=True,
                            candidate_key="account:epoch:bar:policy", now=1000,
                            context_digest="c" * 64)

    def decide(self, prediction=None, **options):
        return policy.decide(self.prediction if prediction is None else prediction,
                             **{**self.options, **options})

    def assertFallback(self, result):
        self.assertTrue(result["allow"])
        self.assertFalse(result["authority_applied"])
        self.assertFalse(result["decision_authority"])
        self.assertFalse(result["economic_validation"])
        self.assertNotIn("loss_probability", result)

    def test_disabled_keeps_strategy_and_never_grants_authority(self):
        for flag in (False, None, 1, "true"):
            with self.subTest(flag=flag):
                result = self.decide(enabled=flag)
                self.assertFallback(result)
                self.assertEqual(result["mode"], "shadow_only")

    def test_model_cannot_create_entry_rejected_by_baseline(self):
        for enabled in (False, True):
            for flag in (False, None, 1, "true"):
                with self.subTest(enabled=enabled, flag=flag):
                    result = self.decide(enabled=enabled, baseline_allowed=flag)
                    self.assertFalse(result["allow"])
                    self.assertFalse(result["authority_applied"])

    def test_one_domain_sample_is_enough_for_experimental_influence(self):
        result = self.decide()
        self.assertTrue(result["allow"])
        self.assertTrue(result["authority_applied"])
        self.assertEqual(result["reason"], "model_accept")
        self.assertEqual(result["domain_sample_count"], 1)
        self.assertFalse(result["economic_validation"])
        json.dumps(result, allow_nan=False)

    def test_high_scores_use_fixed_minority_exploration(self):
        self.prediction["loss_probability"] = 0.7
        keys = {}
        for index in range(100):
            key = f"account:epoch:bar{index}:policy"
            bucket = int(hashlib.sha256((policy.VERSION + ":" + key).encode()).hexdigest(), 16) % 3
            keys.setdefault(bucket, key)
        self.assertEqual(set(keys), {0, 1, 2})
        for bucket, key in keys.items():
            with self.subTest(bucket=bucket):
                result = self.decide(candidate_key=key)
                self.assertEqual(result["allow"], bucket == 0)
                self.assertEqual(result["exploration"], bucket == 0)
                self.assertEqual(result["exploration_bucket"], bucket)
                self.assertEqual(result["reason"], "model_explore" if bucket == 0 else "model_defer")
                self.prediction["model_id"] = "pre-entry-fedcba0987654321abcd"
                self.prediction["artifact_digest"] = "d" * 64
                later = self.decide(candidate_key=key, now=1020)
                self.assertEqual(later["allow"], result["allow"])
                self.assertEqual(later["exploration_bucket"], bucket)

    def test_score_boundaries(self):
        for score in (0, 0.699999, 0.7, 1):
            with self.subTest(score=score):
                self.prediction["loss_probability"] = score
                result = self.decide()
                self.assertTrue(result["authority_applied"])
                if score < 0.7:
                    self.assertTrue(result["allow"])

    def test_unavailable_predictions_fall_back_without_score(self):
        for prediction in ({}, {"available": False}, [], "text", {"available": 1}):
            with self.subTest(prediction=prediction):
                self.assertFallback(self.decide(prediction))
        self.prediction["status"] = "unavailable"
        self.assertFallback(self.decide())

    def test_invalid_scores_are_not_used(self):
        for score in (True, False, None, "0.5", float("nan"), float("inf"), float("-inf"), -0.01, 1.01, 10 ** 1000):
            with self.subTest(score=score):
                self.prediction["loss_probability"] = score
                self.assertFallback(self.decide())

    def test_bad_or_missing_identity_falls_back(self):
        for field in ("model_id", "artifact_digest", "source_fingerprint", "context_digest"):
            for value in (None, "", False, 0):
                with self.subTest(field=field, value=value):
                    changed = {**self.prediction, field: value}
                    self.assertFallback(self.decide(changed))
        self.assertFallback(self.decide(context_digest="d" * 64))
        self.assertFallback(self.decide(candidate_key=""))
        self.assertFallback(self.decide({**self.prediction, "model_id": "x" * 129}))

    def test_artifact_cannot_grant_itself_runtime_authority(self):
        for field in ("automatic_activation", "decision_authority", "real_orders_enabled", "mainnet_candidate_active"):
            for flag in (True, None, 0, "false"):
                with self.subTest(field=field, flag=flag):
                    self.assertFallback(self.decide({**self.prediction, field: flag}))

    def test_stale_future_and_nonfinite_time_fall_back(self):
        for stamp in (879, 1001, None, True, float("nan"), float("inf"), 0, -1):
            with self.subTest(stamp=stamp):
                self.assertFallback(self.decide({**self.prediction, "predicted_at": stamp}))
        self.assertTrue(self.decide({**self.prediction, "predicted_at": 880,
                                   "model_created_at": 870, "trained_through_at": 860})["authority_applied"])
        for now in (None, True, float("nan"), float("inf"), 0):
            self.assertFallback(self.decide(now=now))

    def test_training_metadata_cannot_come_after_prediction(self):
        for field in ("model_created_at", "trained_through_at"):
            for value in (1001, -1, True, None, float("nan")):
                with self.subTest(field=field, value=value):
                    self.assertFallback(self.decide({**self.prediction, field: value}))

    def test_matching_domain_is_required_and_counts_must_be_consistent(self):
        for evidence in (None, {}, {"domain_match": False, "domain_sample_count": 1},
                         {"domain_match": 1, "domain_sample_count": 1}):
            with self.subTest(evidence=evidence):
                self.assertFallback(self.decide({**self.prediction, "control_evidence": evidence}))
        for count in (0, -1, 2, True, 1.0, "1", None):
            with self.subTest(count=count):
                evidence = {"domain_match": True, "domain_sample_count": count}
                self.assertFallback(self.decide({**self.prediction, "control_evidence": evidence}))
        for total in (0, -1, True, 1.0, "1", None):
            self.assertFallback(self.decide({**self.prediction, "training_sample_count": total}))

    def test_inputs_and_artifact_authority_flags_are_unchanged(self):
        before = copy.deepcopy(self.prediction)
        self.decide()
        self.assertEqual(self.prediction, before)
        self.assertFalse(self.prediction["decision_authority"])
        self.assertFalse(self.prediction["automatic_activation"])
        self.assertFalse(self.prediction["real_orders_enabled"])
        unavailable = {**self.prediction, "available": False, "decision_authority": True}
        self.assertFallback(self.decide(unavailable))


if __name__ == "__main__":
    unittest.main()
