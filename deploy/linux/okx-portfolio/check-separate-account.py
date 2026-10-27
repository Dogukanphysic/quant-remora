"""Guard before starting the okx-agent portfolio bot next to the carry bot (read-only GETs, no orders).

Run from /opt/okx-portfolio/app with the portfolio key file loaded.
  default   : the account must hold no carry position (universe b) and no carry order (prefix "rmb-").
  --shared  : the Demo account is deliberately shared; carry legs are reported, not refused.
Always refused: the portfolio code does not reserve every carry coin (it could then trade into a carry leg).
Exit 0 = may start, 1 = carry account in default mode, 2 = account unreadable, 3 = carry coins not reserved.
"""
from decimal import Decimal
import json
import sys

sys.path.insert(0, "/opt/okx-portfolio/app")
from futures_testnet_client import FuturesTestnetClient  # noqa: E402
import futures_testnet_portfolio as portfolio  # noqa: E402

CARRY_COINS = {"DOTUSDT", "ATOMUSDT", "ETCUSDT", "TRXUSDT", "OPUSDT", "APTUSDT", "INJUSDT", "GRTUSDT", "LDOUSDT"}
shared = "--shared" in sys.argv[1:]

missing = sorted(CARRY_COINS - set(portfolio.RESERVED_SYMBOLS))
if missing:
    print(json.dumps(dict(may_start=False, reason="carry coins not reserved", missing=missing, orders_submitted=0)))
    sys.exit(3)
client = FuturesTestnetClient()
try:
    held = sorted(p["symbol"] for p in client.positions()
                  if p.get("symbol") in CARRY_COINS and Decimal(str(p.get("positionAmt", "0"))) != 0)
    carry_orders = [o["symbol"] for o in client.open_orders() if o["clientOrderId"].startswith("rmb-")]
except Exception as exc:                       # cannot read the account: not proof of anything, exit 2
    print(json.dumps(dict(may_start=False, error=str(exc), orders_submitted=0)))
    sys.exit(2)
carry_account = bool(held or carry_orders)
ok = shared or not carry_account
print(json.dumps(dict(may_start=ok, shared_mode=shared, carry_account=carry_account, carry_positions=held,
                      carry_orders=sorted(set(carry_orders)), reserved_ok=True, orders_submitted=0)))
sys.exit(0 if ok else 1)
