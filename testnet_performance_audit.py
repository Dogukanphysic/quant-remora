"""Read-only attribution of exact BTC Testnet round trips and price benchmarks."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite ledger value')
    return result


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def owner(row):
    feature = json.loads(row['entry_feature_json'] or '{}')
    if feature.get('decision_owner'):
        return feature['decision_owner']
    return 'legacy_unattributed'


def audit(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        rounds = db.execute('''SELECT * FROM worker_learning_round_trips
            WHERE status='closed' ORDER BY entry_candle_close_ms, created_ms''').fetchall()
        intents = {row['client_id']: row for row in db.execute('''
            SELECT client_id,side,state,realized_pnl_usdt FROM worker_order_intents
            WHERE state='filled' ''')}
        prices = {row['candle_close_ms']: number(row['close_latest'])
                  for row in db.execute('SELECT candle_close_ms,close_latest FROM worker_decisions')
                  if row['close_latest'] is not None}
        state = db.execute('SELECT completed_round_trips,realized_pnl_usdt FROM worker_state').fetchone()
    groups = defaultdict(lambda: dict(trades=0, wins=0, losses=0, flat=0,
                                      realized=Decimal(0), buy_hold_proxy=Decimal(0),
                                      comparable_trades=0))
    records, errors = [], []
    seen = set()
    for row in rounds:
        entry, exit_ = row['entry_client_id'], row['exit_client_id']
        if entry in seen or not entry or not exit_:
            errors.append(f'duplicate_or_missing_pair:{row["record_id"]}')
            continue
        seen.add(entry)
        buy, sell = intents.get(entry), intents.get(exit_)
        if not buy or not sell or buy['side'] != 'BUY' or sell['side'] != 'SELL' or not row['exact_pnl']:
            errors.append(f'unverified_fill:{row["record_id"]}')
            continue
        pnl = number(row['realized_pnl_usdt'])
        if pnl != number(sell['realized_pnl_usdt']):
            errors.append(f'pnl_mismatch:{row["record_id"]}')
            continue
        cost = number(row['entry_cost_usdt'])
        entry_ms, exit_ms = row['entry_candle_close_ms'], row['exit_candle_close_ms']
        if cost <= 0 or exit_ms <= entry_ms:
            errors.append(f'invalid_round_trip:{row["record_id"]}')
            continue
        category = owner(row)
        group = groups[category]
        group['trades'] += 1
        group['wins' if pnl > 0 else 'losses' if pnl < 0 else 'flat'] += 1
        group['realized'] += pnl
        entry_price, exit_price = prices.get(entry_ms), prices.get(exit_ms)
        benchmark = None
        if entry_price and exit_price and entry_price > 0:
            benchmark = cost * (exit_price / entry_price - 1)
            group['buy_hold_proxy'] += benchmark
            group['comparable_trades'] += 1
        records.append(dict(entry_utc=utc(entry_ms), exit_utc=utc(exit_ms),
                            decision_owner=category, entry_client_id=entry,
                            exit_client_id=exit_, entry_cost_usdt=str(cost),
                            realized_pnl_usdt=str(pnl),
                            same_window_buy_hold_price_proxy_usdt=str(benchmark) if benchmark is not None else None))
    total = sum((g['realized'] for g in groups.values()), Decimal(0))
    if state and (len(records) != state['completed_round_trips'] or total != number(state['realized_pnl_usdt'])):
        errors.append('worker_state_reconciliation_mismatch')
    return dict(schema=1, source=str(path), as_of_utc=datetime.now(timezone.utc).isoformat(),
                scope='current Testnet worker ledger; closed exact round trips only',
                closed_round_trips=len(records), realized_pnl_usdt=str(total),
                no_trade_pnl_usdt='0',
                groups={name: {key: str(val) if isinstance(val, Decimal) else val
                               for key, val in values.items()} for name, values in sorted(groups.items())},
                round_trips=records, errors=errors,
                benchmark_note='Her turun giriş sermayesi, giriş/çıkış mum kapanışları arasında BTC olarak tutulmuş varsayılır. Yalnız fiyat vekilidir; ücret, spread ve kayma hariçtir.',
                attribution_note='Atıf giriş kararının sahibine yapılır. Eski sahibi kayıtsız kararlar ayrı tutulur; sonuç model kârlılığını kanıtlamaz.')


def markdown(result):
    lines = ['# BTC Testnet gerçekleşmiş performans ölçümü', '',
             f'Üretim (UTC): {result["as_of_utc"]}',
             f'Kaynak: `{result["source"]}`', '',
             f'Kapanmış ve doğrulanmış tur: **{result["closed_round_trips"]}**',
             f'Gerçekleşmiş net Testnet PnL: **{result["realized_pnl_usdt"]} USDT**',
             'İşlem yapmama kıyası: **0 USDT**', '',
             '| Giriş kararının sahibi | Tur | Kazanç | Kayıp | Net USDT | Aynı aralıkta BTC tutma fiyat vekili USDT |',
             '|---|---:|---:|---:|---:|---:|']
    for name, group in result['groups'].items():
        proxy = group['buy_hold_proxy'] if group['comparable_trades'] == group['trades'] else 'eksik fiyat'
        lines.append(f'| {name} | {group["trades"]} | {group["wins"]} | {group["losses"]} | {group["realized"]} | {proxy} |')
    lines += ['', result['benchmark_note'], result['attribution_note'],
              'Karşılaştırma her turun kendi giriş sermayesini ve süresini kullanır; toplam dönem boyunca kesintisiz BTC tutma sonucu değildir.',
              'Testnet komisyonları sıfır görünebilir; gerçek piyasa ücret ve kaymasını temsil etmez.',
              f'Defter doğrulama hataları: {len(result["errors"])}', '']
    if result['errors']:
        lines += ['Hatalar:'] + [f'- `{item}`' for item in result['errors']] + ['']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path)
    parser.add_argument('--json-out', type=Path, default=Path('reports/testnet-performance-audit.json'))
    parser.add_argument('--md-out', type=Path, default=Path('reports/testnet-performance-audit.md'))
    args = parser.parse_args()
    if args.db is None:
        import os
        os.environ.setdefault('BINANCE_TESTNET_POLICY_MODE', 'hourly')
        os.environ.setdefault('BINANCE_TESTNET_DECISION_INTERVAL', '15m')
        from binance_testnet_worker import DB_PATH
        args.db = DB_PATH
    result = audit(args.db)
    for path, contents in ((args.json_out, json.dumps(result, indent=2, ensure_ascii=False) + '\n'),
                           (args.md_out, markdown(result))):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding='utf-8')
    print(markdown(result))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
