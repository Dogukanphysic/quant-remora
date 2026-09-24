"""Unified BTC+ETH USD-M Futures bot (Testnet / paper only).

Harmonizes the two local systems:
  * from the BTC Futures mainnet worker: isolated margin, exchange-side stop,
    one-shot mutations, deterministic client ids, intent persisted before POST;
  * from the ETH Spot Testnet worker: fail-closed reconciliation, a learner
    that only gains (veto) authority after a forward-only gate.
The trading rule is the pre-registered research selection (strategy.py).
There is no mainnet path in this package.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import traceback
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from . import learner, strategy
from .exchange import ApiError, TransportError, floor_step

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
LEVERAGE = 2
ALLOCATION_USDT = Decimal("1000")          # max notional per symbol
DAILY_LOSS_LIMIT = Decimal("0.03")         # of total allocation, blocks new entries
MAX_DRAWDOWN = Decimal("0.15")             # from bot peak wallet, blocks new entries
MAX_SIGNAL_AGE_MS = 30 * 60 * 1000
POLICY = f"remora-{strategy.STRATEGY_ID}"


class Halt(RuntimeError):
    pass


def ledger_path(mode):
    return STATE / f"remora-bot-{mode}.sqlite3"


LEARNING_DB = STATE / "remora-bot-learning.sqlite3"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS bot(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT, environment TEXT,
        halted TEXT, day TEXT, day_start_wallet TEXT, peak_wallet TEXT, entries_blocked TEXT, updated_ms INTEGER);
    CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, phase TEXT, qty TEXT, entry_price TEXT,
        entry_bar INTEGER, stop_id TEXT, stop_price TEXT, last_bar INTEGER, pending_id TEXT);
    CREATE TABLE IF NOT EXISTS intents(client_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, kind TEXT, qty TEXT,
        created_ms INTEGER, status TEXT, response TEXT);
    CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, symbol TEXT, bar_ts INTEGER, created_ms INTEGER,
        signal TEXT, prediction REAL, model_authority INTEGER, action TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, symbol TEXT, entry_bar INTEGER, entry_price TEXT,
        exit_ms INTEGER, exit_price TEXT, qty TEXT, exit_reason TEXT, gross_pnl TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, payload TEXT);
    """)
    for s in SYMBOLS:
        db.execute("INSERT OR IGNORE INTO positions(symbol, phase, qty) VALUES (?, 'flat', '0')", (s,))
    db.execute("INSERT OR IGNORE INTO bot(id) VALUES (1)")
    db.commit()
    return db


def event(db, kind, **payload):
    db.execute("INSERT INTO events(ts, kind, payload) VALUES (?,?,?)",
               (int(time.time() * 1000), kind, json.dumps(payload, default=str)))


def client_id(symbol, bar_ts, kind):
    digest = hashlib.sha256(f"{POLICY}|{symbol}|{bar_ts}|{kind}".encode()).hexdigest()[:10]
    return f"rmb-{symbol[:3].lower()}-{kind}-{bar_ts // 1000}-{digest}"[:36]


def bind_account(db, client):
    row = db.execute("SELECT identity, environment FROM bot WHERE id=1").fetchone()
    if row["identity"] is None:
        with db:
            db.execute("UPDATE bot SET identity=?, environment=? WHERE id=1", (client.identity, client.environment))
    elif row["identity"] != client.identity or row["environment"] != client.environment:
        raise Halt("ledger is bound to another account/environment")


def startup(db, client):
    bind_account(db, client)
    if not client.one_way_mode():
        raise Halt("account must be in One-Way position mode")
    for s in SYMBOLS:
        pos = db.execute("SELECT phase FROM positions WHERE symbol=?", (s,)).fetchone()
        qty, _ = client.position(s)
        if pos["phase"] == "flat" and qty == 0 and not client.open_orders(s):
            client.configure(s, LEVERAGE)


def update_risk(db, client, now_ms):
    wallet, _ = client.wallet()
    day = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    row = db.execute("SELECT * FROM bot WHERE id=1").fetchone()
    day_start = Decimal(row["day_start_wallet"]) if row["day"] == day else wallet
    peak = max(Decimal(row["peak_wallet"] or wallet), wallet)
    total = ALLOCATION_USDT * len(SYMBOLS)
    blocked = None
    if day_start - wallet >= total * DAILY_LOSS_LIMIT:
        blocked = "daily_loss_limit"
    elif peak - wallet >= total * MAX_DRAWDOWN:
        blocked = "max_drawdown_limit"      # permanent until reviewed
    if row["entries_blocked"] == "max_drawdown_limit":
        blocked = row["entries_blocked"]
    with db:
        db.execute("UPDATE bot SET day=?, day_start_wallet=?, peak_wallet=?, entries_blocked=?, updated_ms=? WHERE id=1",
                   (day, str(day_start), str(peak), blocked, now_ms))
    return blocked


def reconcile_pending(db, client, pos, now_ms):
    intent = db.execute("SELECT * FROM intents WHERE client_id=?", (pos["pending_id"],)).fetchone()
    try:
        order = client.order(pos["symbol"], intent["client_id"])
    except ApiError as exc:
        if exc.code == -2013 and now_ms - intent["created_ms"] > 120_000:
            with db:
                db.execute("UPDATE intents SET status='absent' WHERE client_id=?", (intent["client_id"],))
                db.execute("UPDATE positions SET pending_id=NULL WHERE symbol=?", (pos["symbol"],))
                event(db, "intent_absent", client_id=intent["client_id"])
            return
        raise
    if order["status"] in ("NEW", "PARTIALLY_FILLED"):
        return
    filled = Decimal(order.get("executedQty", "0"))
    price = Decimal(order.get("avgPrice", "0") or "0")
    with db:
        db.execute("UPDATE intents SET status=?, response=? WHERE client_id=?",
                   (order["status"], json.dumps(order), intent["client_id"]))
        db.execute("UPDATE positions SET pending_id=NULL WHERE symbol=?", (pos["symbol"],))
    if filled == 0:
        return
    if intent["kind"] == "entry":
        on_entry_fill(db, client, pos["symbol"], filled, price)
    else:
        close_trade(db, pos["symbol"], price, now_ms, "strategy_exit")


def on_entry_fill(db, client, symbol, qty, price):
    pos = db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    exit_channel = json.loads(db.execute(
        "SELECT signal FROM decisions WHERE symbol=? AND action='enter' ORDER BY id DESC LIMIT 1",
        (symbol,)).fetchone()[0])["exit_channel"]
    rules = client.rules(symbol)
    stop = floor_step(strategy.protective_stop(price, exit_channel), rules["tick"])
    stop_id = client_id(symbol, pos["last_bar"], "s")
    with db:
        db.execute("UPDATE positions SET phase='long', qty=?, entry_price=?, entry_bar=?, stop_id=?, stop_price=? "
                   "WHERE symbol=?", (str(qty), str(price), pos["last_bar"], stop_id, str(stop), symbol))
        event(db, "entry_filled", symbol=symbol, qty=qty, price=price, stop=stop)
    try:
        client.place_stop(symbol, qty, stop, stop_id)
    except Exception as exc:
        # An unprotected position is not allowed to remain open.
        exit_id = client_id(symbol, pos["last_bar"], "x")
        with db:
            db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,NULL)",
                       (exit_id, symbol, "SELL", "exit", str(qty), int(time.time() * 1000), "prepared"))
        client.market_order(symbol, "SELL", qty, exit_id, True)
        raise Halt(f"protective stop failed for {symbol}; emergency close sent: {exc}")


def close_trade(db, symbol, price, now_ms, reason):
    pos = db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    qty, entry = Decimal(pos["qty"]), Decimal(pos["entry_price"] or "0")
    with db:
        db.execute("INSERT INTO trades(symbol, entry_bar, entry_price, exit_ms, exit_price, qty, exit_reason, gross_pnl)"
                   " VALUES (?,?,?,?,?,?,?,?)", (symbol, pos["entry_bar"], str(entry), now_ms, str(price), str(qty),
                                                 reason, str((price - entry) * qty)))
        db.execute("UPDATE positions SET phase='flat', qty='0', entry_price=NULL, entry_bar=NULL, stop_id=NULL, "
                   "stop_price=NULL WHERE symbol=?", (symbol,))
        event(db, "trade_closed", symbol=symbol, reason=reason, price=price)


def verify_exchange(db, client, pos, now_ms):
    symbol = pos["symbol"]
    qty, _ = client.position(symbol)
    orders = set(client.open_orders(symbol))
    if pos["phase"] == "long":
        if qty == 0:
            close_trade(db, symbol, Decimal(pos["stop_price"]), now_ms, "exchange_stop")
            return db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        if qty != Decimal(pos["qty"]):
            raise Halt(f"{symbol} position mismatch: ledger {pos['qty']} exchange {qty}")
        if pos["stop_id"] not in orders:
            raise Halt(f"{symbol} protective stop missing")
        orders.discard(pos["stop_id"])
    elif qty != 0:
        raise Halt(f"{symbol} untracked exchange position {qty}")
    if orders:
        raise Halt(f"{symbol} untracked open orders {sorted(orders)[:3]}")
    return pos


def learn(ldb, symbol, bars, now_ms):
    with ldb:
        learner.ingest(ldb, symbol, bars, now_ms, "observed")
        learner.train(ldb, now_ms)
        prediction = learner.register_prediction(ldb, symbol, bars[-1].ts, now_ms)
    return prediction, learner.gate(ldb)


def step_symbol(db, ldb, client, market, symbol, now_ms, blocked):
    pos = db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    if pos["pending_id"]:
        reconcile_pending(db, client, pos, now_ms)   # query only; never re-POST
        return "reconciling"
    pos = verify_exchange(db, client, pos, now_ms)
    bars = [b for b in market.closed_bars(symbol) if b.ts + strategy.STEP_MS <= now_ms]
    prediction, gate = learn(ldb, symbol, bars, now_ms)
    sig = strategy.evaluate(bars)
    if pos["last_bar"] is not None and sig.bar_ts <= pos["last_bar"]:
        return "waiting"
    authority = bool(gate["authority"])
    action, reason = "hold", "no_signal"
    if now_ms - (sig.bar_ts + strategy.STEP_MS) > MAX_SIGNAL_AGE_MS:
        reason = "stale_bar"
    elif pos["phase"] == "long" and sig.breakdown:
        action, reason = "exit", "close_below_exit_channel"
    elif pos["phase"] == "flat" and sig.breakout:
        if blocked:
            reason = f"entries_blocked:{blocked}"
        elif authority and prediction is not None and prediction <= 0:
            reason = "model_veto"
        elif sig.vol_scale <= 0:
            reason = "zero_vol_scale"
        else:
            action, reason = "enter", "close_above_entry_channel"
    kind = {"enter": "e", "exit": "x"}.get(action)
    cid = client_id(symbol, sig.bar_ts, kind) if kind else None
    qty = None
    if action == "enter":
        rules, mark = client.rules(symbol), client.mark(symbol)
        qty = floor_step(ALLOCATION_USDT * Decimal(str(sig.vol_scale)) / mark, rules["step"])
        if qty < rules["min_qty"] or qty * mark < rules["min_notional"]:
            action, reason, cid = "hold", "below_exchange_minimum", None
    elif action == "exit":
        qty = Decimal(pos["qty"])
    with db:
        db.execute("INSERT INTO decisions(symbol, bar_ts, created_ms, signal, prediction, model_authority, action, "
                   "reason) VALUES (?,?,?,?,?,?,?,?)", (symbol, sig.bar_ts, now_ms, json.dumps(sig.__dict__),
                                                        prediction, int(authority), action, reason))
        db.execute("UPDATE positions SET last_bar=? WHERE symbol=?", (sig.bar_ts, symbol))
        if cid:
            db.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,NULL)",
                       (cid, symbol, "BUY" if action == "enter" else "SELL", "entry" if action == "enter" else "exit",
                        str(qty), now_ms, "prepared"))
            db.execute("UPDATE positions SET pending_id=? WHERE symbol=?", (cid, symbol))
    if not cid:
        return reason
    if action == "exit" and pos["stop_id"]:
        try:
            client.cancel_stop(pos["stop_id"])
        except ApiError as exc:
            if exc.code not in (-2011, -2013):
                raise
    try:
        client.market_order(symbol, "BUY" if action == "enter" else "SELL", qty, cid, action == "exit")
    except TransportError:
        pass            # outcome unknown: reconciled by GET on the next tick, never re-POSTed
    pos = db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
    reconcile_pending(db, client, pos, now_ms)
    return action


def tick(db, ldb, client, market, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    if db.execute("SELECT halted FROM bot WHERE id=1").fetchone()["halted"]:
        return {"halted": True}
    if hasattr(client, "check_stops"):
        client.check_stops()
    blocked = update_risk(db, client, now_ms)
    return {s: step_symbol(db, ldb, client, market, s, now_ms, blocked) for s in SYMBOLS}


def halt(db, reason):
    with db:
        db.execute("UPDATE bot SET halted=? WHERE id=1", (reason,))
        event(db, "halt", reason=reason)


@contextmanager
def singleton():
    import msvcrt
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "remora-bot.lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def run(mode, client, market, poll_seconds=30, max_cycles=None):
    with singleton():
        db, ldb = connect(ledger_path(mode)), learner.connect(LEARNING_DB)
        startup(db, client)
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                result = tick(db, ldb, client, market)
                write_status(mode, db, ldb, result)
                if result.get("halted"):
                    print("HALTED - inspect status; no automatic restart.")
                    return 1
            except (TransportError, ApiError) as exc:
                event(db, "transient_error", error=str(exc))
                db.commit()
            except Halt as exc:
                halt(db, str(exc))
                write_status(mode, db, ldb, {"halted": True})
                return 1
            except Exception as exc:
                halt(db, f"{type(exc).__name__}: {exc}")
                event(db, "traceback", text=traceback.format_exc())
                db.commit()
                return 1
            cycles += 1
            time.sleep(poll_seconds)
    return 0


def status(mode):
    db, ldb = connect(ledger_path(mode)), learner.connect(LEARNING_DB)
    bot = dict(db.execute("SELECT halted, environment, entries_blocked, peak_wallet, updated_ms FROM bot").fetchone())
    return dict(mode=mode, strategy=strategy.STRATEGY_ID, bot=bot,
                positions=[dict(r) for r in db.execute("SELECT symbol, phase, qty, entry_price, stop_price, last_bar, "
                                                       "pending_id FROM positions")],
                last_decisions=[dict(r) for r in db.execute("SELECT symbol, bar_ts, action, reason, prediction, "
                                                            "model_authority FROM decisions ORDER BY id DESC LIMIT 6")],
                trades=[dict(r) for r in db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 10")],
                learning=learner.status(ldb), real_money=False)


def write_status(mode, db, ldb, result):
    payload = status(mode)
    payload.update(last_tick=result, written_ms=int(time.time() * 1000), pid=os.getpid())
    (STATE / f"remora-bot-{mode}-status.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
