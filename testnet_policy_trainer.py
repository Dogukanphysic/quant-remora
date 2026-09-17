"""Pre-registered trainer for the more active Binance Spot Testnet policy.

The module is deliberately offline: it reads historical files, evaluates four
fixed rules, and returns JSON-serializable report/config dictionaries.  It
never talks to an exchange and only writes when ``write_outputs`` is called
with explicit destinations.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

import low_frequency


POLICY_ID = "btc_daily_momentum_30d_t10_testnet_v1"
LOOKBACK_DAYS = 30
RELEASE_THRESHOLD = 0.10
CANDIDATE_THRESHOLDS = (0.0, 0.03, 0.05, 0.10)
ONE_WAY_STRESSED_COST = 0.002
FOLD_COUNT = 5
TESTNET_NOTIONAL_USDT = 10.0
GATES = {
    "net_return_above": 0.0,
    "profit_factor_at_least": 1.20,
    "positive_folds_at_least": 4,
    "fold_count": FOLD_COUNT,
    "max_drawdown_at_most": 0.35,
    "turnover_units_at_least": 50,
    "required_markets": ("binance_um", "bitstamp"),
}

_TRAINER_SPEC = {
    "schema": 1,
    "policy_family": "daily_long_cash_momentum",
    "lookback_days": LOOKBACK_DAYS,
    "release_threshold": RELEASE_THRESHOLD,
    "candidate_thresholds": CANDIDATE_THRESHOLDS,
    "one_way_stressed_cost": ONE_WAY_STRESSED_COST,
    "fold_count": FOLD_COUNT,
    "gates": GATES,
    "selection": "highest_min_market_turnover_then_total_turnover",
    "runtime_market_diagnostic": {
        "market": "binance_spot_recent",
        "threshold": RELEASE_THRESHOLD,
        "selection_gate": False,
    },
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


TRAINER_VERSION = hashlib.sha256(_canonical_bytes(_TRAINER_SPEC)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def momentum_positions(
    closes: Sequence[float], threshold: float, lookback: int = LOOKBACK_DAYS
) -> list[int]:
    """Return a daily long/cash signal from the fixed lookback rule."""
    if threshold not in CANDIDATE_THRESHOLDS:
        raise ValueError("Threshold is outside the pre-registered candidate set.")
    if lookback != LOOKBACK_DAYS:
        raise ValueError("Only the pre-registered 30-day lookback is supported.")
    values = [float(value) for value in closes]
    if any(value <= 0 for value in values):
        raise ValueError("Closing prices must be positive.")
    positions = [0] * len(values)
    for index in range(lookback, len(values)):
        momentum = values[index] / values[index - lookback] - 1.0
        positions[index] = int(momentum > threshold)
    return positions


def _five_folds(
    closes: Sequence[float], positions: Sequence[int]
) -> list[dict[str, object]]:
    edges = [round(index * len(closes) / FOLD_COUNT)
             for index in range(FOLD_COUNT + 1)]
    return [
        low_frequency.metrics(closes, positions, edges[index], edges[index + 1])
        for index in range(FOLD_COUNT)
    ]


def _market_gate(
    aggregate: Mapping[str, object], folds: Sequence[Mapping[str, object]]
) -> tuple[bool, dict[str, bool]]:
    checks = {
        "net_return_positive": float(aggregate["return"]) > 0.0,
        "profit_factor_at_least_1_20": (
            float(aggregate.get("profit_factor") or 0.0) >= 1.20
        ),
        "positive_folds_at_least_4_of_5": (
            sum(float(fold["return"]) > 0.0 for fold in folds) >= 4
        ),
        "max_drawdown_at_most_35_percent": (
            float(aggregate["max_drawdown"]) <= 0.35
        ),
        "turnover_units_at_least_50": (
            int(aggregate["turnover_units"]) >= 50
        ),
    }
    return all(checks.values()), checks


def evaluate_market(
    closes: Sequence[float], threshold: float
) -> dict[str, object]:
    """Evaluate one registered rule on one market using low_frequency metrics."""
    if len(closes) < 155:
        raise ValueError("Training needs at least 155 complete daily bars per market.")
    positions = momentum_positions(closes, threshold)
    aggregate = low_frequency.metrics(closes, positions, 0, len(closes))
    folds = _five_folds(closes, positions)
    passed, checks = _market_gate(aggregate, folds)
    return {
        "aggregate": aggregate,
        "folds": folds,
        "positive_folds": sum(float(fold["return"]) > 0.0 for fold in folds),
        "gate_checks": checks,
        "gate_pass": passed,
    }


def evaluate_candidates(
    markets: Mapping[str, Sequence[float]],
) -> list[dict[str, object]]:
    expected = set(GATES["required_markets"])
    if set(markets) != expected:
        raise ValueError(f"Markets must be exactly {sorted(expected)}.")
    results: list[dict[str, object]] = []
    for threshold in CANDIDATE_THRESHOLDS:
        market_results = {
            market: evaluate_market(markets[market], threshold)
            for market in GATES["required_markets"]
        }
        turnovers = [
            int(result["aggregate"]["turnover_units"])
            for result in market_results.values()
        ]
        results.append({
            "rule": {
                "family": "momentum",
                "lookback_days": LOOKBACK_DAYS,
                "threshold": threshold,
                "position": "long_or_cash",
                "execution_lag_days": 1,
            },
            "markets": market_results,
            "gate_pass_both_markets": all(
                bool(result["gate_pass"]) for result in market_results.values()
            ),
            "activity": {
                "minimum_market_turnover_units": min(turnovers),
                "total_turnover_units": sum(turnovers),
            },
        })
    return results


def select_candidate(
    evaluations: Sequence[Mapping[str, object]],
) -> Mapping[str, object] | None:
    """Select the passing candidate with the most cross-market activity."""
    eligible = [item for item in evaluations if item["gate_pass_both_markets"]]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (
            int(item["activity"]["minimum_market_turnover_units"]),
            int(item["activity"]["total_turnover_units"]),
            -float(item["rule"]["threshold"]),
        ),
    )


def build_outputs(
    evaluations: Sequence[Mapping[str, object]],
    data_versions: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    """Build deterministic machine report and fail-closed config artifact."""
    selected = select_candidate(evaluations)
    release_compatible = bool(
        selected is not None
        and float(selected["rule"]["threshold"]) == RELEASE_THRESHOLD
    )
    selected_rule = None if selected is None else dict(selected["rule"])
    released_rule = selected_rule if release_compatible else None
    artifact_rule = None if released_rule is None else {
        "family": "momentum",
        "lookback_days": LOOKBACK_DAYS,
        "threshold": f"{float(released_rule['threshold']):.2f}",
    }
    version_basis = {
        "trainer_version": TRAINER_VERSION,
        "policy_id": POLICY_ID,
        "selected_rule": selected_rule,
        "data_versions": data_versions,
    }
    model_version = hashlib.sha256(_canonical_bytes(version_basis)).hexdigest()
    status = "testnet_exploration_candidate" if release_compatible else "rejected"
    report = {
        "schema": 1,
        "kind": "testnet_policy_training_report",
        "policy_id": POLICY_ID,
        "status": status,
        "trainer_version": TRAINER_VERSION,
        "model_version": model_version,
        "one_way_stressed_cost": ONE_WAY_STRESSED_COST,
        "fold_count": FOLD_COUNT,
        "gates": GATES,
        "selection_method": _TRAINER_SPEC["selection"],
        "data_versions": dict(data_versions),
        "evaluations": list(evaluations),
        "selected": selected,
        "release_threshold": RELEASE_THRESHOLD,
        "release_compatible": release_compatible,
        "eligible_candidate_count": sum(
            bool(item["gate_pass_both_markets"]) for item in evaluations
        ),
        "testnet_only": True,
        "paper_eligible": False,
        "real_money_eligible": False,
    }
    artifact = {
        "schema": 1,
        "kind": "testnet_execution_policy_config",
        "policy_id": POLICY_ID,
        "status": status,
        "model_version": model_version,
        "trainer_version": TRAINER_VERSION,
        "environment": "binance_spot_testnet",
        "market_data_source": "binance_public_spot",
        "rule": artifact_rule,
        "order": {
            "symbol": "BTCUSDT",
            "quote_usdt": "10",
            "mode": "long_cash",
        },
        "selection_contract": {
            "lookback_days": LOOKBACK_DAYS,
            "candidate_thresholds": [
                f"{threshold:.2f}" for threshold in CANDIDATE_THRESHOLDS
            ],
            "one_way_stressed_cost": f"{ONE_WAY_STRESSED_COST:.3f}",
            "fold_count": FOLD_COUNT,
            "gates": GATES,
            "required_markets": list(GATES["required_markets"]),
            "selection_method": _TRAINER_SPEC["selection"],
        },
        "selected_metrics": (
            None if not release_compatible else dict(selected["markets"])
        ),
        "testnet_only": True,
        "testnet_execution_eligible": release_compatible,
        "paper_eligible": False,
        "real_money_eligible": False,
        "real_orders_enabled": False,
        "live_trading_enabled": False,
        "data_versions": dict(data_versions),
    }
    return report, artifact


def _load_daily_closes(
    binance_path: Path, bitstamp_path: Path
) -> tuple[dict[str, list[float]], dict[str, dict[str, object]]]:
    import agent
    import binance_archive

    paths = {
        "binance_um": Path(binance_path),
        "bitstamp": Path(bitstamp_path),
    }
    binance_rows = binance_archive.read_dataset(paths["binance_um"], "um", "BTCUSDT")
    bitstamp_rows = agent.read_dataset(paths["bitstamp"])
    daily = {
        "binance_um": low_frequency.resample_daily(binance_rows),
        "bitstamp": low_frequency.resample_daily(bitstamp_rows),
    }
    markets = {
        market: [float(bar["close"]) for bar in bars]
        for market, bars in daily.items()
    }
    versions = {
        market: {
            "sha256": file_sha256(paths[market]),
            "source_file": paths[market].name,
            "complete_daily_bars": len(daily[market]),
            "first_daily_close_ms": (
                int(daily[market][0]["ts"]) if daily[market] else None
            ),
            "last_daily_close_ms": (
                int(daily[market][-1]["ts"]) if daily[market] else None
            ),
        }
        for market in GATES["required_markets"]
    }
    return markets, versions


def train(
    binance_path: Path, bitstamp_path: Path, spot_path: Path | None = None
) -> tuple[dict[str, object], dict[str, object]]:
    """Train in memory; this function performs no filesystem writes."""
    if low_frequency.ONE_WAY_STRESSED_COST != ONE_WAY_STRESSED_COST:
        raise RuntimeError("low_frequency cost assumption changed; re-register trainer.")
    markets, versions = _load_daily_closes(binance_path, bitstamp_path)
    spot_diagnostic = None
    if spot_path is not None:
        import binance_archive

        spot_path = Path(spot_path)
        spot_rows = binance_archive.read_dataset(spot_path, "spot", "BTCUSDT")
        spot_daily = low_frequency.resample_daily(spot_rows)
        if not spot_daily:
            raise ValueError("Binance Spot diagnostic dataset has no complete daily bars.")
        spot_closes = [float(bar["close"]) for bar in spot_daily]
        spot_diagnostic = evaluate_market(spot_closes, RELEASE_THRESHOLD)
        versions["binance_spot_recent"] = {
            "sha256": file_sha256(spot_path),
            "source_file": spot_path.name,
            "complete_daily_bars": len(spot_daily),
            "first_daily_close_ms": int(spot_daily[0]["ts"]),
            "last_daily_close_ms": int(spot_daily[-1]["ts"]),
        }
    report, artifact = build_outputs(evaluate_candidates(markets), versions)
    if spot_diagnostic is not None:
        diagnostic = {
            "role": "runtime_market_diagnostic_not_selection_gate",
            "rule_threshold": f"{RELEASE_THRESHOLD:.2f}",
            "result": spot_diagnostic,
        }
        report["binance_spot_recent_diagnostic"] = diagnostic
        artifact["binance_spot_recent_diagnostic"] = diagnostic
    return report, artifact


def write_outputs(
    report: Mapping[str, object],
    artifact: Mapping[str, object],
    *,
    report_path: Path,
    artifact_path: Path,
) -> None:
    """Write outputs only to the caller-supplied paths."""
    destinations = (Path(report_path), Path(artifact_path))
    if destinations[0].resolve() == destinations[1].resolve():
        raise ValueError("Report and artifact paths must be different.")
    for destination, value in zip(destinations, (report, artifact)):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(destination)


__all__ = [
    "POLICY_ID", "RELEASE_THRESHOLD", "CANDIDATE_THRESHOLDS",
    "ONE_WAY_STRESSED_COST",
    "TRAINER_VERSION", "momentum_positions", "evaluate_market",
    "evaluate_candidates", "select_candidate", "build_outputs", "train",
    "write_outputs", "file_sha256",
]
