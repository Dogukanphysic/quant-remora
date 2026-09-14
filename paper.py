"""Persistent local paper portfolios. Public GET requests only; no exchange orders."""
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from strategies import (NAMES, MODEL_KEYS, BAR_SECONDS, BAR_MS, BAR_LABEL, POLICY_ID,
                        FRESH_WINDOW_SECONDS, Risk, indicators, signal, size_position)
import learning

PORTFOLIOS = tuple(NAMES) + tuple('learned_'+name for name in NAMES)
TOTAL_PAPER_BUDGET_USD = 1000.0
EXPLORATION_STATE_KEY = 'exploration_enabled'
EXPLORATION_RISK = Risk(risk_fraction=.0005, allocation_cap=.025)
EXPLORATION_LOW_RISK = Risk(risk_fraction=.0001, allocation_cap=.005)
EXPLORATION_HOLD_SECONDS = BAR_SECONDS
EXPLORATION_LABEL_GRACE_SECONDS = FRESH_WINDOW_SECONDS
EXPLORATION_SAMPLE_MAX_HOLD_SECONDS = (
    EXPLORATION_HOLD_SECONDS + EXPLORATION_LABEL_GRACE_SECONDS)
PAPER_SOURCE = 'paper_bb15'
EXPLORATION_SOURCE = 'paper_exploration_bb15'
LEGACY_EXPLORATION_MODEL = 'exploration_1h'


def build_default_budgets():
    share = round(TOTAL_PAPER_BUDGET_USD / len(PORTFOLIOS), 2)
    budgets = {name: share for name in PORTFOLIOS}
    budgets[PORTFOLIOS[-1]] = round(
        TOTAL_PAPER_BUDGET_USD - share * (len(PORTFOLIOS) - 1), 2)
    return budgets


DEFAULT_BUDGETS = build_default_budgets()

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'state/paper.sqlite3'
LOCK = ROOT / 'state/paper.lock'
CONTROL_LOCK = ROOT / 'state/paper-control.lock'


def connect(path=DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, strategy TEXT, kind TEXT, detail TEXT)')
    learning.ensure_tables(db)
    db.commit()
    return db


def get(db, key, default=None):
    row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value, allow_nan=False)))


def event(db, now, strategy, kind, detail):
    db.execute('INSERT INTO events(ts,strategy,kind,detail) VALUES (?,?,?,?)',
               (now, strategy, kind, json.dumps(detail, allow_nan=False)))


def quote():
    req = Request('https://www.bitstamp.net/api/v2/ticker/btcusd/',
                  headers={'User-Agent': 'local-crypto-paper/1.0'})
    with urlopen(req, timeout=15) as response:
        raw = json.load(response)
    return {k: float(raw[k]) for k in ('bid', 'ask', 'timestamp')}


def check_quote(q, now):
    if not all(math.isfinite(q[k]) for k in ('bid', 'ask', 'timestamp')):
        raise ValueError('Geçersiz fiyat verisi.')
    if not 0 < q['bid'] <= q['ask'] or not -10 <= now-q['timestamp'] <= 120:
        raise ValueError('Fiyat verisi eski veya tutarsız; sanal işlem yapılmadı.')


def exploration_rotation(candle):
    """Rotate the first-choice account without pinning it to one time of day."""
    learned = ['learned_'+name for name in NAMES]
    if candle is None:
        return learned
    bar = candle // BAR_MS
    bars_per_day = 86_400 // BAR_SECONDS
    start = (bar + bar // bars_per_day) % len(learned)
    return learned[start:] + learned[:start]


def close_position(db, state, name, base_name, p, q, now, reason, risk):
    price = q['bid']*(1-risk.slippage)
    proceeds = p['quantity']*price*(1-risk.fee)
    pnl = proceeds-p['cost']
    state['cash_usd'] += proceeds
    state['realized_pnl_usd'] += pnl
    state['completed_trades'] += 1
    state['position'] = None
    event(db, now, name, 'sell', {'price': price, 'quantity': p['quantity'],
          'pnl_usd': pnl, 'reason': reason, 'entry_ts': p['ts'],
          'entry_mode': p.get('entry_mode', 'strategy')})
    if p.get('learning_policy') == learning.EXPLORATION_MODEL:
        holding_seconds = now-p['ts']
        metadata = {'exit_reason': reason,
                    'holding_seconds': holding_seconds,
                    'risk_tier': p.get('risk_tier'),
                    'risk_fraction': p.get('risk_fraction'),
                    'allocation_cap': p.get('allocation_cap'),
                    'planned_loss_usd': p.get('planned_loss_usd'),
                    'policy_id': POLICY_ID,
                    'bar_seconds': BAR_SECONDS}
        if holding_seconds <= EXPLORATION_SAMPLE_MAX_HOLD_SECONDS:
            learning.add_sample(db, learning.EXPLORATION_MODEL, EXPLORATION_SOURCE,
                                p['ts'], now, p['learning_x'], pnl/p['cost'], metadata)
        else:
            # Preserve the full result for audit while keeping it out of model queries.
            learning.add_sample(db, learning.EXPLORATION_MODEL, EXPLORATION_SOURCE,
                                p['ts'], now, p['learning_x'], pnl/p['cost'], metadata)
            quarantined = learning.quarantine_overlong_exploration_samples(
                db, EXPLORATION_SAMPLE_MAX_HOLD_SECONDS, now)
            event(db, now, name, 'learning_sample_skipped', {
                **metadata,
                'reason': 'exploration_label_horizon_exceeded',
                'observed_exit_reason': reason,
                'max_holding_seconds': EXPLORATION_SAMPLE_MAX_HOLD_SECONDS,
                'quarantined_sample_ids': quarantined})
    elif (p.get('learning_policy') == LEGACY_EXPLORATION_MODEL
          and reason != 'policy_migration_censored'):
        learning.add_sample(db, LEGACY_EXPLORATION_MODEL, 'paper_exploration',
                            p['ts'], now, p['learning_x'], pnl/p['cost'], {
                                'exit_reason': reason,
                                'holding_seconds': now-p['ts'],
                                'legacy_policy': True})
    elif not name.startswith('learned_') and 'learning_x' in p:
        model_key = p.get('model_key', MODEL_KEYS.get(base_name))
        if model_key:
            learning.add_sample(db, model_key, PAPER_SOURCE, p['ts'], now,
                                p['learning_x'], pnl/p['cost'], {
                                    'exit_reason': reason,
                                    'policy_id': p.get('policy_id', POLICY_ID),
                                    'bar_seconds': p.get('bar_seconds', BAR_SECONDS)})


def tick(db, q, rows, now):
    """One atomic update across all portfolios and their event logs."""
    check_quote(q, now)
    risk = Risk()
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    features = indicators(rows) if rows else None
    candle = rows[-1]['ts'] if rows else None
    fresh = (rows is not None and
             0 <= now - (candle/1000+BAR_SECONDS) <= FRESH_WINDOW_SECONDS)
    with db:
        if get(db, 'stop', False):
            return False
        epoch = get(db, 'policy_epoch')
        existing_state = any(get(db, name) is not None for name in PORTFOLIOS)
        if epoch not in (None, POLICY_ID) or (epoch is None and existing_state):
            raise ValueError('Portföy durumu aktif Bollinger 15 dakika politikasıyla uyuşmuyor.')
        if epoch is None:
            put(db, 'policy_epoch', POLICY_ID)
        exploration_enabled = get(db, EXPLORATION_STATE_KEY, False)
        probe_model = learning.model_state(db, learning.EXPLORATION_MODEL)
        existing_probes = [name for name in PORTFOLIOS
                           if ((get(db, name) or {}).get('position') or {}).get(
                               'entry_mode') == 'exploration']
        active_probes = len(existing_probes)
        probe_reserved = False
        learned_order = existing_probes + [
            name for name in exploration_rotation(candle) if name not in existing_probes]
        portfolio_order = list(NAMES) + learned_order
        for name in portfolio_order:
            base_name = name.removeprefix('learned_')
            learned = name.startswith('learned_')
            model_key = MODEL_KEYS[base_name]
            model = learning.model_state(db,model_key)
            state = get(db, name)
            if state is None:
                initial = DEFAULT_BUDGETS[name]
                state = {'initial_usd': initial, 'cash_usd': initial,
                         'position': None, 'last_candle': candle,
                         'day': day, 'day_start_equity': initial, 'daily_halt': False,
                         'realized_pnl_usd': 0., 'completed_trades': 0,
                         'policy_id': POLICY_ID, 'bar_seconds': BAR_SECONDS}
                event(db, now, name, 'initialized',
                      {'initial_usd': initial, 'anchor_candle': candle})
            p = state['position']
            equity = state['cash_usd'] + (p['quantity']*q['bid']*(1-risk.slippage)*(1-risk.fee) if p else 0)
            initial = state.get('initial_usd', DEFAULT_BUDGETS[name])
            state['peak_equity_usd'] = max(
                state.get('peak_equity_usd', max(initial, state.get('equity_usd', initial))), equity)
            state['drawdown_pct'] = (1-equity/state['peak_equity_usd'])*100
            state['drawdown_halt'] = state.get('drawdown_halt',False) or state['drawdown_pct']>=8
            if state['day'] != day:
                state.update(day=day, day_start_equity=equity, daily_halt=False)
            if equity <= state['day_start_equity']*.98:
                state['daily_halt'] = True
            new_bar = candle is not None and (state['last_candle'] is None or candle > state['last_candle'])
            enter = leave = False
            entry_mode = None
            entry_risk = risk
            if new_bar:
                # First valid candle anchors the account. No replay of downtime trades.
                baseline_entry = False
                if state['last_candle'] is not None and fresh:
                    baseline_entry, leave = signal(rows, features, len(rows), base_name)
                enter = baseline_entry
                if baseline_entry:
                    x = learning.vector(rows,features,len(rows))
                    review = learning.assess(model['model'],x,(q['ask']-q['bid'])/q['bid'])
                    event(db,now,name,'model_review',{**review,'mode':model['status'],
                          'version':model.get('version'), 'baseline_entry':True})
                    if learned:
                        enter = bool(model['eligible'] and review['accept'])
                        entry_mode = 'model' if enter else None
                    else:
                        entry_mode = 'strategy'
                in_lower_band_zone = features['bb_percent_b'][-1] <= .25
                can_explore = (exploration_enabled and learned and not p and not enter and
                               active_probes == 0 and
                               not probe_reserved and state['last_candle'] is not None and
                               fresh and rows[-1]['volume'] > 0 and in_lower_band_zone)
                if can_explore:
                    probe_reserved = True
                    x = learning.vector(rows,features,len(rows))
                    review = learning.assess(
                        probe_model['model'], x, (q['ask']-q['bid'])/q['bid'])
                    trusted = bool(probe_model.get('eligible') and review['accept'])
                    entry_risk = EXPLORATION_RISK if (
                        not probe_model.get('eligible') or trusted) else EXPLORATION_LOW_RISK
                    risk_tier = ('training' if not probe_model.get('eligible') else
                                 'accepted' if trusted else 'rejected')
                    entry_mode = 'exploration'
                    enter = True
                    event(db, now, name, 'exploration_review', {
                        **review, 'mode': probe_model['status'],
                        'version': probe_model.get('version'),
                        'risk_fraction': entry_risk.risk_fraction,
                        'allocation_cap': entry_risk.allocation_cap,
                        'risk_tier': risk_tier,
                        'bollinger_percent_b': features['bb_percent_b'][-1],
                        'label_policy': 'bollinger_lower_zone_protective_or_15m_timeout'})
                event(db, now, name, 'decision', {'candle': candle, 'entry': enter,
                      'entry_mode': entry_mode, 'baseline_entry': baseline_entry,
                      'exit': leave, 'fresh': fresh, 'daily_halt': state['daily_halt']})
                state['last_candle'] = candle
            reason = None
            if p:
                if state['drawdown_halt']:
                    reason = 'total_drawdown_halt'
                elif state['daily_halt']:
                    reason = 'daily_loss_halt'
                elif q['bid'] <= p['stop']:
                    reason = 'stop'
                elif q['bid'] >= p['target']:
                    reason = 'target'
                elif (p.get('entry_mode') == 'exploration'
                      and now >= p.get('expires_ts', float('inf'))):
                    reason = 'exploration_timeout'
                elif p.get('entry_mode') != 'exploration' and leave:
                    reason = 'strategy_exit'
            if reason:
                close_position(db, state, name, base_name, p, q, now, reason, risk)
                if p.get('entry_mode') == 'exploration':
                    active_probes -= 1
            elif not p and enter and not state['daily_halt'] and not state['drawdown_halt']:
                if rows[-1]['volume'] <= 0:
                    event(db, now, name, 'entry_blocked', {'reason': 'zero_volume'})
                elif (q['ask']-q['bid'])/q['bid'] <= .01:
                    cash_before = state['cash_usd']
                    p = size_position(cash_before, q['ask'], features['atr'][-1], entry_risk)
                    if p:
                        p['ts'] = now
                        p['learning_x'] = learning.vector(rows,features,len(rows))
                        p['model_version'] = model.get('version')
                        p['model_key'] = model_key
                        p['entry_mode'] = entry_mode
                        p['entry_candle'] = candle
                        p['policy_id'] = POLICY_ID
                        p['bar_seconds'] = BAR_SECONDS
                        p['risk_fraction'] = entry_risk.risk_fraction
                        p['allocation_cap'] = entry_risk.allocation_cap
                        p['planned_loss_usd'] = (p['cost'] - p['quantity']*p['stop']*
                                                 (1-entry_risk.slippage)*(1-entry_risk.fee))
                        if entry_mode == 'exploration':
                            p['learning_policy'] = learning.EXPLORATION_MODEL
                            p['expires_ts'] = now + EXPLORATION_HOLD_SECONDS
                            p['risk_tier'] = risk_tier
                            p['model_version'] = probe_model.get('version')
                            active_probes += 1
                        state['cash_usd'] -= p['cost']
                        state['position'] = p
                        event(db, now, name, 'buy', p)
                else:
                    event(db, now, name, 'entry_blocked', {'reason': 'spread_above_1pct'})
            p = state['position']
            state['equity_usd'] = state['cash_usd'] + (p['quantity']*q['bid']*(1-risk.slippage)*(1-risk.fee) if p else 0)
            if state['equity_usd'] <= state['day_start_equity']*.98:
                state['daily_halt'] = True
            state['peak_equity_usd'] = max(state['peak_equity_usd'],state['equity_usd'])
            state['drawdown_pct'] = (1-state['equity_usd']/state['peak_equity_usd'])*100
            state['drawdown_halt'] = state['drawdown_halt'] or state['drawdown_pct']>=8
            put(db, name, state)
        put(db, 'last_quote', q)
        put(db, 'last_success', now)
    return True


def _acquire_file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    if path.stat().st_size == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def acquire_lock():
    return _acquire_file_lock(LOCK)


def acquire_control_lock():
    """Serialize start and migration without occupying the worker run lock."""
    return _acquire_file_lock(CONTROL_LOCK)


def running():
    handle = acquire_lock()
    if handle is None:
        return True
    handle.close()
    return False


def status_snapshot(db):
    portfolios = {name: get(db, name) for name in PORTFOLIOS}
    initialized = [state for state in portfolios.values() if state is not None]
    initial = sum(state.get('initial_usd', 0) for state in initialized)
    equity = sum(state.get('equity_usd', state.get('cash_usd', 0)) for state in initialized)
    cash = sum(state.get('cash_usd', 0) for state in initialized)
    cutover = get(db, 'policy_cutover')
    epoch = get(db, 'policy_epoch', POLICY_ID if not initialized else None)
    v2_cutover = get(db, 'policy_cutover_v2')
    if epoch == 'bollinger_15m_v2':
        import paper_v2
        v2 = paper_v2.status(db)
        exploration = v2['micro_probe']
        learning_status = {
            'policy': v2['policy_id'],
            'base_portfolios': v2['base_portfolios'],
            'normal_capital': v2['normal_capital'],
            'execution_policy': v2['execution_policy'],
            'shadow_learning': v2['shadow_learning'],
            'challengers': v2['challengers'],
            'v3_test': v2['v3_test'],
            'automatic_retraining': v2['automatic_retraining'],
            'model': v2['model'],
        }
    else:
        exploration = {
            'enabled': get(db, EXPLORATION_STATE_KEY, False),
            'model_key': learning.EXPLORATION_MODEL,
            'label_policy': 'bollinger_lower_zone_protective_or_15m_timeout',
            'selection': 'one_rotating_free_learned_portfolio_in_lower_band_zone_per_closed_15m_bar',
            'training_risk_fraction': EXPLORATION_RISK.risk_fraction,
            'training_allocation_cap': EXPLORATION_RISK.allocation_cap,
            'rejected_context_risk_fraction': EXPLORATION_LOW_RISK.risk_fraction,
            'target_holding_seconds': EXPLORATION_HOLD_SECONDS,
            'label_grace_seconds': EXPLORATION_LABEL_GRACE_SECONDS,
            'max_labeled_holding_seconds': EXPLORATION_SAMPLE_MAX_HOLD_SECONDS,
            'worker_poll_seconds': 30,
        }
        learning_status = learning.status(db)
    return {
        'process_running': running(),
        'stop_requested': get(db, 'stop', False),
        'policy_id': epoch,
        'bar': BAR_LABEL,
        'bar_seconds': BAR_SECONDS,
        'last_success_utc': datetime.fromtimestamp(
            get(db, 'last_success', 0), timezone.utc).isoformat(),
        'last_error': get(db, 'error'),
        'exploration': exploration,
        'policy_cutover': cutover,
        'policy_cutover_v2': v2_cutover,
        'aggregate': {
            'configured_total_initial_usd': TOTAL_PAPER_BUDGET_USD,
            'initialized_total_initial_usd': initial,
            'cash_usd': cash,
            'equity_usd': equity,
            'pnl_usd': equity - initial,
            'return_pct': ((equity / initial - 1) * 100) if initial else None,
            'bb15_period_pnl_usd': (equity-cutover['equity_usd']) if cutover else None,
            'bb15_period_return_pct': (((equity/cutover['equity_usd'])-1)*100
                                       if cutover and cutover['equity_usd'] else None),
            'v2_period_pnl_usd': (equity-v2_cutover['equity_usd']) if v2_cutover else None,
            'v2_period_return_pct': (((equity/v2_cutover['equity_usd'])-1)*100
                                     if v2_cutover and v2_cutover['equity_usd'] else None),
            'completed_trades': sum(state.get('completed_trades', 0) for state in initialized),
            'open_positions': sum(state.get('position') is not None for state in initialized),
        },
        'learning': learning_status,
        'portfolios': portfolios,
    }


def worker():
    lock = acquire_lock()
    if lock is None:
        return
    db = connect()
    from agent import fetch
    try:
        epoch = get(db, 'policy_epoch')
        with db:
            put(db, 'pid', os.getpid())
            put(db, 'started', time.time())
            quarantined = ([] if epoch == 'bollinger_15m_v2' else
                           learning.quarantine_overlong_exploration_samples(
                               db, EXPLORATION_SAMPLE_MAX_HOLD_SECONDS, time.time()))
            if quarantined:
                event(db, time.time(), 'system', 'learning_samples_quarantined', {
                    'reason': 'exploration_label_horizon_exceeded',
                    'count': len(quarantined), 'sample_ids': quarantined,
                    'max_holding_seconds': EXPLORATION_SAMPLE_MAX_HOLD_SECONDS})
        if quarantined:
            learning.refresh(db, force=True)
        while not get(db, 'stop', False):
            begun = time.time()
            with db:
                put(db, 'heartbeat', begun)
            try:
                # A candle failure must not prevent current-quote protective exits.
                try:
                    fetch_count = 200
                    if epoch == 'bollinger_15m_v2':
                        import v2_store
                        fetch_count = max(
                            1000,
                            v2_store.required_fetch_count(db, int(time.time() * 1000)),
                        )
                    rows = fetch(fetch_count)
                    candle_error = None
                except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                    rows, candle_error = None, str(exc)
                q = quote()
                if epoch == 'bollinger_15m_v2':
                    import paper_v2
                    paper_v2.tick(db, q, rows, time.time(), DEFAULT_BUDGETS)
                else:
                    tick(db, q, rows, time.time())
                    learning.refresh(db)
                with db:
                    put(db, 'error', candle_error)
            except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                with db:
                    put(db, 'error', str(exc))
            # Short waits let stop be acknowledged promptly.
            while time.time()-begun < 30 and not get(db, 'stop', False):
                time.sleep(1)
    finally:
        with db:
            put(db, 'stopped', time.time())
        db.close()
        lock.close()


def control(action):
    db = connect()
    try:
        if action == 'start':
            start_guard = acquire_control_lock()
            if start_guard is None:
                raise ValueError('Başlatma veya politika geçişi zaten devam ediyor.')
            try:
                if running():
                    print('Sanal agent zaten çalışıyor.')
                    return
                epoch = get(db, 'policy_epoch')
                if epoch is None:
                    raise ValueError(
                        'Sanal işlem politikası başlatılmadı; '
                        'önce migrate-bollinger-v2 komutunu çalıştırın.')
                if epoch not in (POLICY_ID, 'bollinger_15m_v2'):
                    raise ValueError('Portföy politikası tanınmıyor; uygun geçiş komutunu çalıştırın.')
                with db:
                    put(db, 'stop', False)
                log = ROOT / 'state/worker.log'
                try:
                    with log.open('ab') as stream:
                        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'run'],
                                         cwd=ROOT, stdin=subprocess.DEVNULL,
                                         stdout=stream, stderr=stream,
                                         creationflags=(subprocess.CREATE_NO_WINDOW
                                                        if os.name == 'nt' else 0),
                                         start_new_session=os.name != 'nt')
                except OSError:
                    with db:
                        put(db, 'stop', True)
                    raise
            finally:
                start_guard.close()
            for _ in range(20):
                if running():
                    mode = ('Quant Remora mikro sanal testi ve legacy keşif açık.'
                            if get(db, EXPLORATION_STATE_KEY, False)
                            else 'Quant Remora mikro sanal testi; legacy keşif kapalı.')
                    policy_text = ('v2 shadow/model kapısı' if epoch == 'bollinger_15m_v2'
                                   else 'v1 sabit stratejiler')
                    print('Sanal agent başladı: ' + policy_text + '. ' + mode)
                    return
                time.sleep(.25)
            with db:
                put(db, 'stop', True)
            raise ValueError('Başlangıç doğrulanamadı; state/worker.log dosyasını kontrol edin.')
        elif action == 'stop':
            with db:
                put(db, 'stop', True)
            print('Durdurma istendi. Sanal pozisyonlar korunur; yeniden başlatınca güncel fiyatla devam edilir.')
        elif action == 'status':
            print(json.dumps(status_snapshot(db), indent=2, ensure_ascii=False))
    finally:
        db.close()


def migrate_to_bollinger(path=DB):
    """Atomically cut an existing 1H paper session over to the BB15 policy."""
    control_lock = acquire_control_lock()
    if control_lock is None:
        raise ValueError('Başlatma veya başka bir politika geçişi devam ediyor.')
    migration_lock = acquire_lock()
    if migration_lock is None:
        control_lock.close()
        raise ValueError('Geçiş için önce paper-stop ile workerı durdurun.')
    db = None
    try:
        db = connect(path)
        source_epoch = get(db, 'policy_epoch')
        if source_epoch == 'bollinger_15m_v2':
            raise ValueError(
                'Aktif Bollinger v2 oturumu v1 politikasına geri döndürülemez.')
        if source_epoch == POLICY_ID:
            print('Bollinger 15 dakika geçişi daha önce tamamlanmış.')
            return
        if source_epoch not in (None, 'legacy_1h_v3'):
            raise ValueError(
                'Kaynak portföy politikası tanınmıyor; v1 geçişi reddedildi.')
        existing = {name:get(db, name) for name in PORTFOLIOS}
        if not any(existing.values()):
            with db:
                put(db, 'policy_epoch', POLICY_ID)
            print('Yeni oturum Bollinger 15 dakika politikası için hazırlandı.')
            return
        q = quote()
        now = time.time()
        check_quote(q, now)
        risk = Risk()
        with db:
            put(db, 'archive_hourly_state_before_bb15', {
                'archived_at': now, 'policy_id': 'legacy_1h_v3',
                'portfolios': existing})
            for name, state in existing.items():
                if state is None:
                    continue
                p = state.get('position')
                if p:
                    price = q['bid']*(1-risk.slippage)
                    proceeds = p['quantity']*price*(1-risk.fee)
                    pnl = proceeds-p['cost']
                    state['cash_usd'] += proceeds
                    state['realized_pnl_usd'] += pnl
                    state['completed_trades'] += 1
                    state['position'] = None
                    event(db, now, name, 'sell', {
                        'price': price, 'quantity': p['quantity'], 'pnl_usd': pnl,
                        'reason': 'policy_migration_censored', 'entry_ts': p['ts'],
                        'entry_mode': p.get('entry_mode'),
                        'learning_sample_recorded': False})
                    event(db, now, name, 'learning_sample_skipped', {
                        'reason': 'policy_migration_censored',
                        'old_learning_policy': p.get('learning_policy'),
                        'holding_seconds': now-p['ts']})
                state['last_candle'] = None
                state['policy_id'] = POLICY_ID
                state['bar_seconds'] = BAR_SECONDS
                state['equity_usd'] = state['cash_usd']
                state['peak_equity_usd'] = max(
                    state.get('peak_equity_usd', state['equity_usd']), state['equity_usd'])
                state['drawdown_pct'] = (1-state['equity_usd']/state['peak_equity_usd'])*100
                state['policy_cutover_equity_usd'] = state['equity_usd']
                put(db, name, state)
            cutover_equity = sum((get(db, name) or {}).get('equity_usd', 0)
                                 for name in PORTFOLIOS)
            cutover = {'ts': now, 'from_policy': 'legacy_1h_v3',
                       'to_policy': POLICY_ID, 'equity_usd': cutover_equity,
                       'original_initial_usd': TOTAL_PAPER_BUDGET_USD,
                       'quote': q}
            put(db, 'policy_cutover', cutover)
            put(db, 'policy_epoch', POLICY_ID)
            put(db, 'last_quote', q)
            put(db, 'error', None)
            event(db, now, 'system', 'policy_migration', cutover)
        print(f'Bollinger 15 dakika geçişi tamamlandı. Devreden sanal bakiye: {cutover_equity:.6f} USD.')
    finally:
        if db is not None:
            db.close()
        migration_lock.close()
        control_lock.close()


def configure_exploration(enabled):
    db = connect()
    try:
        with db:
            put(db, EXPLORATION_STATE_KEY, bool(enabled))
        if enabled:
            print('Küçük Bollinger keşif işlemleri açıldı. İlk uygun işlem bir sonraki taze kapanmış 15 dakikalık mumda değerlendirilir.')
        else:
            print('Yeni keşif işlemleri kapatıldı. Açık sanal pozisyonlar kendi çıkış kurallarıyla kapanır.')
    finally:
        db.close()


if __name__ == '__main__':
    worker() if sys.argv[1:] == ['run'] else control(sys.argv[1])
