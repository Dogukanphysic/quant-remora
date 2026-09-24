"""Early observational summaries; no trading decisions, I/O, or execution authority."""

from decimal import Decimal, InvalidOperation
import math


FIELDS = ("entry_regime", "signal_profile", "leverage")
MAX_GROUPS = 8
PROXY = "account_wallet_delta_unverified"
ASSUMPTION = "independent_stationary_binomial_assumption_unverified_in_trading"
CAUTION = (
    "Wallet changes are unverified proxies affected by transfers, fees, funding and "
    "other positions. These exploratory observations do not establish causes, "
    "future trade probabilities, profitability or a ban on any condition. "
    "The Wilson interval assumes independent observations with a constant label "
    "probability; correlated trades and changing markets can invalidate that assumption."
)


def _decimal(value, name):
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid_" + name)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid_" + name) from None
    if not result.is_finite():
        raise ValueError("nonfinite_" + name)
    return result


def _number(value, name):
    result = float(_decimal(value, name))
    if not math.isfinite(result):
        raise ValueError("nonfinite_" + name)
    return result


def _category(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError("invalid_" + name)
    return value


def _validate(sample):
    if not isinstance(sample, dict):
        raise ValueError("invalid_sample")
    sample_id = sample.get("id")
    if not isinstance(sample_id, str) or not sample_id.strip() or len(sample_id) > 512:
        raise ValueError("invalid_sample_id")
    if "label" not in sample:
        raise ValueError("missing_label")
    label = sample["label"]
    if label is not None and (type(label) is not int or label not in (0, 1)):
        raise ValueError("invalid_label")
    delta = _decimal(sample.get("wallet_delta_proxy_usdt"), "wallet_delta_proxy")
    expected_label = 1 if delta < 0 else 0 if delta > 0 else None
    if label != expected_label:
        raise ValueError("label_wallet_delta_conflict")
    closed = _number(sample.get("closed_at"), "closed_at")
    opened = _number(sample.get("opened_at"), "opened_at")
    if not 0 <= opened <= closed:
        raise ValueError("invalid_sample_chronology")
    close_id = sample.get("closed_event_id")
    if type(close_id) is not int or close_id < 1:
        raise ValueError("invalid_closed_event_id")
    quality = sample.get("quality")
    if quality not in {"entry_snapshot_wallet_proxy", "legacy_wallet_proxy"}:
        raise ValueError("invalid_sample_quality")
    context = sample.get("entry_context")
    categories = {}
    if quality == "legacy_wallet_proxy":
        # No reconstruction of conditions from fills, exits or current settings.
        if context is not None:
            raise ValueError("legacy_entry_context_conflict")
    else:
        if not isinstance(context, dict):
            raise ValueError("missing_immutable_entry_context")
        captured = _number(context.get("captured_at"), "captured_at")
        if not 0 <= captured <= opened:
            raise ValueError("entry_context_captured_after_intent")
        decision = context.get("decision")
        if not isinstance(decision, dict):
            raise ValueError("invalid_entry_decision")
        for name, value in (("entry_regime", decision.get("entry_regime")),
                            ("signal_profile", context.get("signal_profile"))):
            value = _category(value, name)
            if value is not None:
                categories[name] = value
        if context.get("leverage") is not None:
            leverage = _decimal(context["leverage"], "entry_leverage")
            if leverage <= 0:
                raise ValueError("invalid_entry_leverage")
            categories["leverage"] = format(leverage.normalize(), "f")
    return {"id": sample_id, "label": label, "delta": delta,
            "closed_at": closed, "closed_event_id": close_id,
            "legacy": quality == "legacy_wallet_proxy", "categories": categories}


def _label_rate(negative, positive):
    count = negative + positive
    interval = None
    if count:
        z = 1.959963984540054
        rate = negative / count
        denominator = 1 + z * z / count
        center = (rate + z * z / (2 * count)) / denominator
        radius = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count * count)) / denominator
        interval = {"lower": max(0.0, center - radius), "upper": min(1.0, center + radius)}
    return {"scope": "observed_negative_wallet_proxy_labels_not_trade_forecast",
            "non_neutral_label_count": count,
            "empirical_rate": negative / count if count else None,
            "beta_prior": {"alpha": 2, "beta": 2},
            "shrunk_mean": (negative + 2) / (count + 4),
            "prior_only": count == 0,
            "wilson_95_descriptive_interval": interval,
            "interval_assumption": ASSUMPTION}


def _counts(rows):
    negative = sum(row["label"] == 1 for row in rows)
    positive = sum(row["label"] == 0 for row in rows)
    return {"closed_sample_count": len(rows), "negative_count": negative,
            "positive_count": positive, "neutral_count": len(rows) - negative - positive,
            "total_negative_wallet_proxy_usdt": str(sum((row["delta"] for row in rows if row["delta"] < 0), Decimal(0))),
            "total_positive_wallet_proxy_usdt": str(sum((row["delta"] for row in rows if row["delta"] > 0), Decimal(0))),
            "net_wallet_proxy_usdt": str(sum((row["delta"] for row in rows), Decimal(0))),
            "negative_label_rate": _label_rate(negative, positive)}


def summarize(samples):
    """Recompute on every closed sample; reject malformed or duplicated evidence.

    Samples are the reconstructed closed rows in btc_futures_learning. Input is
    never mutated. Labels are 1=negative, 0=positive, None=neutral wallet proxy.
    Wilson reference: https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm
    """
    if not isinstance(samples, (list, tuple)):
        raise ValueError("invalid_samples_container")
    rows = [_validate(sample) for sample in samples]
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_closed_sample_id")
    rows.sort(key=lambda row: (row["closed_at"], row["closed_event_id"], row["id"]))
    streak = 0
    for row in reversed(rows):
        if row["label"] != 1:
            break
        streak += 1
    groups = {}
    for row in rows:
        for name, value in row["categories"].items():
            groups.setdefault((name, value), []).append(row)
    # Rank by evidence volume, never selectively expose only losing conditions.
    ranked = sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    group_reports = []
    for (name, value), members in ranked[:MAX_GROUPS]:
        counts = _counts(members)
        group_reports.append({"condition_field": name, "condition_value": value,
                              **counts,
                              "review_flag": "repeated_negative_proxy_for_review" if counts["negative_count"] >= 3 else None,
                              "interpretation": "exploratory_association_not_cause_or_automatic_exclusion"})
    return {"schema": 1,
            "status": "collecting_closed_samples" if not rows else "initial_observation" if len(rows) == 1 else "early_observations",
            "mode": "advisory_only", "decision_authority": False,
            "automatic_activation_enabled": False, "writes_active_config": False,
            "execution_verified": False, "pnl_basis": PROXY,
            "cadence": {"minimum_closed_samples": 1, "retrain_every_new_closed_label": 1,
                        "fixed_wait_days": None, "trigger": "each_new_closed_sample"},
            **_counts(rows), "chronological_trailing_negative_streak": streak,
            "closed_sample_ids": [row["id"] for row in rows],
            "legacy_closed_sample_count": sum(row["legacy"] for row in rows),
            "entry_context_closed_sample_count": sum(not row["legacy"] for row in rows),
            "missing_entry_context_count": sum(row["legacy"] for row in rows),
            "missing_group_field_counts": {name: sum(name not in row["categories"] for row in rows) for name in FIELDS},
            "group_fields": list(FIELDS), "groups": group_reports,
            "group_count": len(groups), "omitted_group_count": max(0, len(groups) - MAX_GROUPS),
            "group_scope": "separate_single_field_pre_entry_conditions_with_overlapping_membership",
            "caveat": CAUTION}
