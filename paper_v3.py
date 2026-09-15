"""Active Bollinger 15M v3 paper test with an isolated local account.

V3 deliberately observes every fresh closed 15-minute bar.  It records the
decision even when no trade is opened, and uses only public quotes plus local
SQLite state.  It has no authenticated exchange or real-order integration.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import Mapping, Sequence

from strategies import BAR_MS, BAR_SECONDS, FRESH_WINDOW_SECONDS, indicators
from v2_engine import DEFAULT_COST, MAX_ENTRY_SPREAD, validate_rows


VERSION = "quant_remora_v5_trainable_paper_v2"
INITIAL_USD = 100.0
RISK_FRACTION = 0.0015
ALLOCATION_CAP = 0.12
STOP_ATR = 1.5
TARGET_ATR = 3.2
MAX_HOLD_BARS = 4
MAX_HOLD_MS = MAX_HOLD_BARS * BAR_MS
DAILY_LOSS_LIMIT = 0.02
DRAWDOWN_LIMIT = 0.08
MIN_TARGET_NET_RETURN = 0.003
MIN_NET_REWARD_RISK = 1.5
LOSS_COOLDOWN_BARS = 4
LOSS_STREAK_COOLDOWN_BARS = 8
BREAKEVEN_ARM_NET_RETURN = 0.001
PROBE_NOTIONAL_USD = 1.0
PROBE_HORIZON_BARS = 8
# The 200,000-candle replay was negative. After reviewing that result and the
# fail-closed collection period, the user explicitly authorized a slightly
# larger bounded paper trial on 2026-09-15. All loss guards remain active.
ENTRY_QUARANTINED = False
ENTRY_AUTHORIZATION = "user_authorized_higher_forward_paper_risk_2026-09-15"
CAPITAL_REQUIRES_ELIGIBLE_MODEL = False


def ensure_tables(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v3_paper_state (
            id INTEGER PRIMARY KEY CHECK (id=1),
            version TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
            initial_usd REAL NOT NULL CHECK (initial_usd > 0),
            cash_usd REAL NOT NULL CHECK (cash_usd >= 0),
            position TEXT,
            realized_pnl_usd REAL NOT NULL,
            completed_trades INTEGER NOT NULL CHECK (completed_trades >= 0),
            equity_usd REAL NOT NULL CHECK (equity_usd >= 0),
            peak_equity_usd REAL NOT NULL CHECK (peak_equity_usd >= 0),
            drawdown_pct REAL NOT NULL CHECK (drawdown_pct >= 0),
            day TEXT NOT NULL,
            day_start_equity REAL NOT NULL CHECK (day_start_equity >= 0),
            daily_halt INTEGER NOT NULL CHECK (daily_halt IN (0,1)),
            drawdown_halt INTEGER NOT NULL CHECK (drawdown_halt IN (0,1)),
            last_decision_ts INTEGER,
            created_ts INTEGER NOT NULL CHECK (created_ts >= 0),
            updated_ts INTEGER NOT NULL CHECK (updated_ts >= created_ts)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v3_paper_decisions (
            decision_ts INTEGER PRIMARY KEY,
            version TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('buy','sell','hold','blocked')),
            strategy TEXT NOT NULL,
            reason TEXT NOT NULL,
            close REAL NOT NULL CHECK (close > 0),
            rsi REAL NOT NULL CHECK (rsi >= 0 AND rsi <= 100),
            percent_b REAL NOT NULL,
            bandwidth REAL NOT NULL CHECK (bandwidth >= 0),
            atr REAL NOT NULL CHECK (atr > 0),
            created_ts INTEGER NOT NULL CHECK (created_ts >= 0)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v3_paper_executions (
            entry_decision_ts INTEGER PRIMARY KEY,
            version TEXT NOT NULL,
            strategy TEXT NOT NULL,
            entry_ts_ms INTEGER NOT NULL CHECK (entry_ts_ms >= 0),
            exit_ts_ms INTEGER,
            entry_reference REAL NOT NULL CHECK (entry_reference > 0),
            entry_price REAL NOT NULL CHECK (entry_price > 0),
            exit_price REAL,
            quantity REAL NOT NULL CHECK (quantity > 0),
            cost_usd REAL NOT NULL CHECK (cost_usd > 0),
            proceeds_usd REAL,
            pnl_usd REAL,
            net_return REAL,
            stop REAL NOT NULL CHECK (stop > 0),
            target REAL NOT NULL CHECK (target > 0),
            exit_reason TEXT,
            status TEXT NOT NULL CHECK (status IN ('open','closed')),
            created_ts INTEGER NOT NULL CHECK (created_ts >= 0),
            updated_ts INTEGER NOT NULL CHECK (updated_ts >= created_ts),
            FOREIGN KEY(entry_decision_ts)
                REFERENCES v3_paper_decisions(decision_ts)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS v3_paper_executions_status "
        "ON v3_paper_executions(status,entry_ts_ms)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v3_shadow_labels (
            decision_ts INTEGER PRIMARY KEY,
            version TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('long','short')),
            entry_ts INTEGER NOT NULL,
            exit_ts INTEGER NOT NULL,
            entry_reference REAL NOT NULL CHECK (entry_reference > 0),
            exit_reference REAL NOT NULL CHECK (exit_reference > 0),
            net_return REAL NOT NULL,
            exit_reason TEXT NOT NULL,
            created_ts INTEGER NOT NULL,
            FOREIGN KEY(decision_ts) REFERENCES v3_paper_decisions(decision_ts)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS v3_probe_executions (
            decision_ts INTEGER PRIMARY KEY,
            version TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('long','short')),
            decision_created_ts INTEGER NOT NULL,
            entry_ts INTEGER NOT NULL,
            exit_ts INTEGER NOT NULL,
            entry_reference REAL NOT NULL CHECK (entry_reference > 0),
            exit_reference REAL NOT NULL CHECK (exit_reference > 0),
            notional_usd REAL NOT NULL CHECK (notional_usd > 0),
            pnl_usd REAL NOT NULL,
            net_return REAL NOT NULL,
            stop REAL NOT NULL CHECK (stop > 0),
            target REAL NOT NULL CHECK (target > 0),
            exit_reason TEXT NOT NULL,
            label_available_ts INTEGER NOT NULL,
            created_ts INTEGER NOT NULL,
            FOREIGN KEY(decision_ts) REFERENCES v3_paper_decisions(decision_ts)
        )
        """
    )
    decision_columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(v3_paper_decisions)")
    }
    if "feature_json" not in decision_columns:
        db.execute("ALTER TABLE v3_paper_decisions ADD COLUMN feature_json TEXT")
    if "context_json" not in decision_columns:
        db.execute("ALTER TABLE v3_paper_decisions ADD COLUMN context_json TEXT")


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite.")
    return float(value)


def _now_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("now_ms must be a non-negative integer.")
    return value


def _day(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, timezone.utc).date().isoformat()


def _decode_position(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    try:
        position = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError("V3 paper position is corrupt.") from exc
    if not isinstance(position, dict):
        raise ValueError("V3 paper position must be an object.")
    for key in ("entry_decision_ts", "entry_ts_ms", "expires_ts_ms"):
        if isinstance(position.get(key), bool) or not isinstance(position.get(key), int):
            raise ValueError(f"V3 position {key} is invalid.")
    for key in (
        "quantity", "entry_reference", "entry", "cost", "stop", "target",
        "fee", "slippage",
    ):
        _finite(position.get(key), f"position.{key}")
    if not isinstance(position.get("strategy"), str) or not position["strategy"]:
        raise ValueError("V3 position strategy is invalid.")
    if "breakeven_armed" in position and not isinstance(position["breakeven_armed"], bool):
        raise ValueError("V3 position breakeven_armed is invalid.")
    return position


def _read_state(db: sqlite3.Connection) -> dict[str, object] | None:
    row = db.execute(
        "SELECT version,enabled,initial_usd,cash_usd,position,realized_pnl_usd,"
        "completed_trades,equity_usd,peak_equity_usd,drawdown_pct,day,"
        "day_start_equity,daily_halt,drawdown_halt,last_decision_ts,created_ts,updated_ts "
        "FROM v3_paper_state WHERE id=1"
    ).fetchone()
    if row is None:
        return None
    return {
        "version": str(row[0]),
        "enabled": bool(row[1]),
        "initial_usd": _finite(row[2], "initial_usd"),
        "cash_usd": _finite(row[3], "cash_usd"),
        "position": _decode_position(row[4]),
        "realized_pnl_usd": _finite(row[5], "realized_pnl_usd"),
        "completed_trades": int(row[6]),
        "equity_usd": _finite(row[7], "equity_usd"),
        "peak_equity_usd": _finite(row[8], "peak_equity_usd"),
        "drawdown_pct": _finite(row[9], "drawdown_pct"),
        "day": str(row[10]),
        "day_start_equity": _finite(row[11], "day_start_equity"),
        "daily_halt": bool(row[12]),
        "drawdown_halt": bool(row[13]),
        "last_decision_ts": None if row[14] is None else int(row[14]),
        "created_ts": int(row[15]),
        "updated_ts": int(row[16]),
    }


def _write_state(db: sqlite3.Connection, state: Mapping[str, object]) -> None:
    position = state.get("position")
    encoded = (
        json.dumps(position, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if position is not None else None
    )
    db.execute(
        "UPDATE v3_paper_state SET version=?,enabled=?,cash_usd=?,position=?,realized_pnl_usd=?,"
        "completed_trades=?,equity_usd=?,peak_equity_usd=?,drawdown_pct=?,day=?,"
        "day_start_equity=?,daily_halt=?,drawdown_halt=?,last_decision_ts=?,updated_ts=? "
        "WHERE id=1",
        (
            state["version"], int(bool(state["enabled"])), state["cash_usd"], encoded,
            state["realized_pnl_usd"], state["completed_trades"],
            state["equity_usd"], state["peak_equity_usd"], state["drawdown_pct"],
            state["day"], state["day_start_equity"], int(bool(state["daily_halt"])),
            int(bool(state["drawdown_halt"])), state["last_decision_ts"],
            state["updated_ts"],
        ),
    )


def enable(db: sqlite3.Connection, now_ms: int) -> dict[str, object]:
    """Enable V3 without resetting any existing paper P&L."""
    ensure_tables(db)
    now = _now_ms(now_ms)
    day = _day(now)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO v3_paper_state "
            "(id,version,enabled,initial_usd,cash_usd,position,realized_pnl_usd,"
            "completed_trades,equity_usd,peak_equity_usd,drawdown_pct,day,"
            "day_start_equity,daily_halt,drawdown_halt,last_decision_ts,created_ts,updated_ts) "
            "VALUES (1,?,1,?,?,NULL,0,0,?,?,0,?,?,0,0,NULL,?,?)",
            (VERSION, INITIAL_USD, INITIAL_USD, INITIAL_USD, INITIAL_USD,
             day, INITIAL_USD, now, now),
        )
        db.execute(
            "UPDATE v3_paper_state SET version=?,enabled=1,updated_ts=? WHERE id=1",
            (VERSION, now),
        )
    result = status(db)
    assert result is not None
    return result


def disable(db: sqlite3.Connection, now_ms: int) -> dict[str, object] | None:
    """Disable new V3 entries; an open position remains protectively managed."""
    ensure_tables(db)
    now = _now_ms(now_ms)
    with db:
        db.execute(
            "UPDATE v3_paper_state SET version=?,enabled=0,updated_ts=? WHERE id=1",
            (VERSION, now),
        )
    return status(db)


def decide(rows: Sequence[Mapping[str, object]], has_position: bool) -> dict[str, object]:
    """Return one causal Quant Remora subset decision for the latest closed candle."""
    import remora_signal
    validate_rows(rows)
    context = remora_signal.evaluate(rows)
    f = indicators(rows)
    i = len(rows) - 1
    required = (
        f["rsi"][i], f["atr"][i], f["bb_middle"][i], f["bb_middle"][i - 1],
        f["bb_upper"][i], f["bb_lower"][i], f["percent_b"][i],
        f["percent_b"][i - 1], f["bandwidth"][i], f["bandwidth"][i - 1],
        f["sma50"][i],
    )
    if any(value is None for value in required):
        raise ValueError("V3 indicators are unavailable.")
    close = float(rows[i]["close"])
    rsi, atr, middle, previous_middle, upper, lower, percent_b, previous_percent_b, bandwidth, previous_bandwidth, sma50 = (
        float(value) for value in required
    )
    previous_close = float(rows[i - 1]["close"])
    previous_4_close = float(rows[i - 4]["close"])
    mean_volume = sum(float(row["volume"]) for row in rows[i - 19:i + 1]) / 20
    open_price = float(rows[i]["open"])
    feature_values = {
        "rsi14": rsi / 100,
        "atr_fraction": atr / close,
        "bollinger_percent_b": percent_b,
        "bollinger_bandwidth": bandwidth,
        "return_15m": close / previous_close - 1,
        "return_1h": close / previous_4_close - 1,
        "relative_volume": float(rows[i]["volume"]) / mean_volume if mean_volume else 0.0,
        "candle_body_fraction": (close - open_price) / open_price,
        "middle_slope": middle / previous_middle - 1,
        "distance_sma50": close / sma50 - 1,
    }
    feature_values.update({
        str(key): _finite(value, f"remora.{key}")
        for key, value in context["features"].items()
    })
    common = {
        "decision_ts": int(rows[i]["ts"]), "close": close, "rsi": rsi,
        "atr": atr, "middle": middle, "upper": upper, "lower": lower,
        "percent_b": percent_b, "bandwidth": bandwidth, "sma50": sma50,
        "features": feature_values,
        "context": context,
    }
    if has_position:
        if context["volatility"] == "extreme":
            return {**common, "action": "sell", "strategy": "remora_risk_exit",
                    "reason": "remora_extreme_volatility"}
        if context["trend"] == "bearish":
            return {**common, "action": "sell", "strategy": "remora_trend_exit",
                    "reason": "remora_1h_trend_bearish"}
        return {**common, "action": "hold", "strategy": "manage_open",
                "reason": "position_managed_by_stop_target_timeout"}
    if context["side"] == "short":
        return {**common, "action": "blocked", "strategy": "remora_short",
                "reason": "short_not_supported_by_spot_paper_account"}
    if context["eligible_long"]:
        return {**common, "action": "buy", "strategy": "remora_long",
                "reason": "remora_1h_trend_15m_trigger_confirmed"}
    if context["side"] == "long":
        failed = ",".join(str(value) for value in context["failed_gates"])
        return {**common, "action": "blocked", "strategy": "remora_long",
                "reason": "remora_gate_failed:" + failed}
    return {**common, "action": "hold", "strategy": "no_entry",
            "reason": "remora_no_15m_trigger"}


def _size(cash: float, ask: float, atr: float) -> dict[str, float] | None:
    values = (cash, ask, atr)
    if not all(math.isfinite(value) and value > 0 for value in values):
        return None
    fee = DEFAULT_COST.fee_each_side
    slip = DEFAULT_COST.slippage_each_side
    entry = ask * (1 + slip)
    stop = ask - STOP_ATR * atr
    target = ask + TARGET_ATR * atr
    if stop <= 0:
        return None
    unit_cost = entry * (1 + fee)
    stop_cash = stop * (1 - slip) * (1 - fee)
    target_cash = target * (1 - slip) * (1 - fee)
    planned_loss = unit_cost - stop_cash
    planned_target = target_cash - unit_cost
    target_net_return = planned_target / unit_cost
    reward_risk = planned_target / planned_loss if planned_loss > 0 else 0.0
    if (
        planned_loss <= 0
        or planned_target <= 0
        or target_net_return < MIN_TARGET_NET_RETURN
        or reward_risk < MIN_NET_REWARD_RISK
    ):
        return None
    quantity = min(
        cash * RISK_FRACTION / planned_loss,
        cash * ALLOCATION_CAP / unit_cost,
    )
    if not math.isfinite(quantity) or quantity <= 0:
        return None
    return {
        "quantity": quantity, "entry_reference": ask, "entry": entry,
        "stop": stop, "target": target, "cost": quantity * unit_cost,
        "fee": fee, "slippage": slip,
        "planned_loss_usd": quantity * planned_loss,
        "planned_target_usd": quantity * planned_target,
        "planned_target_net_return": target_net_return,
        "planned_net_reward_risk": reward_risk,
    }


def _loss_cooldown(db: sqlite3.Connection, decision_ts: int) -> tuple[bool, str]:
    """Block rapid re-entry after a realized loss, with a longer stop-loss streak guard."""
    rows = db.execute(
        "SELECT exit_ts_ms,pnl_usd FROM v3_paper_executions "
        "WHERE status='closed' ORDER BY exit_ts_ms DESC LIMIT 20"
    ).fetchall()
    loss_streak = 0
    last_exit_ts = None
    for exit_ts_ms, pnl_usd in rows:
        if float(pnl_usd) < 0:
            loss_streak += 1
            if last_exit_ts is None:
                last_exit_ts = int(exit_ts_ms)
        else:
            break
    if not loss_streak or last_exit_ts is None:
        return False, ""
    bars = LOSS_STREAK_COOLDOWN_BARS if loss_streak >= 2 else LOSS_COOLDOWN_BARS
    if decision_ts < last_exit_ts + bars * BAR_MS:
        return True, f"v3_loss_cooldown_{bars}_bars"
    return False, ""


def _liquidation(position: Mapping[str, object], bid: float) -> tuple[float, float]:
    exit_price = bid * (1 - float(position["slippage"]))
    proceeds = float(position["quantity"]) * exit_price * (1 - float(position["fee"]))
    return exit_price, proceeds


def _encoded_features(decision: Mapping[str, object]) -> str:
    features = decision.get("features")
    if not isinstance(features, Mapping) or not features:
        raise ValueError("V3 decision features are unavailable.")
    clean = {str(key): _finite(value, f"feature.{key}") for key, value in features.items()}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _encoded_context(decision: Mapping[str, object]) -> str:
    context = decision.get("context", {})
    if not isinstance(context, Mapping):
        raise ValueError("V3 decision context is invalid.")
    return json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _learning_vector(decision: Mapping[str, object]) -> list[float]:
    import learning
    features = decision.get("features")
    if not isinstance(features, Mapping):
        raise ValueError("V3 decision features are unavailable.")
    return [_finite(features.get(name), f"feature.{name}")
            for name in learning.REMORA_FEATURES]


def _model_gate(
    db: sqlite3.Connection, decision: Mapping[str, object], spread: float,
) -> tuple[bool, str, dict[str, object] | None]:
    """Use a promoted forward model as a gate; collect bounded data until it exists."""
    import learning
    learning.ensure_tables(db)
    state = learning.model_state(db, learning.REMORA_MODEL)
    if not bool(state.get("eligible")) or not isinstance(state.get("model"), dict):
        return (not CAPITAL_REQUIRES_ELIGIBLE_MODEL,
                "remora_forward_model_collecting", None)
    assessment = learning.assess(state["model"], _learning_vector(decision), spread)
    return bool(assessment["accept"]), str(assessment["reason"]), assessment


def _record_learning_outcome(
    db: sqlite3.Connection,
    position: Mapping[str, object],
    now_ms: int,
    net_return: float,
    pnl_usd: float,
    reason: str,
) -> bool:
    """Join frozen entry features to the realized result and refresh the learner."""
    import learning
    row = db.execute(
        "SELECT feature_json FROM v3_paper_decisions WHERE decision_ts=?",
        (position["entry_decision_ts"],),
    ).fetchone()
    if row is None or row[0] is None:
        return False
    features = json.loads(str(row[0]))
    decision = {"features": features}
    learning.ensure_tables(db)
    learning.add_sample(
        db,
        learning.REMORA_MODEL,
        learning.REMORA_SOURCE,
        float(position["entry_ts_ms"]) / 1000,
        now_ms / 1000,
        _learning_vector(decision),
        net_return,
        metadata={
            "entry_decision_ts": int(position["entry_decision_ts"]),
            "v3_version": str(position["version"]),
            "strategy": str(position["strategy"]),
            "pnl_usd": pnl_usd,
            "exit_reason": reason,
            "causal_features": True,
        },
    )
    learning.refresh(db)
    return True


def _shadow_net_return(side: str, entry_reference: float, exit_reference: float) -> float:
    fee = DEFAULT_COST.fee_each_side
    slip = DEFAULT_COST.slippage_each_side
    if side == "long":
        cash_out = entry_reference * (1 + slip) * (1 + fee)
        cash_in = exit_reference * (1 - slip) * (1 - fee)
        return cash_in / cash_out - 1
    if side == "short":
        entry_proceeds = entry_reference * (1 - slip) * (1 - fee)
        cover_cost = exit_reference * (1 + slip) * (1 + fee)
        reference_notional = entry_reference * (1 + slip) * (1 + fee)
        return (entry_proceeds - cover_cost) / reference_notional
    raise ValueError("Unknown Remora shadow side.")


def _resolve_shadow_labels(
    db: sqlite3.Connection,
    rows: Sequence[Mapping[str, object]],
    now_ms: int,
) -> int:
    """Label every recorded Remora trigger after H8 without using paper capital."""
    import learning
    by_ts = {int(row["ts"]): index for index, row in enumerate(rows)}
    pending = db.execute(
        "SELECT d.decision_ts,d.atr,d.feature_json,d.context_json,d.created_ts "
        "FROM v3_paper_decisions d LEFT JOIN v3_shadow_labels s "
        "ON s.decision_ts=d.decision_ts "
        "WHERE d.version=? AND d.context_json IS NOT NULL AND s.decision_ts IS NULL "
        "ORDER BY d.decision_ts",
        (VERSION,),
    ).fetchall()
    resolved = 0
    for decision_ts, atr_value, feature_json, context_json, decision_created_ts in pending:
        context = json.loads(str(context_json))
        side = (
            context.get("probe_side", context.get("side"))
            if isinstance(context, dict) else None
        )
        if side not in {"long", "short"}:
            continue
        index = by_ts.get(int(decision_ts))
        if index is None or index + 8 >= len(rows):
            continue
        features = json.loads(str(feature_json))
        vector = _learning_vector({"features": features})
        entry_index = index + 1
        entry_reference = float(rows[entry_index]["open"])
        atr = _finite(atr_value, "shadow.atr")
        if side == "long":
            stop = entry_reference - STOP_ATR * atr
            target = entry_reference + TARGET_ATR * atr
        else:
            stop = entry_reference + STOP_ATR * atr
            target = entry_reference - TARGET_ATR * atr
        if stop <= 0 or target <= 0:
            continue
        exit_index = index + PROBE_HORIZON_BARS
        exit_reference = float(rows[exit_index]["close"])
        reason = "remora_h8_timeout"
        for cursor in range(entry_index, index + PROBE_HORIZON_BARS + 1):
            bar = rows[cursor]
            bar_open = float(bar["open"])
            if side == "long":
                if float(bar["low"]) <= stop:
                    exit_reference = min(stop, bar_open)
                    reason, exit_index = "stop", cursor
                    break
                if float(bar["high"]) >= target:
                    exit_reference = max(target, bar_open)
                    reason, exit_index = "target", cursor
                    break
            else:
                if float(bar["high"]) >= stop:
                    exit_reference = max(stop, bar_open)
                    reason, exit_index = "stop", cursor
                    break
                if float(bar["low"]) <= target:
                    exit_reference = min(target, bar_open)
                    reason, exit_index = "target", cursor
                    break
        net_return = _shadow_net_return(side, entry_reference, exit_reference)
        label_available_ts = int(rows[exit_index]["ts"]) + BAR_MS
        pre_registered = int(decision_created_ts) < label_available_ts
        db.execute(
            "INSERT INTO v3_shadow_labels VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                int(decision_ts), VERSION, side, int(rows[entry_index]["ts"]),
                int(rows[exit_index]["ts"]), entry_reference, exit_reference,
                net_return, reason, now_ms,
            ),
        )
        if pre_registered:
            db.execute(
                "INSERT OR IGNORE INTO v3_probe_executions VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    int(decision_ts), VERSION, side, int(decision_created_ts),
                    int(rows[entry_index]["ts"]), int(rows[exit_index]["ts"]),
                    entry_reference, exit_reference, PROBE_NOTIONAL_USD,
                    PROBE_NOTIONAL_USD * net_return, net_return, stop, target,
                    reason, label_available_ts, now_ms,
                ),
            )
        learning.ensure_tables(db)
        learning.add_sample(
            db, learning.REMORA_MODEL,
            (learning.REMORA_PROBE_SOURCE if pre_registered
             else learning.REMORA_SHADOW_SOURCE),
            int(rows[entry_index]["ts"]) / 1000,
            (int(rows[exit_index]["ts"]) + BAR_MS) / 1000,
            vector, net_return,
            metadata={
                "decision_ts": int(decision_ts), "side": side,
                "exit_reason": reason, "capital_used": False,
                "paper_probe_executed": pre_registered,
                "paper_probe_notional_usd": (
                    PROBE_NOTIONAL_USD if pre_registered else 0.0),
                "pre_registered_before_label": pre_registered,
                "label_horizon_bars": PROBE_HORIZON_BARS,
                "causal_features": True,
            },
        )
        resolved += 1
    if resolved:
        learning.refresh(db)
    return resolved


def seed_historical_samples(
    db: sqlite3.Connection,
    rows: Sequence[Mapping[str, object]],
    max_samples: int = 200,
) -> dict[str, object]:
    """Seed a historical learner without counting replay as forward evidence."""
    import learning
    import remora_signal

    validate_rows(rows)
    if isinstance(max_samples, bool) or not 200 <= max_samples <= 2000:
        raise ValueError("Historical Remora sample count must be 200-2000.")
    if len(rows) < remora_signal.MIN_BARS + PROBE_HORIZON_BARS:
        raise ValueError("Historical Remora seed data is too short.")

    # Recent history is enough for the seed and keeps the one-off command fast.
    # Every evaluated decision still receives the same 1,000 closed bars as the
    # live worker, while the source remains explicitly historical.
    analysis_rows = list(rows[-60_000:])
    f = indicators(analysis_rows)
    candidates = []
    for index in range(max(999, remora_signal.MIN_BARS - 1),
                       len(analysis_rows) - PROBE_HORIZON_BARS):
        current = remora_signal._stoch_rsi(f["rsi"], index)
        previous = remora_signal._stoch_rsi(f["rsi"], index - 1)
        if current is None or previous is None:
            continue
        if previous <= 0.20 < current or previous >= 0.80 > current:
            candidates.append(index)
    if not candidates:
        raise ValueError("Historical Remora seed found no trigger candidates.")

    evaluation_target = min(len(candidates), math.ceil(max_samples * 1.5))
    if evaluation_target == 1:
        chosen = candidates
    else:
        chosen = [
            candidates[round(i * (len(candidates) - 1) / (evaluation_target - 1))]
            for i in range(evaluation_target)
        ]
    samples = []
    for index in chosen:
        window = analysis_rows[index - 999:index + 1]
        decision = decide(window, False)
        context = decision.get("context", {})
        side = context.get("side") if isinstance(context, Mapping) else None
        if side not in {"long", "short"}:
            continue
        entry_index = index + 1
        entry_reference = float(analysis_rows[entry_index]["open"])
        atr = _finite(decision["atr"], "historical.atr")
        stop = entry_reference - STOP_ATR * atr if side == "long" else entry_reference + STOP_ATR * atr
        target = entry_reference + TARGET_ATR * atr if side == "long" else entry_reference - TARGET_ATR * atr
        if stop <= 0 or target <= 0:
            continue
        exit_index = index + PROBE_HORIZON_BARS
        exit_reference = float(analysis_rows[exit_index]["close"])
        reason = "remora_h8_timeout"
        for cursor in range(entry_index, index + PROBE_HORIZON_BARS + 1):
            bar = analysis_rows[cursor]
            bar_open = float(bar["open"])
            if side == "long" and float(bar["low"]) <= stop:
                exit_reference, reason, exit_index = min(stop, bar_open), "stop", cursor
                break
            if side == "long" and float(bar["high"]) >= target:
                exit_reference, reason, exit_index = max(target, bar_open), "target", cursor
                break
            if side == "short" and float(bar["high"]) >= stop:
                exit_reference, reason, exit_index = max(stop, bar_open), "stop", cursor
                break
            if side == "short" and float(bar["low"]) <= target:
                exit_reference, reason, exit_index = min(target, bar_open), "target", cursor
                break
        samples.append({
            "decision_ts": int(analysis_rows[index]["ts"]),
            "entry_ts": int(analysis_rows[entry_index]["ts"]),
            "exit_ts": int(analysis_rows[exit_index]["ts"]),
            "side": side,
            "x": _learning_vector(decision),
            "net_return": _shadow_net_return(side, entry_reference, exit_reference),
            "exit_reason": reason,
        })
    if len(samples) < max_samples:
        raise ValueError(
            f"Historical Remora seed produced only {len(samples)}/{max_samples} samples.")
    if len(samples) > max_samples:
        samples = [
            samples[round(i * (len(samples) - 1) / (max_samples - 1))]
            for i in range(max_samples)
        ]
    learning.ensure_tables(db)
    before_count = int(db.execute(
        "SELECT COUNT(*) FROM learning_samples WHERE strategy=? AND source=?",
        (learning.REMORA_MODEL, learning.REMORA_HISTORICAL_SOURCE),
    ).fetchone()[0])
    with db:
        for sample in samples:
            learning.add_sample(
                db, learning.REMORA_MODEL, learning.REMORA_HISTORICAL_SOURCE,
                sample["entry_ts"] / 1000,
                (sample["exit_ts"] + BAR_MS) / 1000,
                sample["x"], sample["net_return"],
                metadata={
                    "decision_ts": sample["decision_ts"], "side": sample["side"],
                    "exit_reason": sample["exit_reason"], "capital_used": False,
                    "historical_replay": True, "label_horizon_bars": PROBE_HORIZON_BARS,
                    "causal_features": True,
                },
            )
    after_count = int(db.execute(
        "SELECT COUNT(*) FROM learning_samples WHERE strategy=? AND source=?",
        (learning.REMORA_MODEL, learning.REMORA_HISTORICAL_SOURCE),
    ).fetchone()[0])
    learning.refresh(db, force=True)
    state = learning.model_state(db, learning.REMORA_MODEL)
    return {
        "historical_samples_requested": max_samples,
        "historical_samples_selected": len(samples),
        "historical_samples_written": after_count - before_count,
        "historical_samples_total": after_count,
        "model_status": state.get("status"),
        "total_samples": state.get("sample_count", 0),
        "forward_samples": state.get("forward_count", 0),
        "executed_forward_samples": state.get("executed_forward_count", 0),
        "eligible": bool(state.get("eligible")),
    }


def _close(
    db: sqlite3.Connection,
    state: dict[str, object],
    position: Mapping[str, object],
    bid: float,
    now_ms: int,
    reason: str,
) -> dict[str, object]:
    exit_price, proceeds = _liquidation(position, bid)
    pnl = proceeds - float(position["cost"])
    net_return = pnl / float(position["cost"])
    state["cash_usd"] = float(state["cash_usd"]) + proceeds
    state["realized_pnl_usd"] = float(state["realized_pnl_usd"]) + pnl
    state["completed_trades"] = int(state["completed_trades"]) + 1
    state["position"] = None
    cursor = db.execute(
        "UPDATE v3_paper_executions SET exit_ts_ms=?,exit_price=?,proceeds_usd=?,"
        "pnl_usd=?,net_return=?,exit_reason=?,status='closed',updated_ts=? "
        "WHERE entry_decision_ts=? AND status='open'",
        (
            now_ms, exit_price, proceeds, pnl, net_return, reason, now_ms,
            position["entry_decision_ts"],
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("V3 open execution record is missing.")
    _record_learning_outcome(db, position, now_ms, net_return, pnl, reason)
    return {"pnl_usd": pnl, "net_return": net_return, "exit_reason": reason}


def tick(
    db: sqlite3.Connection,
    quote: Mapping[str, object],
    rows: Sequence[Mapping[str, object]] | None,
    now_ms: int,
) -> dict[str, object] | None:
    """Manage V3 protection and process at most one new closed-bar decision."""
    ensure_tables(db)
    now = _now_ms(now_ms)
    state = _read_state(db)
    if state is None:
        return None
    bid = _finite(quote.get("bid"), "quote.bid")
    ask = _finite(quote.get("ask"), "quote.ask")
    if not 0 < bid <= ask:
        raise ValueError("V3 quote is invalid.")
    latest_ts = int(rows[-1]["ts"]) if rows else None
    fresh = bool(
        rows and 0 <= now / 1000 - (latest_ts / 1000 + BAR_SECONDS)
        <= FRESH_WINDOW_SECONDS
    )
    new_bar = bool(
        fresh
        and (state["last_decision_ts"] is None
             or latest_ts > int(state["last_decision_ts"]))
    )
    decision = decide(rows, state["position"] is not None) if new_bar else None
    closed_result = None
    with db:
        shadow_labels_resolved = _resolve_shadow_labels(db, rows, now) if rows else 0
        position = state["position"]
        if position:
            _, liquidation = _liquidation(position, bid)
            current_net_return = (
                liquidation - float(position["cost"])
            ) / float(position["cost"])
            position["best_net_return"] = max(
                float(position.get("best_net_return", current_net_return)),
                current_net_return,
            )
            if current_net_return >= BREAKEVEN_ARM_NET_RETURN:
                position["breakeven_armed"] = True
        else:
            liquidation = 0.0
        equity = float(state["cash_usd"]) + liquidation
        today = _day(now)
        if state["day"] != today:
            state["day"] = today
            state["day_start_equity"] = equity
            state["daily_halt"] = False
        state["peak_equity_usd"] = max(float(state["peak_equity_usd"]), equity)
        state["drawdown_pct"] = (
            (1 - equity / float(state["peak_equity_usd"])) * 100
            if float(state["peak_equity_usd"]) else 0.0
        )
        state["daily_halt"] = bool(
            state["daily_halt"]
            or equity <= float(state["day_start_equity"]) * (1 - DAILY_LOSS_LIMIT)
        )
        state["drawdown_halt"] = bool(
            state["drawdown_halt"] or float(state["drawdown_pct"]) >= DRAWDOWN_LIMIT * 100
        )
        exit_reason = None
        if position:
            if str(position.get("version", "")) != VERSION:
                exit_reason = "v3_legacy_rule_retired"
            elif state["drawdown_halt"]:
                exit_reason = "v3_drawdown_halt"
            elif state["daily_halt"]:
                exit_reason = "v3_daily_loss_halt"
            elif bool(position.get("breakeven_armed", False)) and current_net_return <= 0:
                exit_reason = "v3_breakeven_guard"
            elif bid <= float(position["stop"]):
                exit_reason = "stop"
            elif bid >= float(position["target"]):
                exit_reason = "target"
            elif now >= int(position["expires_ts_ms"]):
                exit_reason = "v3_h4_timeout"
            elif decision and decision["action"] == "sell":
                exit_reason = str(decision["reason"])
        if exit_reason:
            closed_result = _close(db, state, position, bid, now, exit_reason)
            position = None
            equity = float(state["cash_usd"])

        if decision:
            actual_action = str(decision["action"])
            actual_reason = str(decision["reason"])
            strategy = str(decision["strategy"])
            cooldown, cooldown_reason = _loss_cooldown(
                db, int(decision["decision_ts"])
            )
            can_enter = (
                actual_action == "buy"
                and position is None
                and closed_result is None
                and bool(state["enabled"])
                and not state["daily_halt"]
                and not state["drawdown_halt"]
                and not cooldown
                and not ENTRY_QUARANTINED
            )
            spread = (ask - bid) / bid
            plan = _size(float(state["cash_usd"]), ask, float(decision["atr"]))
            model_pass, model_reason, model_assessment = _model_gate(db, decision, spread)
            if model_assessment is not None and model_assessment.get("score") is not None:
                decision["features"]["remora_model_score"] = float(model_assessment["score"])
                decision["features"]["remora_conservative_net_edge"] = float(
                    model_assessment["conservative_net_edge"]
                )
            if (
                can_enter and model_pass
                and spread <= MAX_ENTRY_SPREAD and plan is not None
            ):
                position = {
                    **plan,
                    "entry_decision_ts": int(decision["decision_ts"]),
                    "entry_ts_ms": now,
                    "expires_ts_ms": now + MAX_HOLD_MS,
                    "strategy": strategy,
                    "version": VERSION,
                    "breakeven_armed": False,
                    "best_net_return": -1.0,
                }
                db.execute(
                    "INSERT INTO v3_paper_decisions "
                    "(decision_ts,version,action,strategy,reason,close,rsi,percent_b,"
                    "bandwidth,atr,created_ts,feature_json,context_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision["decision_ts"], VERSION, "buy", strategy,
                        actual_reason, decision["close"], decision["rsi"],
                        decision["percent_b"], decision["bandwidth"],
                        decision["atr"], now, _encoded_features(decision),
                        _encoded_context(decision),
                    ),
                )
                db.execute(
                    "INSERT INTO v3_paper_executions "
                    "(entry_decision_ts,version,strategy,entry_ts_ms,exit_ts_ms,"
                    "entry_reference,entry_price,exit_price,quantity,cost_usd,"
                    "proceeds_usd,pnl_usd,net_return,stop,target,exit_reason,status,"
                    "created_ts,updated_ts) VALUES (?,?,?,?,NULL,?,?,NULL,?,?,"
                    "NULL,NULL,NULL,?,?,NULL,'open',?,?)",
                    (
                        decision["decision_ts"], VERSION, strategy, now,
                        plan["entry_reference"], plan["entry"], plan["quantity"],
                        plan["cost"], plan["stop"], plan["target"], now, now,
                    ),
                )
                state["cash_usd"] = float(state["cash_usd"]) - float(plan["cost"])
                state["position"] = position
                equity = float(state["cash_usd"]) + _liquidation(position, bid)[1]
            else:
                if actual_action == "buy":
                    actual_action = "blocked"
                    if not state["enabled"]:
                        actual_reason = "v3_disabled"
                    elif state["daily_halt"] or state["drawdown_halt"]:
                        actual_reason = "v3_risk_halt"
                    elif position is not None:
                        actual_reason = "v3_position_already_open"
                    elif closed_result is not None:
                        actual_reason = "v3_no_same_bar_reentry"
                    elif cooldown:
                        actual_reason = cooldown_reason
                    elif ENTRY_QUARANTINED:
                        actual_reason = "v3_historical_edge_not_validated"
                    elif not model_pass:
                        actual_reason = "remora_model_" + model_reason
                    elif spread > MAX_ENTRY_SPREAD:
                        actual_reason = "spread_above_limit"
                    else:
                        actual_reason = "position_sizing_or_cost_edge_unavailable"
                db.execute(
                    "INSERT INTO v3_paper_decisions "
                    "(decision_ts,version,action,strategy,reason,close,rsi,percent_b,"
                    "bandwidth,atr,created_ts,feature_json,context_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision["decision_ts"], VERSION, actual_action, strategy,
                        actual_reason, decision["close"], decision["rsi"],
                        decision["percent_b"], decision["bandwidth"],
                        decision["atr"], now, _encoded_features(decision),
                        _encoded_context(decision),
                    ),
                )
            state["last_decision_ts"] = int(decision["decision_ts"])

        state["position"] = position
        state["equity_usd"] = equity
        state["peak_equity_usd"] = max(float(state["peak_equity_usd"]), equity)
        state["drawdown_pct"] = (
            (1 - equity / float(state["peak_equity_usd"])) * 100
            if float(state["peak_equity_usd"]) else 0.0
        )
        state["updated_ts"] = now
        _write_state(db, state)
    return status(db)


def status(db: sqlite3.Connection) -> dict[str, object] | None:
    ensure_tables(db)
    state = _read_state(db)
    if state is None:
        return None
    counts = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(action='buy'),0),"
        "COALESCE(SUM(action='sell'),0),COALESCE(SUM(action='hold'),0),"
        "COALESCE(SUM(action='blocked'),0) FROM v3_paper_decisions"
    ).fetchone()
    executions = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(status='open'),0),"
        "COALESCE(SUM(status='closed'),0),"
        "COALESCE(SUM(CASE WHEN status='closed' THEN pnl_usd ELSE 0 END),0) "
        "FROM v3_paper_executions"
    ).fetchone()
    outcomes = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN status='closed' AND pnl_usd>0 THEN 1 ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' AND pnl_usd<0 THEN 1 ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' AND pnl_usd>0 THEN pnl_usd ELSE 0 END),0),"
        "COALESCE(-SUM(CASE WHEN status='closed' AND pnl_usd<0 THEN pnl_usd ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' AND exit_reason='stop' THEN 1 ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='closed' AND exit_reason LIKE '%timeout' THEN 1 ELSE 0 END),0) "
        "FROM v3_paper_executions"
    ).fetchone()
    gross_profit = float(outcomes[2])
    gross_loss = float(outcomes[3])
    learning_ready = db.execute(
        "SELECT COUNT(*) FROM v3_paper_executions e "
        "JOIN v3_paper_decisions d ON d.decision_ts=e.entry_decision_ts "
        "WHERE e.status='closed' AND d.feature_json IS NOT NULL"
    ).fetchone()
    import learning
    learning.ensure_tables(db)
    remora_model = learning.model_state(db, learning.REMORA_MODEL)
    shadow_counts = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(side='long'),0),COALESCE(SUM(side='short'),0) "
        "FROM v3_shadow_labels WHERE version=?",
        (VERSION,),
    ).fetchone()
    probe_counts = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(side='long'),0),COALESCE(SUM(side='short'),0),"
        "COALESCE(SUM(pnl_usd),0),"
        "COALESCE(SUM(CASE WHEN pnl_usd>0 THEN pnl_usd ELSE 0 END),0),"
        "COALESCE(-SUM(CASE WHEN pnl_usd<0 THEN pnl_usd ELSE 0 END),0) "
        "FROM v3_probe_executions WHERE version=?",
        (VERSION,),
    ).fetchone()
    probe_gross_loss = float(probe_counts[5])
    state.update(
        {
            "pnl_usd": float(state["equity_usd"]) - float(state["initial_usd"]),
            "return_pct": (
                (float(state["equity_usd"]) / float(state["initial_usd"]) - 1) * 100
            ),
            "decision_records": int(counts[0]),
            "buy_decisions": int(counts[1]),
            "sell_decisions": int(counts[2]),
            "hold_decisions": int(counts[3]),
            "blocked_decisions": int(counts[4]),
            "execution_records": int(executions[0]),
            "open_trades": int(executions[1]),
            "closed_trades": int(executions[2]),
            "closed_execution_pnl_usd": float(executions[3]),
            "risk_fraction": RISK_FRACTION,
            "allocation_cap": ALLOCATION_CAP,
            "stop_atr": STOP_ATR,
            "target_atr": TARGET_ATR,
            "max_hold_bars": MAX_HOLD_BARS,
            "min_target_net_return": MIN_TARGET_NET_RETURN,
            "min_net_reward_risk": MIN_NET_REWARD_RISK,
            "loss_cooldown_bars": LOSS_COOLDOWN_BARS,
            "loss_streak_cooldown_bars": LOSS_STREAK_COOLDOWN_BARS,
            "breakeven_arm_net_return": BREAKEVEN_ARM_NET_RETURN,
            "breakout_quarantined": True,
            "signal_profile": "quant_remora_v5_spot_ohlcv_paper_subset",
            "full_sdd_data_available": False,
            "binance_components_dormant": True,
            "dormant_components": [
                "binance_futures_orders", "binance_short_order_execution", "leverage",
                "open_interest", "long_short_ratio", "funding", "full_order_book",
                "exchange_reconciliation", "independent_account_watchdog",
            ],
            "system_state": (
                "CIRCUIT_BREAKER" if state["daily_halt"] or state["drawdown_halt"]
                else "PAUSED" if not state["enabled"] or ENTRY_QUARANTINED
                else "LEARNING" if (
                    CAPITAL_REQUIRES_ELIGIBLE_MODEL
                    and not bool(remora_model.get("eligible")))
                else "TRADING" if state["position"] is not None
                else "READY"
            ),
            "entry_quarantined": bool(
                ENTRY_QUARANTINED or (
                    CAPITAL_REQUIRES_ELIGIBLE_MODEL
                    and not bool(remora_model.get("eligible")))
            ),
            "entry_quarantine_reason": (
                "negative_200k_chronological_replay_after_costs"
                if ENTRY_QUARANTINED else None
            ) or (
                "forward_model_not_eligible"
                if CAPITAL_REQUIRES_ELIGIBLE_MODEL
                and not bool(remora_model.get("eligible")) else None
            ),
            "capital_requires_eligible_model": CAPITAL_REQUIRES_ELIGIBLE_MODEL,
            "entry_authorization": ENTRY_AUTHORIZATION,
            "winning_trades": int(outcomes[0]),
            "losing_trades": int(outcomes[1]),
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "stop_exits": int(outcomes[4]),
            "timeout_exits": int(outcomes[5]),
            "learning_ready_samples": int(learning_ready[0]),
            "remora_learning": {
                key: value for key, value in remora_model.items() if key != "model"
            },
            "shadow_training_labels": int(shadow_counts[0]),
            "shadow_long_labels": int(shadow_counts[1]),
            "shadow_short_labels": int(shadow_counts[2]),
            "forward_paper_probes": int(probe_counts[0]),
            "forward_long_probes": int(probe_counts[1]),
            "forward_short_probes": int(probe_counts[2]),
            "forward_probe_pnl_usd": float(probe_counts[3]),
            "forward_probe_profit_factor": (
                float(probe_counts[4]) / probe_gross_loss if probe_gross_loss else None),
            "forward_probe_notional_usd": PROBE_NOTIONAL_USD,
            "forward_probe_horizon_bars": PROBE_HORIZON_BARS,
            "forward_probe_trigger": "stoch_rsi_cross_30_70_or_hourly_direction",
            "included_in_main_1000_usd": False,
            "real_orders_enabled": False,
        }
    )
    return state


def learning_samples(db: sqlite3.Connection) -> list[dict[str, object]]:
    """Return immutable, causally captured V3 entry features joined to realized outcomes."""
    ensure_tables(db)
    rows = db.execute(
        "SELECT e.entry_decision_ts,e.version,e.strategy,e.entry_ts_ms,e.exit_ts_ms,"
        "e.net_return,e.pnl_usd,e.exit_reason,d.feature_json "
        "FROM v3_paper_executions e JOIN v3_paper_decisions d "
        "ON d.decision_ts=e.entry_decision_ts "
        "WHERE e.status='closed' AND d.feature_json IS NOT NULL "
        "ORDER BY e.entry_decision_ts"
    ).fetchall()
    result = []
    for row in rows:
        features = json.loads(str(row[8]))
        if not isinstance(features, dict):
            raise ValueError("V3 learning feature record is corrupt.")
        result.append({
            "entry_decision_ts": int(row[0]),
            "version": str(row[1]),
            "strategy": str(row[2]),
            "entry_ts_ms": int(row[3]),
            "exit_ts_ms": int(row[4]),
            "net_return": _finite(row[5], "sample.net_return"),
            "pnl_usd": _finite(row[6], "sample.pnl_usd"),
            "exit_reason": str(row[7]),
            "features": {str(key): _finite(value, f"sample.features.{key}")
                         for key, value in features.items()},
        })
    return result


__all__ = [
    "VERSION", "INITIAL_USD", "RISK_FRACTION", "ALLOCATION_CAP",
    "STOP_ATR", "TARGET_ATR", "MAX_HOLD_BARS", "MIN_TARGET_NET_RETURN",
    "MIN_NET_REWARD_RISK", "LOSS_COOLDOWN_BARS", "LOSS_STREAK_COOLDOWN_BARS",
    "BREAKEVEN_ARM_NET_RETURN", "PROBE_NOTIONAL_USD", "PROBE_HORIZON_BARS",
    "ENTRY_QUARANTINED", "ENTRY_AUTHORIZATION", "CAPITAL_REQUIRES_ELIGIBLE_MODEL",
    "ensure_tables", "enable", "disable", "decide", "tick", "status",
    "learning_samples", "seed_historical_samples",
]
