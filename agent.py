"""Crypto research and paper trading with fail-closed Binance Testnet execution.

No real-money order endpoint is supported.
"""
import argparse
import csv
import json
import math
import socket
import ssl
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from strategies import BAR_SECONDS, BAR_LABEL, BAR_MS

FIELDS = ['ts', 'open', 'high', 'low', 'close', 'volume']
ROOT = Path(__file__).resolve().parent
BASE_URL = 'https://www.bitstamp.net'
EXCHANGE = 'bitstamp'
SYMBOL = 'BTC/USD'
DATA_FIELDS = FIELDS + ['exchange', 'symbol']
DEFAULT_DATA = ROOT / 'data/bitstamp-btc-usd-15m.csv'
DEFAULT_RESEARCH_REPORT = ROOT / 'reports/bitstamp-research-15m.json'
DEFAULT_COMPARISON_REPORT = ROOT / 'reports/strategy-comparison-15m.json'
DEFAULT_V2_DATA = ROOT / 'data/bitstamp-btc-usd-15m-200000.csv'
DEFAULT_V2_MODEL = ROOT / 'state/bollinger-v2-model.json'
DEFAULT_V2_REPORT = ROOT / 'reports/bollinger-v2-training.json'


def inspect_tls_reply(host):
    """Diagnose plaintext network interception; never fetch prices over HTTP."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    connection = ssl.create_default_context().wrap_bio(
        incoming, outgoing, server_hostname=host)
    try:
        connection.do_handshake()
    except ssl.SSLWantReadError:
        pass
    with socket.create_connection((host, 443), timeout=10) as sock:
        sock.sendall(outgoing.read())
        return sock.recv(4096)


def describe_tls_reply(reply):
    if reply.startswith(b'HTTP/'):
        for line in reply.decode('latin-1').split('\r\n'):
            if line.lower().startswith('location:'):
                target = urlsplit(line.split(':', 1)[1].strip()).hostname
                if target == 'guvenliinternet.turktelekom.com.tr':
                    return ('Ağ, borsa bağlantısını Türk Telekom Güvenli İnternet '
                            'sayfasına yönlendiriyor. İnternet hattının Güvenli İnternet '
                            'profilini hat sahibiyle/sağlayıcıyla kontrol edin veya '
                            'borsa erişimine izin verilen bir ağda tekrar deneyin.')
        return 'Ağ, TLS yerine düz HTTP yanıtı veriyor. Proxy/ağ filtresi kontrol edilmeli.'
    return 'TLS bağlantısı kurulamadı; ağ/proxy ayarları kontrol edilmeli.'


def public_json(path, params=None):
    url = BASE_URL + path
    if params:
        url += '?' + urlencode(params)
    req = Request(url, headers={'User-Agent': 'local-crypto-research/0.3'})
    try:
        with urlopen(req, timeout=30) as response:
            payload = json.load(response)
    except URLError as exc:
        if 'WRONG_VERSION_NUMBER' not in str(exc):
            raise
        try:
            detail = describe_tls_reply(inspect_tls_reply(urlsplit(BASE_URL).hostname))
        except (OSError, ValueError):
            detail = 'TLS bağlantısı kurulamadı; ağ/proxy ayarları kontrol edilmeli.'
        raise ValueError(detail + ' Veri indirilmedi; research henüz çalıştırılmamalı.') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
        raise ValueError('Bitstamp beklenen API yanıtını döndürmedi.')
    return payload


def validate(rows, step=BAR_SECONDS):
    if not isinstance(step, int) or isinstance(step, bool) or step <= 0:
        raise ValueError('Mum aralığı pozitif tam sayı saniye olmalı.')
    if not rows:
        raise ValueError('Veri boş.')
    interval_ms = step * 1000
    for i, row in enumerate(rows):
        values = [row[k] for k in FIELDS[1:]]
        if not all(math.isfinite(v) for v in values):
            raise ValueError('Sonlu olmayan veri.')
        if min(values[:4]) <= 0 or row['volume'] < 0:
            raise ValueError('Geçersiz fiyat veya hacim.')
        if not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']:
            raise ValueError('Tutarsız OHLC.')
        if row['ts'] % interval_ms:
            raise ValueError(f'Mum zamanı {step} saniyelik aralığa hizalı değil.')
        if i and row['ts'] - rows[i - 1]['ts'] != interval_ms:
            raise ValueError(f'Veride eksik, tekrar veya sırasız {step} saniyelik mum var.')
    return rows


def fetch(count, step=BAR_SECONDS):
    if not 100 <= count <= 200000:
        raise ValueError('Mum sayısı 100–200000 olmalı.')
    if not isinstance(step, int) or isinstance(step, bool) or step <= 0:
        raise ValueError('Mum aralığı pozitif tam sayı saniye olmalı.')
    collected = {}
    # Pin the window before downloading so a rollover cannot include an open candle.
    cutoff = int(time.time()) // step * step
    end = cutoff - 1
    while len(collected) < count:
        params = {'step': step, 'limit': min(1000, count - len(collected)),
                  'end': end, 'exclude_current_candle': 'true'}
        payload = public_json('/api/v2/ohlc/btcusd/', params)
        data = payload['data']
        if data.get('pair', data.get('market')) != SYMBOL:
            raise ValueError('Bitstamp beklenen BTC/USD piyasasını döndürmedi.')
        batch = data.get('ohlc', [])
        if not batch:
            break
        oldest = min(int(c['timestamp']) for c in batch)
        if oldest > end:
            raise ValueError('Bitstamp sayfalama ilerlemiyor.')
        for candle in batch:
            ts = int(candle['timestamp'])
            if ts + step <= cutoff and ts <= end:
                row = {'ts': ts * 1000, **{k: float(candle[k]) for k in FIELDS[1:]}}
                collected[row['ts']] = row
        end = oldest - 1
        time.sleep(0.15)
    if len(collected) < count:
        raise ValueError(f'Yetersiz veri: {len(collected)}/{count}')
    rows = validate(sorted(collected.values(), key=lambda r: r['ts'])[-count:], step)
    if rows[-1]['ts'] != (cutoff - step) * 1000:
        raise ValueError(f'Bitstamp verisi güncel değil; son kapanmış {step} saniyelik mum eksik.')
    return rows


def read_dataset(path, step=BAR_SECONDS):
    with path.open(encoding='utf-8') as stream:
        raw = list(csv.DictReader(stream))
    if any(r.get('exchange') != EXCHANGE or r.get('symbol') != SYMBOL for r in raw):
        raise ValueError('Veri kaynağı/paritesi uyuşmuyor. Bitstamp BTC/USD verisini yeniden indirin.')
    return validate([{'ts': int(r['ts']), **{k: float(r[k]) for k in FIELDS[1:]}}
                     for r in raw], step)


def backtest(rows, fast=20, slow=50, start=None, fee=0.001, slippage=0.0005,
             allocation=0.10, initial=1000.0):
    validate(rows)
    if not 1 <= fast < slow or slow >= len(rows):
        raise ValueError('Ortalamalar 1 <= hızlı < yavaş < veri uzunluğu olmalı.')
    if not (0 <= fee < 1 and 0 <= slippage < 1 and 0 < allocation <= 1 and initial > 0):
        raise ValueError('Geçersiz maliyet veya bakiye.')
    start = slow if start is None else start
    if not slow <= start < len(rows):
        raise ValueError('Geçersiz test başlangıcı.')
    cash, quantity, peak, drawdown = initial, 0.0, initial, 0.0
    trades = []
    closes = [r['close'] for r in rows]
    for i in range(start, len(rows)):
        # Signal uses only candles closed BEFORE this candle opens.
        bullish = sum(closes[i-fast:i]) / fast > sum(closes[i-slow:i]) / slow
        price = rows[i]['open']
        if bullish and quantity == 0:
            budget = cash * allocation
            execution = price * (1 + slippage)
            quantity = budget / (execution * (1 + fee))
            cash -= budget
            trades.append({'ts': rows[i]['ts'], 'side': 'buy', 'price': execution,
                           'quantity': quantity, 'fee': quantity * execution * fee})
        elif not bullish and quantity > 0:
            execution = price * (1 - slippage)
            cash += quantity * execution * (1 - fee)
            trades.append({'ts': rows[i]['ts'], 'side': 'sell', 'price': execution,
                           'quantity': quantity, 'fee': quantity * execution * fee})
            quantity = 0.0
        # Equity values any open position at estimated net liquidation value.
        equity = cash + quantity * rows[i]['close'] * (1 - slippage) * (1 - fee)
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    return {'quote_currency': 'USD', 'initial_usd': initial, 'final_equity_usd': equity,
            'return_pct': (equity / initial - 1) * 100,
            'max_close_drawdown_pct': drawdown * 100,
            'open_quantity_btc': quantity, 'trade_count': len(trades),
            'start_ts': rows[start]['ts'], 'end_ts': rows[-1]['ts'],
            'parameters': {'fast': fast, 'slow': slow, 'fee': fee,
                           'slippage': slippage, 'allocation': allocation},
            'trades': trades}


def train_v2_challenger(data_path=DEFAULT_V2_DATA):
    """Create one immutable forward challenger with an isolated paper account."""
    import paper
    import paper_v2
    import v2_challengers
    import v2_model

    if paper.running():
        raise ValueError(
            'Challenger kontrol sürümünü dondurmak için önce paper-stop çalıştırın.')
    data_path = Path(data_path)
    if not data_path.is_file():
        raise ValueError(f'Veri dosyası bulunamadı: {data_path}')
    rows = read_dataset(data_path)
    control = paper_v2.load_artifact()
    if control is None:
        raise ValueError('Geçerli aktif v2 kontrol modeli bulunamadı.')
    control_version = str(control['model_version'])
    historical = v2_model.build_historical_samples(
        rows, v2_model.MAIN_SPEC, v2_model.COST_SCENARIOS['30bp'])
    historical_last_ts = int(rows[-1]['ts'])
    dataset_hash = v2_model.dataset_digest(rows)
    cohort_id = (
        f'wil-v1:{dataset_hash[:16]}:{control_version[:16]}')

    db = paper.connect()
    try:
        forward = paper_v2.forward_training_samples(db)
        included_forward = []
        for index, sample in enumerate(forward):
            canonical = v2_model._canonical_forward_sample(sample, index)
            if int(canonical['decision_ts']) > historical_last_ts:
                included_forward.append(canonical)
        combined = sorted(
            [*historical, *included_forward],
            key=lambda sample: (int(sample['fill_ts']), str(sample['id'])))
        v2_model._validate_samples(combined)
        # The entire downloaded snapshot is development data even when its last
        # candles contain no candidate.  Forward scoring starts strictly after it.
        training_cutoff = max(
            historical_last_ts + BAR_MS,
            max(int(sample['label_available_ts']) for sample in combined))
        existing = v2_challengers.registered_model_versions(db, cohort_id)
        if len(existing) > 1:
            raise ValueError('Aynı challenger kohortu için birden fazla model bulundu.')
        created = not existing
        if existing:
            challenger = v2_challengers.load_model(db, existing[0])
        else:
            challenger = v2_challengers.train_frozen_cohort(
                combined,
                cohort_id=cohort_id,
                control_model_version=control_version,
                training_cutoff_label_ts=training_cutoff,
                created_ts=max(int(time.time() * 1000), training_cutoff))
            v2_challengers.register_model(db, challenger)
            with db:
                detail = {
                    'cohort_id': cohort_id,
                    'model_version': challenger['model_version'],
                    'control_model_version': control_version,
                    'historical_events': len(historical),
                    'forward_events_included': len(included_forward),
                    'training_events': len(combined),
                    'training_cutoff_label_ts': training_cutoff,
                    'capital_enabled': False,
                    'isolated_paper_initial_usd':
                        v2_challengers.PAPER_INITIAL_USD,
                }
                paper.put(db, 'v2_last_challenger_training', {
                    **detail, 'ts': time.time()})
                paper.event(db, time.time(), 'system',
                            'v2_challenger_registered', detail)
        v2_challengers.ensure_paper_portfolio(
            db, str(challenger['model_version']), int(time.time() * 1000))
        evaluation = v2_challengers.evaluate_model(
            db, str(challenger['model_version']))
        portfolio = v2_challengers.paper_portfolio_snapshot(
            db, str(challenger['model_version']))
        return {
            'created': created,
            'cohort_id': cohort_id,
            'family': challenger['family'],
            'model_version': challenger['model_version'],
            'control_model_version': control_version,
            'historical_events': len(historical),
            'forward_events_included': len(included_forward),
            'training_events': challenger['fitted']['training_events'],
            'training_cutoff_label_ts': challenger['training_cutoff_label_ts'],
            'threshold': challenger['threshold'],
            'status': evaluation['status'],
            'matched_future_events': evaluation['matched_future_events'],
            'capital_enabled': False,
            'main_1000_usd_capital_enabled': False,
            'isolated_paper_capital_enabled': True,
            'isolated_paper_portfolio': portfolio,
            'execution_writes_enabled': True,
            'execution_table': 'v2_challenger_executions',
        }
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('paper-start', 'paper-status', 'paper-stop'):
        sub.add_parser(command)
    sub.add_parser('migrate-bollinger-15m',
                   help='Sanal hesapları 15 dakikalık Bollinger politikasına geçir')
    sub.add_parser('migrate-bollinger-v2',
                   help='Mevcut sanal bakiyeyi güvenli Bollinger v2 politikasına geçir')
    train_v2 = sub.add_parser('train-bollinger-v2',
                              help='15 dakikalık Bollinger v2 meta-modelini eğit')
    train_v2.add_argument('--data', type=Path, default=DEFAULT_V2_DATA)
    train_v2.add_argument('--model', type=Path, default=DEFAULT_V2_MODEL)
    train_v2.add_argument('--report', type=Path, default=DEFAULT_V2_REPORT)
    train_v2.add_argument('--auto-lock', type=Path, help=argparse.SUPPRESS)
    train_challenger = sub.add_parser(
        'train-bollinger-v2-challenger',
        help='Ayrık mikro hesaplı, dondurulmuş ileri model challengerı eğit')
    train_challenger.add_argument('--data', type=Path, default=DEFAULT_V2_DATA)
    sub.add_parser('challenger-status',
                   help='Dondurulmuş challenger ileri test durumunu göster')
    sub.add_parser('v3-on', help='Quant Remora mikro sanal test hesabını aç')
    sub.add_parser('v3-off', help='Remora yeni girişlerini kapat; açık pozisyonu koruyarak yönet')
    sub.add_parser('v3-status', help='Remora sanal hesap, risk ve eğitim durumunu göster')
    seed_remora = sub.add_parser(
        'seed-remora-history',
        help='Remora modelini tarihsel H8 örnekleriyle başlangıç eğitimine hazırla')
    seed_remora.add_argument('--data', type=Path, default=DEFAULT_V2_DATA)
    seed_remora.add_argument('--samples', type=int, default=200)
    binance_download = sub.add_parser(
        'binance-download', help='Binance Vision doğrulanmış 15m arşivini indir')
    binance_download.add_argument('--market', choices=('um', 'spot'), default='um')
    binance_download.add_argument('--symbol', default='BTCUSDT')
    binance_download.add_argument('--start', required=True, help='YYYY-MM')
    binance_download.add_argument('--end', required=True, help='YYYY-MM')
    binance_download.add_argument('--out', type=Path)
    binance_train = sub.add_parser(
        'train-remora-binance', help='Binance arşivinden ayrı offline Remora modeli eğit')
    binance_train.add_argument('--data', type=Path)
    binance_train.add_argument('--market', choices=('um', 'spot'), default='um')
    binance_train.add_argument('--symbol', default='BTCUSDT')
    binance_train.add_argument('--samples', type=int, default=400)
    binance_train.add_argument('--model', type=Path)
    binance_train.add_argument('--report', type=Path)
    binance_rest = sub.add_parser(
        'binance-rest-download', help='Binance public Spot REST 15m mumlarını indir')
    binance_rest.add_argument('--symbol', default='BTCUSDT')
    binance_rest.add_argument('--start', required=True, help='UTC YYYY-MM-DD')
    binance_rest.add_argument('--end', required=True, help='UTC YYYY-MM-DD, dahil')
    binance_rest.add_argument('--out', type=Path)
    binance_blend = sub.add_parser(
        'train-remora-binance-blend',
        help='Spot ve USD-M basis özellikli ayrı offline challenger eğit')
    binance_blend.add_argument('--spot-data', type=Path, required=True)
    binance_blend.add_argument('--futures-data', type=Path, required=True)
    binance_blend.add_argument('--samples', type=int, default=400)
    binance_blend.add_argument('--model', type=Path)
    binance_blend.add_argument('--report', type=Path)
    binance_derivatives = sub.add_parser(
        'binance-derivatives-download',
        help='Checksum doğrulamalı funding ve günlük futures metrics indir')
    binance_derivatives.add_argument('--symbol', default='BTCUSDT')
    binance_derivatives.add_argument('--start', required=True, help='UTC YYYY-MM-DD')
    binance_derivatives.add_argument('--end', required=True, help='UTC YYYY-MM-DD, dahil')
    binance_derivatives.add_argument('--metrics-out', type=Path)
    binance_derivatives.add_argument('--funding-out', type=Path)
    binance_derivatives_train = sub.add_parser(
        'train-remora-binance-derivatives',
        help='Basis, funding, OI, oran ve rejim ablation challengerlarını eğit')
    binance_derivatives_train.add_argument('--spot-data', type=Path, required=True)
    binance_derivatives_train.add_argument('--futures-data', type=Path, required=True)
    binance_derivatives_train.add_argument('--metrics-data', type=Path)
    binance_derivatives_train.add_argument('--funding-data', type=Path)
    binance_derivatives_train.add_argument('--samples', type=int, default=400)
    binance_derivatives_train.add_argument('--model', type=Path)
    binance_derivatives_train.add_argument('--report', type=Path)
    exit_search = sub.add_parser(
        'research-remora-binance-exits',
        help='Remora stop/target/horizon politikasını embargo ve holdout ile araştır')
    exit_search.add_argument('--futures-data', type=Path, required=True)
    exit_search.add_argument('--sample-cache', type=Path, required=True)
    exit_search.add_argument('--report', type=Path)
    long_horizon = sub.add_parser(
        'research-binance-long-horizon',
        help='Beş yıllık USD-M veride maliyet stresli 4H trend ailelerini araştır')
    long_horizon.add_argument('--data', type=Path, required=True)
    long_horizon.add_argument('--report', type=Path)
    low_frequency = sub.add_parser(
        'research-low-frequency-challenger',
        help='Günlük düşük frekanslı long/cash ailelerini Binance seçimi ve Bitstamp çapraz kontrolüyle araştır')
    low_frequency.add_argument('--binance-data', type=Path, required=True)
    low_frequency.add_argument('--bitstamp-data', type=Path, required=True)
    low_frequency.add_argument('--report', type=Path)
    low_frequency.add_argument('--model', type=Path)
    testnet_policy = sub.add_parser(
        'train-binance-testnet-policy',
        help='Daha aktif günlük Testnet challenger politikasını maliyet stresiyle eğit')
    testnet_policy.add_argument(
        '--binance-data', type=Path,
        default=ROOT / 'data/binance-um-btcusdt-15m-5y.csv')
    testnet_policy.add_argument(
        '--bitstamp-data', type=Path,
        default=ROOT / 'data/bitstamp-btc-usd-15m-200000.csv')
    testnet_policy.add_argument(
        '--spot-data', type=Path,
        default=ROOT / 'data/binance-spot-btcusdt-15m-1y.csv')
    testnet_policy.add_argument(
        '--report', type=Path,
        default=ROOT / 'reports/binance-testnet-active-policy.json')
    testnet_policy.add_argument(
        '--config', type=Path,
        default=ROOT / 'config/binance-testnet-active-policy.json')
    sub.add_parser('binance-execution-doctor',
                   help='Binance Spot Testnet public bağlantı ve BTCUSDT filtrelerini doğrula')
    sub.add_parser('binance-testnet-agent-start',
                   help='Ayrı Binance Spot Testnet background workerını başlat')
    sub.add_parser('binance-testnet-agent-status',
                   help='Binance Spot Testnet worker, sinyal ve pozisyon durumunu göster')
    sub.add_parser('binance-testnet-agent-stop',
                   help='Binance Spot Testnet workerını güvenli biçimde durdur')
    sub.add_parser('binance-testnet-agent-reset',
                   help='Aylık Testnet sıfırlamasından sonra durmuş worker defterini arşivle')
    sub.add_parser('binance-testnet-learning-status',
                   help='Testnet günlük öğrenme ve dondurulmuş challenger kanıtını göster')
    sub.add_parser('binance-testnet-account',
                   help='Ortam değişkenlerindeki Testnet anahtarıyla bakiye ve açık emirleri uzlaştır')
    order_check = sub.add_parser('binance-testnet-order-check',
                                 help='Emir oluşturmadan Binance /order/test doğrulaması yap')
    order_check.add_argument('--quote-usdt', type=float, default=5.0)
    sub.add_parser('exploration-on', help='15 dakikalık küçük sanal keşif işlemlerini aç')
    sub.add_parser('exploration-off', help='Yeni keşif işlemlerini kapat')
    sub.add_parser('learn-status', help='Öğrenme verisi ve model doğrulama durumu')
    learn = sub.add_parser('learn-seed', help='Geçmiş sanal işlemlerle başlangıç modeli eğit')
    learn.add_argument('--data', type=Path, default=DEFAULT_DATA)
    sub.add_parser('doctor', help='Bitstamp HTTPS/API ve 15 dakikalık mum verisini kontrol et')
    download = sub.add_parser('download')
    download.add_argument('--candles', type=int, default=3000)
    download.add_argument('--out', type=Path, default=DEFAULT_DATA)
    research = sub.add_parser('research')
    research.add_argument('--data', type=Path, default=DEFAULT_DATA)
    research.add_argument('--out', type=Path, default=DEFAULT_RESEARCH_REPORT)
    compare = sub.add_parser('compare', help='Üç stratejiyi ilerleyen dönemlerde karşılaştır')
    compare.add_argument('--data', type=Path, default=DEFAULT_DATA)
    compare.add_argument('--out', type=Path, default=DEFAULT_COMPARISON_REPORT)
    args = parser.parse_args()
    if args.command == 'migrate-bollinger-15m':
        from paper import migrate_to_bollinger
        migrate_to_bollinger()
        return
    if args.command == 'migrate-bollinger-v2':
        from paper_v2 import migrate
        migrate()
        return
    if args.command == 'train-bollinger-v2':
        if not args.data.is_file():
            raise ValueError(f'Veri dosyası bulunamadı: {args.data}')
        import paper
        import paper_v2
        import v2_model
        auto_lock = args.auto_lock.resolve() if args.auto_lock else None
        if auto_lock and auto_lock != paper_v2.AUTO_RETRAIN_LOCK.resolve():
            raise ValueError('Geçersiz otomatik eğitim kilidi.')
        update_lock_token = None
        try:
            update_lock_token = paper_v2.acquire_model_update_lock('offline_training')
            if update_lock_token is None:
                raise ValueError('Başka bir v2 model güncellemesi devam ediyor.')
            db = paper.connect()
            try:
                forward_samples = paper_v2.forward_training_samples(db)
            finally:
                db.close()
            result = v2_model.train_and_report(
                read_dataset(args.data), args.data, args.model, args.report,
                forward_samples=forward_samples)
            # A deterministic no-change refit can retain its exact model version;
            # in that case restore any forward promotion evidence for that version.
            db = paper.connect()
            try:
                refreshed = paper_v2.refresh_promotion(
                    db, result['artifact'], args.report, args.model,
                    update_lock_token=update_lock_token)
                if refreshed is not None:
                    result['artifact'] = refreshed
                    updated_report = paper_v2._read_training_report(args.report)
                    if updated_report is not None:
                        result['report'] = updated_report
                with db:
                    paper.put(db, 'v2_last_training', {
                        'ts': time.time(),
                        'model_version': result['artifact']['model_version'],
                        'forward_training_events': len(forward_samples),
                        'status': result['artifact']['status'],
                    })
                    paper.event(db, time.time(), 'system', 'v2_training_completed', {
                        'model_version': result['artifact']['model_version'],
                        'forward_training_events': len(forward_samples),
                        'status': result['artifact']['status'],
                    })
            finally:
                db.close()
            contract = result['report']['label_contract']
            summary = {
                'status': result['report']['status'],
                'eligible': result['report']['eligible'],
                'model_version': result['report']['model_version'],
                'historical_events': contract['historical_events'],
                'forward_training_events': contract.get('forward_training_events', 0),
                'total_training_events': contract.get(
                    'total_training_events', contract['historical_events']),
                'walk_forward_30bp': result['report']['walk_forward']['aggregate_cost_metrics']['30bp'],
            }
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        finally:
            if update_lock_token is not None:
                paper_v2.release_model_update_lock(update_lock_token)
            if auto_lock:
                auto_lock.unlink(missing_ok=True)
        return
    if args.command == 'train-bollinger-v2-challenger':
        print(json.dumps(train_v2_challenger(args.data), indent=2,
                         ensure_ascii=False))
        return
    if args.command == 'challenger-status':
        import paper
        import v2_challengers
        db = paper.connect()
        try:
            print(json.dumps(v2_challengers.status(db), indent=2,
                             ensure_ascii=False))
        finally:
            db.close()
        return
    if args.command in ('v3-on', 'v3-off', 'v3-status'):
        import paper
        import paper_v3
        db = paper.connect()
        try:
            if args.command == 'v3-on':
                result = paper_v3.enable(db, int(time.time() * 1000))
            elif args.command == 'v3-off':
                result = paper_v3.disable(db, int(time.time() * 1000))
            else:
                result = paper_v3.status(db)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        finally:
            db.close()
        return
    if args.command == 'seed-remora-history':
        if not args.data.is_file():
            raise ValueError(f'Veri dosyası bulunamadı: {args.data}')
        import paper
        import paper_v3
        db = paper.connect()
        try:
            result = paper_v3.seed_historical_samples(
                db, read_dataset(args.data), args.samples)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        finally:
            db.close()
        return
    if args.command == 'binance-download':
        import binance_archive
        out = args.out or (
            binance_archive.DEFAULT_SPOT_DATA
            if args.market == 'spot' else binance_archive.DEFAULT_DATA)
        result = binance_archive.download_monthly(
            args.market, args.symbol, args.start, args.end, out)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if args.command == 'binance-rest-download':
        import binance_archive
        out = args.out or binance_archive.DEFAULT_SPOT_DATA
        result = binance_archive.download_spot_rest(
            args.symbol, args.start, args.end, out)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if args.command == 'train-remora-binance':
        import binance_archive
        spot = args.market == 'spot'
        data = args.data or (
            binance_archive.DEFAULT_SPOT_DATA if spot else binance_archive.DEFAULT_DATA)
        model = args.model or (
            binance_archive.DEFAULT_SPOT_MODEL if spot else binance_archive.DEFAULT_MODEL)
        report = args.report or (
            binance_archive.DEFAULT_SPOT_REPORT if spot else binance_archive.DEFAULT_REPORT)
        if not data.is_file():
            raise ValueError(f'Binance veri dosyası bulunamadı: {data}')
        result = binance_archive.train_offline(
            data, model, report, args.samples, args.market, args.symbol)
        print(json.dumps({
            'kind': result['kind'], 'candles': result['candles'],
            'samples': result['model_state'].get('sample_count', 0),
            'validation': result['model_state'].get('validation'),
            'deployed': result['deployed'], 'model': str(model.resolve()),
            'report': str(report.resolve()),
        }, indent=2, ensure_ascii=False))
        return
    if args.command == 'train-remora-binance-blend':
        import binance_archive
        model = args.model or binance_archive.DEFAULT_BLEND_MODEL
        report = args.report or binance_archive.DEFAULT_BLEND_REPORT
        result = binance_archive.train_blended_offline(
            args.spot_data, args.futures_data, model, report, args.samples)
        state = result['model_state']
        print(json.dumps({
            'kind': result['kind'], 'candles': result['candles'],
            'samples': result['samples'], 'validation': state.get('validation'),
            'deployed': result['deployed'], 'model': str(model.resolve()),
            'report': str(report.resolve()),
        }, indent=2, ensure_ascii=False))
        return
    if args.command == 'binance-derivatives-download':
        import binance_derivatives
        metrics_out = args.metrics_out or binance_derivatives.DEFAULT_METRICS_DATA
        funding_out = args.funding_out or binance_derivatives.DEFAULT_FUNDING_DATA
        metrics = binance_derivatives.download_metrics(
            args.symbol, args.start, args.end, metrics_out)
        funding = binance_derivatives.download_funding(
            args.symbol, args.start[:7], args.end[:7], funding_out)
        print(json.dumps({
            'metrics': metrics, 'funding': funding,
            'real_orders_enabled': False,
        }, indent=2, ensure_ascii=False))
        return
    if args.command == 'train-remora-binance-derivatives':
        import binance_derivatives
        metrics = args.metrics_data or binance_derivatives.DEFAULT_METRICS_DATA
        funding = args.funding_data or binance_derivatives.DEFAULT_FUNDING_DATA
        model = args.model or binance_derivatives.DEFAULT_MODEL
        report = args.report or binance_derivatives.DEFAULT_REPORT
        result = binance_derivatives.train_derivatives_offline(
            args.spot_data, args.futures_data, metrics, funding,
            model, report, args.samples)
        summary = {
            name: {
                'brier': item['model_state'].get('validation', {}).get('brier'),
                'baseline_brier': item['model_state'].get('validation', {}).get('baseline_brier'),
                'accepted': item['model_state'].get('validation', {}).get('accepted'),
                'quality_pass': item['model_state'].get('validation', {}).get('quality_pass'),
            }
            for name, item in result['evaluations'].items()
        }
        return_summary = {
            name: {
                'accepted': item['accepted'],
                'stressed_sum_trade_returns': item['stressed_sum_trade_returns'],
                'quality_pass': item['quality_pass'],
            }
            for name, item in result['return_evaluations'].items()
        }
        print(json.dumps({
            'kind': result['kind'], 'candles': result['candles'],
            'matched_samples': result['matched_samples'],
            'best_brier_variant': result['best_brier_variant'],
            'best_return_variant': result['best_return_variant'],
            'return_candidate_quality_pass': result['return_candidate_quality_pass'],
            'research_quality_candidate': result['research_quality_candidate'],
            'selected_for_forward_shadow': result['selected_for_forward_shadow'],
            'deployed': result['deployed'], 'evaluations': summary,
            'return_evaluations': return_summary,
            'model': str(model.resolve()), 'report': str(report.resolve()),
        }, indent=2, ensure_ascii=False))
        return
    if args.command == 'research-remora-binance-exits':
        import binance_derivatives
        report_path = args.report or binance_derivatives.DEFAULT_EXIT_REPORT
        result = binance_derivatives.search_exit_policies(
            args.futures_data, args.sample_cache, report_path)
        print(json.dumps({
            'kind': result['kind'], 'events': result['events'],
            'grid_candidates': result['grid_candidates'],
            'segments': result['segments'],
            'eligible_before_holdout': result['eligible_before_holdout'],
            'selected_policy': result['selected_policy'],
            'holdout': result['holdout'], 'holdout_pass': result['holdout_pass'],
            'deployable': result['deployable'],
            'report': str(report_path.resolve()),
        }, indent=2, ensure_ascii=False))
        return
    if args.command == 'research-binance-long-horizon':
        import long_horizon
        report_path = args.report or long_horizon.DEFAULT_REPORT
        result = long_horizon.research(args.data, report_path)
        print(json.dumps({
            'kind': result['kind'], 'candles_15m': result['candles_15m'],
            'bars_4h': result['bars_4h'],
            'configuration_count': result['configuration_count'],
            'eligible_before_holdout': result['eligible_before_holdout'],
            'selected': result['selected'], 'holdout': result['holdout'],
            'holdout_pass': result['holdout_pass'],
            'deployed': result['deployed'],
            'report': str(report_path.resolve()),
        }, indent=2, ensure_ascii=False))
        return
    if args.command == 'research-low-frequency-challenger':
        import low_frequency
        report_path = args.report or low_frequency.DEFAULT_REPORT
        model_path = args.model or low_frequency.DEFAULT_MODEL
        result = low_frequency.research(
            args.binance_data, args.bitstamp_data, report_path, model_path)
        print(json.dumps({
            'kind': result['kind'],
            'configuration_count': result['configuration_count'],
            'selected': result['selected'],
            'external_bitstamp': result['external_bitstamp'],
            'status': result['status'],
            'forward_shadow_candidate': result['forward_shadow_candidate'],
            'deployed': result['deployed'],
            'capital_enabled': result['capital_enabled'],
            'report': str(report_path.resolve()),
            'model': str(model_path.resolve()),
        }, indent=2, ensure_ascii=False))
        return
    if args.command == 'train-binance-testnet-policy':
        import binance_testnet_worker
        import testnet_policy_trainer
        # Share the same stable control mutex as start/stop/reset.  The worker must
        # remain stopped for the entire train-and-publish transition.
        with binance_testnet_worker._control_mutex():
            if binance_testnet_worker._lock_active(
                binance_testnet_worker.ACCOUNT_LOCK_PATH
            ):
                raise ValueError(
                    'Başka bir Testnet policy worker hesap yürütme kilidini tutuyor; '
                    'eğitimden önce o worker durdurulmalıdır.')
            # Hold the same lifetime lease as a worker while checking, training,
            # and replacing the config.  A direct `worker.py run` therefore cannot
            # enter between the stopped-state check and config publication.
            with binance_testnet_worker._process_lock(
                binance_testnet_worker.ACCOUNT_LOCK_PATH
            ):
                status = binance_testnet_worker.status_snapshot()
                if status['running'] or status['desired_running']:
                    raise ValueError(
                        'Testnet policy eğitimi için önce '
                        'binance-testnet-agent-stop kullanın.')
                if (
                    status['position'] != 'cash'
                    or status['pending_client_id'] is not None
                    or status['halted']
                    or not status['pnl_complete']
                    or not status['policy_match']
                ):
                    raise ValueError(
                        'Yeni Testnet policy ayrı deftere geçirilmeden önce eski '
                        'defter nakit, bekleyen emirsiz, uzlaşmış ve sağlıklı '
                        'olmalıdır.')
                report, artifact = testnet_policy_trainer.train(
                    args.binance_data, args.bitstamp_data, args.spot_data)
                if not artifact['testnet_execution_eligible']:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    temporary = args.report.with_suffix(args.report.suffix + '.tmp')
                    temporary.write_text(
                        json.dumps(report, indent=2, sort_keys=True) + '\n',
                        encoding='utf-8')
                    temporary.replace(args.report)
                    raise ValueError(
                        'Hiçbir ön-kayıtlı aday Testnet exploration kapısını '
                        'geçmedi; mevcut config değiştirilmedi.')
                testnet_policy_trainer.write_outputs(
                    report, artifact,
                    report_path=args.report, artifact_path=args.config)
        print(json.dumps({
            'policy_id': artifact['policy_id'],
            'model_version': artifact['model_version'],
            'status': artifact['status'],
            'rule': artifact['rule'],
            'order': artifact['order'],
            'eligible_candidate_count': report['eligible_candidate_count'],
            'report': str(args.report.resolve()),
            'config': str(args.config.resolve()),
            'paper_eligible': artifact['paper_eligible'],
            'real_money_eligible': artifact['real_money_eligible'],
        }, indent=2, ensure_ascii=True))
        return
    if args.command == 'binance-execution-doctor':
        import binance_execution
        print(json.dumps(binance_execution.public_doctor(), indent=2, ensure_ascii=True))
        return
    if args.command == 'binance-testnet-learning-status':
        import binance_testnet_worker
        print(json.dumps(
            binance_testnet_worker.learning_snapshot(refresh=False),
            indent=2,
            ensure_ascii=True,
        ))
        return
    if args.command.startswith('binance-testnet-agent-'):
        import binance_testnet_worker
        try:
            result = binance_testnet_worker.control(args.command.rsplit('-', 1)[1])
        except binance_testnet_worker.WorkerHalt as exc:
            raise ValueError(str(exc)) from exc
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return
    if args.command == 'binance-testnet-account':
        import binance_execution
        client = binance_execution.Client()
        print(json.dumps({"account": client.account(), "open_orders": client.open_orders(),
                          "real_money_supported": False}, indent=2, ensure_ascii=True))
        return
    if args.command == 'binance-testnet-order-check':
        import binance_execution
        result = binance_execution.Client().test_market_buy(args.quote_usdt)
        print(json.dumps({"validated": True, "order_created": False,
                          "response": result}, indent=2, ensure_ascii=True))
        return
    if args.command.startswith('learn-'):
        import paper, learning
        db = paper.connect()
        try:
            if args.command == 'learn-seed':
                if paper.running():
                    raise ValueError('Başlangıç eğitimi için önce paper-stop ile süreci durdurun.')
                learning.seed(db,read_dataset(args.data))
            result = learning.status(db)
            print(json.dumps(result,indent=2,ensure_ascii=False))
            report_path = ROOT / 'reports/learning-status.json'
            report_path.parent.mkdir(parents=True,exist_ok=True)
            report_path.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
        finally:
            db.close()
        return
    if args.command.startswith('paper-'):
        from paper import control
        control(args.command.split('-')[1])
        return
    if args.command.startswith('exploration-'):
        from paper import configure_exploration
        configure_exploration(args.command == 'exploration-on')
        return
    if args.command == 'doctor':
        rows = fetch(100)
        print(f'OK: Bitstamp BTC/USD bağlantısı doğrulandı; '
              f'{len(rows)} kapanmış 15 dakikalık mum geçerli.')
    elif args.command == 'download':
        rows = fetch(args.candles)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=DATA_FIELDS)
            writer.writeheader()
            writer.writerows({**r, 'exchange': EXCHANGE, 'symbol': SYMBOL} for r in rows)
        print(f'Bitstamp BTC/USD: {len(rows)} kapanmış 15 dakikalık mum: {args.out}')
    else:
        if not args.data.is_file():
            raise ValueError(f'Veri dosyası bulunamadı: {args.data}. '
                             'Önce doctor, ardından download komutunu başarıyla tamamlayın.')
        rows = read_dataset(args.data)
        if args.command == 'compare':
            from experiment import save_report
            report = save_report(rows, args.data, args.out)
            print(json.dumps(report['summary'], indent=2, ensure_ascii=False))
            print(f'Rapor: {args.out.with_suffix(".md")}')
            return
        if len(rows) < 500:
            raise ValueError('Araştırma için en az 500 adet 15 dakikalık mum gerekli.')
        split = int(len(rows) * 0.7)
        candidates = [(10, 30), (20, 50), (30, 100)]
        training = [backtest(rows[:split], f, s, start=100) for f, s in candidates]
        best = max(training, key=lambda r: r['return_pct'])['parameters']
        report = {'mode': 'historical_simulation', 'exchange': EXCHANGE,
                  'symbol': SYMBOL, 'bar': BAR_LABEL,
                  'data_file': str(args.data.resolve()),
                  'zero_volume_candles': sum(r['volume'] == 0 for r in rows),
                  'method': 'First 70% parameter selection; final 30% held-out evaluation.',
                  'training': training,
                  'held_out': backtest(rows, best['fast'], best['slow'], start=split),
                  'limitations': ['Not an ML or LLM model; fixed moving-average rules.',
                                  'Assumed costs, not account-specific Bitstamp fees.',
                                  'Bitstamp BTC/USD prices are not OKX BTC/USDT prices.',
                                  'No live orders, stop loss or daily loss circuit breaker.',
                                  'Repeated tuning on this holdout invalidates independence.',
                                  'Drawdown measured at 15-minute closes; intrabar risk excluded.']}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in report['held_out'].items() if k != 'trades'}, indent=2))
        print(f'Rapor: {args.out}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, IndexError) as exc:
        raise SystemExit(f'Hata: {exc}')
