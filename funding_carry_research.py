"""Funding carry research: market-neutral perp-only portfolio on the 10-coin universe (research only).

Pre-registered before results:
  * signal at each rebalance: trailing mean funding rate over L days (L in 1, 3, 7)
  * short the K highest-funding perps, long the K lowest (K in 2, 3), equal notional, dollar neutral
  * rebalance every R hours (R in 8, 24, 72, 168); PnL = price legs + funding actually paid/received
  * cost 0.0007 per unit notional traded per side-change (maker entry + taker exit) on turnover
  * dev 2023-10-01 .. 2025-08-31, holdout 2025-09-01 .. end evaluated ONCE for the best qualifier
  * qualifies if dev net > 0 in each of the dev years (split at 2024-09-01) and Sharpe > 0.5
"""
from __future__ import annotations

import json
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

import universe_research as ur
from remora_bot import explore

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "funding-carry"
DEV_START, SPLIT, DEV_END = (pd.Timestamp(x, tz="UTC") for x in ("2023-10-01", "2024-09-01", "2025-09-01"))
COST = 0.0007


def load():
    close, fund = {}, {}
    for s in explore.UNIVERSE:
        bars, f = ur.load(s)
        close[s] = bars["close"]
        fund[s] = f
    c = pd.DataFrame(close).sort_index()
    # funding paid at each settlement; hourly grid, 0 elsewhere (positive = longs pay shorts)
    fr = pd.DataFrame({s: f.groupby(f.index.floor("h")).sum() for s, f in fund.items()}).reindex(c.index).fillna(0.0)
    return c, fr


def backtest(c, fr, L, K, R, start, end):
    ret = c.pct_change().shift(-1)                 # return over the hour after t (position held from t)
    sig = fr.rolling(L * 24, min_periods=L * 12).sum() / max(L, 1)
    idx = c.index[(c.index >= start) & (c.index < end)]
    rebal = idx[(idx.hour % min(R, 24) == 0) & (((idx - idx[0]) // pd.Timedelta(hours=1)) % R == 0)]
    w = pd.DataFrame(0.0, index=idx, columns=c.columns)
    cur = pd.Series(0.0, index=c.columns)
    for t in idx:
        if t in rebal:
            s = sig.loc[t].dropna()
            if len(s) >= 2 * K:
                o = s.sort_values()
                cur = pd.Series(0.0, index=c.columns)
                cur[o.index[:K]] = 0.5 / K          # long lowest funding
                cur[o.index[-K:]] = -0.5 / K        # short highest funding
        w.loc[t] = cur
    price = (w * ret.loc[idx]).sum(axis=1)
    funding = -(w * fr.shift(-1).loc[idx]).sum(axis=1)   # funding settled during the next hour
    turn = w.diff().abs().sum(axis=1).fillna(w.iloc[0].abs().sum())
    pnl = price + funding - turn * COST
    return pd.DataFrame(dict(pnl=pnl, price=price, funding=funding, cost=turn * COST)).dropna()


def stats(p):
    d = p.pnl.resample("1D").sum()
    return dict(net_pct=round(p.pnl.sum() * 100, 2), funding_pct=round(p.funding.sum() * 100, 2),
                price_pct=round(p.price.sum() * 100, 2), cost_pct=round(p.cost.sum() * 100, 2),
                sharpe=round(d.mean() / d.std() * np.sqrt(365), 2) if d.std() > 0 else None,
                max_dd_pct=round((d.cumsum() - d.cumsum().cummax()).min() * 100, 2))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    c, fr = load()
    rows = []
    for L, K, R in product((1, 3, 7), (2, 3), (8, 24, 72, 168)):
        p = backtest(c, fr, L, K, R, DEV_START, DEV_END)
        a, b = stats(p[p.index < SPLIT]), stats(p[p.index >= SPLIT])
        rows.append(dict(L=L, K=K, R=R, **stats(p), y1_net=a["net_pct"], y2_net=b["net_pct"]))
        print(rows[-1], flush=True)
    dev = pd.DataFrame(rows)
    dev.to_csv(OUT / "development.csv", index=False)
    q = dev[(dev.y1_net > 0) & (dev.y2_net > 0) & (dev.sharpe > 0.5)]
    res = dict(contract=__doc__, qualifiers=len(q))
    if len(q):
        best = q.sort_values("sharpe", ascending=False).iloc[0].to_dict()
        hp = backtest(c, fr, int(best["L"]), int(best["K"]), int(best["R"]), DEV_END, c.index.max())
        res.update(best_dev=best, holdout=stats(hp))
    (OUT / "result.json").write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "contract"}, indent=2, default=str))


if __name__ == "__main__":
    main()
