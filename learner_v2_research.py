"""Pre-registered walk-forward study for the bot's veto learner (v2).

Replaces "wait for 60 live labels" with thousands of strictly causal out-of-sample
predictions: each month the model is refit only on labels that ended before the
prediction bar, then scores the next month.  Model choice uses the development
window only; the authority decision uses the untouched window once.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

import unified_futures_research as ufr
from remora_bot import model_v2

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "learner-v2-research"
HORIZON = 6                    # 24h
COST = 0.0014                  # round trip at base cost
RETRAIN_EVERY = 180            # 30 days of 4h bars
TRAIN_WINDOW = 6 * 365 * 2     # last 2 years of labels per refit, per symbol
MODEL_EVAL = (pd.Timestamp("2023-09-01", tz="UTC"), pd.Timestamp("2024-09-01", tz="UTC"))
GATE_EVAL_START = pd.Timestamp("2024-09-01", tz="UTC")

CONTRACT = {
    "version": "learner-v2-walkforward-v1",
    "target": "next 24h (6x4h) close-to-close return minus 0.14% round-trip cost",
    "features": ["r_4h", "r_1d", "r_7d", "r_30d", "donchian100_position", "vol_30d", "ema50_ema200",
                 "funding_last", "funding_3d_mean", "other_asset_r_1d", "volume_ratio_30d", "symbol"],
    "walk_forward": "refit every 180 bars on labels ending before the prediction bar (purged), 2y window",
    "models": {"ridge": "standardized ridge alpha=10", "hgb": "HistGradientBoosting depth3 lr0.05 300 iters l2=1"},
    "model_choice": "higher MSE skill (1-mse/const_mse) on 2023-09..2024-08 OOS; if both <=0 -> no authority",
    "gate_window": ">= 2024-09-01 (never used for choice), evaluated once",
    "gate": {"n_min": 1000, "mse_skill": "> 0", "ic_nonoverlap_t": ">= 2.0",
             "accepted_minus_rejected_mean": "> 0", "accepted_mean": "> 0"},
    "live": "authority revoked if >= 60 live forward labels exist and live mse_skill <= 0 or accepted mean <= 0",
}


def load_history():
    """4h bars and funding from the verified 5y archives (shared with the live bot's seed)."""
    data = {}
    for s in ("BTCUSDT", "ETHUSDT"):
        k, f = ufr.load(s)
        data[s] = (ufr.resample(k, 4), f)
    return data


def build(symbol, other, data=None):
    data = data or load_history()
    bars, funding = data[symbol]
    return model_v2.feature_frame(bars, data[other][0]["close"], funding, symbol)


Ridge = model_v2.Ridge


def make(name):
    if name == "ridge":
        return Ridge()
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=300, l2_regularization=1.0,
                                         random_state=7)


FEATURES = model_v2.FEATURES
assert FEATURES == CONTRACT["features"]


def walk_forward(frame, name, start):
    frame = frame.dropna(subset=FEATURES)
    frame = frame.sort_index(kind="stable")
    times = frame.index.unique().sort_values()
    eval_times = times[times >= start]
    preds = []
    for k in range(0, len(eval_times), RETRAIN_EVERY):
        block = eval_times[k:k + RETRAIN_EVERY]
        t0 = block[0]
        train = frame[(frame["label_end"] <= t0) & frame["y"].notna()]
        train = train[train.index >= t0 - pd.Timedelta(hours=4 * TRAIN_WINDOW)]
        model = make(name).fit(train[FEATURES].to_numpy(), train["y"].to_numpy())
        test = frame[frame.index.isin(block) & frame["y"].notna()]
        if len(test):
            preds.append(test.assign(pred=model.predict(test[FEATURES].to_numpy()), const=train["y"].mean()))
    return pd.concat(preds)


def score(p):
    y, pr, c = p["y"].to_numpy(), p["pred"].to_numpy(), p["const"].to_numpy()
    mse, cmse = np.mean((y - pr) ** 2), np.mean((y - c) ** 2)
    nonoverlap = p.groupby("symbol_name", group_keys=False).apply(lambda g: g.iloc[::HORIZON])
    ic = nonoverlap[["pred", "y"]].corr(method="spearman").iloc[0, 1]
    n = len(nonoverlap)
    acc, rej = y[pr > 0], y[pr <= 0]
    return dict(n=int(len(p)), n_nonoverlap=int(n), mse_skill=float(1 - mse / cmse), ic=float(ic),
                ic_t=float(ic * np.sqrt((n - 2) / max(1e-12, 1 - ic ** 2))),
                accepted=int(len(acc)), accepted_mean=float(acc.mean()) if len(acc) else None,
                rejected_mean=float(rej.mean()) if len(rej) else None, base_mean=float(y.mean()))


def veto_effect(p, frame):
    """Donchian-100 entries in the window: 24h forward return with and without the veto."""
    rows = []
    for sym, g in frame.groupby("symbol_name"):
        pos = g["donchian100_position"]
        entry = (pos > 1) & (pos.shift(1) <= 1)
        e = p[(p["symbol_name"] == sym) & p.index.isin(g.index[entry.to_numpy()])]
        rows.append(e)
    e = pd.concat(rows)
    kept = e[e["pred"] > 0]
    return dict(entries=int(len(e)), mean_all=float(e["y"].mean()) if len(e) else None,
                kept=int(len(kept)), mean_kept=float(kept["y"].mean()) if len(kept) else None)


def gate(s):
    g = CONTRACT["gate"]
    return bool(s["n"] >= g["n_min"] and s["mse_skill"] > 0 and s["ic_t"] >= 2.0 and s["accepted_mean"] is not None
                and s["accepted_mean"] > 0 and s["rejected_mean"] is not None
                and s["accepted_mean"] - s["rejected_mean"] > 0)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(CONTRACT, sort_keys=True, indent=2)
    (OUT / "contract.json").write_text(blob, encoding="utf-8")
    data = load_history()
    frame = pd.concat([build("BTCUSDT", "ETHUSDT", data), build("ETHUSDT", "BTCUSDT", data)])
    dev_scores = {}
    for name in ("ridge", "hgb"):
        p = walk_forward(frame[frame.index < MODEL_EVAL[1]], name, MODEL_EVAL[0])
        dev_scores[name] = score(p)
    best = max(dev_scores, key=lambda n: dev_scores[n]["mse_skill"])
    report = dict(contract_sha256=hashlib.sha256(blob.encode()).hexdigest(), development=dev_scores)
    if dev_scores[best]["mse_skill"] <= 0:
        report.update(chosen=None, authority=False, reason="no model beat the constant on development OOS")
    else:
        p = walk_forward(frame, best, GATE_EVAL_START)
        s = score(p)
        report.update(chosen=best, gate_window=s, veto_effect=veto_effect(p, frame), authority=gate(s),
                      reason="walk_forward_gate_passed" if gate(s) else "walk_forward_gate_failed")
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
