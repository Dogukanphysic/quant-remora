"""Edge search v4: hold period x side x feature set x model on the 10-coin universe (research only).

Pre-registered before results:
  * development window 2024-09-01 .. 2025-08-31 (walk-forward, retrain every 30 days, 730-day train window)
  * holdout 2025-09-01 .. end, evaluated ONCE, only for the single best development config
  * policy per entry time: go long the top-2 predictions if pred > cost, short the bottom-2 if pred < -cost
    (side="long"/"short" restricts it); entries only every H hours so trades never overlap
  * cost 0.0007 per round trip (post-only maker entry 0.02% + taker exit 0.05%)
  * a config qualifies only with >= 200 dev trades and positive mean net in both dev halves;
    best = highest dev mean net among qualifiers
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

import universe_research as ur
from remora_bot import explore, model_v2

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "edge-search-v4"
DEV_START, DEV_END = pd.Timestamp("2024-09-01", tz="UTC"), pd.Timestamp("2025-09-01", tz="UTC")
COST = 0.0007
HORIZONS = (1, 3, 6, 12, 24)
RETRAIN = pd.Timedelta(days=30)
EXTRA = ["r_4h", "r_12h", "range_1h", "dist_24h_high", "dist_24h_low", "vol_ratio_1d_30d",
         "funding_change_3d", "xs_rank_r_1d", "xs_rank_r_7d", "xs_rank_funding", "hour_sin", "hour_cos"]


def frame():
    explore.configure()
    data = {s: ur.load(s) for s in explore.UNIVERSE}
    btc = data["BTCUSDT"][0]["close"]
    parts = []
    for s, (b, f) in data.items():
        x = model_v2.feature_frame(b, btc, f, s).drop(columns=["y", "label_end"])
        c = b["close"].reindex(x.index)
        h, l = b["high"].reindex(x.index), b["low"].reindex(x.index)
        full = b["close"]
        x["r_4h"] = (full / full.shift(4) - 1).reindex(x.index)
        x["r_12h"] = (full / full.shift(12) - 1).reindex(x.index)
        x["range_1h"] = (h - l) / c
        x["dist_24h_high"] = (c / b["high"].rolling(24).max().reindex(x.index) - 1)
        x["dist_24h_low"] = (c / b["low"].rolling(24).min().reindex(x.index) - 1)
        r = full.pct_change()
        x["vol_ratio_1d_30d"] = (r.rolling(24).std() / r.rolling(720).std()).reindex(x.index)
        x["funding_change_3d"] = x["funding_last"] - x["funding_3d_mean"]
        x["hour_sin"] = np.sin(2 * np.pi * x.index.hour / 24)
        x["hour_cos"] = np.cos(2 * np.pi * x.index.hour / 24)
        for H in HORIZONS:
            fwd = full.shift(-H) / full - 1
            ok = pd.Series(full.index, index=full.index).shift(-H) == full.index + pd.Timedelta(hours=H)
            x[f"y{H}"] = fwd.where(ok).reindex(x.index)
        parts.append(x)
    f = pd.concat(parts)
    for col, src in (("xs_rank_r_1d", "r_1d"), ("xs_rank_r_7d", "r_7d"), ("xs_rank_funding", "funding_last")):
        f[col] = f.groupby(level=0)[src].rank(pct=True)
    return f


def fit_predict(kind, xtr, ytr, xte):
    if kind == "ridge":     # metrics features (v5) have gaps/inf; v4's own features have none
        clean = lambda a: np.clip(np.nan_to_num(a, nan=0.0, posinf=0.0, neginf=0.0), -1e3, 1e3)
        return model_v2.Ridge().fit(clean(xtr), ytr).predict(clean(xte))
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                      min_samples_leaf=500, l2_regularization=1.0, random_state=0)
    return m.fit(np.nan_to_num(xtr), ytr).predict(np.nan_to_num(xte))


def walk_forward(f, feats, kind, H, start, end):
    y = f"y{H}"
    g = f.dropna(subset=model_v2.FEATURES)
    g = g[(g.index.hour % H == 0) | (H == 1)] if H <= 24 else g
    out, t0 = [], start
    while t0 < end:
        t1 = min(t0 + RETRAIN, end)
        tr = f[(f.index < t0 - pd.Timedelta(hours=H + 1)) & (f.index >= t0 - model_v2.TRAIN_WINDOW)].dropna(
            subset=model_v2.FEATURES + [y])
        if kind == "hgb" and len(tr) > 120_000:
            tr = tr.iloc[np.linspace(0, len(tr) - 1, 120_000).astype(int)]
        te = g[(g.index >= t0) & (g.index < t1)].dropna(subset=[y])
        if len(te):
            out.append(te[[y, "symbol_name"]].rename(columns={y: "ret"}).assign(
                pred=fit_predict(kind, tr[feats].to_numpy(), tr[y].to_numpy(), te[feats].to_numpy())))
        t0 = t1
    return pd.concat(out)


def trades(p, side):
    p = p[p.groupby(level=0)["pred"].transform("count") >= 8]
    rk = p.groupby(level=0)["pred"].rank(ascending=False)
    rkb = p.groupby(level=0)["pred"].rank(ascending=True)
    longs = p[(rk <= 2) & (p.pred > COST)].assign(net=lambda d: d.ret - COST)
    shorts = p[(rkb <= 2) & (p.pred < -COST)].assign(net=lambda d: -d.ret - COST)
    return {"long": longs, "short": shorts, "both": pd.concat([longs, shorts])}[side]


def summary(t, mid):
    a, b = t[t.index < mid], t[t.index >= mid]
    return dict(trades=len(t), mean_net_pct=round(t.net.mean() * 100, 4) if len(t) else None,
                win_rate=round((t.net > 0).mean(), 3) if len(t) else None,
                half1_pct=round(a.net.mean() * 100, 4) if len(a) else None,
                half2_pct=round(b.net.mean() * 100, 4) if len(b) else None,
                total_pct=round(t.net.sum() * 100, 1))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    f = frame()
    sets = {"base": model_v2.FEATURES, "extended": model_v2.FEATURES + EXTRA}
    mid = DEV_START + (DEV_END - DEV_START) / 2
    rows, preds = [], {}
    for H in HORIZONS:
        for fs, feats in sets.items():
            for kind in ("ridge", "hgb"):
                if kind == "hgb" and fs == "base":
                    continue
                p = walk_forward(f, feats, kind, H, DEV_START, DEV_END)
                preds[(H, fs, kind)] = p
                ic = p.groupby(level=0).apply(lambda d: d.pred.corr(d.ret, method="spearman")).mean()
                for side in ("long", "short", "both"):
                    s = summary(trades(p, side), mid)
                    rows.append(dict(H=H, features=fs, model=kind, side=side, rank_ic=round(ic, 4), **s))
                    print(rows[-1], flush=True)
    dev = pd.DataFrame(rows)
    dev.to_csv(OUT / "development.csv", index=False)
    q = dev[(dev.trades >= 200) & (dev.half1_pct > 0) & (dev.half2_pct > 0)]
    result = dict(contract=__doc__, qualifiers=len(q))
    if len(q):
        best = q.sort_values("mean_net_pct", ascending=False).iloc[0].to_dict()
        H, fs, kind, side = int(best["H"]), best["features"], best["model"], best["side"]
        hp = walk_forward(f, sets[fs], kind, H, DEV_END, f.index.max())
        hmid = DEV_END + (f.index.max() - DEV_END) / 2
        result.update(best_dev=best, holdout=summary(trades(hp, side), hmid))
    else:
        result.update(best_dev_unqualified=dev.sort_values("mean_net_pct", ascending=False).head(5).to_dict("records"))
    (OUT / "result.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({k: v for k, v in result.items() if k != "contract"}, indent=2, default=str))


if __name__ == "__main__":
    sys.exit(main())
