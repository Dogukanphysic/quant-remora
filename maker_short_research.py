"""Follow-up to maker_research.py: add the short side (USD-M allows it).  Pre-registered before running.

Hypothesis formed AFTER seeing that every long-only policy lost in the second (falling) OOS year, so
even a positive result here is partly hindsight; only forward Demo trades can confirm it.
Same fill model mirrored for shorts: limit SELL at close_t filled iff next bar high > limit; exit limit
BUY at close_{t+6} filled iff bar t+7 low < limit, else taker exit at close_{t+7}.  Funding ignored.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

import maker_research as mr
import universe_research as ur

OUT = Path(__file__).resolve().parent / "reports" / "maker-short-research"
CONTRACT = {
    "version": "maker-long-short-test-v1",
    "basis": "universe walk-forward OOS predictions; maker fill model of maker-execution-test-v1 at offset 0",
    "entry": "long if predicted gross > T; short if predicted gross < -T; T in {0.0004, 0.0010}",
    "policies": ["long_only", "short_only", "long_short"],
    "positive_if": "mean net per filled trade > 0 in BOTH OOS years",
    "caveat": "hypothesis chosen after seeing year-2 long losses; funding not modelled",
}


def simulate(p, data):
    rows = []
    for sym, g in p.groupby("symbol_name"):
        bars = data[sym][0]
        pos = bars.index.get_indexer(g.index)
        c, h, l = (bars[k].to_numpy() for k in ("close", "high", "low"))
        for ts, i, pred in zip(g.index, pos, g["pred"].to_numpy()):
            if i < 0 or i + 7 >= len(c):
                continue
            gross = pred + mr.PROXY_COST
            long_net = short_net = np.nan
            if l[i + 1] < c[i]:
                long_net = (c[i + 6] / c[i] - 1 - 2 * mr.MAKER if h[i + 7] > c[i + 6]
                            else c[i + 7] * (1 - mr.SLIP) / c[i] - 1 - mr.MAKER - mr.TAKER)
            if h[i + 1] > c[i]:
                short_net = (c[i] / c[i + 6] - 1 - 2 * mr.MAKER if l[i + 7] < c[i + 6]
                             else c[i] / (c[i + 7] * (1 + mr.SLIP)) - 1 - mr.MAKER - mr.TAKER)
            rows.append((ts, sym, gross, long_net, short_net))
    return pd.DataFrame(rows, columns=["ts", "symbol", "gross", "long_net", "short_net"])


def stats(net, ts):
    ok = ~np.isnan(net)
    net, ts = net[ok], ts[ok]
    years = {}
    for label, m in (("year1", ts < mr.YEAR_SPLIT), ("year2", ts >= mr.YEAR_SPLIT)):
        years[label] = dict(trades=int(m.sum()), mean_net=float(net[m].mean()) if m.any() else None)
    return dict(trades=int(len(net)), mean_net=float(net.mean()) if len(net) else None,
                win_rate=float((net > 0).mean()) if len(net) else None, **years,
                positive_both_years=all(y["mean_net"] is not None and y["mean_net"] > 0 for y in years.values()))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(CONTRACT, sort_keys=True, indent=2)
    (OUT / "contract.json").write_text(blob, encoding="utf-8")
    frame, data = ur.build()
    sim = simulate(ur.walk_forward(frame), data)
    ts = sim["ts"].to_numpy()
    results = {}
    for T in (0.0004, 0.0010):
        lg, sh = (sim["gross"] > T).to_numpy(), (sim["gross"] < -T).to_numpy()
        ln, sn = sim["long_net"].to_numpy(), sim["short_net"].to_numpy()
        results[f"T_{T}"] = {
            "long_only": stats(np.where(lg, ln, np.nan), ts),
            "short_only": stats(np.where(sh, sn, np.nan), ts),
            "long_short": stats(np.where(lg, ln, np.where(sh, sn, np.nan)), ts),
        }
    report = dict(contract_sha256=hashlib.sha256(blob.encode()).hexdigest(), results=results)
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for T, pols in results.items():
        for name, v in pols.items():
            f = lambda x: "None" if x is None else f"{x * 100:+.3f}%"
            print(f"{T:8s} {name:11s} trades={v['trades']:6d} mean={f(v['mean_net'])} "
                  f"y1={f(v['year1']['mean_net'])} y2={f(v['year2']['mean_net'])} both={v['positive_both_years']}")


if __name__ == "__main__":
    main()
