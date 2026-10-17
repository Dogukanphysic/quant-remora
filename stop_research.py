"""Does the carry bot's protective stop help? Walk-forward on universe b, same book as the live bot.

Pre-registered (written before any result was seen):
  candidates  : stop distance d in {none, 10%, 15% (live), 20%, 25%, 30%} from the weekly entry price
  book        : exactly adaptive_carry_research.py (live carry_adaptive functions, weights learned from the
                no-stop config simulation, as live)
  stop model  : 15m bars; long stopped when low <= entry*(1-d), short when high >= entry*(1+d); fill at
                the stop level, or at the bar open if the bar gaps through it; funding counted only until
                the stop; the leg stays flat until the next Monday; the exit costs one more turnover
  cost        : 0.002 per unit traded (the bot's live learned cost)
  split       : development = weeks before 2025-09-01, holdout = from 2025-09-01
  decision    : pick the candidate with the best development Sharpe. Adopt it live only if its holdout
                Sharpe >= the 15% stop's holdout Sharpe AND its holdout max drawdown is no more than
                2 percentage points worse. Otherwise the live 15% stop stays.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import adaptive_carry_research as acr
from remora_bot import carry_adaptive as ca

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "stop-research"
SYMBOLS = acr.SYMBOLS
COST = 0.002
CANDIDATES = (None, 0.10, 0.15, 0.20, 0.25, 0.30)
SPLIT = "2025-09-01"


def load_bars():
    bars = {}
    for s in SYMBOLS:
        k = pd.read_csv(ROOT / f"data/binance-um-{s.lower()}-15m-3y.csv", usecols=["ts", "open", "high", "low"])
        bars[s] = (k.ts.to_numpy("int64"), k.open.to_numpy(float), k.high.to_numpy(float), k.low.to_numpy(float))
    return bars


def leg(bars, s, w, t0, t1, entry, d):
    """(return of the signed leg, stop time or None)."""
    ts, op, hi, lo = bars[s]
    if d is None or w == 0:
        return None, None
    i, j = np.searchsorted(ts, t0), np.searchsorted(ts, t1)
    if w > 0:
        level = entry * (1 - d)
        hit = np.nonzero(lo[i:j] <= level)[0]
        if not len(hit):
            return None, None
        k = i + hit[0]
        return min(op[k], level) / entry - 1, int(ts[k])
    level = entry * (1 + d)
    hit = np.nonzero(hi[i:j] >= level)[0]
    if not len(hit):
        return None, None
    k = i + hit[0]
    return max(op[k], level) / entry - 1, int(ts[k])


def run(d, opens, funding, bars, rebal, hist, start):
    weights, prev, weeks, stops = None, {}, [], 0
    for t0, t1 in zip(rebal, rebal[1:]):
        if t0 < start or t0 not in dict(hist):
            continue
        window = [(t, r) for t, r in hist if t < t0 and t + ca.WEEK_MS <= t0][-ca.WINDOW_WEEKS:]
        weights = ca.learn_weights(window, weights)
        book = ca.combined_book(funding, t0, weights, SYMBOLS)
        pnl, held = 0.0, {}
        turn = sum(abs(book[s] - prev.get(s, 0.0)) for s in SYMBOLS)
        for s in SYMBOLS:
            w, entry = book[s], opens[s][t0]
            r, t_stop = leg(bars, s, w, t0, t1, entry, d)
            if t_stop is None:
                pnl += w * (opens[s][t1] / entry - 1) - w * ca.funding_sum(funding[s], t0, t1)
                held[s] = w
            else:
                pnl += w * r - w * ca.funding_sum(funding[s], t0, t_stop) - COST * abs(w)
                held[s] = 0.0
                stops += 1
        weeks.append((str(pd.to_datetime(t0, unit="ms").date()), pnl - COST * turn))
        prev = held
    return weeks, stops


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    opens, funding = acr.load()
    bars = load_bars()
    last = min(max(o) for o in opens.values())
    rebal = list(range(acr.ANCHOR, last, ca.WEEK_MS))
    start = int(pd.Timestamp("2024-09-02", tz="UTC").value // 10**6)
    hist = ca.simulate(opens, funding, rebal, ca.DEFAULT_COST, SYMBOLS)
    res = {}
    for d in CANDIDATES:
        weeks, stops = run(d, opens, funding, bars, rebal, hist, start)
        dev = [p for w, p in weeks if w < SPLIT]
        hold = [p for w, p in weeks if w >= SPLIT]
        name = "none" if d is None else f"{int(d * 100)}%"
        res[name] = dict(all=acr.stats([p for _, p in weeks]), dev=acr.stats(dev), holdout=acr.stats(hold),
                         stops_hit=stops)
    best = max(res, key=lambda k: res[k]["dev"]["sharpe"])
    live = res["15%"]["holdout"]
    adopt = bool(best != "15%" and res[best]["holdout"]["sharpe"] >= live["sharpe"]
             and res[best]["holdout"]["max_dd_pct"] >= live["max_dd_pct"] - 2)
    res["decision"] = dict(best_on_dev=best, adopt=adopt, live_stays="15%" if not adopt else None)
    (OUT / "result.json").write_text(json.dumps(dict(doc=__doc__, **res), indent=2))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
