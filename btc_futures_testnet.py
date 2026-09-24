"""BTCUSDT USD-M Futures testnet worker with outcome learning.

Execution reuses the audited isolated-2x engine, but credentials, REST host,
execution ledger and learning ledger are strictly separate from mainnet.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import btc_futures_live as engine


ROOT = Path(__file__).resolve().parent
DB = ROOT / "state/btc-futures-testnet.sqlite3"
LEARNING_DB = ROOT / "state/btc-futures-testnet-learning.sqlite3"
LOCK = ROOT / "state/btc-futures-testnet.lock"
CONFIRM = "BTC FUTURES TESTNET AJANINI BASLAT"
BASES = {
    "testnet": "https://testnet.binancefuture.com",
    "demo": "https://demo-fapi.binance.com",
}


class TestnetClient(engine.Client):
    def __init__(self, *, enabled=False, environment="testnet"):
        if not enabled or os.getenv("BTC_FUTURES_TESTNET_AUTHORIZATION") != CONFIRM:
            raise ValueError("Local explicit Futures testnet authorization required")
        if environment not in BASES:
            raise ValueError("Unsupported Futures test environment")
        self.BASE = BASES[environment]
        self.environment = environment
        self.key = os.environ.get("BTC_FUTURES_TESTNET_API_KEY", "")
        self.secret = os.environ.get("BTC_FUTURES_TESTNET_SECRET_KEY", "")
        if not self.key or not self.secret:
            raise ValueError("Missing local Futures testnet credentials")
        self.identity = hashlib.sha256(
            (environment + ":" + self.key).encode()).hexdigest()
        self.opener = engine.build_opener(engine.NoRedirect())


def connect_learning(path=LEARNING_DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS samples(
        id INTEGER PRIMARY KEY, entry_bar INTEGER UNIQUE, regime TEXT,
        rsi REAL, entry_wallet_usdt TEXT, close_wallet_usdt TEXT,
        pnl_usdt TEXT, label INTEGER, opened_ts REAL, closed_ts REAL)""")
    db.commit()
    return db


def learning_snapshot(db):
    rows = db.execute("""SELECT regime,COUNT(*),
        SUM(CASE WHEN label=1 THEN 1 ELSE 0 END),AVG(CAST(pnl_usdt AS REAL))
        FROM samples WHERE label IS NOT NULL GROUP BY regime""").fetchall()
    open_count = db.execute(
        "SELECT COUNT(*) FROM samples WHERE label IS NULL").fetchone()[0]
    closed = sum(int(row[1]) for row in rows)
    regimes = {
        (row[0] or "unknown"): {
            "closed": int(row[1]), "wins": int(row[2] or 0),
            "mean_pnl_usdt": float(row[3] or 0),
        } for row in rows
    }
    return {
        "status": "adaptive" if closed >= 3 else "collecting",
        "closed_labels": closed, "open_labels": int(open_count),
        "minimum_labels_for_adaptation": 3,
        "regimes": regimes, "decision_authority": True,
    }


def adaptive_permission(learning, regime, bar):
    stats = learning["regimes"].get(regime or "unknown")
    if not stats or stats["closed"] < 3:
        return True, "development_collection"
    if stats["mean_pnl_usdt"] > 0:
        return True, "positive_regime_expectancy"
    # Keep one deterministic exploration opportunity in five base signals.
    explore = (int(bar) // 900_000) % 5 == 0
    return explore, "controlled_exploration" if explore else "negative_regime_filtered"


class LearningClient(TestnetClient):
    def __init__(self, learning_db, **kwargs):
        super().__init__(**kwargs)
        self.learning_db = learning_db
        self.latest_learning_decision = None

    def market(self):
        market = super().market()
        band = dict(market["band"])
        snapshot = learning_snapshot(self.learning_db)
        allowed, reason = adaptive_permission(
            snapshot, band.get("entry_regime"), market["bar"])
        base_enter = bool(band.get("enter"))
        band["enter"] = base_enter and allowed
        self.latest_learning_decision = {
            "base_enter": base_enter, "allowed": allowed, "reason": reason,
            "regime": band.get("entry_regime"), "bar": market["bar"],
        }
        market["band"] = band
        return market


def sync_learning(db, before, after, now=None):
    now = time.time() if now is None else now
    before_phase = before.get("phase") if before else None
    after_phase = after.get("phase")
    if before_phase == "cash" and after_phase == "long":
        decision = after.get("decision") or {}
        db.execute("""INSERT OR IGNORE INTO samples(
            entry_bar,regime,rsi,entry_wallet_usdt,opened_ts)
            VALUES(?,?,?,?,?)""", (
                int(decision.get("bar") or after.get("last_bar")),
                decision.get("entry_regime"), decision.get("rsi"),
                str(after.get("wallet_usdt")), now))
    elif before_phase == "long" and after_phase == "cash":
        row = db.execute("""SELECT id,entry_wallet_usdt FROM samples
            WHERE label IS NULL ORDER BY id DESC LIMIT 1""").fetchone()
        if row:
            close_wallet = engine.dec(after["wallet_usdt"])
            pnl = close_wallet - engine.dec(row[1])
            db.execute("""UPDATE samples SET close_wallet_usdt=?,pnl_usdt=?,
                label=?,closed_ts=? WHERE id=?""",
                       (str(close_wallet), str(pnl), int(pnl > 0), now, row[0]))
    db.commit()
    return learning_snapshot(db)


def enrich_state(execution_db, learning_db, client, state):
    state["environment"] = "binance_usdm_futures_" + client.environment
    state["learning"] = learning_snapshot(learning_db)
    state["learning"]["latest_decision"] = client.latest_learning_decision
    with execution_db:
        engine.write(execution_db, state)
    return state


def configure(execution_db, learning_db, client, margin, use_all):
    state = engine.configure(execution_db, client, margin, use_all)
    return enrich_state(execution_db, learning_db, client, state)


def tick(execution_db, learning_db, client, margin, use_all):
    before = engine.read(execution_db)
    state = engine.tick(execution_db, client, margin, use_all)
    sync_learning(learning_db, before, state, state.get("last_poll"))
    return enrich_state(execution_db, learning_db, client, state)


def public_status():
    status = engine.public_status(DB)
    if LEARNING_DB.exists():
        learning_db = sqlite3.connect(
            LEARNING_DB.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            status["learning"] = learning_snapshot(learning_db)
        finally:
            learning_db.close()
    return status


def main():
    # The mainnet launcher's explicit leverage option must not leak into this
    # separate, currently inactive Futures testnet contract.
    if engine.LEVERAGE != 4:
        raise ValueError("Futures testnet requires BTC_FUTURES_LEVERAGE=4")
    if engine.MODEL_DECISIONS:
        raise ValueError("Futures testnet requires BTC_FUTURES_MODEL_DECISIONS=0")
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "diagnose", "configure",
                                           "order-check", "run"])
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--environment", choices=sorted(BASES), default="testnet")
    parser.add_argument("--margin-usdt", type=Decimal, default=Decimal("1000"))
    parser.add_argument("--use-all-available-balance", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(public_status(), indent=2, ensure_ascii=False))
        return 0
    margin = engine.positive(args.margin_usdt, "margin_usdt")
    if not 15 <= args.poll_seconds <= 300:
        raise ValueError("poll-seconds must be between 15 and 300")
    learning_db = connect_learning()
    try:
        client = LearningClient(learning_db, enabled=args.enabled,
                                environment=args.environment)
        if args.action == "diagnose":
            report = engine.diagnose(client, margin,
                                     args.use_all_available_balance)
            report["environment"] = client.environment
            report["learning"] = learning_snapshot(learning_db)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        if args.action == "order-check":
            report = engine.order_check(client, margin,
                                         args.use_all_available_balance)
            report["environment"] = client.environment
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        with engine.singleton(LOCK):
            execution_db = engine.connect(DB)
            try:
                if args.action == "configure":
                    state = configure(execution_db, learning_db, client, margin,
                                      args.use_all_available_balance)
                    safe = dict(state)
                    safe.pop("identity", None)
                    print(json.dumps(safe, indent=2, ensure_ascii=False))
                    return 0
                if engine.read(execution_db) is None:
                    configure(execution_db, learning_db, client, margin,
                              args.use_all_available_balance)
                while True:
                    state = tick(execution_db, learning_db, client, margin,
                                 args.use_all_available_balance)
                    if state.get("halted"):
                        raise ValueError(f"HALTED: {state['halted']}")
                    time.sleep(args.poll_seconds)
            finally:
                execution_db.close()
    finally:
        learning_db.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
