"""10-coin universe model for exploration mode: walk-forward check, then seed the live model.

Informational (the user waived gating for virtual-money exploration): reports out-of-sample skill
and what the exploration policy itself (each hour: long the top-ranked coin for 6h) would have
returned after costs.  `--seed` then trains the live universe model on all history.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import unified_futures_research as ufr
from remora_bot import explore, model_v2

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "universe-research"
START = pd.Timestamp("2023-09-01", tz="UTC")
OOS_START = pd.Timestamp("2024-09-01", tz="UTC")
RETRAIN_EVERY = 720          # 30 days of 1h bars


def load(symbol):
    stem = "5y" if symbol in ("BTCUSDT", "ETHUSDT") else "3y"
    k = pd.read_csv(ROOT / f"data/binance-um-{symbol.lower()}-15m-{stem}.csv")
    k.index = pd.to_datetime(k["ts"], unit="ms", utc=True)
    f = pd.read_csv(ROOT / f"data/binance-um-{symbol.lower()}-funding-{stem}.csv")
    fs = pd.Series(f["funding_rate"].to_numpy(), index=pd.to_datetime(f["ts"], unit="ms", utc=True))
    bars = ufr.resample(k[k.index >= START][["open", "high", "low", "close", "volume"]], 1)
    return bars, fs[fs.index >= START - pd.Timedelta(days=3)]


def build():
    explore.configure()
    data = {s: load(s) for s in explore.UNIVERSE}
    btc = data["BTCUSDT"][0]["close"]
    return pd.concat([model_v2.feature_frame(b, btc, f, s) for s, (b, f) in data.items()]), data


def walk_forward(frame):
    f = frame.dropna(subset=model_v2.FEATURES).sort_index(kind="stable")
    times = f.index.unique().sort_values()
    eval_times = times[times >= OOS_START]
    out = []
    for k in range(0, len(eval_times), RETRAIN_EVERY):
        block = eval_times[k:k + RETRAIN_EVERY]
        t0 = block[0]
        train = f[(f["label_end"] <= t0) & f["y"].notna() & (f.index >= t0 - model_v2.TRAIN_WINDOW)]
        m = model_v2.Ridge().fit(train[model_v2.FEATURES].to_numpy(), train["y"].to_numpy())
        test = f[f.index.isin(block) & f["y"].notna()]
        out.append(test.assign(pred=m.predict(test[model_v2.FEATURES].to_numpy()), const=train["y"].mean()))
    return pd.concat(out)


def report(p):
    y, pr, c = p["y"].to_numpy(), p["pred"].to_numpy(), p["const"].to_numpy()
    # Exploration policy: every hour, the single top-ranked coin (ties broken by sort order).
    top = p.assign(t=p.index).sort_values("pred", ascending=False).drop_duplicates("t")
    rnd = p["y"].groupby(level=0).mean()
    per_coin = {s: dict(n=int(len(g)), mean_net=float(g["y"].mean()),
                        ic=float(g[["pred", "y"]].corr(method="spearman").iloc[0, 1]))
                for s, g in p.groupby("symbol_name")}
    return dict(
        oos_predictions=int(len(p)), mse_skill=float(1 - np.mean((y - pr) ** 2) / np.mean((y - c) ** 2)),
        ic=float(p[["pred", "y"]].corr(method="spearman").iloc[0, 1]),
        accepted_mean_net=float(y[pr > 0].mean()) if (pr > 0).any() else None,
        rejected_mean_net=float(y[pr <= 0].mean()),
        exploration_policy=dict(trades=int(len(top)), mean_net_per_trade=float(top["y"].mean()),
                                win_rate=float((top["y"] > 0).mean()),
                                vs_random_coin_mean_net=float(rnd.mean())),
        per_coin=per_coin)


def seed(frame):
    db = model_v2.connect(explore.MODEL_DB)
    now_ms = int(time.time() * 1000)
    with db:
        for _, g in frame.groupby("symbol_name"):
            model_v2.ingest(db, g, now_ms, "historical")
        model_v2.train(db, now_ms)
    return model_v2.status(db)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args(argv)
    frame, _ = build()
    OUT.mkdir(parents=True, exist_ok=True)
    result = report(walk_forward(frame))
    (OUT / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "per_coin"}, indent=2))
    if args.seed:
        print(json.dumps(seed(frame), indent=2))


if __name__ == "__main__":
    main()
