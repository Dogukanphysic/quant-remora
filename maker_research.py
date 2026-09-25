"""Would maker (limit) execution turn the universe model's edge positive?  Pre-registered test.

Uses the universe model's strictly walk-forward OOS predictions (universe_research.py) and a
conservative 1h-bar fill model:
  entry  limit BUY at close_t * (1 - offset), filled only if bar t+1 trades THROUGH it (low < limit);
         otherwise the attempt is cancelled (no trade).
  exit   limit SELL at close_{t+6} * (1 + offset), filled only if bar t+7 trades through it
         (high > limit); otherwise market exit at close_{t+7} with taker fee + slippage.
Adverse selection is inherent: buy limits fill when price falls.  Results are reported per year;
a policy only counts as positive if BOTH OOS years are positive.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

import universe_research as ur

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "maker-research"
MAKER, TAKER, SLIP, PROXY_COST = 0.0002, 0.0005, 0.0002, 0.0014
YEAR_SPLIT = pd.Timestamp("2025-09-01", tz="UTC")

CONTRACT = {
    "version": "maker-execution-test-v1",
    "predictions": "universe model walk-forward OOS 2024-09..2026-08 (universe_research.walk_forward)",
    "fill_model": "entry limit filled iff next bar low < limit; exit limit iff bar t+7 high > limit, "
                  "else taker exit at close t+7",
    "costs": {"maker_per_side": MAKER, "taker_per_side": TAKER, "taker_slippage": SLIP},
    "offsets": [0.0, 0.0005],
    "policies": {"gross_gt_0.0004": "predicted gross (pred + 0.14% proxy cost) > maker round trip",
                 "gross_gt_0.0010": "predicted gross > 0.10% (conservative)",
                 "top_ranked_hourly": "exploration: best-ranked coin every hour",
                 "all": "every coin every hour (baseline)"},
    "positive_if": "mean net per filled trade > 0 in BOTH OOS years",
}


def simulate(p, data, offset):
    rows = []
    for sym, g in p.groupby("symbol_name"):
        bars = data[sym][0]
        idx = bars.index
        pos = idx.get_indexer(g.index)
        c, h, l = (bars[k].to_numpy() for k in ("close", "high", "low"))
        n = len(bars)
        for ts, i, pred in zip(g.index, pos, g["pred"].to_numpy()):
            if i < 0 or i + 7 >= n:
                continue
            buy = c[i] * (1 - offset)
            if not l[i + 1] < buy:
                rows.append((ts, sym, pred, False, np.nan))
                continue
            sell = c[i + 6] * (1 + offset)
            if h[i + 7] > sell:
                net = sell / buy - 1 - 2 * MAKER
            else:
                net = c[i + 7] * (1 - SLIP) / buy - 1 - MAKER - TAKER
            rows.append((ts, sym, pred, True, net))
    return pd.DataFrame(rows, columns=["ts", "symbol", "pred", "filled", "net"])


def summarize(sim):
    out = {}
    policies = {
        "gross_gt_0.0004": sim["pred"] + PROXY_COST > 0.0004,
        "gross_gt_0.0010": sim["pred"] + PROXY_COST > 0.0010,
        "top_ranked_hourly": sim.index.isin(sim.sort_values("pred", ascending=False).drop_duplicates("ts").index),
        "all": pd.Series(True, index=sim.index),
    }
    for name, mask in policies.items():
        s = sim[mask]
        f = s[s["filled"]]
        years = {}
        for label, part in (("year1", f[f["ts"] < YEAR_SPLIT]), ("year2", f[f["ts"] >= YEAR_SPLIT])):
            years[label] = dict(trades=int(len(part)), mean_net=float(part["net"].mean()) if len(part) else None,
                                win_rate=float((part["net"] > 0).mean()) if len(part) else None)
        out[name] = dict(signals=int(len(s)), fill_rate=float(s["filled"].mean()) if len(s) else None,
                         trades=int(len(f)), mean_net=float(f["net"].mean()) if len(f) else None,
                         win_rate=float((f["net"] > 0).mean()) if len(f) else None, **years,
                         positive_both_years=bool(all(y["mean_net"] is not None and y["mean_net"] > 0
                                                      for y in years.values())))
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(CONTRACT, sort_keys=True, indent=2)
    (OUT / "contract.json").write_text(blob, encoding="utf-8")
    frame, data = ur.build()
    p = ur.walk_forward(frame)
    report = dict(contract_sha256=hashlib.sha256(blob.encode()).hexdigest(),
                  results={f"offset_{o}": summarize(simulate(p, data, o)) for o in CONTRACT["offsets"]})
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
