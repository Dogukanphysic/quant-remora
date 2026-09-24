"""Pure, operator-enabled adapter for experimental pre-entry model scores.

Scores are uncalibrated account-wallet proxies, not forecasts of trade PnL.
The adapter can only select among entries allowed by the existing strategy.
It has no exchange access, artifact writes, position sizing or exit authority.
"""
from __future__ import annotations

import hashlib
import math


VERSION = "btc-futures-adaptive-explore-v1"
MODE = "experimental_adaptive_explore"
LOSS_SCORE_THRESHOLD = 0.70
MAX_SCORE_AGE_SECONDS = 120


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _digest(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdefABCDEF" for char in value))


def decide(prediction, *, enabled, baseline_allowed, candidate_key, now,
           context_digest):
    """Return a new decision without changing any input or model authority.

    ``prediction`` must already have passed the producer's artifact, feature
    and source-provenance validation. This second check requires a fresh score
    and evidence from at least one matching-domain sample. Invalid evidence
    keeps the existing strategy decision and never claims model authority.

    ``candidate_key`` identifies account, epoch, bar and policy version; it must
    exclude model version and poll time so retries cannot reroll exploration.
    """
    baseline = baseline_allowed is True
    result = {"version": VERSION, "mode": MODE if enabled is True else "shadow_only",
              "baseline_allowed": baseline, "allow": baseline,
              "authority_applied": False, "decision_authority": False,
              "exploration": False, "economic_validation": False}

    def fallback(reason):
        return {**result, "reason": reason}

    if enabled is not True:
        return fallback("shadow_only")
    if not baseline:
        return fallback("baseline_rejected")
    if not isinstance(prediction, dict) or prediction.get("available") is not True:
        return fallback("fallback_prediction_unavailable")
    if prediction.get("status") != "scored":
        return fallback("fallback_prediction_not_scored")
    if not isinstance(candidate_key, str) or not candidate_key.strip():
        return fallback("fallback_candidate_key_invalid")
    if not _digest(context_digest) or prediction.get("context_digest") != context_digest:
        return fallback("fallback_context_mismatch")
    model_id = prediction.get("model_id")
    if (not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 128
            or not _digest(prediction.get("artifact_digest"))
            or not _digest(prediction.get("source_fingerprint"))):
        return fallback("fallback_model_identity_invalid")
    if any(prediction.get(flag) is not False for flag in (
            "automatic_activation", "decision_authority", "real_orders_enabled",
            "mainnet_candidate_active")):
        return fallback("fallback_artifact_authority_invalid")
    score = prediction.get("loss_probability")
    if not _number(score) or not 0 <= score <= 1:
        return fallback("fallback_score_invalid")
    predicted_at = prediction.get("predicted_at")
    if not _number(now) or not _number(predicted_at) or now <= 0 or predicted_at <= 0:
        return fallback("fallback_prediction_time_invalid")
    if predicted_at > now:
        return fallback("fallback_prediction_from_future")
    if now - predicted_at > MAX_SCORE_AGE_SECONDS:
        return fallback("fallback_prediction_stale")
    for field in ("model_created_at", "trained_through_at"):
        if field in prediction and (not _number(prediction[field])
                                    or prediction[field] < 0
                                    or prediction[field] > predicted_at):
            return fallback("fallback_training_time_invalid")
    total = prediction.get("training_sample_count")
    evidence = prediction.get("control_evidence")
    if not _count(total) or not isinstance(evidence, dict):
        return fallback("fallback_domain_evidence_invalid")
    domain_count = evidence.get("domain_sample_count")
    if (evidence.get("domain_match") is not True or not _count(domain_count)
            or domain_count > total):
        return fallback("fallback_domain_evidence_invalid")

    # A candidate is allocated once, independently of polling or model updates.
    bucket = int(hashlib.sha256((VERSION + ":" + candidate_key).encode("utf-8")).hexdigest(), 16) % 3
    high_score = score >= LOSS_SCORE_THRESHOLD
    explore = high_score and bucket == 0
    allow = not high_score or explore
    return {**result, "allow": allow, "authority_applied": True,
            "decision_authority": True, "exploration": explore,
            "reason": "model_explore" if explore else "model_defer" if high_score else "model_accept",
            "model_id": prediction["model_id"],
            "artifact_digest": prediction["artifact_digest"],
            "source_fingerprint": prediction["source_fingerprint"],
            "context_digest": context_digest, "loss_probability": float(score),
            "training_sample_count": total, "domain_sample_count": domain_count,
            "score_interpretation": "uncalibrated_exploratory_wallet_proxy_score",
            "threshold": LOSS_SCORE_THRESHOLD, "exploration_bucket": bucket,
            "exploration_modulus": 3}
