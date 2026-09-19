"""Bounded spot-only strategy research. Never writes execution state or sends orders."""
from collections import deque
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
import statistics

from agent import validate
from low_frequency import _ema
from strategies import rma

ROOT = Path(__file__).resolve().parent
CONTRACT = {
    'version': 1, 'frames_hours': [1, 4],
    'families': ['trend_hysteresis', 'channel_breakout', 'trend_pullback'],
    'variants': ['balanced', 'patient'], 'split': [0.60, 0.80],
    'one_way_costs': [0.0, 0.0015, 0.0025],
    'selection_cost': 0.0025, 'initial_usd': 1000.0,
    'risk_fraction': 0.0025, 'allocation_cap': 0.20,
    'stop_atr': 2.0, 'target_atr': 4.0,
    'minimum_trades_per_segment': 20, 'minimum_profit_factor': 1.15,
    'maximum_drawdown': 0.15, 'positive_subsegments_required': 2,
    'execution': 'closed_signal_next_open_long_cash_no_same_bar_reentry',
    'historical_evidence': 'previously_examined_data_retrospective_not_true_forward',
    'activation': 'proposal_only_no_execution_authority',
}


def load(path, exchange, symbol):
    with path.open(encoding='utf-8') as stream:
        raw = list(csv.DictReader(stream))
    if any(r.get('exchange') != exchange or r.get('symbol') != symbol for r in raw):
        raise ValueError(f'Unexpected source in {path.name}')
    return validate([{'ts': int(r['ts']), **{
        k: float(r[k]) for k in ('open', 'high', 'low', 'close', 'volume')}} for r in raw])


def resample(rows, hours):
    interval = hours * 3600000
    grouped = {}
    for row in rows:
        grouped.setdefault(row['ts'] // interval, []).append(row)
    result = []
    for key, group in sorted(grouped.items()):
        if len(group) != hours * 4 or any(
                r['ts'] != key * interval + i * 900000 for i, r in enumerate(group)):
            continue
        result.append({'ts': key * interval, 'open': group[0]['open'],
                       'close': group[-1]['close'],
                       'high': max(r['high'] for r in group),
                       'low': min(r['low'] for r in group),
                       'volume': sum(r['volume'] for r in group)})
    return validate(result, hours * 3600)


def features(rows):
    closes = [r['close'] for r in rows]
    result = {'fast': _ema(closes, 20), 'slow': _ema(closes, 50),
              'trend': _ema(closes, 200)}
    result['atr'] = rma([max(r['high'] - r['low'],
        abs(r['high'] - rows[i-1]['close']) if i else 0,
        abs(r['low'] - rows[i-1]['close']) if i else 0)
        for i, r in enumerate(rows)], 14)
    window = deque(maxlen=20)
    result['middle'], result['lower'] = [], []
    for close in closes:
        window.append(close)
        mid = sum(window) / len(window)
        result['middle'].append(mid)
        result['lower'].append(mid - 2 * statistics.pstdev(window))
    return result


def signals(rows, f, family, variant):
    entry, exit_ = [False] * len(rows), [False] * len(rows)
    patient = variant == 'patient'
    channel = 72 if patient else 24
    for i in range(204, len(rows)):
        close, previous = rows[i]['close'], rows[i-1]['close']
        trend = close > f['trend'][i] and f['trend'][i] > f['trend'][i-4]
        if family == 'trend_hysteresis':
            buffer = 0.005 if patient else 0.002
            ratio = f['fast'][i] / f['slow'][i] - 1
            entry[i] = trend and ratio > buffer
            exit_[i] = ratio < -buffer or close < f['trend'][i]
        elif family == 'channel_breakout':
            entry[i] = trend and close > max(r['high'] for r in rows[i-channel:i])
            exit_[i] = close < min(r['low'] for r in rows[i-channel//3:i])
        elif family == 'trend_pullback':
            level = 'lower' if patient else 'middle'
            entry[i] = (trend and previous <= f[level][i-1]
                        and close > f[level][i] and close > previous)
            exit_[i] = close < f['trend'][i]
        else:
            raise ValueError('Unknown family')
    return entry, exit_


def simulate(rows, f, entry, exit_, start, end, cost, hold_bars):
    cash, peak = 1000.0, 1000.0
    position = None
    trades = []
    max_dd, exposed = 0.0, 0
    for i in range(max(201, start), end):
        bar = rows[i]
        closed = False
        if position:
            exposed += 1
            # An open exit cannot be exposed to the later intrabar low.
            open_exit = exit_[i-1] or i-position['index'] >= hold_bars
            adverse = bar['open'] if open_exit else max(bar['low'], min(bar['open'], position['stop']))
            trough = cash + position['qty'] * adverse * (1-cost)
            max_dd = max(max_dd, 1-trough/peak)
            price, reason = None, None
            if exit_[i-1] or i-position['index'] >= hold_bars:
                price, reason = bar['open'], 'signal_or_timeout'
            elif bar['low'] <= position['stop']:
                price, reason = min(bar['open'], position['stop']), 'stop'
            elif bar['high'] >= position['target']:
                price, reason = max(bar['open'], position['target']), 'target'
            if price is not None:
                proceeds = position['qty'] * price * (1-cost)
                trades.append({'entry_ts': position['ts'], 'exit_ts': bar['ts'],
                               'pnl': proceeds-position['cost'], 'reason': reason,
                               'net_return': proceeds/position['cost']-1})
                cash += proceeds
                position, closed = None, True
        if position is None and not closed and entry[i-1]:
            atr, price = f['atr'][i-1], bar['open']
            stop = price - 2*atr
            if stop > 0 and atr > 0:
                qty = min(cash*.20/(price*(1+cost)),
                          cash*.0025/(price*(1+cost)-stop*(1-cost)))
                amount = qty * price * (1+cost)
                position = {'qty': qty, 'cost': amount, 'stop': stop,
                            'target': price+4*atr, 'index': i, 'ts': bar['ts']}
                cash -= amount
                exposed += 1
                # Entry at open: both barriers may be reached in this same bar.
                trough = cash + qty * max(bar['low'], stop) * (1-cost)
                max_dd = max(max_dd, 1-trough/peak)
                hit = (min(price, stop) if bar['low'] <= stop else
                       position['target'] if bar['high'] >= position['target'] else None)
                if hit is not None:
                    proceeds = qty*hit*(1-cost)
                    trades.append({'entry_ts': bar['ts'], 'exit_ts': bar['ts'],
                                   'pnl': proceeds-amount,
                                   'net_return': proceeds/amount-1,
                                   'reason': 'stop' if bar['low'] <= stop else 'target'})
                    cash += proceeds
                    position = None
        equity = cash + (position['qty']*bar['close']*(1-cost) if position else 0)
        peak = max(peak, equity)
        max_dd = max(max_dd, 1-equity/peak)
    if position:
        proceeds = position['qty']*rows[end-1]['close']*(1-cost)
        trades.append({'entry_ts': position['ts'], 'exit_ts': rows[end-1]['ts'],
                       'pnl': proceeds-position['cost'],
                       'net_return': proceeds/position['cost']-1, 'reason': 'segment_end'})
        cash += proceeds
    wins = sum(max(t['pnl'], 0) for t in trades)
    losses = -sum(min(t['pnl'], 0) for t in trades)
    pf = wins/losses if losses else None
    return {'return': cash/1000-1, 'pnl_usd': cash-1000, 'trades': len(trades),
            'profit_factor': pf, 'gross_profit': wins, 'gross_loss': losses,
            'max_drawdown': max_dd, 'exposure_fraction': exposed/max(1, end-start),
            'win_rate': sum(t['pnl'] > 0 for t in trades)/len(trades) if trades else 0,
            'trade_records': trades}


def gate(result):
    return (result['trades'] >= 20 and result['return'] > 0
            and (result['profit_factor'] is None and result['gross_profit'] > 0
                 or result['profit_factor'] is not None and result['profit_factor'] >= 1.15)
            and result['max_drawdown'] <= .15)


def compact(result):
    return {k: v for k, v in result.items() if k != 'trade_records'}


def run():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = ROOT / 'reports' / ('strategy-reset-' + stamp)
    directory.mkdir(parents=True, exist_ok=False)
    def save(name, value):
        (directory/name).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    save('contract.json', CONTRACT)  # Freeze finite search BEFORE inspecting outcomes.
    paths = {'bitstamp': ROOT/'data/bitstamp-btc-usd-15m-200000.csv',
             'binance_spot': ROOT/'data/binance-spot-btcusdt-15m-through-20260916.csv'}
    source = load(paths['bitstamp'], 'bitstamp', 'BTC/USD')
    save('sources.json', {key: {'file': path.name,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for key, path in paths.items()})
    prepared, candidates = {}, []
    for hours in CONTRACT['frames_hours']:
        rows = resample(source, hours)
        f = features(rows)
        prepared[hours] = (rows, f)
        n = len(rows)
        for family in CONTRACT['families']:
            for variant in CONTRACT['variants']:
                entry, exit_ = signals(rows, f, family, variant)
                hold = (96 if variant == 'patient' else 48)
                development = simulate(rows, f, entry, exit_, 201, int(n*.6), .0025, hold)
                selection = simulate(rows, f, entry, exit_, int(n*.6), int(n*.8), .0025, hold)
                folds = [simulate(rows, f, entry, exit_, int(n*(.6+j*.2/3)),
                                  int(n*(.6+(j+1)*.2/3)), .0025, hold) for j in range(3)]
                passed = gate(development) and gate(selection) and sum(x['return']>0 for x in folds)>=2
                candidates.append({'id': f'{family}_{hours}h_{variant}', 'hours': hours,
                    'family': family, 'variant': variant, 'hold_bars': hold,
                    'development': compact(development), 'selection': compact(selection),
                    'selection_folds': [compact(x) for x in folds], 'selection_pass': passed})
    passing = [c for c in candidates if c['selection_pass']]
    selected = max(passing, key=lambda c: c['selection']['return']/max(.01, c['selection']['max_drawdown'])) if passing else None
    save('selection.json', {'candidates': candidates, 'selected': selected})
    report = {'contract': CONTRACT, 'selected': selected, 'candidate_count': len(candidates),
              'passing_count': len(passing), 'deployed': False, 'real_orders_enabled': False,
              'status': 'no_candidate_passed_development_selection', 'diagnostics': {}}
    if selected:
        hours = selected['hours']
        spot = load(paths['binance_spot'], 'binance_spot', 'BTCUSDT')
        market_rows = {'bitstamp_tail': prepared[hours][0], 'binance_spot': resample(spot, hours)}
        for name, rows in market_rows.items():
            f = features(rows)
            entry, exit_ = signals(rows, f, selected['family'], selected['variant'])
            start = int(len(rows)*.8) if name == 'bitstamp_tail' else 201
            scenarios = {}
            for cost in CONTRACT['one_way_costs']:
                result = simulate(rows, f, entry, exit_, start, len(rows), cost, selected['hold_bars'])
                scenarios[str(cost)] = compact(result)
                save(f'{name}-trades-cost-{cost}.json', result['trade_records'])
            report['diagnostics'][name] = scenarios
        passed = all(gate(scenarios['0.0025']) for scenarios in report['diagnostics'].values())
        report['status'] = 'retrospective_candidate_requires_new_forward_test' if passed else 'selected_candidate_failed_diagnostics'
    save('report.json', report)
    lines = ['# Strategy reset research', '', f'UTC run: {stamp}', '',
        'Previously examined historical data: retrospective diagnostics, not untouched forward proof.',
        'Long/cash spot only; next-open fills, stop-first double touches, costs on both sides.',
        '1000 USD reference equity, 0.25% planned stop risk, 20% allocation cap.', '',
        '| Candidate | Development net | Selection net | Selection trades | Pass |',
        '|---|---:|---:|---:|---|']
    for c in candidates:
        lines.append(f"| {c['id']} | {c['development']['return']:.2%} | {c['selection']['return']:.2%} | {c['selection']['trades']} | {c['selection_pass']} |")
    lines += ['', f"Result: {report['status']}", '', 'No execution configuration or running worker was modified.']
    (directory/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'directory': str(directory), 'status': report['status'],
                      'selected': selected, 'diagnostics': report['diagnostics']}, indent=2))


if __name__ == '__main__':
    run()
