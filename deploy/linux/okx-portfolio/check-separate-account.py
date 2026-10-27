"""Refuse to start the okx-agent portfolio bot on the carry bot's Demo account (read-only GETs, no orders).

Run from /opt/okx-portfolio/app with the portfolio key file loaded. Exit 0 only when the account holds no
carry position (universe b) and no carry order (client id prefix "rmb-"); otherwise exit 1.
"""
from decimal import Decimal
import json
import sys

sys.path.insert(0, "/opt/okx-portfolio/app")
from futures_testnet_client import FuturesTestnetClient  # noqa: E402

CARRY_COINS = {"DOTUSDT", "ATOMUSDT", "ETCUSDT", "TRXUSDT", "OPUSDT", "APTUSDT", "INJUSDT", "GRTUSDT", "LDOUSDT"}

client = FuturesTestnetClient()
held = sorted(p["symbol"] for p in client.positions()
              if p.get("symbol") in CARRY_COINS and Decimal(str(p.get("positionAmt", "0"))) != 0)
carry_orders = [o["symbol"] for o in client.open_orders() if o["clientOrderId"].startswith("rmb-")]
ok = not held and not carry_orders
print(json.dumps(dict(separate_account=ok, carry_positions=held, carry_orders=sorted(set(carry_orders)),
                      orders_submitted=0)))
sys.exit(0 if ok else 1)
