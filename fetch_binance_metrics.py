"""Download Binance USD-M daily 'metrics' archives (open interest, long/short ratios, taker vol ratio)
from the public data.binance.vision archive and store them as hourly CSVs under data/.

Public, unauthenticated, read-only. Values are the last 5-minute reading of each hour.
"""
from __future__ import annotations

import io
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pandas as pd

from remora_bot.explore import UNIVERSE

ROOT = Path(__file__).resolve().parent
BASE = "https://data.binance.vision/data/futures/um/daily/metrics"
START = pd.Timestamp("2023-09-01")


def one(symbol, day):
    url = f"{BASE}/{symbol}/{symbol}-metrics-{day:%Y-%m-%d}.zip"
    for _ in range(3):
        try:
            with urlopen(url, timeout=30) as r:
                z = zipfile.ZipFile(io.BytesIO(r.read()))
            return pd.read_csv(z.open(z.namelist()[0]))
        except HTTPError as e:
            if e.code == 404:
                return None
        except Exception:
            pass
    return None


def fetch(symbol):
    out = ROOT / f"data/binance-um-{symbol.lower()}-metrics-1h.csv"
    days = pd.date_range(START, pd.Timestamp.utcnow().tz_localize(None).normalize() - pd.Timedelta(days=1))
    with ThreadPoolExecutor(16) as ex:
        frames = [f for f in ex.map(lambda d: one(symbol, d), days) if f is not None and len(f)]
    m = pd.concat(frames)
    m["ts"] = pd.to_datetime(m["create_time"], utc=True)
    m = m.sort_values("ts").drop_duplicates("ts")
    cols = ["sum_open_interest", "sum_open_interest_value", "count_toptrader_long_short_ratio",
            "sum_toptrader_long_short_ratio", "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
    h = m.set_index("ts")[cols].apply(pd.to_numeric, errors="coerce").resample("1h", label="left").last()
    h.insert(0, "ts_ms", h.index.as_unit("ms").asi8)
    h.to_csv(out, index=False)
    print(f"{symbol}: {len(frames)}/{len(days)} days, {len(h)} hours -> {out.name}", flush=True)


if __name__ == "__main__":
    for s in sys.argv[1:] or UNIVERSE:
        fetch(s)
