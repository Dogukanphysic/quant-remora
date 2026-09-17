"""Fail-closed Binance Spot Testnet execution and public market-data adapters.

Real-money execution endpoints are deliberately unavailable. Public production
market data is isolated in a GET-only client without credentials or order
methods. Testnet credentials come only from environment variables and are never
persisted or logged. POST requests are never retried: a transport or server
failure can leave an order's state unknown, so callers must reconcile it by the
deterministic client order id.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    InvalidOperation,
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    localcontext,
)
import hashlib
import hmac
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid


BASE_URL = "https://testnet.binance.vision/api"
PUBLIC_MARKET_DATA_BASE_URLS = (
    "https://data-api.binance.vision/api",
    "https://api.binance.com/api",
)
SYMBOL = "BTCUSDT"
MIN_TESTNET_QUOTE_USD = Decimal("5")
MAX_TESTNET_QUOTE_USD = Decimal("25")
RECV_WINDOW = 5000
_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)

_CLIENT_ORDER_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,36}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
_KLINE_INTERVALS = frozenset({
    "1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
    "8h", "12h", "1d", "3d", "1w", "1M",
})


class AmbiguousOrderError(RuntimeError):
    """The exchange may have accepted an order; reconcile before retrying."""


class BinanceTransportError(ConnectionError):
    """A public or signed read request could not be completed."""


class BinanceAPIError(ValueError):
    """A structured, non-transient error returned by Binance's REST API."""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        path: str,
        http_status: int,
        api_code: int | None,
        signed: bool,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.http_status = http_status
        self.api_code = api_code
        self.signed = signed


class BinanceOrderNotFoundError(BinanceAPIError):
    """Definitive ``-2013`` response to a signed ``GET /v3/order`` lookup."""


class PublicMarketDataClient:
    """Credential-free, GET-only access to allowlisted Binance Spot klines."""

    def __init__(
        self,
        base_urls: tuple[str, ...] = PUBLIC_MARKET_DATA_BASE_URLS,
    ):
        if isinstance(base_urls, str):
            raise ValueError("Public market-data base URLs must be a sequence.")
        try:
            normalized = tuple(url.rstrip("/") for url in base_urls)
        except (TypeError, AttributeError) as exc:
            raise ValueError("Public market-data base URLs are invalid.") from exc
        allowlist = frozenset(PUBLIC_MARKET_DATA_BASE_URLS)
        if (not normalized or len(set(normalized)) != len(normalized)
                or any(url not in allowlist for url in normalized)):
            raise ValueError("Only allowlisted Binance public market-data URLs are supported.")
        self.base_urls = normalized

    def _fetch_klines(self, params: dict[str, object]) -> object:
        """Try each allowlisted public base once for the fixed klines path."""

        query = _encode(params)
        last_error: BaseException | None = None
        last_message = "Binance public market-data request failed."
        for base_url in self.base_urls:
            url = base_url + "/v3/klines?" + query
            request = Request(
                url, method="GET", headers={"User-Agent": "quant-remora/1"}
            )
            try:
                with urlopen(request, timeout=15) as response:
                    body = response.read().decode("utf-8")
                return json.loads(body)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                message = f"Binance public HTTP {exc.code}: {detail}"
                if exc.code not in (418, 429) and exc.code < 500:
                    raise ValueError(message) from exc
                last_error = exc
                last_message = message
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
                last_message = "Binance public market-data transport failed."
        raise BinanceTransportError(
            f"{last_message} All allowlisted public endpoints failed."
        ) from last_error

    def klines(self, interval: str = "1d", limit: int = 32,
               symbol: str = SYMBOL) -> list[list[object]]:
        """Return validated, chronologically ordered, completed Spot candles."""

        symbol = _validate_symbol(symbol)
        if interval not in _KLINE_INTERVALS:
            raise ValueError("Unsupported Binance kline interval.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Kline limit must be an integer between 1 and 1000.")
        request_limit = min(limit + 1, 1000)
        value = self._fetch_klines({
            "symbol": symbol, "interval": interval, "limit": request_limit,
        })
        if not isinstance(value, list):
            raise ValueError("Unexpected Binance public klines response.")

        now_ms = int(time.time() * 1000)
        previous_open_time: int | None = None
        closed: list[list[object]] = []
        for row in value:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                raise ValueError("Unexpected Binance public kline row.")
            if isinstance(row[0], bool) or isinstance(row[6], bool):
                raise ValueError("Unexpected Binance public kline timestamp.")
            try:
                open_time = int(row[0])
                close_time = int(row[6])
            except (TypeError, ValueError) as exc:
                raise ValueError("Unexpected Binance public kline timestamp.") from exc
            if open_time < 0 or close_time <= open_time:
                raise ValueError("Binance public kline timestamps are invalid.")
            if previous_open_time is not None and open_time <= previous_open_time:
                raise ValueError("Binance public klines are not strictly chronological.")
            previous_open_time = open_time

            open_price = _decimal(row[1], "Kline open")
            high_price = _decimal(row[2], "Kline high")
            low_price = _decimal(row[3], "Kline low")
            close_price = _decimal(row[4], "Kline close")
            volume = _decimal(row[5], "Kline volume")
            if (min(open_price, high_price, low_price, close_price) <= 0
                    or volume < 0
                    or low_price > min(open_price, close_price)
                    or high_price < max(open_price, close_price)
                    or low_price > high_price):
                raise ValueError("Binance public kline OHLCV values are invalid.")
            if close_time < now_ms:
                closed.append(list(row))
        return closed[-limit:]


@dataclass(frozen=True)
class SymbolRules:
    """Validated market-order constraints parsed from ``exchangeInfo``."""

    symbol: str
    base_asset: str
    quote_asset: str
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal

    def floor_quantity(self, quantity: object) -> Decimal:
        value = _decimal(quantity, "Quantity")
        if value <= 0:
            raise ValueError("Quantity must be positive.")
        floored = floor_step(value, self.step_size)
        if floored < self.min_qty or floored <= 0:
            raise ValueError(
                f"Quantity is below the {format(self.min_qty, 'f')} minimum "
                "after step-size flooring."
            )
        if floored > self.max_qty:
            raise ValueError(f"Quantity exceeds the {format(self.max_qty, 'f')} maximum.")
        return floored


def _decimal(value: object, label: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal value.") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be a finite decimal value.")
    return number


def _validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise ValueError("Binance symbol must contain only uppercase letters and digits.")
    return symbol


def _validate_client_order_id(client_order_id: str) -> str:
    if not isinstance(client_order_id, str) or not _CLIENT_ORDER_ID.fullmatch(client_order_id):
        raise ValueError(
            "Client order id must be 1-36 Binance-safe ASCII characters."
        )
    return client_order_id


def _validate_order_id(order_id: object, label: str = "Order id") -> int:
    if isinstance(order_id, bool):
        raise ValueError(f"{label} must be a positive integer.")
    if isinstance(order_id, int):
        value = order_id
    elif isinstance(order_id, str) and order_id.isascii() and order_id.isdigit():
        value = int(order_id)
    else:
        raise ValueError(f"{label} must be a positive integer.")
    if value <= 0:
        raise ValueError(f"{label} must be a positive integer.")
    return value


def _encode(params: dict[str, object]) -> str:
    return urlencode([(key, str(value)) for key, value in params.items()])


def sign(params: dict[str, object], secret: str) -> tuple[str, str]:
    payload = _encode(params)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload, signature


def floor_step(value: object, step: object) -> Decimal:
    with localcontext(_DECIMAL_CONTEXT):
        number = _decimal(value, "Quantity")
        increment = _decimal(step, "Step size")
        if number < 0 or increment <= 0:
            raise ValueError(
                "Quantity must be non-negative and step size must be positive."
            )
        return (
            (number / increment).to_integral_value(rounding=ROUND_DOWN)
            * increment
        )


def parse_symbol_rules(exchange_info: object, symbol: str = SYMBOL) -> SymbolRules:
    """Parse market quantity and minimum-notional rules, failing on ambiguity."""

    symbol = _validate_symbol(symbol)
    if not isinstance(exchange_info, dict) or not isinstance(exchange_info.get("symbols"), list):
        raise ValueError("Unexpected Binance exchange-info response.")
    matches = [
        item for item in exchange_info["symbols"]
        if isinstance(item, dict) and item.get("symbol") == symbol
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one exchange-info entry for {symbol}.")
    entry = matches[0]
    if entry.get("status") != "TRADING":
        raise ValueError(f"{symbol} is not TRADING on Binance Spot Testnet.")
    filters_value = entry.get("filters")
    if not isinstance(filters_value, list):
        raise ValueError("Binance symbol filters are missing.")
    filters = {
        item.get("filterType"): item
        for item in filters_value
        if isinstance(item, dict) and isinstance(item.get("filterType"), str)
    }

    # MARKET_LOT_SIZE is authoritative when it supplies a real step. Binance
    # sometimes reports zeroes there, in which case LOT_SIZE is the safe rule.
    lot = filters.get("MARKET_LOT_SIZE")
    if isinstance(lot, dict):
        market_step = _decimal(lot.get("stepSize"), "MARKET_LOT_SIZE step size")
        if market_step <= 0:
            lot = None
    if not isinstance(lot, dict):
        lot = filters.get("LOT_SIZE")
    if not isinstance(lot, dict):
        raise ValueError("Binance LOT_SIZE filter is missing.")

    step_size = _decimal(lot.get("stepSize"), "Lot step size")
    min_qty = _decimal(lot.get("minQty"), "Minimum quantity")
    max_qty = _decimal(lot.get("maxQty"), "Maximum quantity")
    if step_size <= 0 or min_qty < 0 or max_qty <= 0 or max_qty < min_qty:
        raise ValueError("Binance quantity filters are invalid.")

    min_notionals: list[Decimal] = []
    for filter_name in ("NOTIONAL", "MIN_NOTIONAL"):
        item = filters.get(filter_name)
        if isinstance(item, dict) and "minNotional" in item:
            value = _decimal(item["minNotional"], f"{filter_name} minimum notional")
            if value < 0:
                raise ValueError("Binance minimum-notional filter is invalid.")
            min_notionals.append(value)
    if not min_notionals:
        raise ValueError("Binance minimum-notional filter is missing.")

    base_asset, quote_asset = entry.get("baseAsset"), entry.get("quoteAsset")
    if not isinstance(base_asset, str) or not base_asset:
        raise ValueError("Binance base asset is missing.")
    if not isinstance(quote_asset, str) or not quote_asset:
        raise ValueError("Binance quote asset is missing.")
    return SymbolRules(
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        step_size=step_size,
        min_qty=min_qty,
        max_qty=max_qty,
        min_notional=max(min_notionals),
    )


def net_base_quantity(order: object, base_asset: str = "BTC") -> Decimal:
    """Return bought base quantity net of commissions charged in that asset."""

    if not isinstance(order, dict):
        raise ValueError("Unexpected Binance order response.")
    if order.get("side") not in (None, "BUY"):
        raise ValueError("Net acquired base quantity is defined only for BUY orders.")
    fills = order.get("fills")
    if not isinstance(fills, list) or not fills:
        raise ValueError("A FULL Binance order response with fills is required.")
    gross = Decimal("0")
    base_commission = Decimal("0")
    for fill in fills:
        if not isinstance(fill, dict):
            raise ValueError("Unexpected Binance fill response.")
        quantity = _decimal(fill.get("qty"), "Fill quantity")
        commission = _decimal(fill.get("commission", "0"), "Fill commission")
        if quantity < 0 or commission < 0:
            raise ValueError("Fill quantity and commission must be non-negative.")
        gross += quantity
        if fill.get("commissionAsset") == base_asset:
            base_commission += commission
    if "executedQty" in order:
        executed = _decimal(order["executedQty"], "Executed quantity")
        if executed != gross:
            raise ValueError("Binance FULL fills do not match executed quantity.")
    net = gross - base_commission
    if net < 0:
        raise ValueError("Base-asset commission exceeds acquired quantity.")
    return net


class Client:
    def __init__(self, api_key: str | None = None, secret: str | None = None,
                 base_url: str = BASE_URL):
        if base_url.rstrip("/") != BASE_URL:
            raise ValueError("Only Binance Spot Testnet is supported.")
        self.api_key = api_key or os.getenv("BINANCE_TESTNET_API_KEY")
        self.secret = secret or os.getenv("BINANCE_TESTNET_SECRET_KEY")
        self.base_url = BASE_URL

    def _request(self, method: str, path: str, params: dict[str, object] | None = None,
                 signed: bool = False) -> object:
        method = method.upper()
        values = dict(params or {})
        headers = {"User-Agent": "quant-remora/1"}
        if signed:
            if not self.api_key or not self.secret:
                raise ValueError("Binance Testnet API credentials are missing.")
            values.setdefault("recvWindow", RECV_WINDOW)
            values.setdefault("timestamp", int(time.time() * 1000))
            payload, signature = sign(values, self.secret)
            query = payload + "&signature=" + signature
            headers["X-MBX-APIKEY"] = self.api_key
        else:
            query = _encode(values)
        url = self.base_url + path + ("?" + query if query else "")
        request = Request(url, method=method, headers=headers)
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8")
            return json.loads(body)
        except HTTPError as exc:
            decoded = exc.read().decode("utf-8", errors="replace")
            detail = decoded[:500]
            message = f"Binance HTTP {exc.code}: {detail}"
            if method == "POST" and exc.code >= 500:
                raise AmbiguousOrderError(
                    message + "; order state is unknown, reconcile by client order id before retrying."
                ) from exc
            if method == "GET" and (exc.code in (418, 429) or exc.code >= 500):
                raise BinanceTransportError(message) from exc
            try:
                error_body = json.loads(decoded)
            except (json.JSONDecodeError, TypeError):
                error_body = None
            api_code = (
                error_body.get("code")
                if isinstance(error_body, dict)
                and isinstance(error_body.get("code"), int)
                and not isinstance(error_body.get("code"), bool)
                else None
            )
            error_type = (
                BinanceOrderNotFoundError
                if (
                    signed
                    and method == "GET"
                    and path == "/v3/order"
                    and api_code == -2013
                )
                else BinanceAPIError
            )
            raise error_type(
                message,
                method=method,
                path=path,
                http_status=exc.code,
                api_code=api_code,
                signed=signed,
            ) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if method == "POST":
                raise AmbiguousOrderError(
                    "Binance POST outcome is unknown; reconcile by client order id before retrying."
                ) from exc
            raise BinanceTransportError("Binance request could not be completed.") from exc

    def server_time(self) -> dict[str, object]:
        value = self._request("GET", "/v3/time")
        if not isinstance(value, dict):
            raise ValueError("Unexpected Binance server-time response.")
        return value

    def exchange_info(self, symbol: str = SYMBOL) -> dict[str, object]:
        value = self._request("GET", "/v3/exchangeInfo", {"symbol": _validate_symbol(symbol)})
        if not isinstance(value, dict):
            raise ValueError("Unexpected Binance exchange-info response.")
        return value

    def symbol_rules(self, symbol: str = SYMBOL) -> SymbolRules:
        return parse_symbol_rules(self.exchange_info(symbol), symbol)

    def klines(self, interval: str = "1d", limit: int = 32,
               symbol: str = SYMBOL) -> list[list[object]]:
        """Fetch public klines and omit the still-open candle, if present."""

        symbol = _validate_symbol(symbol)
        if interval not in _KLINE_INTERVALS:
            raise ValueError("Unsupported Binance kline interval.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Kline limit must be an integer between 1 and 1000.")
        request_limit = min(limit + 1, 1000)
        value = self._request(
            "GET", "/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": request_limit},
        )
        if not isinstance(value, list):
            raise ValueError("Unexpected Binance klines response.")
        now_ms = int(time.time() * 1000)
        closed: list[list[object]] = []
        for row in value:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                raise ValueError("Unexpected Binance kline row.")
            try:
                close_time = int(row[6])
            except (TypeError, ValueError) as exc:
                raise ValueError("Unexpected Binance kline close time.") from exc
            if close_time < now_ms:
                closed.append(list(row))
        return closed[-limit:]

    def book_ticker(self, symbol: str = SYMBOL) -> dict[str, object]:
        value = self._request(
            "GET", "/v3/ticker/bookTicker", {"symbol": _validate_symbol(symbol)}
        )
        if not isinstance(value, dict):
            raise ValueError("Unexpected Binance book-ticker response.")
        bid = _decimal(value.get("bidPrice"), "Best bid")
        ask = _decimal(value.get("askPrice"), "Best ask")
        if bid <= 0 or ask <= 0 or bid > ask:
            raise ValueError("Binance book ticker contains invalid prices.")
        return value

    def account(self) -> dict[str, object]:
        value = self._request("GET", "/v3/account", {"omitZeroBalances": "true"}, True)
        if not isinstance(value, dict):
            raise ValueError("Unexpected Binance account response.")
        return value

    def open_orders(self, symbol: str = SYMBOL) -> list[dict[str, object]]:
        value = self._request(
            "GET", "/v3/openOrders", {"symbol": _validate_symbol(symbol)}, True
        )
        if not isinstance(value, list):
            raise ValueError("Unexpected Binance open-orders response.")
        return value

    def order_by_client_id(self, client_id: str,
                           symbol: str = SYMBOL) -> dict[str, object]:
        value = self._request(
            "GET", "/v3/order",
            {"symbol": _validate_symbol(symbol),
             "origClientOrderId": _validate_client_order_id(client_id)},
            True,
        )
        if not isinstance(value, dict) or "orderId" not in value:
            raise ValueError("Unexpected Binance order-query response.")
        return value

    def trades_for_order(self, order_id: object,
                         symbol: str = SYMBOL) -> list[dict[str, object]]:
        """Return validated account trades belonging to exactly one order."""

        symbol = _validate_symbol(symbol)
        expected_order_id = _validate_order_id(order_id)
        value = self._request(
            "GET", "/v3/myTrades",
            {"symbol": symbol, "orderId": expected_order_id}, True,
        )
        if not isinstance(value, list):
            raise ValueError("Unexpected Binance account-trades response.")

        validated: list[dict[str, object]] = []
        seen_trade_ids: set[int] = set()
        for trade in value:
            if not isinstance(trade, dict):
                raise ValueError("Unexpected Binance account-trade row.")
            if trade.get("symbol") != symbol:
                raise ValueError("Binance account trade has an unexpected symbol.")
            if _validate_order_id(trade.get("orderId"), "Trade order id") != expected_order_id:
                raise ValueError("Binance account trade belongs to a different order.")
            trade_id = _validate_order_id(trade.get("id"), "Trade id")
            if trade_id in seen_trade_ids:
                raise ValueError("Binance account trades contain a duplicate trade id.")
            seen_trade_ids.add(trade_id)

            price = _decimal(trade.get("price"), "Trade price")
            quantity = _decimal(trade.get("qty"), "Trade quantity")
            commission = _decimal(trade.get("commission"), "Trade commission")
            commission_asset = trade.get("commissionAsset")
            if price <= 0 or quantity <= 0 or commission < 0:
                raise ValueError(
                    "Binance trade price and quantity must be positive and commission non-negative."
                )
            if not isinstance(commission_asset, str) or not commission_asset:
                raise ValueError("Binance trade commission asset is missing.")
            validated.append(dict(trade))
        return validated

    def reconcile_filled_order(self, client_order_id: str,
                               symbol: str = SYMBOL) -> dict[str, object]:
        """Recover FULL-style fills for a FILLED order queried by client id."""

        client_order_id = _validate_client_order_id(client_order_id)
        symbol = _validate_symbol(symbol)
        order = self.order_by_client_id(client_order_id, symbol)
        if order.get("status") != "FILLED":
            raise ValueError("Only a FILLED Binance order can be reconciled with trades.")
        returned_client_id = order.get("clientOrderId", order.get("origClientOrderId"))
        if returned_client_id is not None and returned_client_id != client_order_id:
            raise ValueError("Binance order query returned a different client order id.")
        if order.get("symbol") is not None and order.get("symbol") != symbol:
            raise ValueError("Binance order query returned an unexpected symbol.")

        order_id = _validate_order_id(order.get("orderId"))
        executed_quantity = _decimal(order.get("executedQty"), "Executed quantity")
        if executed_quantity <= 0:
            raise ValueError("A FILLED Binance order must have positive executed quantity.")
        trades = self.trades_for_order(order_id, symbol)

        fills: list[dict[str, object]] = []
        summed_quantity = Decimal("0")
        for trade in trades:
            quantity = _decimal(trade["qty"], "Trade quantity")
            summed_quantity += quantity
            fills.append({
                "price": format(_decimal(trade["price"], "Trade price"), "f"),
                "qty": format(quantity, "f"),
                "commission": format(
                    _decimal(trade["commission"], "Trade commission"), "f"
                ),
                "commissionAsset": trade["commissionAsset"],
                "tradeId": _validate_order_id(trade["id"], "Trade id"),
            })
        if not fills or summed_quantity != executed_quantity:
            raise ValueError(
                "Binance account-trade quantities do not match the order's executed quantity."
            )

        reconciled = dict(order)
        reconciled["fills"] = fills
        return reconciled

    def test_market_buy(self, quote_usdt: object, symbol: str = SYMBOL,
                        client_order_id: str | None = None) -> dict[str, object]:
        amount = _validated_quote_amount(quote_usdt)
        order_id = _validate_client_order_id(
            client_order_id or "remora-test-" + uuid.uuid4().hex[:20]
        )
        params = {
            "symbol": _validate_symbol(symbol), "side": "BUY", "type": "MARKET",
            "quoteOrderQty": format(amount, "f"), "newClientOrderId": order_id,
        }
        value = self._request("POST", "/v3/order/test", params, True)
        return value if isinstance(value, dict) else {"response": value}

    def place_market_buy(self, quote_usdt: object, symbol: str = SYMBOL, *,
                         client_order_id: str) -> dict[str, object]:
        _require_execution_enabled()
        amount = _validated_quote_amount(quote_usdt)
        order_id = _validate_client_order_id(client_order_id)
        params = {
            "symbol": _validate_symbol(symbol), "side": "BUY", "type": "MARKET",
            "quoteOrderQty": format(amount, "f"), "newOrderRespType": "FULL",
            "newClientOrderId": order_id,
        }
        return _validated_order_response(
            self._request("POST", "/v3/order", params, True), order_id
        )

    def place_market_sell(self, quantity: object, symbol: str = SYMBOL, *,
                          client_order_id: str) -> dict[str, object]:
        _require_execution_enabled()
        symbol = _validate_symbol(symbol)
        order_id = _validate_client_order_id(client_order_id)
        rules = self.symbol_rules(symbol)
        floored = rules.floor_quantity(quantity)
        ticker = self.book_ticker(symbol)
        bid = _decimal(ticker["bidPrice"], "Best bid")
        if floored * bid < rules.min_notional:
            raise ValueError(
                f"Sell notional is below the {format(rules.min_notional, 'f')} minimum."
            )
        params = {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": format(floored, "f"), "newOrderRespType": "FULL",
            "newClientOrderId": order_id,
        }
        return _validated_order_response(
            self._request("POST", "/v3/order", params, True), order_id
        )


def _validated_quote_amount(quote_usdt: object) -> Decimal:
    amount = _decimal(quote_usdt, "Testnet quote amount")
    if not MIN_TESTNET_QUOTE_USD <= amount <= MAX_TESTNET_QUOTE_USD:
        raise ValueError("Testnet quote amount must be between 5 and 25 USDT.")
    return amount


def _require_execution_enabled() -> None:
    if os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") != "testnet":
        raise ValueError(
            "Order execution is fail-closed; set BINANCE_ORDER_EXECUTION_ENABLED=testnet."
        )


def _validated_order_response(value: object, client_order_id: str) -> dict[str, object]:
    if not isinstance(value, dict) or "orderId" not in value:
        raise AmbiguousOrderError(
            "Binance did not return an order id; reconcile by client order id before retrying."
        )
    returned_id = value.get("clientOrderId")
    if returned_id is not None and returned_id != client_order_id:
        raise AmbiguousOrderError(
            "Binance returned a different client order id; reconcile before retrying."
        )
    return value


def public_doctor() -> dict[str, object]:
    client = Client()
    started = int(time.time() * 1000)
    server = client.server_time()
    info = client.exchange_info()
    rules = parse_symbol_rules(info)
    symbols = info.get("symbols", [])
    symbol_info = symbols[0]
    filters = {item["filterType"]: item for item in symbol_info.get("filters", [])}
    return {
        "environment": "binance_spot_testnet", "base_url": BASE_URL,
        "server_time_ms": server.get("serverTime"),
        "clock_offset_ms": int(server.get("serverTime", 0)) - started,
        "symbol": SYMBOL, "status": symbol_info["status"],
        "order_types": symbol_info.get("orderTypes", []),
        "lot_size": filters.get("LOT_SIZE"), "notional": filters.get("NOTIONAL"),
        "market_step_size": format(rules.step_size, "f"),
        "min_notional": format(rules.min_notional, "f"),
        "credentials_present": bool(client.api_key and client.secret),
        "order_execution_enabled": os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") == "testnet",
        "real_money_supported": False,
    }


__all__ = [
    "BASE_URL", "PUBLIC_MARKET_DATA_BASE_URLS", "SYMBOL",
    "MIN_TESTNET_QUOTE_USD", "MAX_TESTNET_QUOTE_USD", "AmbiguousOrderError",
    "BinanceTransportError", "BinanceAPIError", "BinanceOrderNotFoundError",
    "PublicMarketDataClient", "SymbolRules", "sign",
    "floor_step", "parse_symbol_rules", "net_base_quantity", "Client", "public_doctor",
]
