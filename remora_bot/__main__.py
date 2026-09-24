"""python -m remora_bot {seed,run,status}  (Testnet / paper only)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import bot, learner, strategy
from .exchange import PaperClient, PublicMarketData, TestnetClient

CONFIRM = "REMORA BTC ETH FUTURES TESTNET BASLAT"


def seed():
    import pandas as pd
    ldb = learner.connect(bot.LEARNING_DB)
    now_ms = int(time.time() * 1000)
    report = {}
    for symbol in bot.SYMBOLS:
        k = pd.read_csv(bot.ROOT / f"data/binance-um-{symbol.lower()}-15m-5y.csv")
        k.index = pd.to_datetime(k["ts"], unit="ms", utc=True)
        g = k.resample("4h", label="left", closed="left")
        h = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                          "close": g["close"].last(), "volume": g["volume"].sum(), "n": g["close"].count()})
        h = h[h["n"] == 16]
        bars = [strategy.Bar(int(t.value // 10**6), r.open, r.high, r.low, r.close, r.volume) for t, r in h.iterrows()]
        with ldb:
            learner.ingest(ldb, symbol, bars, now_ms, "historical")
        report[symbol] = len(bars)
    with ldb:
        learner.train(ldb, now_ms)
    print(json.dumps(dict(seeded_bars=report, learning=learner.status(ldb)), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="remora_bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=("paper", "testnet"), required=True)
    r.add_argument("--poll-seconds", type=float, default=30)
    r.add_argument("--max-cycles", type=int)
    s = sub.add_parser("status")
    s.add_argument("--mode", choices=("paper", "testnet"), default="paper")
    args = p.parse_args(argv)
    if args.cmd == "seed":
        return seed()
    if args.cmd == "status":
        print(json.dumps(bot.status(args.mode), indent=2, default=str))
        return 0
    market = PublicMarketData()
    if args.mode == "testnet":
        if os.environ.get("REMORA_TESTNET_CONFIRM") != CONFIRM:
            raise SystemExit("Testnet run requires REMORA_TESTNET_CONFIRM set by the launcher")
        client = TestnetClient()
    else:
        client = PaperClient(bot.STATE / "remora-bot-paper-exchange.sqlite3", market)
    print(f"remora_bot {args.mode}: {strategy.STRATEGY_ID}, symbols={bot.SYMBOLS}, real_money=False")
    return bot.run(args.mode, client, market, args.poll_seconds, args.max_cycles)


if __name__ == "__main__":
    sys.exit(main())
