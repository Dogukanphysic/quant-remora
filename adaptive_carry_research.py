"""Walk-forward check of the self-updating carry (remora_bot/carry_adaptive.py) on universe b history.

Uses exactly the live functions: weekly prices at Monday 00:00 UTC + settled funding, configs simulated
over the trailing window, weights learned (inverse vol, smoothed), book netted across configs, cost
charged on the book's own turnover. Compared with the single fixed config the bot ran before (L7K3)
evaluated the same way. Evaluation starts 2024-09-02 (first Monday of the development window).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from remora_bot import carry_adaptive as ca, universe

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "adaptive-carry"
SYMBOLS = universe.UNIVERSES["b"]
ANCHOR = 1704067200000


def load():
    opens, funding = {}, {}
    for s in SYMBOLS:
        k = pd.read_csv(ROOT / f"data/binance-um-{s.lower()}-15m-3y.csv", usecols=["ts", "open"])
        opens[s] = dict(zip(k.ts.astype("int64"), k.open.astype(float)))
        f = pd.read_csv(ROOT / f"data/binance-um-{s.lower()}-funding-3y.csv")
        funding[s] = list(zip(f.ts.astype("int64"), f.funding_rate.astype(float)))
    return opens, funding


def stats(xs):
    s = pd.Series(xs)
    dd = (s.cumsum() - s.cumsum().cummax()).min()
    return dict(weeks=len(s), net_pct=round(s.sum() * 100, 1), sharpe=round(s.mean() / s.std() * math.sqrt(52), 2),
                max_dd_pct=round(dd * 100, 1), positive_weeks=int((s > 0).sum()))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    opens, funding = load()
    last = min(max(o) for o in opens.values())
    rebal = list(range(ANCHOR, last, ca.WEEK_MS))
    start = int(pd.Timestamp("2024-09-02", tz="UTC").value // 10**6)
    hist = ca.simulate(opens, funding, rebal, ca.DEFAULT_COST, SYMBOLS)
    by_ts = dict(hist)
    weights, prev_book, adaptive, fixed, prev_fixed, log = None, {}, [], [], {}, []
    for t0, t1 in zip(rebal, rebal[1:]):
        if t0 < start or t0 not in by_ts:
            continue
        window = [(t, r) for t, r in hist if t < t0][-ca.WINDOW_WEEKS:]   # only weeks fully before t0
        window = [(t, r) for t, r in window if t + ca.WEEK_MS <= t0]
        weights = ca.learn_weights(window, weights)
        book = ca.combined_book(funding, t0, weights, SYMBOLS)
        price = sum(book[s] * (opens[s][t1] / opens[s][t0] - 1) for s in SYMBOLS)
        paid = sum(book[s] * ca.funding_sum(funding[s], t0, t1) for s in SYMBOLS)
        turn = sum(abs(book[s] - prev_book.get(s, 0.0)) for s in SYMBOLS)
        adaptive.append(price - paid - ca.DEFAULT_COST * turn)
        prev_book = book
        fixed.append(by_ts[t0]["L7K3"])
        log.append(dict(week=str(pd.to_datetime(t0, unit="ms").date()), pnl=adaptive[-1],
                        weights={k: round(v, 3) for k, v in weights.items()}))
    split = sum(1 for r in log if r["week"] < "2025-09-01")
    res = dict(doc=__doc__, adaptive=stats(adaptive), fixed_L7K3=stats(fixed),
               adaptive_dev=stats(adaptive[:split]), adaptive_holdout=stats(adaptive[split:]),
               fixed_dev=stats(fixed[:split]), fixed_holdout=stats(fixed[split:]),
               last_weights=log[-1]["weights"])
    (OUT / "result.json").write_text(json.dumps(res, indent=2))
    pd.DataFrame(log).to_csv(OUT / "weekly.csv", index=False)
    print(json.dumps({k: v for k, v in res.items() if k != "doc"}, indent=2))


if __name__ == "__main__":
    main()
