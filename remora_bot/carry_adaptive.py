"""Self-updating funding carry: an ensemble of the six researched carry configurations whose weights are
re-learned every week from their recent results, with costs learned from the bot's own live fills.

Every Monday (rebalance ts):
  1. simulate each config's weekly PnL over the last WINDOW_WEEKS on public data (weekly open prices +
     settled funding), charging the cost per unit traded measured from this bot's live fills;
  2. target weight of a config = 1 / its weekly PnL volatility (less erratic configs get more), then the
     weights move only half way from last week's (SMOOTH) so one lucky week cannot swing the book;
  3. book = weighted sum of each config's per-coin weights, scaled to GROSS USDT and by a drawdown
     brake on this bot's own realized PnL; legs below MIN_LEG_USDT stay flat.
The same functions are used by adaptive_carry_research.py on history, so live and research match.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal

CONFIGS = ((1, 2), (1, 3), (3, 2), (3, 3), (7, 2), (7, 3))     # (funding lookback days, coins per side)
WINDOW_WEEKS = 26
SMOOTH = 0.5
GROSS_USDT = Decimal("2000")
MIN_LEG_USDT = Decimal("60")
DEFAULT_COST = 0.0007          # per unit traded until enough live fills: taker 0.05% + slippage allowance
TAKER_FEE = 0.0005
MIN_FILLS_FOR_COST = 5
WEEK_MS = 168 * 3600_000
DAY_MS = 24 * 3600_000
HOUR_MS = 3600_000


def config_name(cfg) -> str:
    return f"L{cfg[0]}K{cfg[1]}"


def funding_sum(events, start_ms, end_ms):
    """Sum of rates whose settlement hour lies in (start, end]; events = [(time_ms, rate), ...]."""
    return sum(rate for t, rate in events if start_ms < t // HOUR_MS * HOUR_MS <= end_ms)


def config_weights(funding, ts, cfg, symbols):
    """Per-coin weights of one config at ts: long the K lowest-funding coins (+0.5/K each), short the K highest."""
    days, k = cfg
    scores = {s: funding_sum(funding[s], ts - days * DAY_MS, ts) for s in symbols}
    ranked = sorted(scores, key=lambda s: (scores[s], s))
    w = {s: 0.0 for s in symbols}
    for s in ranked[:k]:
        w[s] = 0.5 / k
    for s in ranked[-k:]:
        w[s] = -0.5 / k
    return w, scores


def simulate(opens, funding, rebalances, cost, symbols):
    """Weekly net return of every config between consecutive rebalances.
    opens: {symbol: {ts: price}} (price at each rebalance), funding: {symbol: [(ms, rate)]}.
    Returns [(week_start_ts, {config_name: pnl})] for weeks with complete prices."""
    out, prev = [], {cfg: None for cfg in CONFIGS}
    for t0, t1 in zip(rebalances, rebalances[1:]):
        if any(t0 not in opens[s] or t1 not in opens[s] for s in symbols):
            prev = {cfg: None for cfg in CONFIGS}
            continue
        row = {}
        for cfg in CONFIGS:
            w, _ = config_weights(funding, t0, cfg, symbols)
            price = sum(w[s] * (opens[s][t1] / opens[s][t0] - 1) for s in symbols)
            paid = sum(w[s] * funding_sum(funding[s], t0, t1) for s in symbols)   # longs pay positive funding
            turnover = sum(abs(w[s] - (prev[cfg] or {}).get(s, 0.0)) for s in symbols)
            row[config_name(cfg)] = price - paid - cost * turnover
            prev[cfg] = w
        out.append((t0, row))
    return out


def learn_weights(history, previous=None):
    """Inverse-volatility weights over the history window, moved SMOOTH of the way from last week's."""
    names = [config_name(c) for c in CONFIGS]
    target = {}
    for n in names:
        xs = [row[n] for _, row in history]
        if len(xs) < 4:
            target[n] = 1.0
            continue
        mean = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
        target[n] = 1.0 / max(sd, 1e-6)
    total = sum(target.values())
    target = {n: v / total for n, v in target.items()}
    if previous:
        target = {n: SMOOTH * previous.get(n, 0.0) + (1 - SMOOTH) * target[n] for n in names}
        total = sum(target.values())
        target = {n: v / total for n, v in target.items()}
    return target


def combined_book(funding, ts, weights, symbols):
    """{symbol: signed weight} of the ensemble (netted across configs) and each config's raw scores."""
    book = {s: 0.0 for s in symbols}
    for cfg in CONFIGS:
        w, _ = config_weights(funding, ts, cfg, symbols)
        for s in symbols:
            book[s] += weights[config_name(cfg)] * w[s]
    return book


def drawdown_scale(peak_equity: Decimal, equity: Decimal) -> float:
    """Shrink the book while this bot's own realized PnL is in drawdown (fraction of gross exposure)."""
    dd = float((peak_equity - equity) / GROSS_USDT) if peak_equity > equity else 0.0
    if dd >= 0.10:
        return 0.25
    if dd >= 0.05:
        return 0.5
    return 1.0


def learned_cost(fills):
    """Cost per unit traded from live fills [(ref_price, avg_price)]: taker fee + mean absolute slippage."""
    slips = [abs(avg / ref - 1) for ref, avg in fills if ref > 0 and avg > 0]
    if len(slips) < MIN_FILLS_FOR_COST:
        return DEFAULT_COST, len(slips)
    return TAKER_FEE + sum(slips) / len(slips), len(slips)


def leg_notionals(book, scale):
    """Signed USDT per coin; tiny legs are dropped (they would cost more than they add)."""
    out = {}
    for s, w in book.items():
        n = (GROSS_USDT * Decimal(str(round(w * scale, 6)))).quantize(Decimal("1"))
        out[s] = n if abs(n) >= MIN_LEG_USDT else Decimal(0)
    return out


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
