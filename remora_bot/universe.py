"""Coin universes. 'a' is the original 10 large caps; 'b' avoids the top-20-by-volume coins another bot
on the shared Demo account trades (selected 2026-09-29 from 24h volume ranks ~25-45, listed before 2023)."""
import os

UNIVERSES = {
    "a": ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
          "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT"),
    "b": ("DOTUSDT", "ATOMUSDT", "ETCUSDT", "TRXUSDT", "OPUSDT",
          "APTUSDT", "INJUSDT", "ICPUSDT", "GRTUSDT", "LDOUSDT"),
}


def name() -> str:
    value = os.environ.get("REMORA_UNIVERSE", "a").lower()
    if value not in UNIVERSES:
        raise SystemExit(f"REMORA_UNIVERSE must be one of {sorted(UNIVERSES)}")
    return value


def active() -> tuple[str, ...]:
    return UNIVERSES[name()]


def shared_account() -> bool:
    """Universe b exists because the account is shared: judge risk on own PnL only."""
    return name() != "a"
