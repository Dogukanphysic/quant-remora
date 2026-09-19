"""Development-only learned regime gate; offline research, no order authority."""
import hashlib
import json
import statistics
from datetime import datetime, timezone

from strategy_research import ROOT, load, resample, features, signals, simulate, compact, gate


def regime_features(rows, f):
    result = [None] * len(rows)
    for i in range(204, len(rows)):
        movement = rows[i]['close'] - rows[i-24]['close']
        path = sum(abs(rows[j]['close']-rows[j-1]['close']) for j in range(i-23, i+1))
        efficiency = movement/path if path else 0.
        trend = 'up' if efficiency > .3 else 'down' if efficiency < -.3 else 'sideways'
        result[i] = (trend, f['atr'][i]/rows[i]['close'])
    return result


def bucket(value, threshold):
    return None if value is None else value[0] + ('_high_vol' if value[1] > threshold else '_low_vol')


def learn(rows, values, trades, cutoff):
    # Threshold and labels are fitted only before the development boundary.
    threshold = statistics.median(v[1] for v in values[204:cutoff] if v)
    index = {r['ts']: i for i, r in enumerate(rows)}
    grouped = {}
    for trade in trades:
        i = index[trade['entry_ts']]
        if i >= cutoff or trade['exit_ts'] >= rows[cutoff]['ts']:
            raise ValueError('Training trade crosses development cutoff')
        key = bucket(values[i-1], threshold)
        grouped.setdefault(key, []).append(trade['pnl'])
    stats, allowed = {}, []
    for key, pnl in sorted(grouped.items()):
        wins = sum(max(p, 0) for p in pnl)
        losses = -sum(min(p, 0) for p in pnl)
        pf = wins/losses if losses else None
        accepted = len(pnl) >= 12 and sum(pnl) > 0 and (pf is None or pf >= 1.15)
        stats[key] = dict(trades=len(pnl), pnl_usd=sum(pnl), profit_factor=pf, accepted=accepted)
        if accepted:
            allowed.append(key)
    return dict(volatility_threshold=threshold, allowed=allowed, development_buckets=stats)


def filtered_entries(entries, values, model):
    return [bool(entry and bucket(value, model['volatility_threshold']) in model['allowed'])
            for entry, value in zip(entries, values)]


def run():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out = ROOT/'reports'/('regime-study-'+stamp)
    out.mkdir(parents=True)
    def save(name, value):
        (out/name).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    contract = dict(base='trend_hysteresis_4h_balanced', development_fraction=.6,
                    selection_end_fraction=.8, efficiency_window=24, directional_threshold=.3,
                    volatility_split='development_median', minimum_bucket_trades=12,
                    minimum_bucket_pf=1.15, stress_cost_each_side=.0025,
                    evidence='retrospective_previously_examined_data',
                    selection_bias='Base chosen after earlier historical study; not independent proof',
                    activation='none', execution_eligible=False)
    save('contract.json', contract)
    path = ROOT/'data/bitstamp-btc-usd-15m-200000.csv'
    rows = resample(load(path, 'bitstamp', 'BTC/USD'), 4)
    f = features(rows)
    entries, exits = signals(rows, f, 'trend_hysteresis', 'balanced')
    values = regime_features(rows, f)
    n, cutoff, selection_end = len(rows), int(len(rows)*.6), int(len(rows)*.8)
    base_dev = simulate(rows, f, entries, exits, 204, cutoff, .0025, 48)
    model = learn(rows, values, base_dev['trade_records'], cutoff)
    model.update(source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                 training_last_ts=rows[cutoff-1]['ts'], contract=contract)
    save('frozen-model.json', model)  # Write before looking at selection outcomes.
    model_hash = hashlib.sha256((out/'frozen-model.json').read_bytes()).hexdigest()
    filtered = filtered_entries(entries, values, model)
    results = {}
    for name, start, end in [('development', 204, cutoff), ('selection', cutoff, selection_end)]:
        results[name] = {}
        for cost in (0., .0015, .0025):
            results[name][str(cost)] = {
                'base': compact(simulate(rows, f, entries, exits, start, end, cost, 48)),
                'filtered': compact(simulate(rows, f, filtered, exits, start, end, cost, 48))}
    folds = [compact(simulate(rows, f, filtered, exits,
                  cutoff+(selection_end-cutoff)*j//3,
                  cutoff+(selection_end-cutoff)*(j+1)//3, .0025, 48)) for j in range(3)]
    dev = results['development']['0.0025']['filtered']
    selected = results['selection']['0.0025']['filtered']
    passed = gate(dev) and gate(selected) and sum(x['return']>0 for x in folds)>=2
    diagnostics = {}
    if passed:
        diagnostics['tail'] = compact(simulate(rows, f, filtered, exits, selection_end, n, .0025, 48))
        spot = resample(load(ROOT/'data/binance-spot-btcusdt-15m-through-20260916.csv',
                             'binance_spot', 'BTCUSDT'), 4)
        sf = features(spot)
        se, sx = signals(spot, sf, 'trend_hysteresis', 'balanced')
        filtered_spot = filtered_entries(se, regime_features(spot, sf), model)
        diagnostics['binance_spot'] = compact(simulate(spot, sf, filtered_spot, sx, 204, len(spot), .0025, 48))
    status = ('requires_new_forward_evidence' if passed and all(gate(v) for v in diagnostics.values())
              else 'failed_external_diagnostics' if passed else 'failed_development_selection')
    report = dict(status=status, model_sha256=model_hash, model=model, results=results,
                  selection_folds=folds, diagnostics=diagnostics, deployed=False)
    save('report.json', report)
    lines = ['# Piyasa koşulu filtresi deneyi', '',
             'Önceden incelenmiş geçmiş veride araştırma; ileri kârlılık kanıtı değildir.', '',
             '| Bölüm (1000 USD) | Temel net USD | Filtreli net USD | Filtreli işlem |',
             '|---|---:|---:|---:|']
    for name in results:
        r = results[name]['0.0025']
        lines.append(f"| {name} | {r['base']['pnl_usd']:.2f} | {r['filtered']['pnl_usd']:.2f} | {r['filtered']['trades']} |")
    lines += ['', 'Maliyet: alış ve satış başına %0,25 stres varsayımı.',
              'Öğrenme: ilk %60; kontrol: sonraki %20. Eşikler kontrol sonucuna göre değiştirilmedi.',
              'Temel strateji önceki deneyden seçildiği için geçmiş seçim yanlılığı vardır.',
              'Rejim: son 24 mumun yönlü verimliliği; oynaklık eşiği yalnız geliştirme verisinin medyanı.',
              'Kabul edilen koşullar: '+(', '.join(model['allowed']) or 'yok'),
              'Sonuç: '+status, 'Canlı/Testnet yürütme ayarı değiştirilmedi.']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps(dict(directory=str(out), status=status, buckets=model['development_buckets'],
                         selection=results['selection']['0.0025'], folds=folds), indent=2))


if __name__ == '__main__':
    run()
