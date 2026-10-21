"""Would a trend filter help the carry book? Walk-forward on universe b, same book and stop as live.

Pre-registered (written before any result was seen):
  momentum    : m = price at the rebalance / price 7 days earlier - 1
  filter      : at each Monday rebalance, drop a short leg if m > theta and a long leg if m < -theta
                (the dropped leg stays flat that week; the rest is not re-scaled)
  candidates  : theta in {none, 5%, 10%, 20%}
  book, stop  : live adaptive book (carry_adaptive), 30% stop on 15m bars, cost 0.002 (stop_research.py)
  split       : development = weeks before 2025-09-01, holdout = from 2025-09-01
  decision    : pick the candidate with the best development Sharpe. Adopt it live only if its holdout
                Sharpe >= the unfiltered holdout Sharpe AND its holdout max drawdown is no more than
                2 percentage points worse. Otherwise nothing changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import adaptive_carry_research as acr
import stop_research as sr
from remora_bot import carry_adaptive as ca

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "trend-research"
SYMBOLS = acr.SYMBOLS
STOP = 0.30
CANDIDATES = (None, 0.05, 0.10, 0.20)


def run(theta, opens, funding, bars, rebal, hist, start):
    weights, prev, weeks, dropped = None, {}, [], 0
    by_ts = dict(hist)
    for t0, t1 in zip(rebal, rebal[1:]):
        if t0 < start or t0 not in by_ts:
            continue
        window = [(t, r) for t, r in hist if t < t0 and t + ca.WEEK_MS <= t0][-ca.WINDOW_WEEKS:]
        weights = ca.learn_weights(window, weights)
        book = ca.combined_book(funding, t0, weights, SYMBOLS)
        if theta is not None:
            for s in SYMBOLS:
                m = opens[s][t0] / opens[s][t0 - 7 * ca.DAY_MS] - 1
                if (book[s] < 0 and m > theta) or (book[s] > 0 and m < -theta):
                    book[s] = 0.0
                    dropped += 1
        pnl, held = 0.0, {}
        turn = sum(abs(book[s] - prev.get(s, 0.0)) for s in SYMBOLS)
        for s in SYMBOLS:
            w, entry = book[s], opens[s][t0]
            r, t_stop = sr.leg(bars, s, w, t0, t1, entry, STOP)
            if t_stop is None:
                pnl += w * (opens[s][t1] / entry - 1) - w * ca.funding_sum(funding[s], t0, t1)
                held[s] = w
            else:
                pnl += w * r - w * ca.funding_sum(funding[s], t0, t_stop) - sr.COST * abs(w)
                held[s] = 0.0
        weeks.append((str(pd.to_datetime(t0, unit="ms").date()), pnl - sr.COST * turn))
        prev = held
    return weeks, dropped


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    opens, funding = acr.load()
    bars = sr.load_bars()
    last = min(max(o) for o in opens.values())
    rebal = list(range(acr.ANCHOR, last, ca.WEEK_MS))
    start = int(pd.Timestamp("2024-09-02", tz="UTC").value // 10**6)
    hist = ca.simulate(opens, funding, rebal, ca.DEFAULT_COST, SYMBOLS)
    res = {}
    for theta in CANDIDATES:
        weeks, dropped = run(theta, opens, funding, bars, rebal, hist, start)
        dev = [p for w, p in weeks if w < sr.SPLIT]
        hold = [p for w, p in weeks if w >= sr.SPLIT]
        name = "none" if theta is None else f"{int(theta * 100)}%"
        res[name] = dict(all=acr.stats([p for _, p in weeks]), dev=acr.stats(dev), holdout=acr.stats(hold),
                         legs_dropped=dropped)
    best = max(res, key=lambda k: res[k]["dev"]["sharpe"])
    base = res["none"]["holdout"]
    adopt = bool(best != "none" and res[best]["holdout"]["sharpe"] >= base["sharpe"]
                 and res[best]["holdout"]["max_dd_pct"] >= base["max_dd_pct"] - 2)
    res["decision"] = dict(best_on_dev=best, adopt=adopt)
    (OUT / "result.json").write_text(json.dumps(dict(doc=__doc__, **res), indent=2))
    for k, v in res.items():
        if k == "decision":
            print(k, v)
            continue
        print(f"{k:5s} dropped={v['legs_dropped']:3d} | dev net {v['dev']['net_pct']:6.1f}% sh {v['dev']['sharpe']:5.2f} "
              f"dd {v['dev']['max_dd_pct']:6.1f} | hold net {v['holdout']['net_pct']:6.1f}% sh "
              f"{v['holdout']['sharpe']:5.2f} dd {v['holdout']['max_dd_pct']:6.1f}")


if __name__ == "__main__":
    main()
