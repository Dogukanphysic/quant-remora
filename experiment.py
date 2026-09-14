"""Walk-forward research. Strategy selection sees only the preceding data."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from strategies import (indicators, signal, Risk, size_position, NAMES, RULES,
                        BAR_LABEL, POLICY_ID)


def simulate(rows, strategy, start, end, risk=None, initial=1000.0):
    risk = risk or Risk()
    risk.validate()
    if not 21 <= start < end <= len(rows):
        raise ValueError('Geçersiz test aralığı.')
    # Slice at end as a second barrier against accidentally reading later folds.
    features = indicators(rows[:end])
    cash, position = initial, None
    peak, drawdown = initial, 0.0
    trades, curve = [], []
    exposure = skipped = 0

    def sell(index, reference, reason):
        nonlocal cash, position
        price = reference * (1 - risk.slippage)
        proceeds = position['quantity'] * price * (1 - risk.fee)
        pnl = proceeds - position['cost']
        cash += proceeds
        trades.append({'entry_ts': position['ts'], 'exit_ts': rows[index]['ts'],
                       'entry_price': position['entry'], 'exit_price': price,
                       'quantity_btc': position['quantity'], 'pnl_usd': pnl,
                       'stop': position['stop'], 'target': position['target'], 'exit_reason': reason})
        position = None

    for i in range(start, end):
        row = rows[i]
        entry, leave = signal(rows, features, i, strategy)
        exited = False
        if row['volume'] <= 0:
            skipped += 1
        else:
            if position:
                # Gaps are filled at open, not optimistically at the stop price.
                if row['open'] <= position['stop']:
                    sell(i, row['open'], 'stop_gap')
                    exited = True
                elif row['open'] >= position['target']:
                    sell(i, position['target'], 'target_gap_conservative')
                    exited = True
                elif leave:
                    sell(i, row['open'], 'strategy_exit')
                    exited = True
            if position is None and not exited and entry:
                position = size_position(cash, row['open'], features['atr'][i-1], risk)
                if position:
                    cash -= position['cost']
                    position['ts'] = row['ts']
            if position:
                exposure += 1
                # Unknown intrabar ordering: stop wins if stop and target both touch.
                if row['low'] <= position['stop']:
                    sell(i, position['stop'], 'stop')
                elif row['high'] >= position['target']:
                    sell(i, position['target'], 'target')
        if position and row['volume'] <= 0:
            exposure += 1
        equity = cash + (position['quantity'] * row['close'] * (1-risk.slippage) * (1-risk.fee)
                         if position else 0)
        peak = max(peak, equity)
        drawdown = max(drawdown, 1-equity/peak)
        curve.append({'ts': row['ts'], 'equity_usd': equity})
    # Force fold-end liquidation only on a tradable last candle, explicitly logged.
    if position and rows[end-1]['volume'] > 0:
        sell(end-1, rows[end-1]['close'], 'window_end')
    wins = sum(t['pnl_usd'] > 0 for t in trades)
    gross_profit = sum(max(t['pnl_usd'], 0) for t in trades)
    gross_loss = -sum(min(t['pnl_usd'], 0) for t in trades)
    return {'strategy': strategy, 'initial_usd': initial, 'final_equity_usd': equity,
            'return_pct': (equity/initial-1)*100, 'max_close_drawdown_pct': drawdown*100,
            'completed_trades': len(trades), 'win_rate_pct': wins/len(trades)*100 if trades else None,
            'profit_factor': gross_profit/gross_loss if gross_loss else None,
            'exposure_pct': exposure/(end-start)*100, 'zero_volume_bars': skipped,
            'open_quantity_btc': position['quantity'] if position else 0,
            'start_ts': rows[start]['ts'], 'end_ts': rows[end-1]['ts'],
            'trades': trades, 'equity_curve': curve}


def compact(result):
    return {k: v for k, v in result.items() if k not in ('trades', 'equity_curve')}


def buy_hold(rows, start, end, fraction, risk):
    quantity = 1000*fraction/(rows[start]['open']*(1+risk.slippage)*(1+risk.fee))
    equity = 1000*(1-fraction)+quantity*rows[end-1]['close']*(1-risk.slippage)*(1-risk.fee)
    return (equity/1000-1)*100


def run(rows):
    from agent import validate
    validate(rows)
    if len(rows) < 1500:
        raise ValueError('Karşılaştırma için en az 1500 adet 15 dakikalık mum gerekli.')
    risk = Risk()
    cuts = [int(len(rows)*x) for x in (0.4, 0.6, 0.8)] + [len(rows)]
    folds = []
    for start, end in zip(cuts, cuts[1:]):
        training = [compact(simulate(rows, name, 100, start, risk)) for name in NAMES]
        # Require a minimum sample in training; otherwise retain cash.
        eligible = [r for r in training if r['completed_trades'] >= 10 and r['return_pct'] > 0]
        selected = max(eligible, key=lambda r: r['return_pct'])['strategy'] if eligible else 'cash'
        tests = [simulate(rows, name, start, end, risk) for name in NAMES]
        chosen = next((r for r in tests if r['strategy'] == selected), None)
        folds.append({'training_end_exclusive_ts': rows[start]['ts'], 'selected_from_training': selected,
                      'training': training, 'tests': tests,
                      'selected_test_return_pct': chosen['return_pct'] if chosen else 0,
                      'buy_hold_100pct_return_pct': buy_hold(rows, start, end, 1, risk),
                      'buy_hold_25pct_return_pct': buy_hold(rows, start, end, .25, risk)})
    summary = []
    for name in NAMES:
        tests = [next(r for r in fold['tests'] if r['strategy'] == name) for fold in folds]
        summary.append({'strategy': name, 'positive_windows': sum(r['return_pct'] > 0 for r in tests),
                        'mean_window_return_pct': sum(r['return_pct'] for r in tests)/len(tests),
                        'worst_window_drawdown_pct': max(r['max_close_drawdown_pct'] for r in tests),
                        'completed_trades': sum(r['completed_trades'] for r in tests)})
    return {'exchange': 'bitstamp', 'symbol': 'BTC/USD', 'bar': BAR_LABEL,
            'policy_id': POLICY_ID, 'mode': 'historical_research_not_live',
            'risk': asdict(risk), 'rules': RULES,
            'data_count': len(rows), 'data_start_ts': rows[0]['ts'], 'data_end_ts': rows[-1]['ts'],
            'folds': folds, 'summary': summary,
            'selection': 'Expanding training windows, positive net return and >=10 closed trades; best training return wins; otherwise cash.',
            'limitations': ['Each test starts at 1000 USD with no carried position.',
                            'All fixed candidates are reported, not independently certified winners.',
                            '15-minute close drawdown misses intrabar extremes.',
                            'Stops are simulated and gaps may exceed planned risk.',
                            'No fills in zero-volume bars; liquidity/queue not modeled.',
                            'Window-end liquidation is a research boundary convention.',
                            'This historical experiment does not simulate the daily loss circuit breaker or persistent paper state and sends no live orders.',
                            'Previously inspected data is not a pristine holdout; forward paper testing remains required.']}


def date(ts):
    return datetime.fromtimestamp(ts/1000, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def save_report(rows, data_path, out):
    report = run(rows)
    report['data_sha256'] = hashlib.sha256(data_path.read_bytes()).hexdigest()
    report['data_file'] = str(data_path.resolve())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    lines = ['# 15 dakikalık Bollinger strateji karşılaştırması', '',
             f"Bitstamp BTC/USD · {len(rows)} adet 15 dakikalık mum · {date(rows[0]['ts'])} — {date(rows[-1]['ts'])}", '',
             'Üç ilerleyen test penceresi; her pencere 1000 USD ile sıfırdan başlar. Sonuçlar sanal işlemlerdir.', '',
             '| Strateji | Pozitif pencere | Ortalama pencere getirisi | En kötü pencere düşüşü* | Kapanan işlem |',
             '|---|---:|---:|---:|---:|']
    for item in report['summary']:
        lines.append(f"| {NAMES[item['strategy']]} | {item['positive_windows']}/3 | {item['mean_window_return_pct']:.2f}% | {item['worst_window_drawdown_pct']:.2f}% | {item['completed_trades']} |")
    lines += ['', '*Düşüş 15 dakikalık kapanışlarda ölçülür; tüm dönem düşüşü değildir. Ortalama getiri yıllık veya bileşik getiri değildir.', '',
              '## Pencereler ve referanslar', '',
              '| Test dönemi (UTC) | Önceki veriden seçilen | Seçilenin getirisi | %25 al-tut | %100 al-tut |',
              '|---|---|---:|---:|---:|']
    for fold in report['folds']:
        first = fold['tests'][0]
        lines.append(f"| {date(first['start_ts'])} — {date(first['end_ts'])} | {NAMES.get(fold['selected_from_training'], 'Nakit')} | {fold['selected_test_return_pct']:.2f}% | {fold['buy_hold_25pct_return_pct']:.2f}% | {fold['buy_hold_100pct_return_pct']:.2f}% |")
    lines += ['', '## Kurallar', ''] + [f'- **{NAMES[k]}:** {v}' for k, v in RULES.items()]
    lines += ['', '## Ortak risk ve uygulama', '',
              'Kararlar yalnız kapanmış 15 dakikalık mumdan üretilir ve sonraki açılışta uygulanır. Bollinger bantları 20 kapanışın basit ortalaması ile popülasyon standart sapmasının ±2 katıdır; ATR14 Wilder yumuşatmasıyla hesaplanır.',
              'Başlangıç stop mesafesi 2×ATR14, hedef bu mesafenin 2 katıdır. Planlanan stop kaybı maliyetler dahil bakiyenin %0,1’i; pozisyon maliyeti en fazla %5’idir. Boşluklar zarar sınırını aşabilir.',
              'Komisyon %0,1 ve kayma %0,05/yön varsayımıdır. Stop ve hedef aynı mumda görülürse stop önce sayılır. Sıfır hacimli mumda işlem yapılmaz.',
              'Eğitim penceresinde en az 10 kapanmış işlem ve pozitif net getiri şartıyla en yüksek getirili strateji seçilir; şart sağlanmazsa nakitte kalınır. Test sonucu seçime girmez.', '',
              '## Sınırlar', '',
              'Bu, makine öğrenmesi eğitimi veya kazanç kanıtı değildir. Önceden incelenen veri tamamen dokunulmamış test verisi sayılamaz. Yeni veride sanal işlem takibi gereklidir.',
              'Bu tarihsel karşılaştırma günlük zarar kesicisini ve sürekli sanal hesap durumunu simüle etmez; gerçek emir göndermez. %100 al-tut referansının piyasa riski stratejilerden daha yüksektir.',
              'Pencere sonunda likit son mumda pozisyon kapatılır; bu karşılaştırma sınırı gerçek sürekli işlem davranışı değildir. Emir sırası, derinlik ve gerçek dolumlar modellenmez.', '',
              '## Kaynaklar', '',
              '- [TradingView Bollinger Bantları](https://www.tradingview.com/support/solutions/43000501840-bollinger-bands-bb/)',
              '- [TradingView ATR](https://www.tradingview.com/support/solutions/43000501823-average-true-range-atr/)',
              '- [TradingView strateji testleri](https://www.tradingview.com/pine-script-docs/concepts/strategies/)',
              '- [Backtest overfitting araştırması](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)', '',
              'Parametreler araştırma hipotezimizdir; kaynakların doğruladığı kazançlı bir sistem değildir. Ayrıntılı işlem ve bakiye kayıtları aynı adlı JSON dosyasındadır.']
    out.with_suffix('.md').write_text('\n'.join(lines), encoding='utf-8')
    return report
