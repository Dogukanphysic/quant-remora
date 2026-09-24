"""User-operated BTCUSDT USD-M Futures mainnet worker.

The worker is deliberately separate from the Spot ledger.  It is long-only,
uses isolated margin, defaults to 4x (explicit 10x opt-in), and requires stop and
take-profit orders for every open position.  Import, status, diagnose and
order-check never submit a real order.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agent import validate
from btc_live import bollinger_touch
from btc_futures_signals import apply_profile
from btc_futures_model_policy import decide as model_entry_decision
from strategy_research import features


ROOT = Path(__file__).resolve().parent
DB = ROOT / "state/btc-futures-live.sqlite3"
SYMBOL = "BTCUSDT"
INTERVAL = "15m"
STEP_SECONDS = 900
LEVERAGE = int(os.getenv("BTC_FUTURES_LEVERAGE", "4"))
if LEVERAGE not in {4, 10}:
    raise ValueError("BTC_FUTURES_LEVERAGE must be 4 or 10")
CONTRACT_BY_LEVERAGE = {
    2: "btc-usdm-isolated-2x-bollinger-long-v1",
    4: "btc-usdm-isolated-4x-bollinger-long-v2",
    10: "btc-usdm-isolated-10x-bollinger-long-v3",
}
CONTRACT = CONTRACT_BY_LEVERAGE[LEVERAGE]
LEGACY_CONTRACTS = {contract for leverage, contract in CONTRACT_BY_LEVERAGE.items()
                    if leverage < LEVERAGE}
CONFIRM = f"{LEVERAGE}X ISOLATED BTC FUTURES MAINNET AJANINI BASLAT"
PREFIX = "qrf-"
MISSING_ORDER_CODES = {-2013, -2011}
COOLDOWN_BARS = 4
# Five years of 15-minute USD-M data were split into development data and a
# final one-year holdout.  The current Bollinger variants did not remain net
# profitable after costs in both periods.  Validated mode therefore blocks new
# entries.  Exploratory mode is an explicit operator override: it permits the
# same closed-candle signal while retaining isolated margin, exchange-side
# protection, cooldown and an allocation drawdown stop.
ENTRY_MODE = os.getenv("BTC_FUTURES_ENTRY_MODE", "validated").strip().lower()
if ENTRY_MODE not in {"validated", "exploratory"}:
    raise ValueError("BTC_FUTURES_ENTRY_MODE must be validated or exploratory")
ENTRY_VALIDATED = ENTRY_MODE == "exploratory"
_model_decisions_option = os.getenv("BTC_FUTURES_MODEL_DECISIONS", "0")
if _model_decisions_option not in {"0", "1"}:
    raise ValueError("BTC_FUTURES_MODEL_DECISIONS must be 0 or 1")
MODEL_DECISIONS = _model_decisions_option == "1"
ENTRY_GATE_REASON = (None if ENTRY_VALIDATED else
                     "blocked_no_robust_out_of_sample_edge")
ENTRY_GATE_MIGRATION_HALT = "StrategyEntryGateMigration"
SIGNAL_PROFILE = os.getenv("BTC_FUTURES_SIGNAL_PROFILE", "trend").strip().lower()
if SIGNAL_PROFILE not in {"trend", "responsive"}:
    raise ValueError("BTC_FUTURES_SIGNAL_PROFILE must be trend or responsive")
RISK_PROFILES = {
    "standard": (Decimal("2"), Decimal("4")),
    "moderate": (Decimal("2.5"), Decimal("5")),
    "aggressive": (Decimal("3"), Decimal("6")),
}
RISK_PROFILE = os.getenv("BTC_FUTURES_RISK_PROFILE", "moderate").strip().lower()
if RISK_PROFILE not in RISK_PROFILES:
    raise ValueError(
        "BTC_FUTURES_RISK_PROFILE must be standard, moderate or aggressive")
LOSS_LIMIT_RATE = (Decimal("0.10") if ENTRY_MODE == "exploratory" else
                   Decimal("0.05"))


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int, code: int | None,
                 *, method=None, path=None, retry_safe=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.method = method
        self.path = path
        self.retry_safe = bool(retry_safe and method == "GET" and code == -1021)


class TransportError(RuntimeError):
    def __init__(self, message: str, *, retry_safe: bool,
                 method=None, path=None, request_sent=None):
        super().__init__(message)
        self.retry_safe = retry_safe
        self.method = method
        self.path = path
        self.request_sent = request_sent


def dec(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid financial value") from exc
    if not number.is_finite():
        raise ValueError("Invalid financial value")
    return number


def positive(value, label: str) -> Decimal:
    number = dec(value)
    if number <= 0:
        raise ValueError(f"{label} must be positive")
    return number


def grid(value: Decimal, step: Decimal, *, up: bool = False) -> Decimal:
    return (value / step).to_integral_value(
        rounding=ROUND_UP if up else ROUND_DOWN) * step


def filter_map(filters):
    return {row["filterType"]: row for row in filters}


def order_quantity(margin_usdt, ask, filters, leverage=None):
    """Size from explicitly allocated margin; never from the whole account."""
    margin = positive(margin_usdt, "margin_usdt")
    price = positive(ask, "ask")
    leverage = LEVERAGE if leverage is None else leverage
    if dec(leverage) != LEVERAGE:
        raise ValueError(f"Only selected {LEVERAGE}x leverage is supported")
    rows = filter_map(filters)
    lot = rows.get("MARKET_LOT_SIZE") or rows.get("LOT_SIZE")
    notional = rows.get("MIN_NOTIONAL") or rows.get("NOTIONAL")
    if not lot or not notional:
        raise ValueError("Missing Futures sizing filters")
    step = positive(lot["stepSize"], "stepSize")
    quantity = grid(margin * Decimal("0.98") * LEVERAGE / price, step)
    minimum = positive(lot["minQty"], "minQty")
    maximum = positive(lot["maxQty"], "maxQty")
    min_notional = positive(notional.get("notional", notional.get("minNotional")),
                            "minNotional")
    if quantity < minimum or quantity > maximum or quantity * price < min_notional:
        raise ValueError("Allocated Futures margin is below/above exchange limits")
    return quantity


def validate_leverage_bracket(payload, notional, leverage=None):
    """Use account-specific returned tiers; do not multiply adjusted caps twice."""
    target = LEVERAGE if leverage is None else leverage
    amount = dec(notional)
    if amount < 0 or dec(target) not in {4, 10}:
        raise ValueError("Invalid leverage bracket request")
    rows = payload if isinstance(payload, list) else [payload]
    matches = [row for row in rows if isinstance(row, dict) and row.get("symbol") == SYMBOL]
    if len(matches) != 1:
        raise ValueError("Unique BTCUSDT leverage bracket not found")
    tiers = matches[0].get("brackets", [])
    eligible = []
    for tier in tiers:
        floor, cap = dec(tier["notionalFloor"]), positive(tier["notionalCap"], "notionalCap")
        maximum = positive(tier["initialLeverage"], "initialLeverage")
        if floor < 0 or floor >= cap:
            raise ValueError("Invalid leverage bracket boundaries")
        if floor <= amount < cap:
            eligible.append((maximum, cap))
    if len(eligible) != 1 or dec(target) > eligible[0][0]:
        raise ValueError("Selected leverage/notional exceeds account bracket")
    return {"leverage": int(target), "notional_usdt": str(amount),
            "tier_cap_usdt": str(eligible[0][1])}


def protection_prices(entry, atr, filters, risk_profile=None):
    entry, atr = positive(entry, "entry"), positive(atr, "atr")
    profile = RISK_PROFILE if risk_profile is None else str(risk_profile).lower()
    if profile not in RISK_PROFILES:
        raise ValueError("Unsupported Futures risk profile")
    stop_atr, target_atr = RISK_PROFILES[profile]
    row = filter_map(filters).get("PRICE_FILTER")
    if not row:
        raise ValueError("Missing PRICE_FILTER")
    tick = positive(row["tickSize"], "tickSize")
    stop = grid(entry - stop_atr * atr, tick)
    target = grid(entry + target_atr * atr, tick, up=True)
    if stop <= 0 or not stop < entry < target:
        raise ValueError("Invalid Futures protection prices")
    return stop, target


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("API redirect refused")


class Client:
    BASE = "https://fapi.binance.com"
    TIME_SYNC_TTL = 60.0
    MAX_TIME_SYNC_RTT = 1.0
    TIME_SYNC_ATTEMPTS = 3
    RECV_WINDOW = 5000
    ALLOWED = {
        ("GET", "/fapi/v1/time"),
        ("GET", "/fapi/v1/exchangeInfo"),
        ("GET", "/fapi/v1/ticker/bookTicker"),
        ("GET", "/fapi/v1/klines"),
        ("GET", "/fapi/v1/premiumIndex"),
        ("GET", "/fapi/v1/openOrders"),
        ("GET", "/fapi/v1/openAlgoOrders"),
        ("GET", "/fapi/v1/algoOrder"),
        ("GET", "/fapi/v1/order"),
        ("GET", "/fapi/v1/userTrades"),
        ("GET", "/fapi/v1/commissionRate"),
        ("GET", "/fapi/v1/positionSide/dual"),
        ("GET", "/fapi/v1/multiAssetsMargin"),
        ("GET", "/fapi/v1/leverageBracket"),
        ("GET", "/fapi/v2/positionRisk"),
        ("GET", "/fapi/v2/account"),
        ("GET", "/fapi/v3/account"),
        ("POST", "/fapi/v1/marginType"),
        ("POST", "/fapi/v1/multiAssetsMargin"),
        ("POST", "/fapi/v1/leverage"),
        ("POST", "/fapi/v1/order"),
        ("POST", "/fapi/v1/order/test"),
        ("POST", "/fapi/v1/algoOrder"),
        ("DELETE", "/fapi/v1/order"),
        ("DELETE", "/fapi/v1/algoOrder"),
    }

    def __init__(self, *, live=False):
        if not live or os.getenv("BTC_FUTURES_LIVE_AUTHORIZATION") != CONFIRM:
            raise ValueError("Local explicit Futures mainnet authorization required")
        self.key = os.environ.get("BTC_FUTURES_MAINNET_API_KEY", "")
        self.secret = os.environ.get("BTC_FUTURES_MAINNET_SECRET_KEY", "")
        if not self.key or not self.secret:
            raise ValueError("Missing local Futures mainnet credentials")
        self.identity = hashlib.sha256(self.key.encode()).hexdigest()
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, params=None, signed=False):
        if (method, path) not in self.ALLOWED:
            raise ValueError("Unsupported Futures API operation")
        # Only a rejected signed read can be repeated. Each attempt rebuilds
        # the timestamp, signature and request; mutations remain one-shot.
        for attempt in range(2 if signed and method == "GET" else 1):
            try:
                return self._request_once(method, path, params, signed)
            except ApiError as exc:
                if signed and exc.code == -1021:
                    self._time_anchor = None
                    if method == "GET" and attempt == 0:
                        continue
                raise

    def _sync_time(self):
        self._time_anchor = None
        for _ in range(self.TIME_SYNC_ATTEMPTS):
            started = time.monotonic()
            result = self._request_once("GET", "/fapi/v1/time")
            received = time.monotonic()
            server = result.get("serverTime") if isinstance(result, dict) else None
            if type(server) is not int or server <= 0:
                raise TransportError("Invalid Futures time synchronization response",
                                     retry_safe=True)
            elapsed = received - started
            if 0 <= elapsed <= self.MAX_TIME_SYNC_RTT:
                # Map server time to a monotonic clock. A short RTT bounds the
                # midpoint estimate's uncertainty below Binance's 1s ahead limit.
                self._time_anchor = (server + elapsed * 500, received)
                return
        raise TransportError("Futures time synchronization RTT exceeds safe bound; "
                             "signed request not sent", retry_safe=True)

    def _signed_timestamp(self):
        anchor = getattr(self, "_time_anchor", None)
        if anchor is None or not 0 <= time.monotonic() - anchor[1] < self.TIME_SYNC_TTL:
            self._sync_time()
            anchor = self._time_anchor
        return int(anchor[0] + (time.monotonic() - anchor[1]) * 1000)

    def _request_once(self, method, path, params=None, signed=False):
        if (method, path) not in self.ALLOWED:
            raise ValueError("Unsupported Futures API operation")
        values = dict(params or {})
        if signed:
            try:
                timestamp = self._signed_timestamp()
            except TransportError:
                # Failure of the nested public clock read is not evidence that
                # replaying a partially completed mutation cycle is safe.
                raise TransportError(
                    f"Futures clock synchronization failed before {method} {path}; "
                    "signed request not sent",
                    retry_safe=method == "GET", method=method, path=path,
                    request_sent=False) from None
            values.update(timestamp=timestamp, recvWindow=self.RECV_WINDOW)
        query = urlencode(values)
        headers = {}
        if signed:
            query += "&signature=" + hmac.new(
                self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.key
        url = self.BASE + path
        body = query.encode() if method == "POST" else None
        if method in {"GET", "DELETE"} and query:
            url += "?" + query
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            code = None
            try:
                payload = json.loads(exc.read(8192))
                candidate = payload.get("code") if isinstance(payload, dict) else None
                if type(candidate) is int and -99999 <= candidate < 0:
                    code = candidate
            except Exception:
                pass
            finally:
                exc.close()
            hints = {
                -2015: ("Authorization rejected; check the same API key, allowed IP "
                        "and Futures permission; run NetworkCheck after a VPN/network change"),
                -1021: "Request timestamp outside allowed window",
                -1022: "Invalid request signature",
                -2013: "Order not found",
                -2021: "Protection trigger would execute immediately",
                -4046: "Margin type already selected",
                -4047: "Open orders prevent margin type changes",
                -4048: "An open position prevents margin type changes",
                -4067: "Open orders prevent position mode changes",
                -4068: "An open position prevents position mode changes",
                -4120: "Conditional orders require the Futures Algo Order endpoint",
            }
            detail = f"; Binance code {code}" if code is not None else ""
            if code in hints:
                detail += "; " + hints[code]
            retry_safe = signed and method == "GET" and code == -1021
            retry_note = ("safe to retry read after time synchronization" if retry_safe
                          else "no automatic order retry")
            raise ApiError(
                f"Futures API HTTP {exc.code} ({method} {path}){detail}; "
                f"{retry_note}", exc.code, code, method=method, path=path,
                retry_safe=retry_safe) from None
        except Exception:
            retry_safe = method == "GET"
            detail = ("safe to retry read" if retry_safe else
                      "mutation outcome may be unknown; no automatic retry")
            raise TransportError(
                f"Futures API transport/response failure ({method} {path}); {detail}",
                retry_safe=retry_safe, method=method, path=path) from None

    def market(self):
        info = self.request("GET", "/fapi/v1/exchangeInfo")
        symbol = next(row for row in info["symbols"] if row["symbol"] == SYMBOL)
        if symbol["status"] != "TRADING" or "MARKET" not in symbol["orderTypes"]:
            raise ValueError("BTCUSDT USD-M Futures unavailable")
        book = self.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": SYMBOL})
        bid, ask = positive(book["bidPrice"], "bid"), positive(book["askPrice"], "ask")
        if bid > ask:
            raise ValueError("Invalid Futures book")
        raw = self.request("GET", "/fapi/v1/klines",
                           {"symbol": SYMBOL, "interval": INTERVAL, "limit": 1000})
        now_ms = int(self.request("GET", "/fapi/v1/time")["serverTime"])
        rows = [dict(ts=int(row[0]), open=float(row[1]), high=float(row[2]),
                     low=float(row[3]), close=float(row[4]), volume=float(row[5]))
                for row in raw if int(row[6]) < now_ms]
        rows = validate(rows, STEP_SECONDS)
        if (len(rows) < 205 or
                not 0 <= now_ms - rows[-1]["ts"] - STEP_SECONDS * 1000
                <= STEP_SECONDS * 1000):
            raise ValueError("Insufficient or stale closed 15m Futures history")
        band = bollinger_touch(rows)
        atr = positive(features(rows)["atr"][-1], "atr")
        mark = self.request("GET", "/fapi/v1/premiumIndex", {"symbol": SYMBOL})
        mark_price = positive(mark["markPrice"], "markPrice")
        return dict(now=now_ms / 1000, bar=rows[-1]["ts"], bid=str(bid), ask=str(ask),
                    mark=str(mark_price), atr=str(atr), filters=symbol["filters"],
                    band=band)

    def account(self):
        # V3 intentionally omits account-level canTrade/multiAssetsMargin.
        # V2 remains the documented endpoint carrying both fields needed by
        # the execution safety gate, as well as per-asset availableBalance.
        return self.request("GET", "/fapi/v2/account", signed=True)

    def positions(self):
        return self.request("GET", "/fapi/v2/positionRisk", {"symbol": SYMBOL}, True)

    def position_mode(self):
        return self.request("GET", "/fapi/v1/positionSide/dual", signed=True)

    def multi_assets_mode(self):
        return self.request("GET", "/fapi/v1/multiAssetsMargin", signed=True)

    def configure_single_asset_mode(self):
        result = self.request("POST", "/fapi/v1/multiAssetsMargin",
                              {"multiAssetsMargin": "false"}, True)
        if self.multi_assets_mode().get("multiAssetsMargin") is not False:
            raise ValueError("Binance did not confirm Single-Asset Mode")
        return result

    def leverage_bracket(self):
        return self.request("GET", "/fapi/v1/leverageBracket", {"symbol": SYMBOL}, True)

    def commission(self):
        return self.request("GET", "/fapi/v1/commissionRate", {"symbol": SYMBOL}, True)

    def open_orders(self):
        regular = self.request(
            "GET", "/fapi/v1/openOrders", {"symbol": SYMBOL}, True)
        algos = self.request("GET", "/fapi/v1/openAlgoOrders",
                             {"symbol": SYMBOL, "algoType": "CONDITIONAL"}, True)
        return list(regular) + [self._normalize_algo(row) for row in algos]

    @staticmethod
    def _normalize_algo(row):
        return {
            **row,
            "clientOrderId": row.get("clientAlgoId"),
            "type": row.get("orderType"),
            "status": row.get("algoStatus"),
            "origQty": row.get("quantity"),
            "stopPrice": row.get("triggerPrice"),
            "_order_channel": "algo",
        }

    def configure_isolated_leverage(self):
        """Initial configuration only; caller must verify flat and no orders."""
        try:
            self.request("POST", "/fapi/v1/marginType",
                         {"symbol": SYMBOL, "marginType": "ISOLATED"}, True)
        except ApiError as exc:
            if exc.code != -4046:
                raise
        return self.configure_leverage()

    def configure_leverage(self):
        """Change leverage without changing margin/position modes or orders."""
        result = self.request("POST", "/fapi/v1/leverage",
                              {"symbol": SYMBOL, "leverage": LEVERAGE}, True)
        if (result.get("symbol") != SYMBOL or
                dec(result.get("leverage", 0)) != LEVERAGE):
            raise ValueError(f"Binance did not confirm selected {LEVERAGE}x leverage")
        positive(result.get("maxNotionalValue"), "maxNotionalValue")
        return result

    def submit_entry(self, quantity, client_id):
        return self.request("POST", "/fapi/v1/order", {
            "symbol": SYMBOL, "side": "BUY", "type": "MARKET",
            "quantity": str(quantity), "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }, True)

    def submit_protection(self, side, quantity, stop_price, client_id):
        if side not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("Invalid protection order type")
        return self.request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": SYMBOL,
            "side": "SELL", "type": side,
            "quantity": str(quantity), "triggerPrice": str(stop_price),
            "reduceOnly": "true", "workingType": "MARK_PRICE",
            "clientAlgoId": client_id,
        }, True)

    def test_entry(self, quantity):
        return self.request("POST", "/fapi/v1/order/test", {
            "symbol": SYMBOL, "side": "BUY", "type": "MARKET",
            "quantity": str(quantity), "newClientOrderId": "qrf-test-entry",
        }, True)

    def lookup(self, client_id):
        return self.request("GET", "/fapi/v1/order",
                            {"symbol": SYMBOL, "origClientOrderId": client_id}, True)

    def lookup_protection(self, client_id):
        row = self.request("GET", "/fapi/v1/algoOrder",
                           {"clientAlgoId": client_id}, True)
        return self._normalize_algo(row)

    def cancel(self, client_id):
        return self.request("DELETE", "/fapi/v1/order",
                            {"symbol": SYMBOL, "origClientOrderId": client_id}, True)

    def cancel_protection(self, client_id):
        return self.request("DELETE", "/fapi/v1/algoOrder",
                            {"clientAlgoId": client_id}, True)


def connect(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,value TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY, ts REAL, kind TEXT, payload TEXT)""")
    db.commit()
    return db


def read(db):
    row = db.execute("SELECT value FROM state WHERE id=1").fetchone()
    return json.loads(row[0]) if row else None


def write(db, state):
    db.execute("INSERT OR REPLACE INTO state VALUES (1,?)",
               (json.dumps(state, allow_nan=False),))


def event(db, kind, payload, now=None):
    db.execute("INSERT INTO events(ts,kind,payload) VALUES (?,?,?)",
               (time.time() if now is None else now, kind,
                json.dumps(payload, allow_nan=False)))


def record_order_observation(db, state, role, client_id, source, result=None,
                             error=None, now=None):
    """Persist observed fields only; an API acknowledgement is not a fill."""
    row = result if isinstance(result, dict) else {}
    raw_status = row.get("status", row.get("algoStatus"))
    known = {"NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"}
    payload = {
        "schema": 1, "role": role, "client_id": client_id,
        "entry_client_id": state.get("entry_client_id") or state.get("pending_entry"),
        "status": raw_status if raw_status in known else "UNKNOWN",
        "observation_source": source,
    }
    for output, inputs in {
        "quantity": ("origQty", "quantity"),
        "executed_quantity": ("executedQty",),
        "average_price": ("avgPrice", "actualPrice"),
        "order_type": ("type", "orderType"), "side": ("side",),
    }.items():
        for key in inputs:
            if row.get(key) is not None:
                payload[output] = row[key]
                break
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error_code"] = error.code if isinstance(error, ApiError) else None
    # Repeated identical open-order polls do not inflate the evidence count.
    previous = db.execute("SELECT payload FROM events WHERE kind='order_observation' "
                          "AND json_extract(payload,'$.client_id')=? ORDER BY id DESC LIMIT 1",
                          (client_id,)).fetchone()
    if previous and json.loads(previous[0]) == payload:
        return
    with db:
        event(db, "order_observation", payload, now)


def observe_order_call(db, state, role, client_id, source, operation, now):
    try:
        result = operation()
    except Exception as exc:
        record_order_observation(db, state, role, client_id,
                                 "submit_error" if source == "submit_response" else source,
                                 error=exc, now=now)
        raise
    record_order_observation(db, state, role, client_id, source, result, now=now)
    return result


def learning_entry_context(state, market, wallet, quantity):
    """Capture only information known before submitting this entry.

    The separate learner consumes these durable events.  This capture neither
    trains inside the execution loop nor changes the active entry decision.
    """
    return {
        "schema": 1, "decision": dict(state["decision"]),
        "bar": market["bar"], "captured_at": market["now"],
        "wallet_before_usdt": str(wallet), "quantity": str(quantity),
        "risk_profile": RISK_PROFILE, "leverage": LEVERAGE,
        "signal_profile": SIGNAL_PROFILE,
        "atr": str(market["atr"]), "reference_ask": str(market["ask"]),
    }


def entry_shadow_prediction(db, context, now):
    """Observe the candidate before submitting; it has no execution authority."""
    try:
        from btc_futures_learning import predict_entry
        source = next(row[2] for row in db.execute("PRAGMA database_list")
                      if row[1] == "main")
        if not source:
            raise ValueError("A durable source is required")
        return predict_entry(
            Path(source).parent / "btc-futures-mainnet-learning.sqlite3",
            context, now=now, source_path=Path(source))
    except Exception as exc:
        return {"available": False, "status": "unavailable",
                "reason": "shadow_prediction_failed", "error_type": type(exc).__name__,
                "decision_authority": False, "automatic_activation": False,
                "real_orders_enabled": False}


def learning_outcome_context(state, market, wallet, exit_order):
    """Wallet changes are account-level proxy labels, not verified trade PnL."""
    entry = state.get("learning_entry")
    before = entry.get("wallet_before_usdt") if isinstance(entry, dict) else None
    delta = str(dec(wallet) - dec(before)) if before is not None else None
    return {
        "schema": 1, "entry": entry, "closed_at": market["now"],
        "wallet_after_usdt": str(wallet), "wallet_delta_proxy_usdt": delta,
        "pnl_basis": "account_wallet_delta_unverified",
        "exit_reason": (exit_order or {}).get("type") or "unknown_flat_reconciliation",
        "exit_order": exit_order, "execution_verified": False,
    }


def usdt_balance(account):
    if account.get("canTrade") is not True:
        raise ValueError("Futures account cannot trade")
    if account.get("multiAssetsMargin") is True:
        raise ValueError("Multi-assets mode is not supported")
    rows = [row for row in account.get("assets", []) if row.get("asset") == "USDT"]
    if len(rows) != 1:
        raise ValueError("Unique USDT Futures balance not found")
    return positive(rows[0]["availableBalance"], "availableBalance"), dec(
        rows[0]["walletBalance"])


def position_snapshot(rows, expected_leverage=None):
    expected_leverage = LEVERAGE if expected_leverage is None else expected_leverage
    matches = [row for row in rows if row.get("symbol") == SYMBOL]
    if len(matches) != 1:
        raise ValueError("Unique BTCUSDT position not found")
    row = matches[0]
    amount = dec(row["positionAmt"])
    if amount < 0:
        raise ValueError("Short positions are outside this contract")
    if row.get("positionSide") not in {None, "BOTH"}:
        raise ValueError("Hedge mode is outside this contract")
    if row.get("marginType", "").lower() != "isolated":
        raise ValueError("BTCUSDT margin type must be isolated")
    if int(row.get("leverage", 0)) != int(expected_leverage):
        raise ValueError(
            f"BTCUSDT leverage must remain exactly {expected_leverage}x")
    return dict(quantity=amount, entry=dec(row.get("entryPrice", "0")),
                mark=dec(row.get("markPrice", "0")),
                unrealized=dec(row.get("unRealizedProfit", "0")),
                liquidation=dec(row.get("liquidationPrice", "0")))


def account_modes(client):
    if client.position_mode().get("dualSidePosition") is not False:
        raise ValueError("One-way position mode is required")
    if client.multi_assets_mode().get("multiAssetsMargin") is not False:
        raise ValueError("Single-asset mode is required")


def owned_protections(orders):
    return [row for row in orders
            if str(row.get("clientOrderId", "")).startswith(PREFIX)
            and row.get("type") in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}]


def validate_protections(orders, state, quantity):
    expected = {state.get("stop_client_id"): "STOP_MARKET",
                state.get("target_client_id"): "TAKE_PROFIT_MARKET"}
    seen = {}
    for row in orders:
        client_id = row.get("clientOrderId")
        if client_id in expected:
            if row.get("type") != expected[client_id] or row.get("side") != "SELL":
                raise ValueError("Protection order shape mismatch")
            if row.get("reduceOnly") is not True:
                raise ValueError("Protection order is not reduce-only")
            if dec(row.get("origQty", "0")) != quantity:
                raise ValueError("Protection quantity mismatch")
            seen[client_id] = row
    return seen


def initialize(db, client, market, margin_usdt, use_all_available=False):
    existing = read(db)
    if existing:
        if existing["identity"] != client.identity:
            raise ValueError("Futures account binding mismatch")
        configured_mode = existing.get("allocation_mode", "fixed")
        expected_mode = "all_available" if use_all_available else "fixed"
        if (existing["contract"] != CONTRACT or configured_mode != expected_mode or
                (not use_all_available and
                 dec(existing["margin_usdt"]) != margin_usdt)):
            raise ValueError("Futures contract/allocation mismatch")
        return existing
    available, wallet = usdt_balance(client.account())
    allocation = available if use_all_available else margin_usdt
    if available < allocation:
        raise ValueError("Allocated Futures margin is not available")
    if client.open_orders():
        raise ValueError("Untracked BTCUSDT Futures orders exist")
    position = position_snapshot(client.positions())
    if position["quantity"] != 0:
        raise ValueError("Existing BTCUSDT Futures position cannot be adopted")
    state = {
        "schema": 1, "contract": CONTRACT, "identity": client.identity,
        "symbol": SYMBOL, "interval": INTERVAL, "leverage": LEVERAGE,
        "margin_type": "isolated", "margin_usdt": str(allocation),
        "allocation_mode": "all_available" if use_all_available else "fixed",
        "phase": "cash", "quantity": "0", "entry_price": "0",
        "stop_price": None, "target_price": None,
        "entry_client_id": None, "stop_client_id": None,
        "target_client_id": None, "pending_entry": None,
        "learning_entry": None,
        "last_bar": market["bar"], "last_poll": market["now"],
        "filled_orders": 0, "completed_round_trips": 0,
        "initial_wallet_usdt": str(wallet), "wallet_usdt": str(wallet),
        "unrealized_pnl_usdt": "0", "marked_pnl_usdt": "0",
        "halted": None, "decision": None,
        "entry_validation_passed": ENTRY_VALIDATED,
        "entry_gate_reason": ENTRY_GATE_REASON,
        "entry_mode": ENTRY_MODE,
        "model_decisions_enabled": MODEL_DECISIONS,
        "model_control_latest": None,
        "signal_profile": SIGNAL_PROFILE,
        "last_exit_bar": None, "cooldown_until_bar": None,
        "risk_profile": RISK_PROFILE,
        "next_risk_profile": RISK_PROFILE,
        "stop_atr": str(RISK_PROFILES[RISK_PROFILE][0]),
        "target_atr": str(RISK_PROFILES[RISK_PROFILE][1]),
    }
    with db:
        event(db, "configured", {"margin_usdt": str(allocation),
                                  "allocation_mode": state["allocation_mode"],
                                  "leverage": LEVERAGE,
                                  "risk_profile": RISK_PROFILE,
                                  "entry_mode": ENTRY_MODE,
                                  "loss_limit_rate": str(LOSS_LIMIT_RATE)},
              market["now"])
        write(db, state)
    return state


def configure(db, client, margin_usdt, use_all_available=False):
    if client.position_mode().get("dualSidePosition") is not False:
        raise ValueError("One-way position mode is required")
    if client.open_orders():
        raise ValueError("Close existing BTCUSDT Futures orders first")
    positions = client.positions()
    raw = [row for row in positions if row.get("symbol") == SYMBOL]
    if len(raw) != 1 or dec(raw[0]["positionAmt"]) != 0:
        raise ValueError("BTCUSDT Futures must be flat before configuration")
    validate_leverage_bracket(client.leverage_bracket(), 0)
    if client.multi_assets_mode().get("multiAssetsMargin") is not False:
        client.configure_single_asset_mode()
    account_modes(client)
    client.configure_isolated_leverage()
    market = client.market()
    return initialize(db, client, market, margin_usdt, use_all_available)


def recover_pending_entry(db, client, state, market):
    client_id = state.get("pending_entry")
    if not client_id:
        return state
    position = position_snapshot(client.positions())
    try:
        order = observe_order_call(db, state, "entry", client_id, "lookup",
                                   lambda: client.lookup(client_id), market["now"])
    except ApiError as exc:
        if exc.code == -2013 and position["quantity"] == 0:
            state["pending_entry"] = None
            state["learning_entry"] = None
            with db:
                event(db, "unsent_entry_recovered", {"client_id": client_id}, market["now"])
                write(db, state)
            return state
        raise
    if position["quantity"] == 0:
        if state.get("phase") == "long" and order.get("status") == "FILLED":
            _, wallet = usdt_balance(client.account())
            return close_epoch(db, client, state, market, wallet)
        if order.get("status") in {"CANCELED", "EXPIRED", "REJECTED"}:
            state["pending_entry"] = None
            state["learning_entry"] = None
            with db:
                event(db, "entry_terminal_without_position", order, market["now"])
                write(db, state)
            return state
        raise ValueError("Entry outcome unresolved; manual review required")
    return activate_long(db, client, state, market, position, client_id)


def activate_long(db, client, state, market, position, entry_client_id):
    quantity = position["quantity"]
    entry = position["entry"]
    stop, target = protection_prices(
        entry, market["atr"], market["filters"], RISK_PROFILE)
    stop_atr, target_atr = RISK_PROFILES[RISK_PROFILE]
    token = str(market["bar"])[-12:]
    stop_id = state.get("stop_client_id") or f"{PREFIX}s-{token}"
    target_id = state.get("target_client_id") or f"{PREFIX}t-{token}"
    state.update(phase="long", quantity=str(quantity), entry_price=str(entry),
                 stop_price=str(stop), target_price=str(target),
                 entry_client_id=entry_client_id, stop_client_id=stop_id,
                 target_client_id=target_id, risk_profile=RISK_PROFILE,
                 next_risk_profile=RISK_PROFILE, stop_atr=str(stop_atr),
                 target_atr=str(target_atr))
    if isinstance(state.get("learning_entry"), dict):
        state["learning_entry"] = {
            **state["learning_entry"], "quantity": str(quantity),
            "entry_price": str(entry), "stop_price": str(stop),
            "target_price": str(target), "filled_at": market["now"],
        }
    with db:
        write(db, state)  # Protection identifiers are durable before POST.
    existing = validate_protections(client.open_orders(), state, quantity)
    for client_id, order_type, trigger in (
            (stop_id, "STOP_MARKET", stop),
            (target_id, "TAKE_PROFIT_MARKET", target)):
        if client_id in existing:
            continue
        try:
            client.lookup_protection(client_id)
            raise ValueError("Terminal protection id cannot be reused")
        except ApiError as exc:
            if exc.code not in MISSING_ORDER_CODES:
                raise
        observe_order_call(db, state, "stop" if client_id == stop_id else "target",
                           client_id, "submit_response",
                           lambda: client.submit_protection(order_type, quantity, trigger, client_id),
                           market["now"])
    orders = client.open_orders()
    seen = validate_protections(orders, state, quantity)
    if set(seen) != {stop_id, target_id}:
        state["halted"] = "ProtectionIncomplete"
        with db:
            write(db, state)
        raise ValueError("Both exchange-side protections were not confirmed")
    state.update(pending_entry=None, filled_orders=state["filled_orders"] + 1)
    with db:
        event(db, "long_activated", {"quantity": str(quantity), "entry": str(entry),
                                     "entry_client_id": entry_client_id,
                                     "client_id": entry_client_id,
                                     "stop": str(stop), "target": str(target),
                                     "risk_profile": RISK_PROFILE,
                                     "stop_atr": str(stop_atr),
                                     "target_atr": str(target_atr),
                                     "learning_entry": state.get("learning_entry")},
              market["now"])
        write(db, state)
    return state


def close_epoch(db, client, state, market, wallet):
    exit_order = None
    entry_client_id = state.get("entry_client_id") or state.get("pending_entry")
    for client_id in (state.get("stop_client_id"), state.get("target_client_id")):
        if not client_id:
            continue
        try:
            role = "stop" if client_id == state.get("stop_client_id") else "target"
            order = observe_order_call(db, state, role, client_id, "lookup",
                                       lambda: client.lookup_protection(client_id), market["now"])
            if order.get("status") == "FILLED":
                exit_order = {"client_id": client_id, "type": order.get("type"),
                              "status": order.get("status")}
            if order.get("status") in {"NEW", "PARTIALLY_FILLED"}:
                observe_order_call(db, state, role, client_id, "cancel_response",
                                   lambda: client.cancel_protection(client_id), market["now"])
        except ApiError as exc:
            if exc.code not in MISSING_ORDER_CODES:
                raise
    remaining = owned_protections(client.open_orders())
    if remaining:
        raise ValueError("Stale protection orders remain")
    learning_outcome = learning_outcome_context(state, market, wallet, exit_order)
    state.update(phase="cash", quantity="0", entry_price="0",
                 stop_price=None, target_price=None, entry_client_id=None,
                 stop_client_id=None, target_client_id=None, pending_entry=None,
                 learning_entry=None,
                 completed_round_trips=state["completed_round_trips"] + 1,
                 filled_orders=state["filled_orders"] + 1,
                 wallet_usdt=str(wallet), unrealized_pnl_usdt="0",
                 last_exit_bar=market["bar"],
                 cooldown_until_bar=market["bar"] + COOLDOWN_BARS * STEP_SECONDS * 1000,
                 entry_validation_passed=ENTRY_VALIDATED,
                 entry_gate_reason=ENTRY_GATE_REASON)
    with db:
        event(db, "round_trip_closed", {"wallet_usdt": str(wallet),
                                         "entry_client_id": entry_client_id,
                                         "exit_order": exit_order,
                                         "cooldown_bars": COOLDOWN_BARS,
                                         "learning_outcome": learning_outcome},
              market["now"])
        write(db, state)
    return state


def migrate_legacy_leverage(db, client, state):
    """Migrate leverage only; preserve position, protections and entry evidence."""
    if state.get("pending_entry"):
        raise ValueError("Resolve pending entry before leverage migration")
    rows = [row for row in client.positions() if row.get("symbol") == SYMBOL]
    if len(rows) != 1:
        raise ValueError("Unique BTCUSDT position not found during migration")
    current_leverage = int(rows[0].get("leverage", 0))
    source_leverage = next((value for value, contract in CONTRACT_BY_LEVERAGE.items()
                            if contract == state["contract"]), None)
    if (state["contract"] not in LEGACY_CONTRACTS or
            state.get("leverage") != source_leverage or
            current_leverage not in {source_leverage, LEVERAGE}):
        raise ValueError("Unexpected contract/exchange leverage during migration")
    before = position_snapshot(rows, expected_leverage=current_leverage)

    def verify_tracked_position(position):
        quantity = position["quantity"]
        if quantity:
            if state.get("phase") != "long":
                raise ValueError("Legacy open position is not tracked as long")
            if dec(state.get("quantity", "0")) != quantity:
                raise ValueError("Legacy position quantity does not match ledger")
            if dec(state.get("entry_price", "0")) != position["entry"]:
                raise ValueError("Legacy position entry price does not match ledger")
        elif state.get("phase") not in {"cash", "long"}:
            raise ValueError("Unknown legacy position phase")
        orders = client.open_orders()
        expected_ids = {state.get("stop_client_id"), state.get("target_client_id")} - {None}
        if any(row.get("clientOrderId") not in expected_ids for row in orders):
            raise ValueError("Untracked BTCUSDT Futures orders exist")
        seen = validate_protections(orders, state, quantity or dec(state["quantity"]))
        if quantity and (len(expected_ids) != 2 or set(seen) != expected_ids):
            raise ValueError("Protection orders must be intact before leverage migration")
        if quantity and LEVERAGE == 10:
            stop = positive(state.get("stop_price"), "stop_price")
            target = positive(state.get("target_price"), "target_price")
            if (dec(seen[state["stop_client_id"]].get("stopPrice")) != stop or
                    dec(seen[state["target_client_id"]].get("stopPrice")) != target):
                raise ValueError("Protection trigger does not match ledger")
            if position["liquidation"] > 0 and stop <= position["liquidation"]:
                raise ValueError("Stop must be above the long liquidation price")

    verify_tracked_position(before)
    notional = before["quantity"] * positive(before["mark"], "mark")
    validate_leverage_bracket(client.leverage_bracket(), notional)
    if current_leverage != LEVERAGE:
        # marginType and positionSide are deliberately not changed on restart.
        result = client.configure_leverage()
        if notional > positive(result.get("maxNotionalValue"), "maxNotionalValue"):
            raise ValueError("Existing notional exceeds confirmed leverage cap")
    # A response alone is insufficient. A prior ambiguous POST may already have
    # applied the target: reconcile via GET without sending another POST.
    after = position_snapshot(client.positions(), expected_leverage=LEVERAGE)
    if before["quantity"] == 0 and after["quantity"] != 0:
        raise ValueError("Unexpected position opened during leverage migration")
    verify_tracked_position(after)
    previous_contract = state["contract"]
    state.update(contract=CONTRACT, leverage=LEVERAGE,
                 leverage_migration_pending=None)
    with db:
        event(db, ("open_position_leverage_migrated" if after["quantity"] else
                   "leverage_contract_migrated"),
              {"from": previous_contract, "to": CONTRACT, "leverage": LEVERAGE,
               "quantity": str(after["quantity"]),
               "already_applied": current_leverage == LEVERAGE}, time.time())
        write(db, state)
    return state


def tick(db, client, margin_usdt, use_all_available=False):
    state = read(db)
    if not state:
        raise ValueError("Run configure before starting the worker")
    if (state["identity"] != client.identity or
            state["contract"] not in LEGACY_CONTRACTS | {CONTRACT}):
        raise ValueError("Futures ledger binding mismatch")
    configured_mode = state.get("allocation_mode", "fixed")
    expected_mode = "all_available" if use_all_available else "fixed"
    if (configured_mode != expected_mode or
            (not use_all_available and dec(state["margin_usdt"]) != margin_usdt)):
        raise ValueError("Configured Futures margin mismatch")
    if state.get("halted") == ENTRY_GATE_MIGRATION_HALT:
        state.update(halted=None, entry_validation_passed=ENTRY_VALIDATED,
                     entry_gate_reason=ENTRY_GATE_REASON)
        with db:
            event(db, "entry_gate_migration_applied",
                  {"entry_validation_passed": ENTRY_VALIDATED,
                   "entry_gate_reason": ENTRY_GATE_REASON}, time.time())
            write(db, state)
    if state.get("halted"):
        return state
    account_modes(client)
    if state["contract"] in LEGACY_CONTRACTS:
        state = migrate_legacy_leverage(db, client, state)
    market = client.market()
    state = recover_pending_entry(db, client, state, market)
    available, wallet = usdt_balance(client.account())
    position = position_snapshot(
        client.positions(), expected_leverage=state.get("leverage", LEVERAGE))
    orders = client.open_orders()
    external = [row for row in orders if not str(row.get("clientOrderId", "")).startswith(PREFIX)]
    if external:
        raise ValueError("Untracked BTCUSDT Futures orders exist")
    if state["phase"] == "long":
        if position["quantity"] == 0:
            state = close_epoch(db, client, state, market, wallet)
        else:
            seen = validate_protections(orders, state, position["quantity"])
            for client_id, observed in seen.items():
                record_order_observation(
                    db, state, "stop" if client_id == state["stop_client_id"] else "target",
                    client_id, "open_orders", observed, now=market["now"])
            if set(seen) != {state["stop_client_id"], state["target_client_id"]}:
                state["halted"] = "ProtectionMissing"
            state.update(quantity=str(position["quantity"]),
                         unrealized_pnl_usdt=str(position["unrealized"]))
    elif position["quantity"] != 0:
        raise ValueError("Untracked BTCUSDT Futures position exists")
    fresh = state["last_bar"] is None or market["bar"] > state["last_bar"]
    band = apply_profile(market["band"], SIGNAL_PROFILE)
    cooldown_until = state.get("cooldown_until_bar")
    cooldown_active = bool(cooldown_until and market["bar"] < cooldown_until)
    raw_enter = bool(band["enter"])
    entry_allowed = raw_enter and ENTRY_VALIDATED and not cooldown_active
    state["decision"] = {
        "owner": ("bollinger_responsive_reclaim_15m_v1" if SIGNAL_PROFILE == "responsive"
                  else "bollinger_trend_reclaim_or_squeeze_15m_v2"),
        "signal_profile": SIGNAL_PROFILE,
        "strict_enter": bool(market["band"]["enter"]),
        "raw_enter": raw_enter, "enter": entry_allowed,
        "entry_regime": band["entry_regime"],
        "lower": band["lower"], "upper": band["upper"],
        "entry_limit": band["entry_limit"], "rsi": band["rsi"],
        "lower_zone_reached": band.get("lower_zone_reached"),
        "lower_touched": band.get("lower_touched"),
        "volume_confirmed": band.get("volume_confirmed"),
        "squeeze": band.get("squeeze"), "breakout": band.get("breakout"),
        "ema50": band.get("ema50"), "ema200": band.get("ema200"),
        "recovery_confirmed": band.get("recovery_confirmed"),
        "trend_confirmed": band.get("trend_confirmed"),
        "histogram_rising": band.get("histogram_rising"),
        "entry_validation_passed": ENTRY_VALIDATED,
        "entry_gate_reason": ("cooldown_after_exit" if cooldown_active else
                              None if ENTRY_VALIDATED else ENTRY_GATE_REASON),
        "cooldown_until_bar": cooldown_until,
        "bar": market["bar"], "based_on": "closed_15m_futures_klines",
    }
    state.update(last_bar=market["bar"], last_poll=market["now"],
                 wallet_usdt=str(wallet),
                 entry_validation_passed=ENTRY_VALIDATED,
                 entry_gate_reason=ENTRY_GATE_REASON,
                 entry_mode=ENTRY_MODE,
                 model_decisions_enabled=MODEL_DECISIONS,
                 signal_profile=SIGNAL_PROFILE,
                 next_risk_profile=RISK_PROFILE,
                 marked_pnl_usdt=str(wallet + position["unrealized"]
                                     - dec(state["initial_wallet_usdt"])))
    risk_basis = dec(state["margin_usdt"])
    loss_limit = risk_basis * LOSS_LIMIT_RATE
    state["loss_limit_usdt"] = str(loss_limit)
    state["loss_limit_rate"] = str(LOSS_LIMIT_RATE)
    if state["phase"] == "cash" and dec(state["marked_pnl_usdt"]) <= -loss_limit:
        state["halted"] = "AllocationLossLimit"
        with db:
            event(db, "loss_limit_halt", {"marked_pnl_usdt": state["marked_pnl_usdt"],
                                           "limit_usdt": str(loss_limit)}, market["now"])
            write(db, state)
        return state
    if state["phase"] == "cash" and fresh and entry_allowed:
        entry_margin = available if use_all_available else margin_usdt
        if available < entry_margin:
            raise ValueError("Allocated Futures margin is no longer available")
        quantity = order_quantity(entry_margin, market["ask"], market["filters"])
        validate_leverage_bracket(client.leverage_bracket(), quantity * dec(market["ask"]))
        client_id = f"{PREFIX}e-{str(market['bar'])[-12:]}"
        context = learning_entry_context(state, market, wallet, quantity)
        prediction = entry_shadow_prediction(db, context, market["now"])
        # Keep the scored pre-entry snapshot immutable. Runtime control is
        # separate from model evidence and never rewrites an earlier forecast.
        if MODEL_DECISIONS:
            context_digest = hashlib.sha256(json.dumps(
                context, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            candidate_key = hashlib.sha256(json.dumps([
                state["identity"], state.get("epoch"), state.get("account_epoch"),
                state["initial_wallet_usdt"], market["bar"],
            ], separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            control = model_entry_decision(
                prediction, enabled=True, baseline_allowed=True,
                candidate_key=candidate_key, now=market["now"], context_digest=context_digest)
            state["model_control_latest"] = {
                **control, "bar": market["bar"],
                "prediction_status": prediction.get("status"),
                "prediction_reason": prediction.get("reason"),
                "control_evidence": prediction.get("control_evidence"),
            }
            state["decision"]["model_control_reason"] = control["reason"]
            state["decision"]["enter"] = control["allow"]
            if not control["allow"]:
                with db:
                    event(db, "model_entry_decision", state["model_control_latest"], market["now"])
                    write(db, state)  # Consume this candle without inventing a trade.
                return state
        state["pending_entry"] = client_id
        state["last_entry_margin_usdt"] = str(entry_margin)
        state["learning_entry"] = {**context, "shadow_prediction": prediction}
        with db:
            if MODEL_DECISIONS:
                event(db, "model_entry_decision", state["model_control_latest"], market["now"])
            event(db, "entry_intent", {"client_id": client_id,
                                       "quantity": str(quantity),
                                       "margin_usdt": str(entry_margin),
                                       "learning_entry": state["learning_entry"]},
                  market["now"])
            write(db, state)
        observe_order_call(db, state, "entry", client_id, "submit_response",
                           lambda: client.submit_entry(quantity, client_id), market["now"])
        position = position_snapshot(client.positions())
        if position["quantity"] <= 0:
            raise ValueError("Entry response did not create a confirmed long position")
        state = activate_long(db, client, state, market, position, client_id)
    with db:
        write(db, state)
    return state


def diagnose(client, margin_usdt, use_all_available=False):
    report = {"orders_submitted": 0,
              "allocation_mode": "all_available" if use_all_available else "fixed",
              "checks": {}}

    def balance_check():
        available, _ = usdt_balance(client.account())
        allocation = available if use_all_available else margin_usdt
        if available < allocation:
            raise ValueError(
                f"Futures USDT available {available} is below allocated {allocation}")
        return {"available_usdt": str(available), "allocation_usdt": str(allocation)}

    def flat_position_check():
        rows = [row for row in client.positions() if row.get("symbol") == SYMBOL]
        if len(rows) != 1:
            raise ValueError("Unique BTCUSDT Futures position not found")
        if dec(rows[0].get("positionAmt", "0")) != 0:
            raise ValueError("BTCUSDT Futures position must be flat before configuration")
        return True

    checks = {
        "account_modes": lambda: account_modes(client),
        "account_balance": balance_check,
        "market": client.market,
        "position_flat": flat_position_check,
        "open_orders": lambda: not bool(client.open_orders()),
        "commission": client.commission,
        "leverage_bracket": lambda: validate_leverage_bracket(client.leverage_bracket(), 0),
    }
    for name, check in checks.items():
        try:
            value = check()
            report["checks"][name] = {"ok": value is not False}
            if name == "account_balance" and isinstance(value, dict):
                report["checks"][name].update(value)
        except (ValueError, RuntimeError) as exc:
            report["checks"][name] = {"ok": False, "reason": str(exc)}
        except Exception as exc:
            report["checks"][name] = {"ok": False, "reason": type(exc).__name__}
    report["ok"] = all(row.get("ok") for row in report["checks"].values())
    return report


def order_check(client, margin_usdt, use_all_available=False):
    account_modes(client)
    available, _ = usdt_balance(client.account())
    allocation = available if use_all_available else margin_usdt
    if available < allocation:
        raise ValueError("Allocated Futures margin is not available")
    market = client.market()
    quantity = order_quantity(allocation, market["ask"], market["filters"])
    validate_leverage_bracket(client.leverage_bracket(), quantity * dec(market["ask"]))
    client.test_entry(quantity)
    return {"ok": True, "orders_submitted": 0,
            "test_endpoint": "/fapi/v1/order/test", "ledger_modified": False,
            "symbol": SYMBOL, "quantity": str(quantity), "leverage": LEVERAGE,
            "allocation_mode": "all_available" if use_all_available else "fixed",
            "allocation_usdt": str(allocation)}


def network_check():
    """Inspect public reachability and egress IP without credentials or a ledger."""
    opener = build_opener(NoRedirect())
    endpoints = (
        ("futures_public_time", "https://fapi.binance.com/fapi/v1/time"),
        ("public_ip_primary", "https://checkip.amazonaws.com/"),
        ("public_ip_secondary", "https://api.ipify.org"),
    )
    checks = {}
    for name, url in endpoints:
        started = time.monotonic()
        try:
            with opener.open(Request(url, method="GET"), timeout=5) as response:
                status = response.status
                payload = response.read(8192)
            if status != 200:
                checks[name] = {"ok": False, "http_status": status}
                continue
            if name == "futures_public_time":
                value = json.loads(payload)
                server = value.get("serverTime") if isinstance(value, dict) else None
                if type(server) is not int or server <= 0:
                    raise ValueError("Invalid server time")
                checks[name] = {"ok": True, "http_status": status}
            else:
                address = str(ipaddress.ip_address(payload.decode("ascii").strip()))
                checks[name] = {"ok": True, "http_status": status,
                                "public_ip": address}
        except HTTPError as exc:
            checks[name] = {"ok": False, "http_status": exc.code,
                            "error_type": "HTTPError"}
            exc.close()
        except Exception as exc:
            # Never include response bodies, proxy credentials or exception URLs.
            checks[name] = {"ok": False, "error_type": type(exc).__name__}
        finally:
            checks[name]["elapsed_seconds"] = round(time.monotonic() - started, 3)
    first = checks["public_ip_primary"].get("public_ip")
    second = checks["public_ip_secondary"].get("public_ip")
    consistent = first is not None and first == second
    return {
        "ok": all(row["ok"] for row in checks.values()) and consistent,
        "checked_at": time.time(), "checks": checks,
        "public_ip": first if consistent else None,
        "public_ip_consistent": consistent,
        "orders_submitted": 0, "ledger_modified": False,
        "worker_started": False, "credentials_used": False,
        "authentication_checked": False,
        "note": ("Public connectivity is not account authorization. These IP services "
                 "may use a different route from Binance with split-tunnel VPNs. "
                 "Verify the same API key IP allowlist and Futures permission, "
                 "then run Diagnose locally."),
    }


def error_details(exc):
    """Persist only structured diagnostics; never exception bodies or URLs."""
    detail = {"type": type(exc).__name__}
    if isinstance(exc, ApiError):
        detail.update(http_status=exc.status, binance_code=exc.code)
        if exc.code == -2015 or exc.status == 401:
            detail.update(category="authentication",
                          operator_action="check_same_api_key_ip_allowlist_and_futures_permission")
    if isinstance(exc, (ApiError, TransportError)):
        if (exc.method, exc.path) in Client.ALLOWED:
            detail.update(method=exc.method, path=exc.path)
            if isinstance(exc, TransportError) and type(exc.request_sent) is bool:
                detail["request_sent"] = exc.request_sent
    return detail


def record_worker_failure(db, exc, identity):
    """Best-effort terminal failure record, without claiming a fresh poll."""
    try:
        state = read(db)
        if not state or state.get("identity") != identity:
            return
        detail = error_details(exc)
        now = time.time()
        state.update(last_error="fatal_worker_failure", last_error_detail=detail,
                     last_error_at=now, worker_stopped_at=now)
        with db:
            event(db, "worker_failed", detail, now)
            write(db, state)
    except Exception:
        # Keep the original failure visible even if the ledger cannot be written.
        pass


def worker_step(db, client, margin_usdt, use_all_available=False):
    """Run one cycle; retry only failures from read-only HTTP requests."""
    try:
        state = tick(db, client, margin_usdt, use_all_available)
    except (TransportError, ApiError) as exc:
        if not exc.retry_safe:
            raise
        state = read(db)
        if not state:
            raise
        failures = int(state.get("transient_failures", 0)) + 1
        error_code = ("retryable_get_timestamp_failure" if isinstance(exc, ApiError)
                      else "retryable_get_transport_failure")
        previous_error = state.get("last_error")
        state.update(transient_failures=failures,
                     last_error=error_code, last_error_detail=error_details(exc),
                     last_error_at=time.time())
        with db:
            if failures == 1 or previous_error != error_code:
                kind = ("read_timestamp_retry_started" if isinstance(exc, ApiError)
                        else "read_transport_retry_started")
                event(db, kind, {"error": error_code,
                                 **state["last_error_detail"]}, time.time())
            write(db, state)
        return state, True
    if not state.get("halted") and state.get("last_error") in {
            "retryable_get_transport_failure", "retryable_get_timestamp_failure",
            "fatal_worker_failure"}:
        failures = int(state.get("transient_failures", 0))
        previous_error = state["last_error"]
        state.update(transient_failures=0, last_error=None, last_error_detail=None,
                     last_error_at=None, worker_stopped_at=None)
        with db:
            kind = {"retryable_get_transport_failure": "read_transport_recovered",
                    "retryable_get_timestamp_failure": "read_timestamp_recovered",
                    "fatal_worker_failure": "worker_recovered"}[previous_error]
            event(db, kind, {"failures": failures}, time.time())
            write(db, state)
    return state, False


@contextmanager
def singleton(path):
    import msvcrt
    with path.open("a+b") as handle:
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


def public_status(path=DB):
    if not path.exists():
        return {"status": "not_configured", "real_orders_submitted": 0}
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        state = read(db)
    finally:
        db.close()
    if state:
        state.pop("identity", None)
        if state.get("phase") == "cash":
            # Older workers may have persisted the final open-position mark
            # before close reconciliation.  Cash has no unrealized PnL.
            state["unrealized_pnl_usdt"] = "0"
        try:
            from btc_futures_learning import status as learning_status
            state["learning"] = learning_status(
                Path(path).parent / "btc-futures-mainnet-learning.sqlite3")
        except Exception as exc:
            state["learning"] = {
                "status": "unavailable", "error_type": type(exc).__name__,
                "decision_authority": False, "automatic_activation": False,
                "real_orders_enabled": False,
            }
        return state
    return {"status": "not_configured", "real_orders_submitted": 0,
            "ledger_present": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "network-check", "diagnose", "configure",
                                           "order-check", "run"])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--margin-usdt", type=Decimal)
    parser.add_argument("--use-all-available-balance", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(public_status(), indent=2))
        return 0
    if args.action == "network-check":
        report = network_check()
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    margin = positive(args.margin_usdt, "margin_usdt")
    if args.poll_seconds < 15 or args.poll_seconds > 300:
        raise ValueError("poll-seconds must be between 15 and 300")
    client = Client(live=args.live)
    if args.action == "diagnose":
        report = diagnose(client, margin, args.use_all_available_balance)
        print(json.dumps(report, indent=2))
        return 0
    if args.action == "order-check":
        print(json.dumps(order_check(client, margin,
                                     args.use_all_available_balance), indent=2))
        return 0
    DB.parent.mkdir(parents=True, exist_ok=True)
    with singleton(DB.with_suffix(".lock")):
        db = connect()
        try:
            if args.action == "configure":
                state = configure(db, client, margin,
                                  args.use_all_available_balance)
                safe = dict(state)
                safe.pop("identity", None)
                print(json.dumps(safe, indent=2))
                return 0
            if read(db) is None:
                # Run already requires the same explicit mainnet confirmation as
                # Configure. First start may therefore create the selected isolated
                # contract and ledger without a second launcher invocation.
                configure(db, client, margin, args.use_all_available_balance)
            while True:
                state, retrying = worker_step(
                    db, client, margin, args.use_all_available_balance)
                if state.get("halted"):
                    raise ValueError(f"HALTED: {state['halted']}")
                if retrying:
                    time.sleep(max(15, args.poll_seconds))
                    continue
                time.sleep(args.poll_seconds)
        except Exception as exc:
            if args.action == "run":
                record_worker_failure(db, exc, client.identity)
            raise
        finally:
            db.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
