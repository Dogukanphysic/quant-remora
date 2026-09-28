"""Edge search v5: v4's contract plus Binance positioning data (open interest, long/short ratios,
taker buy/sell volume ratio from data.binance.vision 'metrics').

Pre-registered before results: identical windows, policy, cost, qualification rule and single holdout
evaluation as edge_search_v4.  Feature sets: base+metrics and base+v4 extra+metrics.
"""
from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

import edge_search_v4 as v4
from remora_bot import explore, model_v2

OUT = v4.ROOT / "reports" / "edge-search-v5"
METRICS = ["oi_chg_1h", "oi_chg_4h", "oi_chg_24h", "oi_value_ratio_30d", "top_ls_account", "top_ls_position",
           "global_ls", "global_ls_z30d", "taker_ratio_log", "taker_ratio_log_4h", "taker_ratio_log_24h",
           "xs_rank_oi_chg_24h", "xs_rank_global_ls"]


def add_metrics(f):
    parts = []
    for s in explore.UNIVERSE:
        m = pd.read_csv(v4.ROOT / f"data/binance-um-{s.lower()}-metrics-1h.csv")
        m.index = pd.to_datetime(m["ts_ms"], unit="ms", utc=True)
        oi, oiv = m["sum_open_interest"], m["sum_open_interest_value"]
        tk = np.log(m["sum_taker_long_short_vol_ratio"].clip(lower=1e-6))
        gl = m["count_long_short_ratio"]
        x = pd.DataFrame({
            "oi_chg_1h": oi / oi.shift(1) - 1, "oi_chg_4h": oi / oi.shift(4) - 1, "oi_chg_24h": oi / oi.shift(24) - 1,
            "oi_value_ratio_30d": oiv / oiv.rolling(720, min_periods=100).mean(),
            "top_ls_account": m["count_toptrader_long_short_ratio"], "top_ls_position": m["sum_toptrader_long_short_ratio"],
            "global_ls": gl, "global_ls_z30d": (gl - gl.rolling(720, min_periods=100).mean()) / gl.rolling(720, min_periods=100).std(),
            "taker_ratio_log": tk, "taker_ratio_log_4h": tk.rolling(4).mean(), "taker_ratio_log_24h": tk.rolling(24).mean(),
        })
        x["symbol_name"] = s
        parts.append(x.reset_index(names="ts"))
    mm = pd.concat(parts)
    g = f.reset_index(names="ts").merge(mm, on=["ts", "symbol_name"], how="left").set_index("ts")
    g["xs_rank_oi_chg_24h"] = g.groupby(level=0)["oi_chg_24h"].rank(pct=True)
    g["xs_rank_global_ls"] = g.groupby(level=0)["global_ls"].rank(pct=True)
    return g


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    f = add_metrics(v4.frame())
    base = model_v2.FEATURES
    sets = {"base+metrics": base + METRICS, "all": base + v4.EXTRA + METRICS}
    mid = v4.DEV_START + (v4.DEV_END - v4.DEV_START) / 2
    rows = []
    for H in v4.HORIZONS:
        for fs, feats in sets.items():
            for kind in ("ridge", "hgb"):
                p = v4.walk_forward(f, feats, kind, H, v4.DEV_START, v4.DEV_END)
                ic = p.groupby(level=0).apply(lambda d: d.pred.corr(d.ret, method="spearman")).mean()
                for side in ("long", "short", "both"):
                    rows.append(dict(H=H, features=fs, model=kind, side=side, rank_ic=round(ic, 4),
                                     **v4.summary(v4.trades(p, side), mid)))
                    print(rows[-1], flush=True)
    dev = pd.DataFrame(rows)
    dev.to_csv(OUT / "development.csv", index=False)
    q = dev[(dev.trades >= 200) & (dev.half1_pct > 0) & (dev.half2_pct > 0)]
    result = dict(contract=__doc__, qualifiers=len(q))
    if len(q):
        best = q.sort_values("mean_net_pct", ascending=False).iloc[0].to_dict()
        H, fs, kind, side = int(best["H"]), best["features"], best["model"], best["side"]
        end = f.index.max()
        hp = v4.walk_forward(f, sets[fs], kind, H, v4.DEV_END, end)
        result.update(best_dev=best, holdout=v4.summary(v4.trades(hp, side), v4.DEV_END + (end - v4.DEV_END) / 2))
    else:
        result.update(best_dev_unqualified=dev.sort_values("mean_net_pct", ascending=False).head(5).to_dict("records"))
    (OUT / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({k: v for k, v in result.items() if k != "contract"}, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
