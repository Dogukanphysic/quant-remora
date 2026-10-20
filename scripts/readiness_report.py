"""Weekly mainnet-readiness report for the carry bot (read-only: opens the ledger read-only, sends nothing).

Compares the live Demo results week by week with the walk-forward study the live settings came from
(reports/stop-research, 30% stop, live cost) and checks the agreed readiness criteria.
Usage: python scripts/readiness_report.py   ->  prints and writes reports/readiness/<date>.md
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "state" / "remora-bot-carry-testnet-b.sqlite3"
BACKTEST = ROOT / "reports" / "stop-research" / "result.json"
OUT = ROOT / "reports" / "readiness"
WEEK_MS = 7 * 86400_000
TR = dt.timezone(dt.timedelta(hours=3))
MIN_WEEKS, GOOD_WEEKS = 8, 12
MAX_DD_LIMIT = -15.0          # % of the book
MIN_ANNUAL = 10.0             # % of the book per year


def day(ms):
    return dt.datetime.fromtimestamp(ms / 1000, TR).strftime("%d.%m")


def main():
    db = sqlite3.connect(f"file:{LEDGER.as_posix()}?mode=ro", uri=True)
    books = {}
    for ts, notional in db.execute("SELECT rebalance_ts, notional FROM carry_targets"):
        books.setdefault(ts, []).append(notional)
    gross = {ts: (sum(abs(float(n)) for n in ns) if all(n is not None for n in ns) else 2000.0)
             for ts, ns in books.items()}
    weeks = sorted(books)
    pnl = {ts: 0.0 for ts in weeks}
    for exit_ms, p in db.execute("SELECT exit_ms, gross_pnl FROM trades"):
        # a leg closed by the Monday rebalance belongs to the week that just ended
        ts = max((w for w in weeks if w <= exit_ms - 2 * 3600_000), default=weeks[0])
        pnl[ts] += float(p)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    done = [w for w in weeks if w + WEEK_MS <= now]
    rows = [(w, pnl[w], pnl[w] / gross[w] * 100) for w in done]
    rets = [r for _, _, r in rows]

    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rets:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    n = len(rets)
    mean = sum(rets) / n if n else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else float("nan")
    sharpe = mean / sd * math.sqrt(52) if n > 1 and sd > 0 else float("nan")
    halts = db.execute("SELECT ts, payload FROM events WHERE kind='halt' ORDER BY ts").fetchall()
    recent_halts = [h for h in halts if h[0] > now - 4 * WEEK_MS]
    transient = db.execute("SELECT COUNT(*) FROM events WHERE kind='transient_error' AND ts > ?",
                           (now - WEEK_MS,)).fetchone()[0]
    bt = json.loads(BACKTEST.read_text())["30%"]["holdout"]
    bt_week = bt["net_pct"] / bt["weeks"]

    def mark(ok):
        return "✅" if ok else "❌"

    crit = [
        (f"En az {MIN_WEEKS} tamamlanmış hafta (ideal {GOOD_WEEKS})", n >= MIN_WEEKS, f"{n} hafta"),
        (f"Yıllık getiri ≥ %{MIN_ANNUAL:.0f}", n >= MIN_WEEKS and mean * 52 >= MIN_ANNUAL,
         f"%{mean * 52:.1f}/yıl" + ("" if n >= MIN_WEEKS else " (az veri)")),
        (f"En kötü düşüş > %{MAX_DD_LIMIT:.0f}", dd > MAX_DD_LIMIT, f"%{dd:.1f}"),
        ("Son 4 haftada açıklanamayan durma yok", not recent_halts, f"{len(recent_halts)} durma"),
        ("Bot 7/24 sunucuda (kişisel PC değil)", False, "elle doğrulanacak"),
        ("Ayrı, paylaşılmayan hesap", False, "şu an diğer botla ortak"),
        ("Mainnet kodu + güvenlik incelemesi", False, "henüz yok"),
    ]
    lines = [
        f"# Mainnet hazırlık raporu — {dt.datetime.now(TR).strftime('%d.%m.%Y %H:%M')}",
        "",
        "Salt okunur. Demo (sanal para) sonuçları; geçmiş test = stop-research, %30 stop, %0.2 maliyet, son 52 hafta.",
        "",
        "## Haftalık sonuçlar (gerçekleşmiş)",
        "",
        "| Hafta | Pozisyon (USDT) | Sonuç (USDT) | Getiri |",
        "|---|---|---|---|",
    ] + [f"| {day(w)}–{day(w + WEEK_MS)} | {gross[w]:.0f} | {p:+.2f} | %{r:+.2f} |" for w, p, r in rows] + [
        "",
        "## Canlı ve geçmiş test karşılaştırması",
        "",
        "| | Canlı Demo | Geçmiş test |",
        "|---|---|---|",
        f"| Hafta sayısı | {n} | {bt['weeks']} |",
        f"| Ortalama haftalık getiri | %{mean:+.2f} | %{bt_week:+.2f} |",
        f"| Kârlı hafta oranı | %{(sum(r > 0 for r in rets) / n * 100) if n else 0:.0f} | %{bt['positive_weeks'] / bt['weeks'] * 100:.0f} |",
        f"| Yıllık Sharpe | {sharpe:.2f} | {bt['sharpe']:.2f} |",
        f"| En kötü düşüş | %{dd:.1f} | %{bt['max_dd_pct']:.1f} |",
        "",
        f"Son 7 günde geçici bağlantı hatası: {transient}. Toplam durma: {len(halts)}.",
        "",
        "## Hazırlık ölçütleri",
        "",
    ] + [f"- {mark(ok)} {name}: {val}" for name, ok, val in crit] + [
        "",
        f"**Sonuç: {'HAZIR değil' if not all(ok for _, ok, _ in crit) else 'ölçütler sağlandı'}** "
        f"({sum(ok for _, ok, _ in crit)}/{len(crit)} ölçüt).",
    ]
    text = "\n".join(lines)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{dt.date.today().isoformat()}.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
