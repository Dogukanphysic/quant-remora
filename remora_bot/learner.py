"""Pooled BTC+ETH ridge learner with a forward-only authority gate.

Target: next 4h close-to-close return net of round-trip cost (a proxy, not a
trade result).  A prediction is stored before its label bar closes; only such
predictions count toward the gate.  Historical seed rows train the model but
never fill forward counters.  Authority is veto-only: the model may block an
entry of the base rule, it can never open a trade.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Sequence

import numpy as np

from .strategy import STEP_MS, Bar

VERSION = "pooled-ridge-next4h-v1"
ROUND_TRIP_COST = 0.0014
WARMUP = 200
WINDOW = 6000
RIDGE = 10.0
SYMBOL_CODE = {"BTCUSDT": 0.0, "ETHUSDT": 1.0}
GATE = {"forward_scored": 60, "accepted": 20}


def features(bars: Sequence[Bar], i: int, symbol: str) -> list[float] | None:
    if i < WARMUP:
        return None
    c = [b.close for b in bars[: i + 1]]
    hi = max(b.high for b in bars[i - 100:i])
    lo = min(b.low for b in bars[i - 100:i])
    rets = [c[j] / c[j - 1] - 1 for j in range(i - 179, i + 1)]
    ema = lambda n: _ema(c[-(n * 4):], n)
    return [c[-1] / c[-2] - 1, c[-1] / c[-7] - 1, c[-1] / c[-43] - 1, c[-1] / c[-181] - 1,
            (c[-1] - lo) / (hi - lo) if hi > lo else 0.5,
            float(np.std(rets, ddof=1)), ema(50) / ema(200) - 1, SYMBOL_CODE[symbol]]


def _ema(values, n):
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


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


def ingest(db, symbol: str, bars: Sequence[Bar], now_ms: int, origin: str) -> None:
    """Insert feature rows for closed bars and seal labels whose next bar has closed."""
    for i in range(WARMUP, len(bars)):
        if bars[i].ts + STEP_MS > now_ms:
            continue
        x = features(bars, i, symbol)
        late = origin == "observed" and now_ms - (bars[i].ts + STEP_MS) > 30 * 60 * 1000
        db.execute("INSERT OR IGNORE INTO samples VALUES (?,?,?,NULL,NULL,?)",
                   (symbol, bars[i].ts, json.dumps(x), "backfill" if late else origin))
    for a, b in zip(bars, bars[1:]):
        if b.ts - a.ts == STEP_MS and b.ts + STEP_MS <= now_ms:
            y = (b.close / a.close) - 1 - ROUND_TRIP_COST
            db.execute("UPDATE samples SET y=?, label_end=? WHERE symbol=? AND ts=? AND y IS NULL",
                       (y, b.ts + STEP_MS, symbol, a.ts))


def _fit(x, y):
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale < 1e-12] = 1.0
    z = np.column_stack([np.ones(len(x)), (x - mean) / scale])
    penalty = np.eye(z.shape[1]) * RIDGE
    penalty[0, 0] = 0
    beta = np.linalg.solve(z.T @ z + penalty, z.T @ y)
    return dict(mean=mean.tolist(), scale=scale.tolist(), beta=beta.tolist(), train_mean=float(y.mean()))


def predict(model: dict, x: Sequence[float]) -> float:
    z = [1.0] + [(v - m) / s for v, m, s in zip(x, model["mean"], model["scale"])]
    return float(np.dot(z, model["beta"]))


def train(db, now_ms: int) -> None:
    rows = db.execute("SELECT x, y, label_end FROM samples WHERE y IS NOT NULL ORDER BY ts, symbol").fetchall()
    last = db.execute("SELECT last_label FROM models ORDER BY id DESC LIMIT 1").fetchone()
    if len(rows) < 400:
        return
    newest = max(r[2] for r in rows)
    if last and last[0] >= newest:
        return
    rows = rows[-WINDOW:]
    model = _fit(np.array([json.loads(r[0]) for r in rows]), np.array([r[1] for r in rows]))
    model.update(version=VERSION, samples=len(rows), train_last_label=newest)
    model["digest"] = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    db.execute("INSERT INTO models(created_ms, last_label, value) VALUES (?,?,?)", (now_ms, newest, json.dumps(model)))


def register_prediction(db, symbol: str, bar_ts: int, now_ms: int) -> float | None:
    """Score the just-closed bar before its label exists."""
    latest = db.execute("SELECT id, value, last_label FROM models ORDER BY id DESC LIMIT 1").fetchone()
    row = db.execute("SELECT x, y FROM samples WHERE symbol=? AND ts=?", (symbol, bar_ts)).fetchone()
    if not latest or not row or row[1] is not None or latest[2] > bar_ts + STEP_MS:
        return None
    existing = db.execute("SELECT prediction FROM predictions WHERE symbol=? AND ts=?", (symbol, bar_ts)).fetchone()
    if existing:
        return existing[0]
    value = predict(json.loads(latest[1]), json.loads(row[0]))
    if not math.isfinite(value):
        return None
    db.execute("INSERT INTO predictions VALUES (?,?,?,?,?)", (symbol, bar_ts, latest[0], value, now_ms))
    return value


def gate(db) -> dict:
    rows = db.execute(
        "SELECT p.prediction, s.y, m.value FROM predictions p JOIN samples s ON s.symbol=p.symbol AND s.ts=p.ts "
        "JOIN models m ON m.id=p.model_id WHERE s.y IS NOT NULL AND p.created_ms < s.label_end").fetchall()
    n = len(rows)
    if n == 0:
        return dict(authority=False, forward_scored=0, reason="collecting")
    pred = np.array([r[0] for r in rows])
    y = np.array([r[1] for r in rows])
    const = np.array([json.loads(r[2])["train_mean"] for r in rows])
    mse, const_mse = float(np.mean((y - pred) ** 2)), float(np.mean((y - const) ** 2))
    acc = y[pred > 0]
    status = dict(forward_scored=n, forward_mse=mse, constant_mse=const_mse, accepted=int(len(acc)),
                  accepted_mean_net=float(acc.mean()) if len(acc) else None,
                  rejected_mean_net=float(y[pred <= 0].mean()) if (pred <= 0).any() else None)
    ok = (n >= GATE["forward_scored"] and len(acc) >= GATE["accepted"] and mse < const_mse
          and acc.mean() > 0 and (not (pred <= 0).any() or acc.mean() > y[pred <= 0].mean()))
    status.update(authority=bool(ok), reason="forward_gate_passed" if ok else "forward_gate_not_passed")
    return status


def status(db) -> dict:
    counts = dict(db.execute("SELECT origin, COUNT(*) FROM samples WHERE y IS NOT NULL GROUP BY origin"))
    latest = db.execute("SELECT id, created_ms, value FROM models ORDER BY id DESC LIMIT 1").fetchone()
    return dict(version=VERSION, label_counts=counts, model_id=latest[0] if latest else None,
                model_samples=json.loads(latest[2])["samples"] if latest else 0, gate=gate(db))
