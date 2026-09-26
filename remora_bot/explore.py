"""Exploration mode: learn from executed trades across a 10-coin universe (virtual money only).

Every closed 1h bar: score all coins with the pooled universe model and open a fixed 200 USDT long in
every flat coin (best-ranked first, up to ENTRIES_PER_HOUR / MAX_OPEN) even if its prediction is
negative, hold it 6 bars (the model's horizon), then close it.  The rank is recorded with each trade
so the model's ordering can be judged on executed results.  Each closed trade's realized net return is fed back into the model's training set,
where it replaces the market-proxy label of its entry bar and weighs 5x.  Order safety is the same
code path as the main bot (intent persisted before POST, never re-POSTed, exchange-side stop).
"""
from __future__ import annotations

import json
import time
from decimal import Decimal

from . import bot, model_v2, strategy

UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
            "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT")
NOTIONAL = Decimal("200")
HOLD_BARS = 6
MAX_OPEN = 10                 # one position per coin at most, so every coin can be traded and learned
ENTRIES_PER_HOUR = 10
MAKER_FEE, TAKER_FEE = 0.0002, 0.0005   # post-only entry, market exit; RESULT responses omit commission
MODEL_DB = bot.STATE / "remora-bot-model-v3-universe.sqlite3"


def configure():
    bot.configure_interval(1)
    model_v2.configure(1, universe=True)


def _last_closed_bar(now_ms: int) -> int:
    return (now_ms // strategy.STEP_MS - 1) * strategy.STEP_MS


def sync_realized(db, model_db, now_ms: int) -> int:
    """Feed every closed ledger trade (wins and losses) to the model; idempotent by trade id."""
    added = 0
    with model_db:
        for t in db.execute("SELECT id, symbol, entry_bar, entry_price, exit_price, exit_reason FROM trades"):
            entry, exit_ = float(t["entry_price"] or 0), float(t["exit_price"] or 0)
            if entry <= 0 or exit_ <= 0 or t["entry_bar"] is None:
                continue
            net = exit_ / entry - 1 - MAKER_FEE - TAKER_FEE
            added += model_v2.record_realized(model_db, f"ledger:{t['id']}", t["symbol"], int(t["entry_bar"]),
                                              net, t["exit_reason"], now_ms)
    return added


def _record(sig, prediction, rank, **extra):
    return dict(sig.__dict__, decision_owner="exploration", interval=strategy.INTERVAL, rank=rank,
                model_approved=bool(prediction is not None and prediction > 0), profitability_proven=False,
                stop_distance=strategy.model_stop_distance(sig.vol_annual), **extra)


def tick(db, client, market, model_db, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    if db.execute("SELECT halted FROM bot WHERE id=1").fetchone()["halted"]:
        return {"halted": True}
    if hasattr(client, "check_stops"):
        client.check_stops()
    blocked = bot.update_risk(db, client, now_ms)
    positions = {r["symbol"]: r for r in db.execute("SELECT * FROM positions")}
    result = {}
    for s in UNIVERSE:                               # pending intents first: query only, never re-POST
        if positions[s]["pending_id"]:
            bot.reconcile_pending(db, client, positions[s], now_ms)
            result[s] = "reconciling"
    latest = _last_closed_bar(now_ms)
    last_done = max((r["last_bar"] or 0) for r in positions.values())
    new_bar = latest > last_done
    for s in UNIVERSE:                               # cheap between bars: only open positions
        if s not in result and (new_bar or positions[s]["phase"] == "long"):
            positions[s] = bot.verify_exchange(db, client, positions[s], now_ms)
    added = sync_realized(db, model_db, now_ms)
    if not new_bar:
        return dict(result, waiting=True, realized_added=added)

    bars = {s: [b for b in market.closed_bars(s) if b.ts + strategy.STEP_MS <= now_ms] for s in UNIVERSE}
    usable = {s: b for s, b in bars.items() if b and b[-1].ts == latest}
    if "BTCUSDT" not in usable:                      # BTC is every coin's market factor
        return dict(result, skipped="btc_bar_missing")
    predictions = model_v2.update(model_db, usable, {s: market.funding(s) for s in usable}, now_ms)
    signals = {s: strategy.evaluate(b) for s, b in usable.items()}
    stale = now_ms - (latest + strategy.STEP_MS) > bot.MAX_SIGNAL_AGE_MS
    ranked = sorted((s for s in usable if predictions.get(s) is not None), key=lambda s: -predictions[s])
    rank = {s: i + 1 for i, s in enumerate(ranked)}

    positions = {r["symbol"]: r for r in db.execute("SELECT * FROM positions")}
    open_after_exits = 0
    for s, pos in positions.items():                 # exits: fixed 6-bar hold
        if s not in usable or s in result or pos["phase"] != "long":
            continue
        held = (latest - int(pos["entry_bar"])) // strategy.STEP_MS
        if held >= HOLD_BARS and not stale:
            result[s] = bot.commit_and_execute(db, client, pos, latest, now_ms, "exit", "exploration_hold_complete",
                                               None, _record(signals[s], predictions.get(s), rank.get(s), held=held),
                                               predictions.get(s), True)
        else:
            open_after_exits += 1
            result[s] = "holding"

    positions = {r["symbol"]: r for r in db.execute("SELECT * FROM positions")}
    # Flat coins in rank order (best prediction first), then any coin without a prediction.
    order = [s for s in ranked if s in usable] + [s for s in usable if s not in rank]
    open_count, entered = open_after_exits, 0
    for s in order:
        if s in result:
            continue
        pos, sig, pred = positions[s], signals[s], predictions.get(s)
        if stale:
            action, reason = "hold", "stale_bar"
        elif pred is None:
            action, reason = "hold", "no_model_prediction"
        elif pos["pending_id"] or pos["phase"] != "flat":
            action, reason = "hold", "already_active"
        elif blocked:
            action, reason = "hold", f"entries_blocked:{blocked}"
        elif open_count >= MAX_OPEN:
            action, reason = "hold", "max_open_positions"
        elif entered >= ENTRIES_PER_HOUR:
            action, reason = "hold", "hourly_entry_limit"
        elif sig.vol_annual <= 0:
            action, reason = "hold", "zero_volatility"
        else:
            action, reason = "enter", "exploration_ranked_entry"
        result[s] = bot.commit_and_execute(db, client, pos, latest, now_ms, action, reason, NOTIONAL,
                                           _record(sig, pred, rank.get(s)), pred, True, limit_entry=True)
        if action == "enter":
            entered += 1
            open_count += 1
    return dict(result, realized_added=added, top=ranked[:3])


def entry_fill_stats(db) -> dict:
    """Real maker fill rate of post-only entries (terminal intents only)."""
    rows = db.execute("SELECT status, response FROM intents WHERE kind='entry' AND status NOT IN ('prepared')").fetchall()
    done = [(s, json.loads(r) if r else {}) for s, r in rows]
    filled = sum(1 for _, r in done if float(r.get("executedQty", 0) or 0) > 0)
    rejected = sum(1 for s, _ in done if s == "EXPIRED")
    return dict(attempts=len(done), filled=filled, post_only_rejected=rejected,
                fill_rate=round(filled / len(done), 3) if done else None)


def status(db, model_db) -> dict:
    return dict(universe=UNIVERSE, notional_usdt=str(NOTIONAL), hold_bars=HOLD_BARS, max_open=MAX_OPEN,
                entries_per_hour=ENTRIES_PER_HOUR,
                open_positions=[dict(r) for r in db.execute(
                    "SELECT symbol, qty, entry_price, stop_price, entry_bar FROM positions WHERE phase='long'")],
                model=model_v2.status(model_db))
