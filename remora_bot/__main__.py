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


def seed_v2(bar_hours=4):
    """Train the v2 model on the verified 5y BTC+ETH history (same features as the walk-forward study)."""
    import learner_v2_research as research
    from . import model_v2
    research.configure(bar_hours)          # also configures model_v2 for the same interval
    data = research.load_history()
    db = model_v2.connect(bot.model_db_path(bar_hours))
    now_ms = int(time.time() * 1000)
    with db:
        for symbol, other in (("BTCUSDT", "ETHUSDT"), ("ETHUSDT", "BTCUSDT")):
            model_v2.ingest(db, research.build(symbol, other, data), now_ms, "historical")
        model_v2.train(db, now_ms)
    print(json.dumps(model_v2.status(db), indent=2))


def keycheck():
    """Read-only: which virtual-money environment accepts the key? Never contacts mainnet, never orders."""
    import hashlib
    import hmac
    from urllib.parse import urlencode
    from urllib.request import Request, build_opener
    from .exchange import KEY_ENV, SECRET_ENV, ApiError, _NoRedirect, _open
    key, secret = os.environ.get(KEY_ENV, "").strip(), os.environ.get(SECRET_ENV, "").strip()
    if not key or not secret:
        raise SystemExit("key/secret missing")
    opener = build_opener(_NoRedirect())
    targets = {
        "Futures Demo Trading (demo-fapi.binance.com)": ("https://demo-fapi.binance.com", "/fapi/v1/time", "/fapi/v2/account"),
        "Futures Testnet (testnet.binancefuture.com)": ("https://testnet.binancefuture.com", "/fapi/v1/time", "/fapi/v2/account"),
        "Spot Testnet (testnet.binance.vision) - bu botta CALISMAZ": ("https://testnet.binance.vision", "/api/v3/time", "/api/v3/account"),
    }
    for name, (base, time_path, account_path) in targets.items():
        try:
            server = _open(opener, Request(base + time_path))["serverTime"]
            query = urlencode(dict(timestamp=server, recvWindow=10000))
            query += "&signature=" + hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            _open(opener, Request(f"{base}{account_path}?{query}", headers={"X-MBX-APIKEY": key}))
            result = "KABUL EDILDI"
        except ApiError as exc:
            result = f"reddedildi (Binance code {exc.code})"
        except Exception as exc:
            result = f"baglanti hatasi ({type(exc).__name__})"
        print(f"{name}: {result}")


def positions():
    """Read-only: exchange positions and open orders for the exploration universe. Sends no orders."""
    from .explore import UNIVERSE
    client = TestnetClient()
    print(f"Ortam: {client.environment} (salt okunur; emir gonderilmez)")
    found = False
    for s in UNIVERSE:
        qty, entry = client.position(s)
        orders = client.open_orders(s)
        if qty != 0 or orders:
            found = True
            side = "LONG" if qty > 0 else "SHORT" if qty < 0 else "-"
            print(f"  {s}: pozisyon {side} {abs(qty)} (giris {entry}), acik emir/stop: {orders or 'yok'}")
    if not found:
        print("  10 coinde acik pozisyon veya emir yok.")
    import datetime as dt
    tz = dt.timezone(dt.timedelta(hours=3))
    print("\nSon emirler (clientOrderId oneki kaynagi gosterir: rmb-=bu bot, web_=site, android_/ios_=mobil):")
    for s in UNIVERSE:
        for o in client.order_history(s, limit=5):
            if o.get("executedQty") in (None, "0", "0.0", "0.000"):
                continue
            t = dt.datetime.fromtimestamp(o["time"] / 1000, tz).strftime("%d.%m %H:%M")
            print(f"  {t} {s} {o['side']:4s} {o.get('origType', o['type']):12s} qty={o['executedQty']:>10s} "
                  f"reduceOnly={o.get('reduceOnly')} clientOrderId={o['clientOrderId']}")


def clear_halt(mode):
    from .explore import UNIVERSE
    client = TestnetClient()
    db = bot.connect(bot.ledger_path(mode))
    try:
        print(bot.clear_halt(db, client, UNIVERSE))
        return 0
    except bot.Halt as exc:
        print(f"NOT CLEARED: {exc}")
        return 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="remora_bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    sub.add_parser("keycheck")
    sub.add_parser("positions")
    sub.add_parser("clear-halt")
    sv = sub.add_parser("seed-v2")
    sv.add_argument("--interval", choices=("4h", "1h"), default="4h")
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=("paper", "testnet"), required=True)
    r.add_argument("--interval", choices=("4h", "1h"), default=os.environ.get("REMORA_INTERVAL", "4h"))
    r.add_argument("--poll-seconds", type=float, default=30)
    r.add_argument("--max-cycles", type=int)
    s = sub.add_parser("status")
    s.add_argument("--mode", choices=("paper", "testnet"), default="paper")
    args = p.parse_args(argv)
    if args.cmd == "seed":
        return seed()
    if args.cmd == "keycheck":
        return keycheck()
    if args.cmd == "positions":
        return positions()
    if args.cmd == "clear-halt":
        return clear_halt("testnet")
    if args.cmd == "seed-v2":
        return seed_v2(int(args.interval.rstrip("h")))
    if args.cmd == "status":
        print(json.dumps(bot.status(args.mode), indent=2, default=str))
        return 0
    bot.configure_interval(int(args.interval.rstrip("h")))
    market = PublicMarketData()
    if args.mode == "testnet":
        if os.environ.get("REMORA_TESTNET_CONFIRM") != CONFIRM:
            raise SystemExit("Testnet run requires REMORA_TESTNET_CONFIRM set by the launcher")
        client = TestnetClient()
    else:
        client = PaperClient(bot.STATE / "remora-bot-paper-exchange.sqlite3", market)
    print(f"remora_bot {args.mode}: {strategy.STRATEGY_ID}, interval={strategy.INTERVAL}, "
          f"symbols={bot.SYMBOLS}, real_money=False")
    return bot.run(args.mode, client, market, args.poll_seconds, args.max_cycles)


if __name__ == "__main__":
    sys.exit(main())
