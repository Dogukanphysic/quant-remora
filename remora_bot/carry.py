"""Funding-ranked market-neutral carry on the 10-coin universe (Demo/Testnet, virtual money only).

The rule selected by funding_carry_research.py (passed its single holdout evaluation):
every Monday 00:00 UTC, rank the coins by funding paid over the previous 72h; short the 2 highest,
long the 2 lowest, 500 USDT notional per leg (dollar neutral), hold one week.

Order safety follows the main bot: every step is persisted as an intent before its single POST,
client ids are deterministic per (coin, rebalance, step) so nothing is ever re-POSTed, each leg gets an
exchange-side STOP_MARKET (15% adverse), and any exchange/ledger mismatch halts the bot.
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import ROUND_CEILING, Decimal

from . import bot, universe
from .exchange import ApiError, TransportError, floor_step

UNIVERSE = universe.active()
# Per-universe parameters are each universe's own pre-registered research pick (funding_carry_research.py):
# a: 72h lookback, 2 per side; b: 7-day lookback, 3 per side. Gross exposure stays 2000 USDT.
K, LOOKBACK_DAYS = {"a": (2, 3), "b": (3, 7)}[universe.name()]
LOOKBACK_MS = LOOKBACK_DAYS * 24 * 3600_000
REBALANCE_MS = 168 * 3600_000
ANCHOR_MS = 1704067200000                 # Monday 2024-01-01 00:00 UTC
MAX_LATE_MS = 24 * 3600_000               # a missed rebalance is skipped, never chased
LEG_NOTIONAL = (Decimal("1000") / K).quantize(Decimal("1"))
STOP_DISTANCE = Decimal("0.15")
POLICY = f"remora-funding-carry-v1-{universe.name()}"


def ledger_path(mode):
    suffix = "" if universe.name() == "a" else f"-{universe.name()}"
    return bot.STATE / f"remora-bot-carry-{mode}{suffix}.sqlite3"


def connect(path):
    db = bot.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS carry_targets(rebalance_ts INTEGER, symbol TEXT, side TEXT, score REAL, "
               "rank INTEGER, PRIMARY KEY(rebalance_ts, symbol))")
    db.commit()
    bot.ensure_symbols(db, UNIVERSE)
    return db


def rebalance_ts(now_ms):
    return ANCHOR_MS + (now_ms - ANCHOR_MS) // REBALANCE_MS * REBALANCE_MS


def client_id(symbol, ts, kind):
    digest = hashlib.sha256(f"{POLICY}|{symbol}|{ts}|{kind}".encode()).hexdigest()[:8]
    return f"rmb-c{symbol[:4].lower()}-{kind}-{ts // 1000}-{digest}"[:36]


def funding_score(series, ts):
    """Funding paid in (ts-lookback, ts]; settlement times are floored to the hour like the research grid."""
    if series is None or len(series) == 0:
        return None
    hours = series.index.floor("h").as_unit("ms").asi8
    mask = (hours > ts - LOOKBACK_MS) & (hours <= ts)
    return float(series[mask].sum()) if mask.any() else None


def targets_for(scores):
    """{symbol: 'long'|'short'|'flat'} from {symbol: score}; needs every coin scored."""
    ranked = sorted(scores, key=lambda s: (scores[s], s))
    side = {s: "flat" for s in scores}
    for s in ranked[:K]:
        side[s] = "long"
    for s in ranked[-K:]:
        side[s] = "short"
    return side, {s: i + 1 for i, s in enumerate(ranked)}


def _sign(phase):
    return {"long": 1, "short": -1}.get(phase, 0)


def verify(db, client, pos, now_ms):
    symbol = pos["symbol"]
    qty, _ = client.position(symbol)
    orders = set(client.open_orders(symbol))
    if pos["phase"] in ("long", "short"):
        if qty == 0:
            close_trade(db, symbol, Decimal(pos["stop_price"]), now_ms, "exchange_stop")
            return db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        if qty != _sign(pos["phase"]) * Decimal(pos["qty"]):
            raise bot.Halt(f"{symbol} position mismatch: ledger {pos['phase']} {pos['qty']} exchange {qty}")
        if pos["stop_id"] not in orders:
            raise bot.Halt(f"{symbol} protective stop missing")
        orders.discard(pos["stop_id"])
    elif qty != 0:
        raise bot.Halt(f"{symbol} untracked exchange position {qty}")
    if orders:
        raise bot.Halt(f"{symbol} untracked open orders {sorted(orders)[:3]}")
    return pos


def close_trade(db, symbol, price, now_ms, reason):
    pos = db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    qty, entry = Decimal(pos["qty"]), Decimal(pos["entry_price"] or "0")
    pnl = (price - entry) * qty * _sign(pos["phase"])
    with db:
        db.execute("INSERT INTO trades(symbol, entry_bar, entry_price, exit_ms, exit_price, qty, exit_reason, gross_pnl)"
                   " VALUES (?,?,?,?,?,?,?,?)", (symbol, pos["entry_bar"], str(entry), now_ms, str(price),
                                                 f"{'-' if pos['phase'] == 'short' else ''}{qty}", reason, str(pnl)))
        db.execute("UPDATE positions SET phase='flat', qty='0', entry_price=NULL, entry_bar=NULL, stop_id=NULL, "
                   "stop_price=NULL WHERE symbol=?", (symbol,))
        bot.event(db, "trade_closed", symbol=symbol, reason=reason, price=price, pnl=pnl)


def on_entry_fill(db, client, symbol, side, qty, price, ts):
    rules = client.rules(symbol)
    if side == "long":
        stop, stop_side = floor_step(price * (1 - STOP_DISTANCE), rules["tick"]), "SELL"
    else:
        raw = price * (1 + STOP_DISTANCE)
        stop = (raw / rules["tick"]).to_integral_value(rounding=ROUND_CEILING) * rules["tick"]
        stop_side = "BUY"
    stop_id = client_id(symbol, ts, "s")
    with db:
        db.execute("UPDATE positions SET phase=?, qty=?, entry_price=?, entry_bar=?, stop_id=?, stop_price=? "
                   "WHERE symbol=?", (side, str(qty), str(price), ts, stop_id, str(stop), symbol))
        bot.event(db, "entry_filled", symbol=symbol, side=side, qty=qty, price=price, stop=stop)
    try:
        client.place_stop(symbol, qty, stop, stop_id, side=stop_side)
    except Exception as exc:
        exit_id = client_id(symbol, ts, "z")
        close_side = "SELL" if side == "long" else "BUY"
        with db:
            db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,NULL)",
                       (exit_id, symbol, close_side, "exit", str(qty), int(time.time() * 1000), "prepared"))
        client.market_order(symbol, close_side, qty, exit_id, True)
        raise bot.Halt(f"protective stop failed for {symbol}; emergency close sent: {exc}")


def reconcile(db, client, pos, now_ms):
    intent = db.execute("SELECT * FROM intents WHERE client_id=?", (pos["pending_id"],)).fetchone()
    try:
        order = client.order(pos["symbol"], intent["client_id"])
    except ApiError as exc:
        if exc.code == -2013 and now_ms - intent["created_ms"] > 120_000:
            with db:
                db.execute("UPDATE intents SET status='absent' WHERE client_id=?", (intent["client_id"],))
                db.execute("UPDATE positions SET pending_id=NULL WHERE symbol=?", (pos["symbol"],))
                bot.event(db, "intent_absent", client_id=intent["client_id"])
            return
        raise
    if order["status"] in ("NEW", "PARTIALLY_FILLED"):
        return                                   # market orders settle within seconds; re-query next tick
    filled = Decimal(order.get("executedQty", "0"))
    price = Decimal(order.get("avgPrice", "0") or "0")
    with db:
        db.execute("UPDATE intents SET status=?, response=? WHERE client_id=?",
                   (order["status"], json.dumps(order), intent["client_id"]))
        db.execute("UPDATE positions SET pending_id=NULL WHERE symbol=?", (pos["symbol"],))
    if filled == 0:
        return
    ts = int(intent["client_id"].split("-")[3]) * 1000
    if intent["kind"] == "entry":
        on_entry_fill(db, client, pos["symbol"], "long" if intent["side"] == "BUY" else "short", filled, price, ts)
    else:
        close_trade(db, pos["symbol"], price, now_ms, "rebalance_exit")


def _send(db, client, pos, ts, now_ms, kind, side, qty, record, reason):
    cid = client_id(pos["symbol"], ts, {"entry": "e", "exit": "x"}[kind])
    with db:
        db.execute("INSERT INTO decisions(symbol, bar_ts, created_ms, signal, prediction, model_authority, action, "
                   "reason) VALUES (?,?,?,?,?,?,?,?)", (pos["symbol"], ts, now_ms, json.dumps(record, default=str),
                                                        record.get("score"), 0, kind, reason))
        db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,NULL)",
                   (cid, pos["symbol"], side, kind, str(qty), now_ms, "prepared"))
        db.execute("UPDATE positions SET pending_id=? WHERE symbol=?", (cid, pos["symbol"]))
    if kind == "exit" and pos["stop_id"]:
        try:
            client.cancel_stop(pos["stop_id"])
        except ApiError as exc:
            if exc.code not in (-2011, -2013):
                raise
    try:
        client.market_order(pos["symbol"], side, qty, cid, kind == "exit")
    except TransportError:
        pass                                     # outcome unknown: reconciled by GET, never re-POSTed
    reconcile(db, client, db.execute("SELECT * FROM positions WHERE symbol=?", (pos["symbol"],)).fetchone(), now_ms)
    return kind


def _intent_exists(db, cid):
    return db.execute("SELECT 1 FROM intents WHERE client_id=?", (cid,)).fetchone() is not None


def ensure_targets(db, market, now_ms, blocked):
    ts = rebalance_ts(now_ms)
    if db.execute("SELECT 1 FROM carry_targets WHERE rebalance_ts=?", (ts,)).fetchone():
        return ts
    if now_ms - ts > MAX_LATE_MS:
        return None                              # too late for this week; keep last week's book as is
    scores = {s: funding_score(market.funding(s), ts) for s in UNIVERSE}
    if any(v is None for v in scores.values()):
        return None
    side, rank = targets_for(scores)
    if blocked:                                  # risk limit: go flat, open nothing
        side = {s: "flat" for s in UNIVERSE}
    with db:
        for s in UNIVERSE:
            db.execute("INSERT INTO carry_targets VALUES (?,?,?,?,?)", (ts, s, side[s], scores[s], rank[s]))
        bot.event(db, "carry_targets", rebalance_ts=ts, blocked=blocked, targets=side, scores=scores)
    return ts


def tick(db, client, market, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    if db.execute("SELECT halted FROM bot WHERE id=1").fetchone()["halted"]:
        return {"halted": True}
    blocked = bot.update_risk(db, client, now_ms, universe.shared_account())
    positions = {r["symbol"]: r for r in db.execute("SELECT * FROM positions")}
    result = {}
    for s in UNIVERSE:                           # pending intents: query only
        if positions[s]["pending_id"]:
            reconcile(db, client, positions[s], now_ms)
            result[s] = "reconciling"
    for s in UNIVERSE:
        if s not in result:
            positions[s] = verify(db, client, positions[s], now_ms)
    ts = ensure_targets(db, market, now_ms, blocked)
    if ts is None:
        ts = db.execute("SELECT MAX(rebalance_ts) FROM carry_targets").fetchone()[0]
        if ts is None:
            return dict(result, waiting="first_rebalance")
    targets = {r["symbol"]: dict(r) for r in db.execute("SELECT * FROM carry_targets WHERE rebalance_ts=?", (ts,))}
    for s in UNIVERSE:
        if s in result:
            continue
        pos, tgt = positions[s], targets[s]
        record = dict(rebalance_ts=ts, score=tgt["score"], rank=tgt["rank"], target=tgt["side"], policy=POLICY)
        if pos["phase"] != "flat" and pos["phase"] != tgt["side"]:
            if _intent_exists(db, client_id(s, ts, "x")):
                raise bot.Halt(f"{s} still {pos['phase']} after this rebalance's exit")
            side = "SELL" if pos["phase"] == "long" else "BUY"
            result[s] = _send(db, client, pos, ts, now_ms, "exit", side, Decimal(pos["qty"]), record,
                              f"rebalance_to_{tgt['side']}")
        elif pos["phase"] == "flat" and tgt["side"] != "flat" and not _intent_exists(db, client_id(s, ts, "e")):
            if now_ms - ts > MAX_LATE_MS:
                result[s] = "entry_window_passed"
                continue
            rules, mark = client.rules(s), client.mark(s)
            qty = floor_step(LEG_NOTIONAL / mark, rules["step"])
            if qty < rules["min_qty"] or qty * mark < rules["min_notional"]:
                result[s] = "below_exchange_minimum"
                continue
            result[s] = _send(db, client, pos, ts, now_ms, "entry", "BUY" if tgt["side"] == "long" else "SELL",
                              qty, record, f"carry_{tgt['side']}")
        else:
            result[s] = pos["phase"] if pos["phase"] != "flat" else "flat"
    return dict(result, rebalance_ts=ts)


def status(mode):
    db = connect(ledger_path(mode))
    ts = db.execute("SELECT MAX(rebalance_ts) FROM carry_targets").fetchone()[0]
    trades = [dict(r) for r in db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 20")]
    total = sum(Decimal(t["gross_pnl"]) for t in db.execute("SELECT gross_pnl FROM trades"))
    return dict(mode=mode, strategy=POLICY, real_money=False,
                bot=dict(db.execute("SELECT halted, environment, entries_blocked, peak_wallet, updated_ms FROM bot"
                                    ).fetchone()),
                rebalance_ts=ts, next_rebalance_ts=(rebalance_ts(int(time.time() * 1000)) + REBALANCE_MS),
                targets=[dict(r) for r in db.execute("SELECT symbol, side, score, rank FROM carry_targets "
                                                     "WHERE rebalance_ts=? ORDER BY rank", (ts,))] if ts else [],
                positions=[dict(r) for r in db.execute("SELECT symbol, phase, qty, entry_price, stop_price, pending_id "
                                                       "FROM positions WHERE symbol IN (%s)" % ",".join("?" * len(UNIVERSE)),
                                                       UNIVERSE)],
                trades=trades, total_gross_pnl=str(total))


def write_status(mode, result):
    import os
    payload = status(mode)
    payload.update(last_tick=result, written_ms=int(time.time() * 1000), pid=os.getpid())
    (bot.STATE / f"remora-bot-carry-{mode}-status.json").write_text(json.dumps(payload, indent=2, default=str),
                                                                   encoding="utf-8")


def run(mode, client, market, poll_seconds=30, max_cycles=None):
    import traceback
    with bot.singleton():
        db = connect(ledger_path(mode))
        try:
            bot.startup(db, client, UNIVERSE)
        except ApiError as exc:
            print(f"STARTUP FAILED, nothing traded: {exc}")
            return 1
        print(f"CARRY ON ({universe.name()}: {', '.join(UNIVERSE)}): short top-{K} / long bottom-{K} "
              f"{LOOKBACK_DAYS}d funding, {LEG_NOTIONAL} USDT per leg, weekly "
              "(Monday 00:00 UTC), virtual money only")
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                result = tick(db, client, market)
                write_status(mode, result)
                if result.get("halted"):
                    print("HALTED - inspect status; no automatic restart.")
                    return 1
            except (TransportError, ApiError) as exc:
                bot.event(db, "transient_error", error=str(exc))
                db.commit()
            except bot.Halt as exc:
                bot.halt(db, str(exc))
                write_status(mode, {"halted": True})
                return 1
            except Exception as exc:
                bot.halt(db, f"{type(exc).__name__}: {exc}")
                bot.event(db, "traceback", text=traceback.format_exc())
                db.commit()
                return 1
            cycles += 1
            time.sleep(poll_seconds)
    return 0


def close_explore(client, mode="testnet"):
    """Switch-over: close the exploration bot's own positions through ITS ledger (so its records stay exact),
    marking them carry_switch so they are never used as model labels. Bot must be stopped."""
    from . import explore
    db = bot.connect(bot.ledger_path(mode))
    bot.ensure_symbols(db, explore.UNIVERSE)
    # A halt caused by another bot's coin must not block closing our own legs: only the positions this
    # ledger holds are verified (each still halts on its own mismatch), foreign coins are left alone.
    now_ms = int(time.time() * 1000)
    before = db.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
    lines = []
    for pos in db.execute("SELECT * FROM positions WHERE phase!='flat' OR pending_id IS NOT NULL").fetchall():
        if pos["pending_id"]:
            bot.reconcile_pending(db, client, pos, now_ms)
            pos = db.execute("SELECT * FROM positions WHERE symbol=?", (pos["symbol"],)).fetchone()
        pos = bot.verify_exchange(db, client, pos, now_ms)
        if pos["phase"] == "long" and not pos["pending_id"]:
            bot.commit_and_execute(db, client, pos, now_ms // 1000 * 1000, now_ms, "exit", "carry_switch", None,
                                   dict(decision_owner="carry_switch"), None, False)
            lines.append(f"{pos['symbol']}: kesif pozisyonu kapatildi")
    with db:
        db.execute("UPDATE trades SET exit_reason='carry_switch' WHERE id>?", (before,))
    left = [r[0] for r in db.execute("SELECT symbol FROM positions WHERE phase!='flat' OR pending_id IS NOT NULL")]
    if left:
        raise bot.Halt(f"exploration positions not closed yet: {left}")
    return lines
