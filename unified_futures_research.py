"""Pre-registered BTC+ETH USD-M strategy selection for the unified Testnet bot.

The contract (splits, costs, grid, gates, selection rule) is written and hashed
before any result is computed.  Holdout is evaluated once, for the selected
candidate only.  Offline research: no exchange credentials, no orders.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "reports" / "unified-futures-research"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
DEV_END = pd.Timestamp("2024-09-01", tz="UTC")
SEL_END = pd.Timestamp("2025-09-01", tz="UTC")

CONTRACT = {
    "version": "unified-futures-research-v1",
    "data": "Binance USD-M 15m klines + funding, 2021-09..2026-08, SHA-256 verified archives",
    "splits": {"development": "< 2024-09-01", "selection": "2024-09-01 .. 2025-08-31",
               "holdout": ">= 2025-09-01 (selected candidate only, evaluated once)"},
    "fill": "decide on closed bar t, hold over (t, t+1]; close-to-close returns",
    "cost_per_side": {"base": 0.0007, "stress": 0.0012},
    "funding": "position pays funding_rate * position at each funding timestamp",
    "sizing": {"unit": "1x notional", "voltarget": "min(1, 0.40 / realized_vol_annual)"},
    "gates": {
        "development_stress": "both symbols: net>0, sharpe>=0.5, max_dd<=0.40, entries>=30",
        "selection_stress": "both symbols: net>0, sharpe>=0.3",
        "plateau": "at least one grid neighbour also passes the development gate",
    },
    "selection_rule": "max over survivors of min(dev, sel) equal-weight portfolio sharpe at base cost; tie -> fewer entries",
    "holdout_pass": "equal-weight portfolio net>0 at stress cost",
    "no_survivor": "report failure; bot runs the best-ranked development candidate as explicit Testnet exploration only",
}


def load(symbol: str) -> tuple[pd.DataFrame, pd.Series]:
    k = pd.read_csv(ROOT / f"data/binance-um-{symbol.lower()}-15m-5y.csv")
    k.index = pd.to_datetime(k["ts"], unit="ms", utc=True)
    f = pd.read_csv(ROOT / f"data/binance-um-{symbol.lower()}-funding-5y.csv")
    fs = pd.Series(f["funding_rate"].to_numpy(), index=pd.to_datetime(f["ts"], unit="ms", utc=True))
    return k[["open", "high", "low", "close", "volume"]], fs


def resample(k: pd.DataFrame, hours: int) -> pd.DataFrame:
    rule = f"{hours}h"
    g = k.resample(rule, label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(), "n": g["close"].count()})
    return out[out["n"] == hours * 4].drop(columns="n")


def funding_per_bar(bars: pd.DataFrame, funding: pd.Series, hours: int) -> np.ndarray:
    # A bar labelled t (left edge) closes at t+h; position decided at that close
    # earns the NEXT bar, so funding in (close_t, close_{t+1}] belongs to bar t+1.
    close_time = bars.index + pd.Timedelta(hours=hours)
    bucket = np.searchsorted(close_time.values, funding.index.values, side="left")
    out = np.zeros(len(bars))
    ok = bucket < len(bars)
    np.add.at(out, bucket[ok], funding.to_numpy()[ok])
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy()


def state_machine(enter_long, exit_long, enter_short, exit_short, time_stop=None):
    pos = np.zeros(len(enter_long))
    p, held = 0, 0
    for i in range(len(pos)):
        if p == 1 and (exit_long[i] or (time_stop and held >= time_stop)):
            p = 0
        elif p == -1 and (exit_short[i] or (time_stop and held >= time_stop)):
            p = 0
        if p == 0:
            if enter_long[i]:
                p, held = 1, 0
            elif enter_short[i]:
                p, held = -1, 0
        held += 1
        pos[i] = p
    return pos


def signal(bars: pd.DataFrame, hours: int, family: str, params: dict) -> np.ndarray:
    c = bars["close"].to_numpy()
    h, l = bars["high"].to_numpy(), bars["low"].to_numpy()
    ls = params["mode"] == "long_short"
    none = np.zeros(len(c), dtype=bool)
    if family == "tsmom":
        n = params["lookback_h"] // hours
        r = np.full(len(c), np.nan)
        r[n:] = c[n:] / c[:-n] - 1
        s = np.where(r > params["threshold"], 1.0, np.where(r < -params["threshold"], -1.0 if ls else 0.0, 0.0))
        return np.nan_to_num(s)
    if family == "ema_cross":
        f, s = ema(c, params["fast"]), ema(c, params["slow"])
        out = np.where(f > s, 1.0, -1.0 if ls else 0.0)
        out[: params["slow"]] = 0
        return out
    if family == "donchian":
        n = params["n"]
        hi = pd.Series(h).rolling(n).max().shift(1).to_numpy()
        lo = pd.Series(l).rolling(n).min().shift(1).to_numpy()
        xhi = pd.Series(h).rolling(n // 2).max().shift(1).to_numpy()
        xlo = pd.Series(l).rolling(n // 2).min().shift(1).to_numpy()
        with np.errstate(invalid="ignore"):
            return state_machine(c > hi, c < xlo, (c < lo) if ls else none, c > xhi)
    mid = pd.Series(c).rolling(20).mean().to_numpy()
    sd = pd.Series(c).rolling(20).std(ddof=0).to_numpy()
    up, dn = mid + params["k"] * sd, mid - params["k"] * sd
    e50, e200 = ema(c, 50), ema(c, 200)
    bull, bear = e50 > e200, e50 < e200
    with np.errstate(invalid="ignore"):
        if family == "bb_breakout":
            return state_machine((c > up) & bull, c < mid, ((c < dn) & bear) if ls else none, c > mid)
        if family == "bb_reversion":
            trend = params["trend_filter"]
            el = (c < dn) & (bull if trend else True)
            es = ((c > up) & (bear if trend else True)) if ls else none
            return state_machine(el, c >= mid, es, c <= mid, time_stop=params["time_stop_bars"])
    raise ValueError(family)


def grid():
    for hours in (1, 4):
        for mode in ("long_short", "long_only"):
            for lb, th in itertools.product((24, 72, 168, 336, 720), (0.0, 0.02)):
                yield hours, "tsmom", dict(mode=mode, lookback_h=lb, threshold=th)
            for fast, slow in ((10, 40), (20, 50), (20, 100), (50, 200)):
                yield hours, "ema_cross", dict(mode=mode, fast=fast, slow=slow)
            for n in (20, 55, 100):
                yield hours, "donchian", dict(mode=mode, n=n)
            for k in (2.0, 2.5):
                yield hours, "bb_breakout", dict(mode=mode, k=k)
                for tf in (False, True):
                    yield hours, "bb_reversion", dict(mode=mode, k=k, trend_filter=tf, time_stop_bars=48)


def returns(bars, fund, pos, sizing, cost, hours):
    c = bars["close"].to_numpy()
    r = np.zeros(len(c))
    r[1:] = c[1:] / c[:-1] - 1
    if sizing == "voltarget":
        per_year = 24 * 365 / hours
        vol = pd.Series(r).rolling(int(30 * 24 / hours)).std().shift(0).to_numpy() * np.sqrt(per_year)
        scale = np.nan_to_num(np.minimum(1.0, 0.40 / vol), nan=0.0)
        pos = pos * scale
    held = np.zeros(len(c))
    held[1:] = pos[:-1]            # decided at close t, earns bar t+1
    turnover = np.abs(np.diff(np.concatenate([[0.0], pos])))
    net = held * r - held * fund - turnover * cost
    return net, pos


def metrics(net, pos, index, hours):
    if len(net) == 0:
        return dict(net=0.0, sharpe=0.0, max_dd=0.0, entries=0, pf=None, exposure=0.0)
    eq = np.cumprod(1 + net)
    dd = float(np.max(1 - eq / np.maximum.accumulate(eq)))
    per_year = 24 * 365 / hours
    sd = net.std()
    daily = pd.Series(net, index=index).resample("1D").sum()
    gain, loss = daily[daily > 0].sum(), -daily[daily < 0].sum()
    sign = np.sign(pos)
    entries = int(np.sum((sign != 0) & (np.concatenate([[0], sign[:-1]]) != sign)))
    return dict(net=float(eq[-1] - 1), sharpe=float(net.mean() / sd * np.sqrt(per_year)) if sd > 0 else 0.0,
                max_dd=dd, entries=entries, pf=float(gain / loss) if loss > 0 else None,
                exposure=float(np.mean(sign != 0)))


def segments(index):
    return {"development": index < DEV_END, "selection": (index >= DEV_END) & (index < SEL_END),
            "holdout": index >= SEL_END}


def evaluate(data, hours, family, params, sizing, cost, which):
    per_symbol, nets = {}, {}
    for sym, (bars, fund) in data[hours].items():
        pos = signal(bars, hours, family, params)
        net, sized = returns(bars, fund, pos, sizing, cost, hours)
        mask = segments(bars.index)[which]
        per_symbol[sym] = metrics(net[mask], sized[mask], bars.index[mask], hours)
        nets[sym] = pd.Series(net[mask], index=bars.index[mask])
    port = pd.concat(nets, axis=1).dropna().mean(axis=1)
    per_symbol["portfolio"] = metrics(port.to_numpy(), np.ones(len(port)), port.index, hours)
    per_symbol["portfolio"]["entries"] = sum(per_symbol[s]["entries"] for s in SYMBOLS)
    return per_symbol


def dev_gate(m):
    return all(m[s]["net"] > 0 and m[s]["sharpe"] >= 0.5 and m[s]["max_dd"] <= 0.40 and m[s]["entries"] >= 30
               for s in SYMBOLS)


def sel_gate(m):
    return all(m[s]["net"] > 0 and m[s]["sharpe"] >= 0.3 for s in SYMBOLS)


def neighbours(a, b):
    (ha, fa, pa, sa), (hb, fb, pb, sb) = a, b
    if (ha, fa, sa) != (hb, fb, sb) or pa == pb:
        return False
    diff = [k for k in pa if pa[k] != pb[k]]
    return len(diff) == 1 and diff[0] != "mode"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(CONTRACT, sort_keys=True, indent=2)
    (OUT / "contract.json").write_text(blob, encoding="utf-8")
    contract_hash = hashlib.sha256(blob.encode()).hexdigest()

    raw = {s: load(s) for s in SYMBOLS}
    data = {h: {s: (b := resample(k, h), funding_per_bar(b, f, h)) for s, (k, f) in raw.items()} for h in (1, 4)}
    hashes = {s: hashlib.sha256((ROOT / f"data/binance-um-{s.lower()}-15m-5y.csv").read_bytes()).hexdigest() for s in SYMBOLS}

    stress, base = CONTRACT["cost_per_side"]["stress"], CONTRACT["cost_per_side"]["base"]
    rows = []
    candidates = [(h, f, p, s) for h, f, p in grid() for s in ("unit", "voltarget")]
    for cand in candidates:
        h, f, p, s = cand
        dev_s = evaluate(data, h, f, p, s, stress, "development")
        rows.append(dict(cand=cand, dev_stress=dev_s, dev_pass=dev_gate(dev_s)))
    passed_dev = [r["cand"] for r in rows if r["dev_pass"]]
    for r in rows:
        r["plateau"] = any(neighbours(r["cand"], o) for o in passed_dev)
        if r["dev_pass"] and r["plateau"]:
            h, f, p, s = r["cand"]
            r["sel_stress"] = evaluate(data, h, f, p, s, stress, "selection")
            r["sel_pass"] = sel_gate(r["sel_stress"])
            if r["sel_pass"]:
                r["dev_base"] = evaluate(data, h, f, p, s, base, "development")
                r["sel_base"] = evaluate(data, h, f, p, s, base, "selection")
                r["score"] = min(r["dev_base"]["portfolio"]["sharpe"], r["sel_base"]["portfolio"]["sharpe"])
    survivors = [r for r in rows if r.get("sel_pass")]
    survivors.sort(key=lambda r: (-r["score"], r["dev_base"]["portfolio"]["entries"]))
    if survivors:
        chosen, status = survivors[0], "selected"
    else:
        chosen = max(rows, key=lambda r: min(r["dev_stress"][s]["sharpe"] for s in SYMBOLS))
        status = "no_survivor_exploration_only"
    h, f, p, s = chosen["cand"]
    holdout = {c: evaluate(data, h, f, p, s, v, "holdout") for c, v in (("base", base), ("stress", stress))}
    buy_hold = {}
    for seg in ("development", "selection", "holdout"):
        buy_hold[seg] = {}
        for sym in SYMBOLS:
            bars = data[4][sym][0]
            mask = segments(bars.index)[seg]
            r = np.r_[0.0, bars["close"].to_numpy()[1:] / bars["close"].to_numpy()[:-1] - 1][mask]
            buy_hold[seg][sym] = metrics(r, np.ones(len(r)), bars.index[mask], 4)

    def label(c):
        return dict(timeframe_h=c[0], family=c[1], params=c[2], sizing=c[3])

    report = dict(contract_sha256=contract_hash, data_sha256=hashes, candidates=len(rows),
                  passed_development=len(passed_dev), passed_plateau=sum(1 for r in rows if r["dev_pass"] and r["plateau"]),
                  passed_selection=len(survivors), status=status, chosen=label(chosen["cand"]),
                  chosen_metrics={k: chosen.get(k) for k in ("dev_stress", "sel_stress", "dev_base", "sel_base")},
                  holdout=holdout, holdout_pass=holdout["stress"]["portfolio"]["net"] > 0,
                  buy_and_hold=buy_hold,
                  survivors=[dict(label(r["cand"]), score=r["score"],
                                  sel_portfolio_net=r["sel_base"]["portfolio"]["net"]) for r in survivors[:15]],
                  top_development=[dict(label(r["cand"]), btc_sharpe=r["dev_stress"]["BTCUSDT"]["sharpe"],
                                        eth_sharpe=r["dev_stress"]["ETHUSDT"]["sharpe"], dev_pass=r["dev_pass"])
                                   for r in sorted(rows, key=lambda r: -min(r["dev_stress"][s]["sharpe"] for s in SYMBOLS))[:15]])
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    (OUT / "selected_strategy.json").write_text(json.dumps(dict(
        contract_sha256=contract_hash, status=status, **label(chosen["cand"]),
        holdout_pass=report["holdout_pass"], real_money_eligible=False, testnet_only=True), indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("candidates", "passed_development", "passed_plateau",
                                             "passed_selection", "status", "chosen", "holdout_pass")}, indent=2))
    return report


if __name__ == "__main__":
    main()
