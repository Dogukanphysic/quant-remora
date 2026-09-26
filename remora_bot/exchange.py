"""Exchange access for the unified bot.

* PublicMarketData  - mainnet USD-M public klines (signals and learning use real market prices).
* TestnetClient     - signed USD-M Futures Testnet client.  The host is fixed; there is no
                      mainnet code path.  Mutations are sent once and never retried.
* PaperClient       - same interface, simulated fills at public prices; needs no keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import strategy
from .strategy import Bar

PUBLIC_BASE = "https://fapi.binance.com"
TESTNET_BASE = "https://testnet.binancefuture.com"
# Both are virtual-money environments; keys from binance.com "Demo Trading" only work on demo-fapi.
TEST_HOSTS = {"testnet": TESTNET_BASE, "demo": "https://demo-fapi.binance.com"}
KEY_ENV, SECRET_ENV = "REMORA_FUTURES_TESTNET_API_KEY", "REMORA_FUTURES_TESTNET_SECRET_KEY"
HOST_ENV = "REMORA_FUTURES_TEST_ENV"


class ApiError(RuntimeError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class TransportError(RuntimeError):
    """Outcome unknown for mutations; safe to retry only for reads."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("API redirect refused")


def _open(opener, request):
    try:
        with opener.open(request, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        code = None
        try:
            body = json.loads(exc.read(8192))
            if isinstance(body, dict) and type(body.get("code")) is int:
                code = body["code"]
        except Exception:
            pass
        finally:
            exc.close()
        raise ApiError(f"HTTP {exc.code} {request.get_method()} {request.full_url.split('?')[0]}"
                       + (f"; Binance code {code}" if code is not None else ""), exc.code, code) from None
    except Exception:
        raise TransportError(f"transport failure {request.get_method()} {request.full_url.split('?')[0]}") from None


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class PublicMarketData:
    def __init__(self, base=PUBLIC_BASE):
        self.base = base
        self.opener = build_opener(_NoRedirect())

    def closed_bars(self, symbol: str, limit: int | None = None) -> list[Bar]:
        # 1h needs > 720 bars for 30-day windows; Binance allows up to 1500.
        limit = limit or (1000 if strategy.INTERVAL == "4h" else 1500)
        url = f"{self.base}/fapi/v1/klines?" + urlencode(dict(symbol=symbol, interval=strategy.INTERVAL, limit=limit))
        raw = _open(self.opener, Request(url))
        now = int(_open(self.opener, Request(f"{self.base}/fapi/v1/time"))["serverTime"])
        return [Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                for r in raw if int(r[6]) < now]

    def funding(self, symbol: str, limit: int = 1000):
        import pandas as pd
        url = f"{self.base}/fapi/v1/fundingRate?" + urlencode(dict(symbol=symbol, limit=limit))
        rows = _open(self.opener, Request(url))
        return pd.Series([float(r["fundingRate"]) for r in rows],
                         index=pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms", utc=True))


class TestnetClient:
    ALLOWED = {
        ("GET", "/fapi/v1/time"), ("GET", "/fapi/v1/exchangeInfo"), ("GET", "/fapi/v1/premiumIndex"),
        ("GET", "/fapi/v1/positionSide/dual"), ("GET", "/fapi/v2/account"), ("GET", "/fapi/v2/positionRisk"),
        ("GET", "/fapi/v1/openOrders"), ("GET", "/fapi/v1/openAlgoOrders"), ("GET", "/fapi/v1/order"),
        ("GET", "/fapi/v1/algoOrder"), ("GET", "/fapi/v1/ticker/bookTicker"), ("DELETE", "/fapi/v1/order"),
        ("GET", "/fapi/v1/allOrders"),
        ("POST", "/fapi/v1/marginType"), ("POST", "/fapi/v1/leverage"), ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/algoOrder"), ("DELETE", "/fapi/v1/algoOrder"),
    }
    def __init__(self):
        choice = os.environ.get(HOST_ENV, "testnet")
        if choice not in TEST_HOSTS:
            raise ValueError(f"{HOST_ENV} must be one of {sorted(TEST_HOSTS)}")
        self.base = TEST_HOSTS[choice]
        self.environment = f"binance_usdm_futures_{choice}"
        self.key, self.secret = os.environ.get(KEY_ENV, "").strip(), os.environ.get(SECRET_ENV, "").strip()
        if not self.key or not self.secret:
            raise ValueError(f"Missing {KEY_ENV}/{SECRET_ENV} in the process environment")
        self.identity = hashlib.sha256(self.key.encode()).hexdigest()
        self.opener = build_opener(_NoRedirect())
        self._anchor = None

    MAX_SYNC_RTT = 2.5          # seconds; a fresh TLS handshake over a VPN alone takes ~0.6 s
    RECV_WINDOW = 10_000        # ms; covers MAX_SYNC_RTT plus request transit with margin

    def _timestamp(self):
        if self._anchor is None or time.monotonic() - self._anchor[1] > 60:
            started = time.monotonic()
            server = int(self._send("GET", "/fapi/v1/time", {}, False)["serverTime"])
            done = time.monotonic()
            if done - started > self.MAX_SYNC_RTT:
                raise TransportError("time sync RTT too high; signed request not sent")
            # serverTime was stamped before `done`, so advancing it from `done` is a lower bound
            # on server time: never ahead (Binance rejects >1 s ahead), at most one RTT behind.
            self._anchor = (server, done)
        return int(self._anchor[0] + (time.monotonic() - self._anchor[1]) * 1000)

    def _send(self, method, path, params, signed):
        if (method, path) not in self.ALLOWED:
            raise ValueError(f"operation not allowed: {method} {path}")
        values = dict(params)
        headers = {}
        if signed:
            values.update(timestamp=self._timestamp(), recvWindow=self.RECV_WINDOW)
        query = urlencode(values)
        if signed:
            query += "&signature=" + hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.key
        url = self.base + path
        if method != "POST" and query:
            url += "?" + query
        return _open(self.opener, Request(url, data=query.encode() if method == "POST" else None,
                                          headers=headers, method=method))

    def request(self, method, path, params=None, signed=False):
        try:
            return self._send(method, path, params or {}, signed)
        except ApiError as exc:
            if exc.code == -1021 and method == "GET":
                self._anchor = None
                return self._send(method, path, params or {}, signed)
            raise

    # --- reads -----------------------------------------------------------
    def rules(self, symbol):
        info = self.request("GET", "/fapi/v1/exchangeInfo")
        row = next(s for s in info["symbols"] if s["symbol"] == symbol)
        if row["status"] != "TRADING":
            raise ValueError(f"{symbol} not trading on Testnet")
        f = {x["filterType"]: x for x in row["filters"]}
        return dict(step=Decimal(f["MARKET_LOT_SIZE"]["stepSize"]), min_qty=Decimal(f["MARKET_LOT_SIZE"]["minQty"]),
                    tick=Decimal(f["PRICE_FILTER"]["tickSize"]), min_notional=Decimal(f["MIN_NOTIONAL"]["notional"]))

    def mark(self, symbol):
        return Decimal(self.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])

    def one_way_mode(self):
        return self.request("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"] is False

    def wallet(self):
        acct = self.request("GET", "/fapi/v2/account", signed=True)
        usdt = next(a for a in acct["assets"] if a["asset"] == "USDT")
        return Decimal(usdt["walletBalance"]), Decimal(usdt["availableBalance"])

    def position(self, symbol):
        rows = self.request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, True)
        row = next(r for r in rows if r["symbol"] == symbol and r.get("positionSide", "BOTH") == "BOTH")
        return Decimal(row["positionAmt"]), Decimal(row["entryPrice"])

    def open_orders(self, symbol):
        regular = self.request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, True)
        algos = self.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol, "algoType": "CONDITIONAL"}, True)
        return [r["clientOrderId"] for r in regular] + [r.get("clientAlgoId") for r in algos]

    def order(self, symbol, client_id):
        return self.request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, True)

    def stop_order(self, client_id):
        row = self.request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_id}, True)
        return dict(status=row.get("algoStatus"), client_id=row.get("clientAlgoId"))

    # --- mutations (sent once) ------------------------------------------
    def configure(self, symbol, leverage):
        try:
            self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, True)
        except ApiError as exc:
            if exc.code != -4046:          # already isolated
                raise
        result = self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, True)
        if int(result["leverage"]) != leverage:
            raise ValueError("leverage not confirmed")

    def market_order(self, symbol, side, qty, client_id, reduce_only):
        params = dict(symbol=symbol, side=side, type="MARKET", quantity=str(qty), newClientOrderId=client_id,
                      newOrderRespType="RESULT")
        if reduce_only:
            params["reduceOnly"] = "true"
        return self.request("POST", "/fapi/v1/order", params, True)

    def best_bid(self, symbol):
        return Decimal(self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})["bidPrice"])

    def limit_buy_post_only(self, symbol, qty, price, client_id):
        """GTX: rejected (EXPIRED) instead of taking liquidity, so an entry never pays taker fees."""
        return self.request("POST", "/fapi/v1/order", dict(
            symbol=symbol, side="BUY", type="LIMIT", timeInForce="GTX", quantity=str(qty), price=str(price),
            newClientOrderId=client_id, newOrderRespType="RESULT"), True)

    def cancel_order(self, symbol, client_id):
        return self.request("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id}, True)

    def place_stop(self, symbol, qty, trigger, client_id):
        return self.request("POST", "/fapi/v1/algoOrder", dict(
            algoType="CONDITIONAL", symbol=symbol, side="SELL", type="STOP_MARKET", quantity=str(qty),
            triggerPrice=str(trigger), reduceOnly="true", workingType="MARK_PRICE", clientAlgoId=client_id), True)

    def cancel_stop(self, client_id):
        return self.request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_id}, True)


class PaperClient:
    """Keyless simulation with the TestnetClient interface; fills at public mark +/- slippage."""
    environment = "paper_simulation"
    identity = "paper"
    FEE, SLIP = Decimal("0.0005"), Decimal("0.0002")

    def __init__(self, path: Path, market: PublicMarketData, start_usdt=Decimal("10000")):
        self.market = market
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("CREATE TABLE IF NOT EXISTS paper(k TEXT PRIMARY KEY, v TEXT)")
        if self.db.execute("SELECT 1 FROM paper WHERE k='wallet'").fetchone() is None:
            with self.db:
                self.db.execute("INSERT INTO paper VALUES ('wallet', ?)", (json.dumps(dict(usdt=str(start_usdt))),))

    def _get(self, k, default):
        row = self.db.execute("SELECT v FROM paper WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, k, v):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO paper VALUES (?,?)", (k, json.dumps(v)))

    def rules(self, symbol):
        return dict(step=Decimal("0.001"), min_qty=Decimal("0.001"), tick=Decimal("0.1"), min_notional=Decimal("100"))

    def mark(self, symbol):
        url = f"{self.market.base}/fapi/v1/premiumIndex?symbol={symbol}"
        return Decimal(_open(self.market.opener, Request(url))["markPrice"])

    def one_way_mode(self):
        return True

    def wallet(self):
        usdt = Decimal(self._get("wallet", {})["usdt"])
        return usdt, usdt

    def position(self, symbol):
        p = self._get(f"pos:{symbol}", dict(qty="0", entry="0"))
        return Decimal(p["qty"]), Decimal(p["entry"])

    def open_orders(self, symbol):
        stop = self._get(f"stop:{symbol}", None)
        return [stop["client_id"]] if stop else []

    def order(self, symbol, client_id):
        o = self._get(f"order:{client_id}", None)
        if o is None:
            raise ApiError("order not found", 400, -2013)
        if o["status"] == "NEW" and self.mark(symbol) <= Decimal(o["price"]):
            o = self._fill_limit(symbol, o)
        return o

    MAKER_FEE = Decimal("0.0002")

    def best_bid(self, symbol):
        return self.mark(symbol)

    def limit_buy_post_only(self, symbol, qty, price, client_id):
        if self._get(f"order:{client_id}", None):
            raise ApiError("duplicate client id", 400, -4116)
        order = dict(clientOrderId=client_id, symbol=symbol, side="BUY", status="NEW", executedQty="0",
                     avgPrice="0", price=str(price), origQty=str(qty))
        self._put(f"order:{client_id}", order)
        return order

    def _fill_limit(self, symbol, o):
        px, qty = Decimal(o["price"]), Decimal(o["origQty"])
        qty_now, entry = self.position(symbol)
        new_qty = qty_now + qty
        wallet = Decimal(self._get("wallet", {})["usdt"]) - px * qty * self.MAKER_FEE
        self._put("wallet", dict(usdt=str(wallet)))
        self._put(f"pos:{symbol}", dict(qty=str(new_qty), entry=str((entry * qty_now + px * qty) / new_qty)))
        o = dict(o, status="FILLED", executedQty=str(qty), avgPrice=str(px))
        self._put(f"order:{o['clientOrderId']}", o)
        return o

    def cancel_order(self, symbol, client_id):
        o = self._get(f"order:{client_id}", None)
        if o is None or o["status"] != "NEW":
            raise ApiError("unknown order", 400, -2011)
        self._put(f"order:{client_id}", dict(o, status="CANCELED"))
        return dict(o, status="CANCELED")

    def stop_order(self, client_id):
        return dict(status="NEW", client_id=client_id)

    def configure(self, symbol, leverage):
        return None

    def market_order(self, symbol, side, qty, client_id, reduce_only):
        if self._get(f"order:{client_id}", None):
            raise ApiError("duplicate client id", 400, -4116)
        px = self.mark(symbol) * (1 + self.SLIP if side == "BUY" else 1 - self.SLIP)
        qty = Decimal(str(qty))
        qty_now, entry = self.position(symbol)
        wallet = Decimal(self._get("wallet", {})["usdt"]) - px * qty * self.FEE
        if side == "BUY":
            new_qty = qty_now + qty
            entry = (entry * qty_now + px * qty) / new_qty
        else:
            new_qty = qty_now - qty
            wallet += (px - entry) * qty
        self._put("wallet", dict(usdt=str(wallet)))
        self._put(f"pos:{symbol}", dict(qty=str(new_qty), entry=str(entry if new_qty else 0)))
        order = dict(clientOrderId=client_id, symbol=symbol, side=side, status="FILLED",
                     executedQty=str(qty), avgPrice=str(px))
        self._put(f"order:{client_id}", order)
        return order

    def place_stop(self, symbol, qty, trigger, client_id):
        self._put(f"stop:{symbol}", dict(client_id=client_id, qty=str(qty), trigger=str(trigger)))
        return dict(clientAlgoId=client_id, algoStatus="NEW")

    def _stop_symbols(self):
        return [k.split(":", 1)[1] for (k,) in self.db.execute("SELECT k FROM paper WHERE k LIKE 'stop:%'")]

    def cancel_stop(self, client_id):
        for sym in self._stop_symbols():
            stop = self._get(f"stop:{sym}", None)
            if stop and stop["client_id"] == client_id:
                with self.db:
                    self.db.execute("DELETE FROM paper WHERE k=?", (f"stop:{sym}",))
        return dict(clientAlgoId=client_id, algoStatus="CANCELED")

    def check_stops(self):
        """Simulate exchange-side stop triggering at mark price."""
        for sym in self._stop_symbols():
            stop = self._get(f"stop:{sym}", None)
            if stop and self.mark(sym) <= Decimal(stop["trigger"]):
                qty, _ = self.position(sym)
                with self.db:
                    self.db.execute("DELETE FROM paper WHERE k=?", (f"stop:{sym}",))
                if qty > 0:
                    self.market_order(sym, "SELL", qty, stop["client_id"] + "-fill", True)
