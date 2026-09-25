"""v2 learner: pooled BTC+ETH ridge on 12 causal features, target = next 24h net return.

The same feature_frame() is used by learner_v2_research.py and by the live bot,
so offline and live features cannot drift apart.  Walk-forward result
(reports/learner-v2-research): no out-of-sample edge; decision authority is
therefore only an explicit user opt-in on virtual-money environments.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

HORIZON = 6                              # bars: 24h on 4h bars, 6h on 1h bars
COST = 0.0014
TRAIN_WINDOW = pd.Timedelta(days=730)
WARMUP = 250
SYMBOL_CODE = {"BTCUSDT": 0.0, "ETHUSDT": 1.0}


def features_for(bar_hours: int) -> list[str]:
    return [f"r_{bar_hours}h", "r_1d", "r_7d", "r_30d", "donchian100_position", "vol_30d", "ema50_ema200",
            "funding_last", "funding_3d_mean", "other_asset_r_1d", "volume_ratio_30d", "symbol"]


def configure(bar_hours: int) -> None:
    """Select the bar interval (4h default, 1h). Day-based windows scale with it; 4h is unchanged."""
    global BAR_HOURS, BAR, BAR_MS, PER_DAY, FEATURES, VERSION
    if bar_hours not in (1, 4):
        raise ValueError("model_v2 supports 1h or 4h bars")
    BAR_HOURS, BAR, BAR_MS = bar_hours, pd.Timedelta(hours=bar_hours), bar_hours * 3600 * 1000
    PER_DAY = 24 // bar_hours
    FEATURES = features_for(bar_hours)
    VERSION = "pooled-ridge-next24h-v2" if bar_hours == 4 else "pooled-ridge-1h-next6h-v2"


configure(4)


def _ema(values: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(values).ewm(span=n, adjust=False).mean().to_numpy()


def feature_frame(bars: pd.DataFrame, other_close: pd.Series, funding: pd.Series, symbol: str) -> pd.DataFrame:
    """bars: OHLCV at BAR_HOURS, indexed by UTC open time. Row t uses only data closed at t+BAR."""
    d = PER_DAY
    c, h, l, v = (bars[x].to_numpy(dtype=float) for x in ("close", "high", "low", "volume"))
    s = pd.Series(c)
    hi = pd.Series(h).rolling(100).max().shift(1)
    lo = pd.Series(l).rolling(100).min().shift(1)
    r = s.pct_change()
    close_time = bars.index + BAR
    fund = funding.sort_index()
    fr = fund.to_numpy(dtype=float)
    pos = np.searchsorted(fund.index.values, close_time.values, side="right") - 1
    fmean = pd.Series(fr).rolling(9, min_periods=1).mean().to_numpy()
    safe = np.clip(pos, 0, None)
    ob = other_close.reindex(bars.index)
    x = pd.DataFrame({
        FEATURES[0]: s / s.shift(1) - 1, "r_1d": s / s.shift(d) - 1, "r_7d": s / s.shift(7 * d) - 1,
        "r_30d": s / s.shift(30 * d) - 1, "donchian100_position": ((s - lo) / (hi - lo)).clip(0, 1.5),
        "vol_30d": r.rolling(30 * d).std(), "ema50_ema200": _ema(c, 50) / _ema(c, 200) - 1,
        "funding_last": np.where(pos >= 0, fr[safe] if len(fr) else np.nan, np.nan),
        "funding_3d_mean": np.where(pos >= 0, fmean[safe] if len(fr) else np.nan, np.nan),
        "other_asset_r_1d": (ob / ob.shift(d) - 1).to_numpy(),
        "volume_ratio_30d": pd.Series(v) / pd.Series(v).rolling(30 * d).mean(),
        "symbol": SYMBOL_CODE[symbol],
    })
    x.index = bars.index
    closes = pd.Series(c, index=bars.index)
    contiguous = pd.Series(bars.index, index=bars.index).shift(-HORIZON) == bars.index + HORIZON * BAR
    y = (closes.shift(-HORIZON) / closes - 1 - COST).where(contiguous)
    return x.assign(y=y.to_numpy(), label_end=bars.index + (HORIZON + 1) * BAR, symbol_name=symbol).iloc[WARMUP:]


class Ridge:
    def fit(self, x, y):
        self.m, self.s = x.mean(0), x.std(0)
        self.s[self.s < 1e-12] = 1
        z = np.column_stack([np.ones(len(x)), (x - self.m) / self.s])
        p = np.eye(z.shape[1]) * 10
        p[0, 0] = 0
        self.b = np.linalg.solve(z.T @ z + p, z.T @ y)
        return self

    def predict(self, x):
        return np.column_stack([np.ones(len(x)), (x - self.m) / self.s]) @ self.b

    def to_json(self):
        return dict(mean=self.m.tolist(), scale=self.s.tolist(), beta=self.b.tolist())


def predict_json(model: Mapping, x: Sequence[float]) -> float:
    z = [1.0] + [(a - m) / s for a, m, s in zip(x, model["mean"], model["scale"])]
    return float(np.dot(z, model["beta"]))


def bars_frame(bars) -> pd.DataFrame:
    return pd.DataFrame([dict(open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume) for b in bars],
                        index=pd.to_datetime([b.ts for b in bars], unit="ms", utc=True))


# --- persistence ------------------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS samples(symbol TEXT, ts INTEGER, x TEXT, y REAL, label_end INTEGER,"
               " origin TEXT, PRIMARY KEY(symbol, ts))")
    db.execute("CREATE TABLE IF NOT EXISTS models(id INTEGER PRIMARY KEY, created_ms INTEGER, last_label INTEGER,"
               " value TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS predictions(symbol TEXT, ts INTEGER, model_id INTEGER, prediction REAL,"
               " created_ms INTEGER, PRIMARY KEY(symbol, ts))")
    return db


def ingest(db, frame: pd.DataFrame, now_ms: int, origin: str) -> None:
    symbol = frame["symbol_name"].iloc[0]
    ts = frame.index.as_unit("ms").asi8
    label_end = pd.DatetimeIndex(frame["label_end"]).as_unit("ms").asi8
    x = frame[FEATURES].to_numpy(dtype=float)
    y = frame["y"].to_numpy(dtype=float)
    for i in range(len(frame)):
        t = int(ts[i])
        if t + BAR_MS > now_ms or np.isnan(x[i]).any():
            continue
        late = origin == "observed" and now_ms - (t + BAR_MS) > 30 * 60 * 1000
        db.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,NULL,NULL,?)",
                   (symbol, t, json.dumps(x[i].tolist()), "backfill" if late else origin))
        if not math.isnan(y[i]) and int(label_end[i]) <= now_ms:
            db.execute("UPDATE samples SET y=?, label_end=? WHERE symbol=? AND ts=? AND y IS NULL",
                       (float(y[i]), int(label_end[i]), symbol, t))


def train(db, now_ms: int) -> None:
    newest = db.execute("SELECT MAX(label_end) FROM samples WHERE y IS NOT NULL").fetchone()[0]
    last = db.execute("SELECT last_label FROM models ORDER BY id DESC LIMIT 1").fetchone()
    if newest is None or (last and last[0] >= newest):
        return
    start = now_ms - int(TRAIN_WINDOW.total_seconds() * 1000)
    rows = db.execute("SELECT x, y FROM samples WHERE y IS NOT NULL AND label_end <= ? AND ts >= ?",
                      (now_ms, start)).fetchall()
    if len(rows) < 500:
        return
    y = np.array([r[1] for r in rows])
    model = Ridge().fit(np.array([json.loads(r[0]) for r in rows]), y).to_json()
    model.update(version=VERSION, samples=len(rows), train_mean=float(y.mean()), train_last_label=newest)
    model["digest"] = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    db.execute("INSERT INTO models(created_ms, last_label, value) VALUES (?,?,?)", (now_ms, newest, json.dumps(model)))


def register_prediction(db, symbol: str, bar_ts: int, now_ms: int) -> float | None:
    row = db.execute("SELECT x, y FROM samples WHERE symbol=? AND ts=?", (symbol, bar_ts)).fetchone()
    latest = db.execute("SELECT id, value, last_label FROM models ORDER BY id DESC LIMIT 1").fetchone()
    if not row or not latest or row[1] is not None or latest[2] > bar_ts + BAR_MS:
        return None
    existing = db.execute("SELECT prediction FROM predictions WHERE symbol=? AND ts=?", (symbol, bar_ts)).fetchone()
    if existing:
        return existing[0]
    value = predict_json(json.loads(latest[1]), json.loads(row[0]))
    if not math.isfinite(value):
        return None
    db.execute("INSERT INTO predictions VALUES (?,?,?,?,?)", (symbol, bar_ts, latest[0], value, now_ms))
    return value


def update(db, bars_by_symbol: Mapping[str, list], funding_by_symbol: Mapping[str, pd.Series], now_ms: int,
           origin: str = "observed") -> dict:
    frames = {s: bars_frame(b) for s, b in bars_by_symbol.items()}
    with db:
        for s, f in frames.items():
            other = next(o for o in frames if o != s)
            ingest(db, feature_frame(f, frames[other]["close"], funding_by_symbol[s], s), now_ms, origin)
        train(db, now_ms)
        return {s: register_prediction(db, s, int(f.index[-1].value // 10**6), now_ms) for s, f in frames.items()}


def live_forward(db) -> dict:
    rows = db.execute(
        "SELECT p.prediction, s.y, m.value FROM predictions p JOIN samples s ON s.symbol=p.symbol AND s.ts=p.ts "
        "JOIN models m ON m.id=p.model_id WHERE s.y IS NOT NULL AND p.created_ms < s.label_end").fetchall()
    if not rows:
        return dict(forward_scored=0)
    pred, y = np.array([r[0] for r in rows]), np.array([r[1] for r in rows])
    const = np.array([json.loads(r[2])["train_mean"] for r in rows])
    acc = y[pred > 0]
    return dict(forward_scored=len(rows), mse_skill=float(1 - np.mean((y - pred) ** 2) / np.mean((y - const) ** 2)),
                accepted=int(len(acc)), accepted_mean_net=float(acc.mean()) if len(acc) else None)


def status(db) -> dict:
    counts = dict(db.execute("SELECT origin, COUNT(*) FROM samples WHERE y IS NOT NULL GROUP BY origin"))
    latest = db.execute("SELECT id, value FROM models ORDER BY id DESC LIMIT 1").fetchone()
    model = json.loads(latest[1]) if latest else {}
    version = model.get("version", VERSION)
    report = "reports/learner-v2-research-1h" if "-1h-" in version else "reports/learner-v2-research"
    return dict(version=version, label_counts=counts, model_id=latest[0] if latest else None,
                model_samples=model.get("samples", 0), live_forward=live_forward(db),
                walk_forward_oos=f"no edge ({report})", profitability_proven=False)
