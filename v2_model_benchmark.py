"""Fixed, leakage-aware model-family benchmark for Bollinger 15M v2.

This module is research-only.  It never writes the live model or paper ledger.
Every family selects its own small hyperparameter/threshold grid on an inner
chronological block and is scored once on purged outer walk-forward blocks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
import time
from typing import Mapping, Sequence

from agent import read_dataset
from v2_engine import MAIN_SPEC, purged_expanding_walk_forward
import v2_model


SEED = v2_model.TRAINING_SEED
OUTER_FOLDS = 5
MIN_OUTER_TRAIN_EVENTS = 1_000
THRESHOLDS = v2_model.THRESHOLD_GRID
RETURN_THRESHOLDS = (0.0, 0.001, 0.002, 0.003)

# This compact grid is deliberately fixed in source before reading outer-fold
# results.  Expanding it after seeing the report would invalidate the comparison.
FAMILY_CONFIGS: dict[str, tuple[dict[str, object], ...]] = {
    "logistic_l2": tuple(
        {"id": f"logistic-c{c:g}", "family": "logistic_l2", "c": c}
        for c in (0.05, 0.2, 1.0)
    ),
    "random_forest": (
        {"id": "rf-d3-l40", "family": "random_forest", "max_depth": 3, "min_samples_leaf": 40},
        {"id": "rf-d5-l40", "family": "random_forest", "max_depth": 5, "min_samples_leaf": 40},
        {"id": "rf-d5-l80", "family": "random_forest", "max_depth": 5, "min_samples_leaf": 80},
    ),
    "extra_trees": (
        {"id": "et-d3-l40", "family": "extra_trees", "max_depth": 3, "min_samples_leaf": 40},
        {"id": "et-d5-l40", "family": "extra_trees", "max_depth": 5, "min_samples_leaf": 40},
        {"id": "et-d5-l80", "family": "extra_trees", "max_depth": 5, "min_samples_leaf": 80},
    ),
    "hist_gradient_boosting": (
        {"id": "hgb-l7-r1", "family": "hist_gradient_boosting", "max_leaf_nodes": 7, "l2": 1.0},
        {"id": "hgb-l7-r5", "family": "hist_gradient_boosting", "max_leaf_nodes": 7, "l2": 5.0},
        {"id": "hgb-l15-r5", "family": "hist_gradient_boosting", "max_leaf_nodes": 15, "l2": 5.0},
    ),
    "weighted_interaction_logistic": tuple(
        {
            "id": f"wil-c{c:g}", "family": "weighted_interaction_logistic",
            "c": c, "interactions": True, "magnitude_weighted": True,
        }
        for c in (0.05, 0.2, 1.0)
    ),
    "ridge_expected_return": tuple(
        {
            "id": f"ridge-a{alpha:g}", "family": "ridge_expected_return",
            "alpha": alpha, "interactions": True, "score_kind": "expected_net_return",
        }
        for alpha in (1.0, 10.0, 100.0)
    ),
    "huber_gradient_return": (
        {
            "id": "huber-d1-n50", "family": "huber_gradient_return",
            "max_depth": 1, "n_estimators": 50, "score_kind": "expected_net_return",
        },
        {
            "id": "huber-d1-n100", "family": "huber_gradient_return",
            "max_depth": 1, "n_estimators": 100, "score_kind": "expected_net_return",
        },
        {
            "id": "huber-d2-n50", "family": "huber_gradient_return",
            "max_depth": 2, "n_estimators": 50, "score_kind": "expected_net_return",
        },
    ),
}


def _matrix(samples: Sequence[Mapping[str, object]], *, interactions: bool = False):
    import numpy as np

    x = np.asarray([[float(value) for value in sample["x"]] for sample in samples], dtype=float)
    if interactions:
        strategy = x[:, -1]
        x = np.concatenate((x, x[:, :-1] * strategy[:, None]), axis=1)
    classification = np.asarray(
        [int(float(sample["net_return"]) > 0) for sample in samples], dtype=int
    )
    returns = np.asarray([float(sample["net_return"]) for sample in samples], dtype=float)
    return x, classification, returns


def _estimator(config: Mapping[str, object]):
    family = str(config["family"])
    if family == "logistic_l2":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(
            StandardScaler(),
            LogisticRegression(
                C=float(config["c"]), solver="liblinear", random_state=SEED,
                max_iter=2_000,
            ),
        )
    if family == "random_forest":
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(
            n_estimators=200, max_depth=int(config["max_depth"]),
            min_samples_leaf=int(config["min_samples_leaf"]), max_features=0.7,
            bootstrap=True, n_jobs=-1, random_state=SEED,
        )
    if family == "extra_trees":
        from sklearn.ensemble import ExtraTreesClassifier

        return ExtraTreesClassifier(
            n_estimators=200, max_depth=int(config["max_depth"]),
            min_samples_leaf=int(config["min_samples_leaf"]), max_features=0.7,
            bootstrap=False, n_jobs=-1, random_state=SEED,
        )
    if family == "hist_gradient_boosting":
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(
            learning_rate=0.05, max_iter=150,
            max_leaf_nodes=int(config["max_leaf_nodes"]),
            min_samples_leaf=40, l2_regularization=float(config["l2"]),
            early_stopping=False, random_state=SEED,
        )
    if family == "weighted_interaction_logistic":
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(
            StandardScaler(),
            LogisticRegression(
                C=float(config["c"]), solver="liblinear", random_state=SEED,
                max_iter=2_000,
            ),
        )
    if family == "ridge_expected_return":
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(StandardScaler(), Ridge(alpha=float(config["alpha"])))
    if family == "huber_gradient_return":
        from sklearn.ensemble import GradientBoostingRegressor

        return GradientBoostingRegressor(
            loss="huber", learning_rate=0.03,
            n_estimators=int(config["n_estimators"]),
            max_depth=int(config["max_depth"]), min_samples_leaf=40,
            random_state=SEED,
        )
    raise ValueError(f"Unknown family: {family}")


def _scores(
    config: Mapping[str, object],
    train: Sequence[Mapping[str, object]],
    evaluation: Sequence[Mapping[str, object]],
) -> tuple[list[float], float | None]:
    interactions = bool(config.get("interactions", False))
    train_x, train_y, train_returns = _matrix(train, interactions=interactions)
    evaluation_x, _, _ = _matrix(evaluation, interactions=interactions)
    score_kind = str(config.get("score_kind", "probability"))
    model = _estimator(config)
    if score_kind == "expected_net_return":
        model.fit(train_x, train_returns)
        scores = [float(value) for value in model.predict(evaluation_x)]
        if not all(math.isfinite(value) for value in scores):
            raise ValueError("Estimator produced an invalid expected-return score.")
        return scores, None

    base_rate = float(train_y.mean())
    if len(set(int(value) for value in train_y)) < 2:
        return [base_rate] * len(evaluation), base_rate
    if bool(config.get("magnitude_weighted", False)):
        import numpy as np

        nonzero = np.abs(train_returns[np.abs(train_returns) > 0])
        scale = float(np.median(nonzero)) if len(nonzero) else 1.0
        weights = np.clip(np.abs(train_returns) / scale, 0.5, 3.0)
        model.fit(train_x, train_y, logisticregression__sample_weight=weights)
    else:
        model.fit(train_x, train_y)
    probabilities = [float(value) for value in model.predict_proba(evaluation_x)[:, 1]]
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in probabilities):
        raise ValueError("Estimator produced an invalid probability.")
    return probabilities, base_rate


def _thresholds(config: Mapping[str, object]) -> Sequence[float]:
    return (
        RETURN_THRESHOLDS
        if config.get("score_kind") == "expected_net_return"
        else THRESHOLDS
    )


def _inner_blocks(samples: Sequence[Mapping[str, object]]):
    ordered = sorted(
        samples, key=lambda sample: (int(sample["fill_ts"]), str(sample.get("id", "")))
    )
    split = max(40, int(len(ordered) * 0.70))
    validation = ordered[split:]
    if len(validation) < 40:
        return [], []
    validation_start = int(validation[0]["fill_ts"])
    cutoff = validation_start - v2_model.BAR_MS
    train = [
        sample for sample in ordered[:split]
        if int(sample["label_available_ts"]) <= cutoff
    ]
    return train, validation


def _select(
    family: str, train_samples: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    inner_train, validation = _inner_blocks(train_samples)
    fallback = {
        "family": family,
        "config": dict(FAMILY_CONFIGS[family][0]),
        "threshold": (
            RETURN_THRESHOLDS[-1]
            if FAMILY_CONFIGS[family][0].get("score_kind") == "expected_net_return"
            else 0.70
        ),
        "cash_selected": True,
        "reason": "insufficient_inner_history",
        "evaluated": 0,
        "passing": 0,
    }
    if len(inner_train) < 100 or len({float(row["net_return"]) > 0 for row in inner_train}) < 2:
        return fallback

    midpoint = int(validation[len(validation) // 2]["fill_ts"])
    best_any = None
    best_passing = None
    evaluated = 0
    passing = 0
    for config in FAMILY_CONFIGS[family]:
        scores, _ = _scores(config, inner_train, validation)
        for threshold in _thresholds(config):
            evaluated += 1
            accepted = v2_model._accepted(validation, scores, threshold)
            metrics = v2_model._cost_metrics(accepted)
            m30, m40 = metrics["30bp"], metrics["40bp"]
            first = [pair for pair in accepted if int(pair[0]["fill_ts"]) < midpoint]
            second = [pair for pair in accepted if int(pair[0]["fill_ts"]) >= midpoint]
            halves_positive = all(
                v2_model._cost_metrics(part)["40bp"]["sum_return"] > 0
                for part in (first, second)
            )
            pf40 = float(m40["profit_factor"] or 0.0)
            qualifies = bool(
                m40["trades"] >= 5
                and m30["sum_return"] > 0
                and m40["sum_return"] > 0
                and pf40 >= 1.10
                and halves_positive
            )
            score = float(m40["sum_return"]) - 0.25 * float(m40["max_drawdown"])
            candidate = {
                "family": family,
                "config": dict(config),
                "threshold": float(threshold),
                "cash_selected": not qualifies,
                "reason": "inner_cost_stability_pass" if qualifies else "inner_cost_stability_failed",
                "score": score,
                "inner_train_events": len(inner_train),
                "inner_validation_events": len(validation),
                "inner_metrics": metrics,
            }
            if best_any is None or score > best_any[0]:
                best_any = (score, candidate)
            if qualifies:
                passing += 1
                if best_passing is None or score > best_passing[0]:
                    best_passing = (score, candidate)

    chosen = dict((best_passing or best_any)[1])
    chosen["cash_selected"] = best_passing is None
    chosen["reason"] = (
        "inner_cost_stability_pass" if best_passing is not None
        else "cash_outperformed_admissible_candidates"
    )
    chosen["evaluated"] = evaluated
    chosen["passing"] = passing
    return chosen


def _historical_gate(folds: Sequence[Mapping[str, object]], metrics: Mapping[str, object]):
    m30 = metrics["30bp"]
    m40 = metrics["40bp"]
    positive_folds = sum(
        float(fold["cost_metrics"]["40bp"]["compounded_return"]) > 0
        for fold in folds
    )
    checks = {
        "trades_at_least_30": int(m40["trades"]) >= 30,
        "positive_folds_4_of_5": positive_folds >= 4,
        "30bp_compounded_positive": float(m30["compounded_return"]) > 0,
        "40bp_compounded_positive": float(m40["compounded_return"]) > 0,
        "40bp_profit_factor_at_least_1_2": float(m40["profit_factor"] or 0.0) >= 1.20,
    }
    return {
        "qualified_for_forward_shadow_trial": all(checks.values()),
        "checks": checks,
        "positive_folds": positive_folds,
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }


def benchmark(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    historical_samples = v2_model.build_historical_samples(
        rows, MAIN_SPEC, v2_model.COST_SCENARIOS["30bp"]
    )
    outer_folds = purged_expanding_walk_forward(
        historical_samples, folds=OUTER_FOLDS,
        min_train_events=MIN_OUTER_TRAIN_EVENTS, embargo_ms=v2_model.BAR_MS,
    )
    family_reports: dict[str, object] = {}
    for family in FAMILY_CONFIGS:
        fold_reports = []
        aggregate_returns = {label: [] for label in v2_model.COST_SCENARIOS}
        weighted_brier = 0.0
        weighted_baseline = 0.0
        probability_count = 0
        for fold in outer_folds:
            train = list(fold["train"])
            validation = list(fold["validation"])
            selection = _select(family, train)
            scores, base_rate = _scores(selection["config"], train, validation)
            outcomes = [int(float(sample["net_return"]) > 0) for sample in validation]
            brier = (
                fmean((score - outcome) ** 2 for score, outcome in zip(scores, outcomes))
                if base_rate is not None else None
            )
            baseline_brier = (
                fmean((base_rate - outcome) ** 2 for outcome in outcomes)
                if base_rate is not None else None
            )
            threshold = None if selection["cash_selected"] else float(selection["threshold"])
            accepted = v2_model._accepted(validation, scores, threshold)
            cost_metrics = v2_model._cost_metrics(accepted)
            for label, cost in v2_model.COST_SCENARIOS.items():
                aggregate_returns[label].extend(
                    v2_model._sample_return(sample, cost) for sample, _ in accepted
                )
            if brier is not None and baseline_brier is not None:
                weighted_brier += brier * len(validation)
                weighted_baseline += baseline_brier * len(validation)
                probability_count += len(validation)
            fold_reports.append({
                "fold": int(fold["fold"]),
                "train_events": len(train),
                "validation_events": len(validation),
                "purged_count": int(fold["purged_count"]),
                "selection": selection,
                "accepted_ids": [str(sample["id"]) for sample, _ in accepted],
                "cost_metrics": cost_metrics,
                "brier": brier,
                "baseline_brier": baseline_brier,
            })
        aggregate = {
            label: v2_model._trade_metrics(values)
            for label, values in aggregate_returns.items()
        }
        family_reports[family] = {
            "configs": [dict(config) for config in FAMILY_CONFIGS[family]],
            "full_data_selection": _select(family, historical_samples),
            "folds": fold_reports,
            "aggregate_cost_metrics": aggregate,
            "brier": weighted_brier / probability_count if probability_count else None,
            "baseline_brier": (
                weighted_baseline / probability_count if probability_count else None
            ),
            "historical_gate": _historical_gate(fold_reports, aggregate),
        }

    qualified = [
        (family, report) for family, report in family_reports.items()
        if report["historical_gate"]["qualified_for_forward_shadow_trial"]
        and not report["full_data_selection"]["cash_selected"]
    ]
    qualified.sort(
        key=lambda item: float(item[1]["aggregate_cost_metrics"]["40bp"]["compounded_return"]),
        reverse=True,
    )
    champion = qualified[0][0] if qualified else None
    return {
        "schema": 1,
        "kind": "bollinger_15m_v2_model_family_benchmark",
        "created_utc": int(time.time()),
        "research_only": True,
        "paper_or_live_promotion": False,
        "methodology": {
            "outer_folds": OUTER_FOLDS,
            "minimum_outer_train_events": MIN_OUTER_TRAIN_EVENTS,
            "inner_split": "first 70% fit / last 30% selection; one-bar embargo and label-availability purge",
            "thresholds": list(THRESHOLDS),
            "expected_return_thresholds": list(RETURN_THRESHOLDS),
            "selection_cost_baseline": "40bp",
            "cash_is_explicit": True,
            "outer_results_not_used_to_tune_grid": True,
            "warning": "Historical qualification can only nominate a shadow trial; true-forward evidence is still required.",
        },
        "dataset": {
            "rows": len(rows),
            "ohlcv_digest": v2_model.dataset_digest(rows),
            "historical_events": len(historical_samples),
        },
        "families": family_reports,
        "champion": {
            "family": champion,
            "action": (
                "shadow_trial_candidate_only" if champion is not None
                else "keep_current_cash_model"
            ),
            "reason": (
                "historical_gate_passed_but_forward_gate_still_required" if champion is not None
                else "no_family_passed_the_precommitted_historical_stability_gate"
            ),
        },
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    rows = read_dataset(args.data)
    report = benchmark(rows)
    report["dataset"]["path"] = str(args.data.resolve())
    report["dataset"]["file_sha256"] = _sha256(args.data)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_suffix(args.report.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(args.report)
    concise = {
        "report": str(args.report.resolve()),
        "champion": report["champion"],
        "families": {
            family: {
                "full_data_cash_selected": payload["full_data_selection"]["cash_selected"],
                "30bp": payload["aggregate_cost_metrics"]["30bp"],
                "40bp": payload["aggregate_cost_metrics"]["40bp"],
                "positive_folds": payload["historical_gate"]["positive_folds"],
                "qualified": payload["historical_gate"]["qualified_for_forward_shadow_trial"],
            }
            for family, payload in report["families"].items()
        },
    }
    print(json.dumps(concise, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
