"""Cost-stressed daily trend challenger selected on Binance and checked on Bitstamp."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
from typing import Mapping, Sequence

import binance_archive


ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT = ROOT / "reports/low-frequency-challenger.json"
DEFAULT_MODEL = ROOT / "state/low-frequency-challenger.json"
DAY_MS = 86_400_000
ONE_WAY_STRESSED_COST = 0.002
BITSTAMP_HISTORY = ROOT / "data/bitstamp-btc-usd-15m-200000.csv"
_HISTORICAL_DAILY: list[dict[str, float | int]] | None = None


def resample_daily(rows: Sequence[Mapping[str, object]]) -> list[dict[str, float | int]]:
    """Keep only complete, exactly aligned UTC days."""
    grouped: dict[int, list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(int(row["ts"]) // DAY_MS, []).append(row)
    result = []
    expected = {index * 900_000 for index in range(96)}
    for day, group in sorted(grouped.items()):
        group = sorted(group, key=lambda item: int(item["ts"]))
        if len(group) != 96 or {int(item["ts"]) % DAY_MS for item in group} != expected:
            continue
        result.append({
            "ts": day * DAY_MS,
            "open": float(group[0]["open"]),
            "high": max(float(item["high"]) for item in group),
            "low": min(float(item["low"]) for item in group),
            "close": float(group[-1]["close"]),
            "volume": sum(float(item["volume"]) for item in group),
        })
    return result


def _ema(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    result[period - 1] = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        previous = result[index - 1]
        assert previous is not None
        result[index] = alpha * values[index] + (1 - alpha) * previous
    return result


def configurations(closes: Sequence[float]) -> list[tuple[dict[str, object], list[int]]]:
    count = len(closes)
    periods = (5, 10, 20, 30, 50, 75, 100, 150, 200, 300)
    emas = {period: _ema(closes, period) for period in periods}
    result: list[tuple[dict[str, object], list[int]]] = []
    for fast in (5, 10, 20, 30, 50, 75):
        for slow in (50, 75, 100, 150, 200, 300):
            if fast >= slow:
                continue
            for buffer in (0.0, 0.005, 0.01, 0.02, 0.03):
                positions, state = [0] * count, 0
                for index in range(count):
                    if emas[fast][index] is None or emas[slow][index] is None:
                        continue
                    ratio = float(emas[fast][index]) / float(emas[slow][index]) - 1
                    if ratio > buffer:
                        state = 1
                    elif ratio < -buffer:
                        state = 0
                    positions[index] = state
                result.append(({"family": "ema", "fast": fast, "slow": slow,
                                "buffer": buffer}, positions))
    for lookback in (20, 30, 60, 90, 120, 180, 270, 365):
        for threshold in (0.0, 0.03, 0.05, 0.10, 0.20, 0.30):
            positions = [0] * count
            for index in range(lookback, count):
                positions[index] = int(
                    closes[index] / closes[index - lookback] - 1 > threshold)
            result.append(({"family": "momentum", "lookback": lookback,
                            "threshold": threshold}, positions))
    for entry in (20, 30, 55, 90, 120, 180, 270, 365):
        for exit_ in (5, 10, 20, 30, 55, 90):
            if exit_ >= entry:
                continue
            positions, state = [0] * count, 0
            for index in range(max(entry, exit_), count):
                if not state and closes[index] > max(closes[index-entry:index]):
                    state = 1
                elif state and closes[index] < min(closes[index-exit_:index]):
                    state = 0
                positions[index] = state
            result.append(({"family": "donchian", "entry": entry,
                            "exit": exit_}, positions))
    for period in (20, 50, 100, 150, 200, 300):
        for buffer in (0.0, 0.01, 0.02, 0.05, 0.10):
            positions, state = [0] * count, 0
            for index in range(period, count):
                average = sum(closes[index-period:index]) / period
                ratio = closes[index] / average - 1
                if ratio > buffer:
                    state = 1
                elif ratio < -buffer:
                    state = 0
                positions[index] = state
            result.append(({"family": "sma", "period": period,
                            "buffer": buffer}, positions))
    return result


def positions_for(rule: Mapping[str, object], closes: Sequence[float]) -> list[int]:
    for candidate, positions in configurations(closes):
        if candidate == dict(rule):
            return positions
    raise ValueError("Low-frequency rule is outside the registered search space.")


def daily_signal(bars: Sequence[Mapping[str, object]], rule: Mapping[str, object]) -> tuple[int, float]:
    if rule != {"family": "momentum", "lookback": 30, "threshold": 0.2}:
        raise ValueError("Forward shadow supports only the frozen selected rule.")
    if len(bars) < 31:
        raise ValueError("Forward shadow needs 31 complete daily bars.")
    momentum = float(bars[-1]["close"]) / float(bars[-31]["close"]) - 1
    return int(momentum > 0.20), momentum


def ensure_shadow_tables(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS low_frequency_shadow_state (
            id INTEGER PRIMARY KEY CHECK(id=1), model_version TEXT NOT NULL,
            activated_ts INTEGER NOT NULL, last_day INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position IN (0,1)),
            entry_day INTEGER, entry_cost REAL)
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS low_frequency_shadow_decisions (
            day_ts INTEGER PRIMARY KEY, model_version TEXT NOT NULL,
            close REAL NOT NULL, momentum REAL NOT NULL,
            desired_position INTEGER NOT NULL CHECK(desired_position IN (0,1)),
            created_ts INTEGER NOT NULL)
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS low_frequency_shadow_executions (
            entry_day INTEGER PRIMARY KEY, model_version TEXT NOT NULL,
            exit_day INTEGER, entry_cost REAL NOT NULL, exit_proceeds REAL,
            net_return REAL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
            created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL)
    """)


def _artifact() -> dict[str, object] | None:
    if not DEFAULT_MODEL.exists():
        return None
    value = json.loads(DEFAULT_MODEL.read_text(encoding="utf-8"))
    return value if value.get("forward_shadow_candidate") else None


def _daily_history(live_rows: Sequence[Mapping[str, object]]) -> list[dict[str, float | int]]:
    global _HISTORICAL_DAILY
    if _HISTORICAL_DAILY is None:
        if not BITSTAMP_HISTORY.exists():
            raise ValueError("Bitstamp history is missing for low-frequency shadow.")
        import agent
        _HISTORICAL_DAILY = resample_daily(agent.read_dataset(BITSTAMP_HISTORY))
    merged = {int(bar["ts"]): bar for bar in _HISTORICAL_DAILY}
    merged.update({int(bar["ts"]): bar for bar in resample_daily(live_rows)})
    return [merged[key] for key in sorted(merged)]


def tick_shadow(db: sqlite3.Connection, quote: Mapping[str, object],
                live_rows: Sequence[Mapping[str, object]] | None, now_ms: int) -> None:
    artifact = _artifact()
    if artifact is None or not live_rows:
        return
    ensure_shadow_tables(db)
    bars = _daily_history(live_rows)
    desired, momentum = daily_signal(bars, artifact["rule"])
    latest_day = int(bars[-1]["ts"])
    state = db.execute(
        "SELECT model_version,last_day,position,entry_day,entry_cost "
        "FROM low_frequency_shadow_state WHERE id=1").fetchone()
    if state is None or str(state[0]) != str(artifact["model_version"]):
        with db:
            db.execute("DELETE FROM low_frequency_shadow_state")
            db.execute(
                "INSERT INTO low_frequency_shadow_state VALUES (1,?,?,?,?,NULL,NULL)",
                (artifact["model_version"], now_ms, latest_day, 0))
        return
    if latest_day <= int(state[1]):
        return
    from v2_engine import DEFAULT_COST
    bid, ask = float(quote["bid"]), float(quote["ask"])
    fee, slip = DEFAULT_COST.fee_each_side, DEFAULT_COST.slippage_each_side
    position, entry_day, entry_cost = int(state[2]), state[3], state[4]
    with db:
        db.execute(
            "INSERT OR IGNORE INTO low_frequency_shadow_decisions VALUES (?,?,?,?,?,?)",
            (latest_day, artifact["model_version"], float(bars[-1]["close"]),
             momentum, desired, now_ms))
        if desired and not position:
            entry_cost = ask * (1 + slip) * (1 + fee)
            entry_day = latest_day
            position = 1
            db.execute(
                "INSERT INTO low_frequency_shadow_executions VALUES "
                "(?,?,NULL,?,NULL,NULL,'open',?,?)",
                (entry_day, artifact["model_version"], entry_cost, now_ms, now_ms))
        elif not desired and position:
            proceeds = bid * (1 - slip) * (1 - fee)
            net_return = proceeds / float(entry_cost) - 1
            db.execute(
                "UPDATE low_frequency_shadow_executions SET exit_day=?,exit_proceeds=?,"
                "net_return=?,status='closed',updated_ts=? WHERE entry_day=? AND status='open'",
                (latest_day, proceeds, net_return, now_ms, entry_day))
            position, entry_day, entry_cost = 0, None, None
        db.execute(
            "UPDATE low_frequency_shadow_state SET last_day=?,position=?,entry_day=?,entry_cost=? WHERE id=1",
            (latest_day, position, entry_day, entry_cost))


def shadow_status(db: sqlite3.Connection) -> dict[str, object]:
    artifact = _artifact()
    if artifact is None:
        return {"status": "unavailable", "capital_enabled": False,
                "real_orders_enabled": False}
    ensure_shadow_tables(db)
    state = db.execute(
        "SELECT activated_ts,last_day,position,entry_day FROM low_frequency_shadow_state WHERE id=1"
    ).fetchone()
    counts = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(status='closed'),0),"
        "COALESCE(SUM(CASE WHEN status='closed' THEN net_return ELSE 0 END),0) "
        "FROM low_frequency_shadow_executions").fetchone()
    return {
        "status": "forward_shadow" if state else "ready_to_activate",
        "model_version": artifact["model_version"], "rule": artifact["rule"],
        "activated_ts": None if state is None else int(state[0]),
        "last_day": None if state is None else int(state[1]),
        "position": None if state is None else ("long" if state[2] else "cash"),
        "entry_day": None if state is None or state[3] is None else int(state[3]),
        "execution_records": int(counts[0]), "closed_executions": int(counts[1]),
        "sum_net_return": float(counts[2]), "capital_enabled": False,
        "real_orders_enabled": False,
    }


def metrics(closes: Sequence[float], positions: Sequence[int], start: int, end: int) -> dict[str, object]:
    values, previous, turnover, gross_profit, gross_loss = [], 0, 0, 0.0, 0.0
    for index in range(max(1, start), end):
        position = int(positions[index - 1])
        change = abs(position - previous)
        value = position * (closes[index] / closes[index - 1] - 1)
        value -= change * ONE_WAY_STRESSED_COST
        values.append(value)
        turnover += change
        gross_profit += max(0.0, value)
        gross_loss += max(0.0, -value)
        previous = position
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= max(1e-9, 1 + value)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
    mean = statistics.mean(values) if values else 0.0
    deviation = statistics.pstdev(values) if values else 0.0
    return {
        "bars": len(values), "return": equity - 1,
        "annualized_sharpe": mean / deviation * math.sqrt(365)
        if deviation > 1e-12 else 0.0,
        "max_drawdown": max_drawdown, "turnover_units": turnover,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
    }


def _five_folds(closes: Sequence[float], positions: Sequence[int]) -> list[dict[str, object]]:
    edges = [round(index * len(closes) / 5) for index in range(6)]
    return [metrics(closes, positions, edges[index], edges[index + 1])
            for index in range(5)]


def _external_gate(aggregate: Mapping[str, object], folds: Sequence[Mapping[str, object]]) -> bool:
    return bool(
        sum(float(fold["return"]) > 0 for fold in folds) >= 4
        and float(aggregate["return"]) > 0
        and float(aggregate["annualized_sharpe"]) >= 0.5
        and float(aggregate["profit_factor"] or 0) >= 1.25
        and float(aggregate["max_drawdown"]) <= 0.35
        and int(aggregate["turnover_units"]) >= 10
    )


def research(binance_path: Path, bitstamp_path: Path, report_path: Path = DEFAULT_REPORT,
             model_path: Path = DEFAULT_MODEL) -> dict[str, object]:
    import agent
    binance_rows = binance_archive.read_dataset(binance_path, "um", "BTCUSDT")
    bitstamp_rows = agent.read_dataset(bitstamp_path)
    binance_bars, bitstamp_bars = resample_daily(binance_rows), resample_daily(bitstamp_rows)
    if len(binance_bars) < 1_500 or len(bitstamp_bars) < 1_500:
        raise ValueError("Low-frequency research needs at least 1,500 complete daily bars.")
    binance_closes = [float(bar["close"]) for bar in binance_bars]
    evaluated = []
    for rule, positions in configurations(binance_closes):
        folds = _five_folds(binance_closes, positions)
        aggregate = metrics(binance_closes, positions, 0, len(binance_closes))
        positive = sum(float(fold["return"]) > 0 for fold in folds)
        passed = bool(
            positive == 5 and float(aggregate["return"]) > 0
            and float(aggregate["annualized_sharpe"]) >= 0.5
            and float(aggregate["profit_factor"] or 0) >= 1.25
            and float(aggregate["max_drawdown"]) <= 0.35
            and int(aggregate["turnover_units"]) >= 10)
        evaluated.append({"rule": rule, "positions": positions, "folds": folds,
                          "aggregate": aggregate, "passed": passed,
                          "score": (positive, min(float(x["return"]) for x in folds),
                                    float(aggregate["return"]))})
    eligible = [item for item in evaluated if item["passed"]]
    selected = max(eligible, key=lambda item: item["score"]) if eligible else None
    external = None
    external_pass = False
    if selected:
        bitstamp_closes = [float(bar["close"]) for bar in bitstamp_bars]
        external_positions = positions_for(selected["rule"], bitstamp_closes)
        external_folds = _five_folds(bitstamp_closes, external_positions)
        external_aggregate = metrics(
            bitstamp_closes, external_positions, 0, len(bitstamp_closes))
        external_pass = _external_gate(external_aggregate, external_folds)
        external = {"folds": external_folds, "aggregate": external_aggregate,
                    "gate_pass": external_pass}
    selected_public = None if selected is None else {
        key: value for key, value in selected.items() if key != "positions"}
    basis = {"rule": None if selected is None else selected["rule"],
             "cost": ONE_WAY_STRESSED_COST, "external_pass": external_pass}
    version = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
    report = {
        "kind": "daily_low_frequency_long_cash_challenger",
        "configuration_count": len(evaluated),
        "binance_daily_bars": len(binance_bars),
        "bitstamp_daily_bars": len(bitstamp_bars),
        "one_way_stressed_cost": ONE_WAY_STRESSED_COST,
        "selected": selected_public, "eligible_on_binance": len(eligible),
        "external_bitstamp": external,
        "model_version": version,
        "status": "forward_shadow" if external_pass else "rejected",
        "forward_shadow_candidate": external_pass,
        "deployed": False, "capital_enabled": False, "real_orders_enabled": False,
        "caveat": "All historical periods were inspected; only new forward observations can promote this candidate.",
    }
    artifact = {
        "schema": 1, "kind": report["kind"], "model_version": version,
        "rule": None if selected is None else selected["rule"],
        "status": report["status"], "forward_shadow_candidate": external_pass,
        "capital_enabled": False, "real_orders_enabled": False,
    }
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    Path(model_path).write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return report


__all__ = ["DEFAULT_REPORT", "DEFAULT_MODEL", "ONE_WAY_STRESSED_COST",
           "resample_daily", "configurations", "positions_for", "daily_signal",
           "metrics", "research", "ensure_shadow_tables", "tick_shadow", "shadow_status"]
