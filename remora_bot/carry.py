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
import os
import time
from decimal import ROUND_CEILING, Decimal

from . import bot, carry_adaptive, universe
from .exchange import ApiError, TransportError, floor_step

UNIVERSE = universe.active()
# Per-universe parameters are each universe's own pre-registered research pick (funding_carry_research.py):
# a: 72h lookback, 2 per side; b: 7-day lookback, 3 per side. Gross exposure stays 2000 USDT.
K, LOOKBACK_DAYS = {"a": (2, 3), "b": (3, 7)}[universe.name()]
LOOKBACK_MS = LOOKBACK_DAYS * 24 * 3600_000
REBALANCE_MS = 168 * 3600_000
ANCHOR_MS = 1704067200000                 # Monday 2024-01-01 00:00 UTC
MAX_LATE_MS = 24 * 3600_000               # a missed rebalance is skipped, never chased...
# ...except the very first one: a fresh ledger joins the current week's book immediately (user choice,
# 2026-09-29) using that Monday's signal; the entry window then runs from when the book was set.
LEG_NOTIONAL = (Decimal("1000") / K).quantize(Decimal("1"))
STOP_DISTANCE = Decimal("0.15")
POLICY = f"remora-funding-carry-v1-{universe.name()}"     # unchanged by adaptive mode: keeps client ids stable
# Adaptive (self-updating) mode: see carry_adaptive.py. Off -> the single fixed config above.
ADAPTIVE = os.environ.get("REMORA_CARRY_ADAPTIVE") == "true"
RESIZE_TOLERANCE = Decimal("0.3")        # same-side legs are only re-sized when >30% off the new target


def ledger_path(mode):
    suffix = "" if universe.name() == "a" else f"-{universe.name()}"
    return bot.STATE / f"remora-bot-carry-{mode}{suffix}.sqlite3"


def connect(path):
    db = bot.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS carry_targets(rebalance_ts INTEGER, symbol TEXT, side TEXT, score REAL, "
               "rank INTEGER, PRIMARY KEY(rebalance_ts, symbol))")
    cols = {r[1] for r in db.execute("PRAGMA table_info(carry_targets)")}
    if "notional" not in cols:           # signed USDT per leg (adaptive); NULL = fixed LEG_NOTIONAL
        db.execute("ALTER TABLE carry_targets ADD COLUMN notional TEXT")
    db.execute("CREATE TABLE IF NOT EXISTS carry_model(rebalance_ts INTEGER PRIMARY KEY, weights TEXT, cost REAL, "
               "fills INTEGER, scale REAL, window TEXT, created_ms INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS carry_refs(client_id TEXT PRIMARY KEY, ref TEXT)")
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


ZERO_CONFIRM_TICKS = 10       # ~5 min of consistent "flat + stop still live" before believing it
_zero_seen: dict[str, int] = {}


def _stop_state(client, stop_id):
    try:
        return client.stop_order(stop_id).get("status")
    except ApiError:
        return None


def verify(db, client, pos, now_ms):
    symbol = pos["symbol"]
    qty, _ = client.position(symbol)
    if pos["phase"] in ("long", "short") and qty == 0:
        qty, _ = client.position(symbol)                       # a flaky zero is re-read once
    orders = set(client.open_orders(symbol))
    if pos["phase"] in ("long", "short"):
        if qty == 0:
            state = _stop_state(client, pos["stop_id"]) if pos["stop_id"] else None
            if state in STOP_LIVE:
                # Flat but our stop is still working: the stop did NOT fire. Either the position read
                # was wrong (Demo glitch) or the leg was closed outside the bot. Never book it on one read.
                n = _zero_seen.get(symbol, 0) + 1
                _zero_seen[symbol] = n
                bot.event(db, "position_zero_unconfirmed", symbol=symbol, stop_id=pos["stop_id"], seen=n)
                db.commit()
                if n < ZERO_CONFIRM_TICKS:
                    return pos                                   # skip this leg this tick
                try:
                    client.cancel_stop(pos["stop_id"])
                except ApiError as exc:
                    if exc.code not in (-2011, -2013):
                        raise
                _zero_seen.pop(symbol, None)
                close_trade(db, symbol, client.mark(symbol), now_ms, "closed_outside_bot")
                return db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
            _zero_seen.pop(symbol, None)
            close_trade(db, symbol, Decimal(pos["stop_price"]), now_ms, "exchange_stop")
            return db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        _zero_seen.pop(symbol, None)
        if qty != _sign(pos["phase"]) * Decimal(pos["qty"]):
            raise bot.Halt(f"{symbol} position mismatch: ledger {pos['phase']} {pos['qty']} exchange {qty}")
        if pos["stop_id"] not in orders:
            orders = set(client.open_orders(symbol))          # a flaky listing is re-read once
        if pos["stop_id"] not in orders:
            before = pos["stop_id"]
            pos = restore_stop(db, client, pos, qty, now_ms)  # halts if it cannot be restored safely
            if pos["stop_id"] == before:
                orders.add(before)                            # confirmed live by a direct query
            else:
                orders = set(client.open_orders(symbol))
                if pos["stop_id"] not in orders:
                    raise bot.Halt(f"{symbol} protective stop missing after restore")
        orders.discard(pos["stop_id"])
    elif qty != 0:
        raise bot.Halt(f"{symbol} untracked exchange position {qty}")
    if orders:
        raise bot.Halt(f"{symbol} untracked open orders {sorted(orders)[:3]}")
    return pos


STOP_LIVE = {"NEW", "WORKING", "PARTIALLY_FILLED"}


def restore_stop(db, client, pos, qty, now_ms):
    """The ledger's stop is not in the open-order list while the position is open. Ask for that exact
    stop by id: if it is still live the listing was wrong (keep it); if it is gone, place ONE
    replacement with a new deterministic id (never a second one) and record it."""
    symbol = pos["symbol"]
    try:
        state = client.stop_order(pos["stop_id"]).get("status")
    except ApiError:
        state = None                                     # unknown to the exchange
    if state in STOP_LIVE:
        bot.event(db, "stop_listing_mismatch", symbol=symbol, stop_id=pos["stop_id"], status=state)
        db.commit()
        return pos
    new_id = client_id(symbol, int(pos["entry_bar"]), "r")
    if new_id == pos["stop_id"] or _intent_exists(db, new_id):
        raise bot.Halt(f"{symbol} protective stop missing again after one restore ({state})")
    side = "SELL" if pos["phase"] == "long" else "BUY"
    with db:
        db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,NULL)",
                   (new_id, symbol, side, "stop_restore", str(abs(qty)), now_ms, "prepared"))
        bot.event(db, "stop_restore", symbol=symbol, old=pos["stop_id"], old_status=state, new=new_id,
                  trigger=pos["stop_price"])
    try:
        client.place_stop(symbol, Decimal(pos["qty"]), Decimal(pos["stop_price"]), new_id, side=side)
    except Exception as exc:
        raise bot.Halt(f"{symbol} protective stop missing and restore failed: {exc}")
    with db:
        db.execute("UPDATE positions SET stop_id=? WHERE symbol=?", (new_id, symbol))
        db.execute("UPDATE intents SET status='placed' WHERE client_id=?", (new_id,))
    return db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()


def _own_stops(client, symbol):
    prefix = f"rmb-c{symbol[:4].lower()}-"
    return [o for o in client.open_orders(symbol) if o and o.startswith(prefix) and o.split("-")[2] in ("s", "r")]


def _revert_false_close(db, client, symbol, qty, now_ms):
    """Ledger says flat. If the exchange still holds the leg the ledger last closed, with that leg's own
    stop still live, the close was booked from a wrong zero read: undo it. If the leg is gone but our
    stop is still open, cancel the orphan stop. Anything else is left to verify() (which halts)."""
    stops = _own_stops(client, symbol)
    last = db.execute("SELECT * FROM trades WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
    if qty == 0:
        for sid in stops:
            client.cancel_stop(sid)
        if stops:
            with db:
                bot.event(db, "orphan_stop_cancelled", symbol=symbol, stops=stops)
            return f"{symbol}: sahipsiz stop iptal edildi"
        return None
    if not last or len(stops) != 1 or abs(Decimal(last["qty"])) != abs(qty) or \
            (Decimal(last["qty"]) < 0) != (qty < 0) or last["exit_reason"] not in ("exchange_stop", "closed_outside_bot"):
        return None
    filled = None
    for (payload,) in db.execute("SELECT payload FROM events WHERE kind='entry_filled' ORDER BY id DESC"):
        e = json.loads(payload)
        if e.get("symbol") == symbol:
            filled = e
            break
    stop_price = (filled or {}).get("stop")
    for (payload,) in db.execute("SELECT payload FROM events WHERE kind='stop_restore' ORDER BY id DESC"):
        e = json.loads(payload)
        if e.get("symbol") == symbol and e.get("new") == stops[0]:
            stop_price = e.get("trigger")
            break
    if stop_price is None:
        return None
    phase = "short" if qty < 0 else "long"
    with db:
        db.execute("DELETE FROM trades WHERE id=?", (last["id"],))
        db.execute("UPDATE positions SET phase=?, qty=?, entry_price=?, entry_bar=?, stop_id=?, stop_price=? "
                   "WHERE symbol=?", (phase, str(abs(qty)), last["entry_price"], last["entry_bar"], stops[0],
                                      str(stop_price), symbol))
        bot.event(db, "false_close_reverted", symbol=symbol, trade_id=last["id"], pnl=last["gross_pnl"],
                  reason=last["exit_reason"])
    return f"{symbol}: yanlis kapanis kaydi geri alindi ({last['gross_pnl']} USDT silindi), pozisyon yeniden izleniyor"


def repair(db, client, now_ms=None):
    """User-run (bot stopped): reconcile every leg with the exchange, restore missing stops, record legs
    closed outside the bot (manually or by a stop) at the current mark, then clear the halt.
    Any other mismatch keeps the halt."""
    now_ms = now_ms or int(time.time() * 1000)
    row = db.execute("SELECT halted, identity, environment FROM bot WHERE id=1").fetchone()
    if row["identity"] and (row["identity"] != client.identity or row["environment"] != client.environment):
        raise bot.Halt("key/environment does not match this ledger")
    lines = []
    for s in UNIVERSE:
        pos = db.execute("SELECT * FROM positions WHERE symbol=?", (s,)).fetchone()
        if pos["pending_id"]:
            reconcile(db, client, pos, now_ms)
            pos = db.execute("SELECT * FROM positions WHERE symbol=?", (s,)).fetchone()
            if pos["pending_id"]:
                raise bot.Halt(f"{s} order intent still unresolved; try again in a minute")
        qty, _ = client.position(s)
        if qty == 0:
            qty, _ = client.position(s)                          # never act on a single zero read
        if pos["phase"] in ("long", "short") and qty == 0:
            stop = pos["stop_id"]
            try:
                if stop:
                    client.cancel_stop(stop)
            except ApiError:
                pass
            close_trade(db, s, client.mark(s), now_ms, "closed_outside_bot")
            lines.append(f"{s}: borsada kapali bulundu, kayda islendi")
            continue
        if pos["phase"] == "flat":
            line = _revert_false_close(db, client, s, qty, now_ms)
            if line:
                lines.append(line)
                pos = db.execute("SELECT * FROM positions WHERE symbol=?", (s,)).fetchone()
        before = pos["stop_id"]
        pos = verify(db, client, pos, now_ms)
        if pos["phase"] != "flat" and pos["stop_id"] != before:
            lines.append(f"{s}: eksik stop yeniden konuldu ({pos['stop_price']})")
    if row["halted"]:
        with db:
            db.execute("UPDATE bot SET halted=NULL WHERE id=1")
            bot.event(db, "halt_cleared", previous=row["halted"], by="carry-repair")
        lines.append(f"halt temizlendi (onceki: {row['halted']})")
    return lines


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


def _send(db, client, pos, ts, now_ms, kind, side, qty, record, reason, ref=None):
    cid = client_id(pos["symbol"], ts, {"entry": "e", "exit": "x"}[kind])
    with db:
        if ref is not None:              # price seen just before the order: live slippage -> learned cost
            db.execute("INSERT OR REPLACE INTO carry_refs VALUES (?,?)", (cid, str(ref)))
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


def _book_set_ms(db, ts):
    """When this week's book was set (>= its Monday for a late first start)."""
    for (payload,) in db.execute("SELECT payload FROM events WHERE kind='carry_targets' ORDER BY id DESC"):
        p = json.loads(payload)
        if p.get("rebalance_ts") == ts:
            return max(ts, p.get("set_ms", ts))
    return ts


def _intent_exists(db, cid):
    return db.execute("SELECT 1 FROM intents WHERE client_id=?", (cid,)).fetchone() is not None


def live_fills(db):
    rows = db.execute("SELECT r.ref, i.response FROM carry_refs r JOIN intents i USING(client_id) "
                      "WHERE i.response IS NOT NULL ORDER BY i.created_ms DESC LIMIT 50").fetchall()
    out = []
    for ref, response in rows:
        avg = float(json.loads(response).get("avgPrice") or 0)
        if avg > 0:
            out.append((float(ref), avg))
    return out


def _own_equity_and_peak(db):
    """This ledger's realized equity curve (base = gross exposure); independent of the shared wallet."""
    equity = peak = carry_adaptive.GROSS_USDT
    for (pnl,) in db.execute("SELECT gross_pnl FROM trades ORDER BY id"):
        equity += Decimal(pnl)
        peak = max(peak, equity)
    return equity, peak


def adaptive_book(db, market, ts, blocked):
    """Learn this week's config weights and the netted book; persisted before any order."""
    rebal = [ts - n * REBALANCE_MS for n in range(carry_adaptive.WINDOW_WEEKS + 1, -1, -1)]
    since = rebal[0] - 7 * carry_adaptive.DAY_MS - carry_adaptive.HOUR_MS
    funding = {s: market.funding_since(s, since) for s in UNIVERSE}
    opens = {s: market.opens(s) for s in UNIVERSE}
    if any(ts not in opens[s] for s in UNIVERSE):
        return None
    cost, n_fills = carry_adaptive.learned_cost(live_fills(db))
    window = carry_adaptive.simulate(opens, funding, rebal, cost, UNIVERSE)
    prev = db.execute("SELECT weights FROM carry_model WHERE rebalance_ts<? ORDER BY rebalance_ts DESC LIMIT 1",
                      (ts,)).fetchone()
    weights = carry_adaptive.learn_weights(window, json.loads(prev[0]) if prev else None)
    book = carry_adaptive.combined_book(funding, ts, weights, UNIVERSE)
    equity, peak = _own_equity_and_peak(db)
    scale = 0.0 if blocked else carry_adaptive.drawdown_scale(peak, equity)
    notional = carry_adaptive.leg_notionals(book, scale)
    summary = {n: round(sum(r[n] for _, r in window) * 100, 2) for n in weights}
    return dict(book=book, notional=notional, weights=weights, cost=cost, fills=n_fills, scale=scale,
                window=dict(weeks=len(window), net_pct_by_config=summary))


def ensure_targets(db, market, now_ms, blocked):
    ts = rebalance_ts(now_ms)
    if db.execute("SELECT 1 FROM carry_targets WHERE rebalance_ts=?", (ts,)).fetchone():
        return ts
    first = db.execute("SELECT 1 FROM carry_targets LIMIT 1").fetchone() is None
    if now_ms - ts > MAX_LATE_MS and not first:
        return None                              # too late for this week; keep last week's book as is
    if ADAPTIVE:
        m = adaptive_book(db, market, ts, blocked)
        if m is None:
            return None
        ranked = sorted(UNIVERSE, key=lambda s: (m["book"][s], s))
        with db:
            db.execute("INSERT INTO carry_model VALUES (?,?,?,?,?,?,?)",
                       (ts, carry_adaptive.dumps(m["weights"]), m["cost"], m["fills"], m["scale"],
                        carry_adaptive.dumps(m["window"]), now_ms))
            for i, s in enumerate(ranked):
                n = m["notional"][s]
                side = "long" if n > 0 else "short" if n < 0 else "flat"
                db.execute("INSERT INTO carry_targets VALUES (?,?,?,?,?,?)", (ts, s, side, m["book"][s], i + 1, str(n)))
            bot.event(db, "carry_targets", rebalance_ts=ts, blocked=blocked, adaptive=True, set_ms=now_ms,
                      notional={s: str(v) for s, v in m["notional"].items()}, weights=m["weights"], cost=m["cost"],
                      scale=m["scale"])
        return ts
    scores = {s: funding_score(market.funding(s), ts) for s in UNIVERSE}
    if any(v is None for v in scores.values()):
        return None
    side, rank = targets_for(scores)
    if blocked:                                  # risk limit: go flat, open nothing
        side = {s: "flat" for s in UNIVERSE}
    with db:
        for s in UNIVERSE:
            db.execute("INSERT INTO carry_targets VALUES (?,?,?,?,?,NULL)", (ts, s, side[s], scores[s], rank[s]))
        bot.event(db, "carry_targets", rebalance_ts=ts, blocked=blocked, targets=side, scores=scores,
                  set_ms=now_ms)
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
            if _zero_seen.get(s):
                result[s] = "position_unconfirmed"       # no orders on a leg whose read is in doubt
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
        record = dict(rebalance_ts=ts, score=tgt["score"], rank=tgt["rank"], target=tgt["side"], policy=POLICY,
                      notional=tgt["notional"], adaptive=tgt["notional"] is not None)
        target_usdt = abs(Decimal(tgt["notional"])) if tgt["notional"] is not None else LEG_NOTIONAL
        exit_sent = _intent_exists(db, client_id(s, ts, "x"))
        resize = (pos["phase"] == tgt["side"] and pos["phase"] != "flat" and tgt["notional"] is not None
                  and not exit_sent and not _intent_exists(db, client_id(s, ts, "e"))
                  and abs(Decimal(pos["qty"]) * Decimal(pos["entry_price"]) - target_usdt)
                  > RESIZE_TOLERANCE * target_usdt)
        if (pos["phase"] != "flat" and pos["phase"] != tgt["side"]) or resize:
            if exit_sent:
                raise bot.Halt(f"{s} still {pos['phase']} after this rebalance's exit")
            side = "SELL" if pos["phase"] == "long" else "BUY"
            result[s] = _send(db, client, pos, ts, now_ms, "exit", side, Decimal(pos["qty"]), record,
                              "resize" if resize else f"rebalance_to_{tgt['side']}", ref=client.mark(s))
        elif pos["phase"] == "flat" and tgt["side"] != "flat" and not _intent_exists(db, client_id(s, ts, "e")):
            if now_ms - _book_set_ms(db, ts) > MAX_LATE_MS:
                result[s] = "entry_window_passed"
                continue
            rules, mark = client.rules(s), client.mark(s)
            qty = floor_step(target_usdt / mark, rules["step"])
            if qty < rules["min_qty"] or qty * mark < rules["min_notional"]:
                result[s] = "below_exchange_minimum"
                continue
            result[s] = _send(db, client, pos, ts, now_ms, "entry", "BUY" if tgt["side"] == "long" else "SELL",
                              qty, record, f"carry_{tgt['side']}", ref=mark)
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
                adaptive=ADAPTIVE,
                model=_model_status(db),
                targets=[dict(r) for r in db.execute("SELECT symbol, side, score, rank, notional FROM carry_targets "
                                                     "WHERE rebalance_ts=? ORDER BY rank", (ts,))] if ts else [],
                positions=[dict(r) for r in db.execute("SELECT symbol, phase, qty, entry_price, stop_price, pending_id "
                                                       "FROM positions WHERE symbol IN (%s)" % ",".join("?" * len(UNIVERSE)),
                                                       UNIVERSE)],
                trades=trades, total_gross_pnl=str(total))


def _model_status(db):
    rows = db.execute("SELECT rebalance_ts, weights, cost, fills, scale, window FROM carry_model "
                      "ORDER BY rebalance_ts DESC LIMIT 2").fetchall()
    if not rows:
        return None
    cur = dict(rows[0])
    cur.update(weights=json.loads(cur["weights"]), window=json.loads(cur["window"]))
    if len(rows) > 1:
        cur["previous_weights"] = json.loads(rows[1]["weights"])
    return cur


def write_status(mode, result):
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
        mode_text = (f"ADAPTIVE ensemble of {len(carry_adaptive.CONFIGS)} configs, weights re-learned weekly "
                     f"from the last {carry_adaptive.WINDOW_WEEKS} weeks + live fill costs, gross "
                     f"{carry_adaptive.GROSS_USDT} USDT" if ADAPTIVE else
                     f"short top-{K} / long bottom-{K} {LOOKBACK_DAYS}d funding, {LEG_NOTIONAL} USDT per leg")
        print(f"CARRY ON ({universe.name()}: {', '.join(UNIVERSE)}): {mode_text}, weekly (Monday 00:00 UTC), "
              "virtual money only")
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
