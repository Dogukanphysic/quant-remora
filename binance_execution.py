"""Fail-closed Binance Spot Testnet execution adapter.

Real-money endpoints are deliberately unavailable in this version. Credentials
come only from environment variables and are never persisted or logged.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid


BASE_URL = "https://testnet.binance.vision/api"
SYMBOL = "BTCUSDT"
MAX_TESTNET_QUOTE_USD = Decimal("25")
RECV_WINDOW = 5000


def _encode(params: dict[str, object]) -> str:
    return urlencode([(key, str(value)) for key, value in params.items()])


def sign(params: dict[str, object], secret: str) -> tuple[str, str]:
    payload = _encode(params)
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload, signature


def floor_step(value: object, step: object) -> Decimal:
    number, increment = Decimal(str(value)), Decimal(str(step))
    if number < 0 or increment <= 0:
        raise ValueError("Quantity and step must be positive.")
    return (number / increment).to_integral_value(rounding=ROUND_DOWN) * increment


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
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise ValueError(f"Binance HTTP {exc.code}: {detail}") from exc

    def server_time(self) -> dict[str, object]:
        return self._request("GET", "/v3/time")

    def exchange_info(self, symbol: str = SYMBOL) -> dict[str, object]:
        return self._request("GET", "/v3/exchangeInfo", {"symbol": symbol})

    def account(self) -> dict[str, object]:
        return self._request("GET", "/v3/account", {"omitZeroBalances": "true"}, True)

    def open_orders(self, symbol: str = SYMBOL) -> list[dict[str, object]]:
        value = self._request("GET", "/v3/openOrders", {"symbol": symbol}, True)
        if not isinstance(value, list):
            raise ValueError("Unexpected Binance open-orders response.")
        return value

    def test_market_buy(self, quote_usdt: object, symbol: str = SYMBOL) -> dict[str, object]:
        amount = Decimal(str(quote_usdt))
        if not Decimal("5") <= amount <= MAX_TESTNET_QUOTE_USD:
            raise ValueError("Testnet quote amount must be between 5 and 25 USDT.")
        params = {"symbol": symbol, "side": "BUY", "type": "MARKET",
                  "quoteOrderQty": format(amount, "f"),
                  "newClientOrderId": "remora-test-" + uuid.uuid4().hex[:20]}
        value = self._request("POST", "/v3/order/test", params, True)
        return value if isinstance(value, dict) else {"response": value}

    def place_market_buy(self, quote_usdt: object, symbol: str = SYMBOL) -> dict[str, object]:
        if os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") != "testnet":
            raise ValueError("Order execution is fail-closed; set BINANCE_ORDER_EXECUTION_ENABLED=testnet.")
        amount = Decimal(str(quote_usdt))
        if not Decimal("5") <= amount <= MAX_TESTNET_QUOTE_USD:
            raise ValueError("Testnet quote amount must be between 5 and 25 USDT.")
        params = {"symbol": symbol, "side": "BUY", "type": "MARKET",
                  "quoteOrderQty": format(amount, "f"), "newOrderRespType": "FULL",
                  "newClientOrderId": "remora-" + uuid.uuid4().hex[:24]}
        value = self._request("POST", "/v3/order", params, True)
        if not isinstance(value, dict) or "orderId" not in value:
            raise ValueError("Binance did not return an order id; reconcile before retrying.")
        return value


def public_doctor() -> dict[str, object]:
    client = Client()
    started = int(time.time() * 1000)
    server = client.server_time()
    info = client.exchange_info()
    symbols = info.get("symbols", []) if isinstance(info, dict) else []
    if len(symbols) != 1 or symbols[0].get("status") != "TRADING":
        raise ValueError("BTCUSDT is not TRADING on Binance Spot Testnet.")
    filters = {item["filterType"]: item for item in symbols[0].get("filters", [])}
    return {"environment": "binance_spot_testnet", "base_url": BASE_URL,
            "server_time_ms": server.get("serverTime"),
            "clock_offset_ms": int(server.get("serverTime", 0)) - started,
            "symbol": SYMBOL, "status": symbols[0]["status"],
            "order_types": symbols[0].get("orderTypes", []),
            "lot_size": filters.get("LOT_SIZE"), "notional": filters.get("NOTIONAL"),
            "credentials_present": bool(client.api_key and client.secret),
            "order_execution_enabled": os.getenv("BINANCE_ORDER_EXECUTION_ENABLED") == "testnet",
            "real_money_supported": False}


__all__ = ["BASE_URL", "SYMBOL", "MAX_TESTNET_QUOTE_USD", "sign", "floor_step",
           "Client", "public_doctor"]
