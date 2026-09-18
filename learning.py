"""Small local supervised trade filter. No dependencies, no changes to risk limits."""
import hashlib
import json
import math
import statistics

FEATURES = ['rsi14', 'atr_fraction', 'bollinger_percent_b', 'bollinger_bandwidth',
            'return_15m', 'return_1h', 'relative_volume', 'candle_body_fraction']
REMORA_FEATURES = FEATURES + [
    'ema20_1h_distance', 'ema50_1h_distance', 'ema200_1h_distance',
    'ema20_1h_slope', 'ema50_1h_slope', 'ema200_1h_slope',
    'atr_percentile', 'stoch_rsi', 'previous_stoch_rsi', 'vwap_distance_atr',
    'trend_bullish', 'trend_bearish', 'regime_trending', 'volatility_extreme',
]
MIN_SAMPLES = 200
MIN_FORWARD = 60
THRESHOLD = .55
MODEL_SCHEMA = 5
EXPLORATION_MODEL = 'bb15_exploration_v1'
REMORA_MODEL = 'quant_remora_v5_forward'
REMORA_SOURCE = 'paper_remora_v3'
REMORA_SHADOW_SOURCE = 'paper_remora_shadow_h8'
REMORA_PROBE_SOURCE_LEGACY = 'paper_remora_probe_h8'
REMORA_PROBE_SOURCE_V2 = 'paper_remora_probe_h8_v2'
REMORA_PROBE_SOURCE = REMORA_PROBE_SOURCE_V2
REMORA_HISTORICAL_SOURCE_LEGACY = 'historical_remora_h8'
REMORA_HISTORICAL_SOURCE_V2 = 'historical_remora_h8_v2'
REMORA_HISTORICAL_SOURCE = REMORA_HISTORICAL_SOURCE_V2
REMORA_EVIDENCE_HORIZON_SECONDS = 8 * 15 * 60
REMORA_FORWARD_VALIDATION_COUNT = 30
FORWARD_SOURCES = frozenset({
    'paper_bb15', 'paper_exploration_bb15', REMORA_SOURCE,
    REMORA_SHADOW_SOURCE, REMORA_PROBE_SOURCE_LEGACY, REMORA_PROBE_SOURCE_V2,
})
EXECUTED_FORWARD_SOURCES = frozenset({
    'paper_bb15', 'paper_exploration_bb15', REMORA_SOURCE,
    REMORA_PROBE_SOURCE_LEGACY, REMORA_PROBE_SOURCE_V2,
})
EXTRA_COST_BUFFER = .001  # Additional 0.10% round-trip stress, beyond costs in labels.
MIN_NET_EDGE = .0005  # 0.05% of position cost; no forced dollar target.


def vector(rows, f, index):
    j = index-1
    if j < 50:
        raise ValueError('Model girdisi için en az 51 kapanmış mum gerekli.')
    r = rows[j]
    volume = sum(x['volume'] for x in rows[j-20:j])/20
    width = max(f['bb_upper'][j]-f['bb_lower'][j], 1e-12)
    return [f['rsi'][j]/100, f['atr'][j]/r['close'],
            (r['close']-f['bb_lower'][j])/width,
            width/f['bb_middle'][j],
            r['close']/rows[j-1]['close']-1,
            r['close']/rows[j-4]['close']-1,
            r['volume']/volume if volume > 0 else 0,
            (r['close']-r['open'])/r['open']]


def sigmoid(x):
    return 1/(1+math.exp(-max(-35, min(35, x))))


def fit(samples, feature_names=FEATURES):
    xs = [s['x'] for s in samples]
    if not xs or any(len(x) != len(feature_names) or not all(math.isfinite(v) for v in x) for x in xs):
        raise ValueError('Geçersiz model girdileri.')
    mean = [statistics.mean(x[j] for x in xs) for j in range(len(feature_names))]
    scale = [max(statistics.pstdev(x[j] for x in xs), 1e-8) for j in range(len(feature_names))]
    z = [[1]+[max(-8,min(8,(x[j]-mean[j])/scale[j])) for j in range(len(feature_names))] for x in xs]
    weights = [0.]*(len(feature_names)+1)
    for _ in range(250):
        gradient = [0.]*len(weights)
        for row, sample in zip(z, samples):
            error = sigmoid(sum(a*b for a,b in zip(weights,row)))-int(sample['net_return'] > 0)
            for j, value in enumerate(row):
                gradient[j] += error*value
        for j in range(len(weights)):
            weights[j] -= .08*(gradient[j]/len(samples)+(.02*weights[j] if j else 0))
    wins = [s['net_return'] for s in samples if s['net_return']>0]
    losses = [-s['net_return'] for s in samples if s['net_return']<=0]
    outcomes = [s['net_return'] for s in samples]
    return {'schema':MODEL_SCHEMA, 'features': list(feature_names), 'mean': mean, 'scale': scale, 'weights': weights,
            'mean_win':statistics.mean(wins) if wins else 0,
            'mean_loss':statistics.mean(losses) if losses else 0,
            'tail_loss':statistics.mean(sorted(losses,reverse=True)[:max(1,math.ceil(len(losses)*.1))]) if losses else 0,
            'uncertainty_buffer':statistics.pstdev(outcomes)/math.sqrt(len(outcomes)),
            'train_count': len(samples), 'threshold': THRESHOLD,
            'base_win_rate': sum(s['net_return']>0 for s in samples)/len(samples)}


def predict(model, x):
    z = [1]+[max(-8,min(8,(v-m)/s)) for v,m,s in zip(x, model['mean'], model['scale'])]
    return sigmoid(sum(a*b for a,b in zip(z,model['weights'])))


def assess(model, x, spread=0.):
    if model is None or model.get('schema') != MODEL_SCHEMA:
        return {'accept':False, 'reason':'model_unavailable', 'score':None}
    if not math.isfinite(spread) or spread<0:
        raise ValueError('Geçersiz spread.')
    p = predict(model,x)
    expectancy = p*model['mean_win']-(1-p)*model['mean_loss']
    conservative = expectancy-model['uncertainty_buffer']-EXTRA_COST_BUFFER-spread
    return {'score':p, 'expected_net_return':expectancy,
            'conservative_net_edge':conservative, 'spread_allowance':spread,
            'mean_win':model['mean_win'], 'mean_loss':model['mean_loss'],
            'tail_loss':model['tail_loss'],
            'accept':p>=THRESHOLD and conservative>=MIN_NET_EDGE,
            'reason':'accepted' if p>=THRESHOLD and conservative>=MIN_NET_EDGE else 'insufficient_net_edge'}


def is_forward(sample):
    return sample.get('source') in FORWARD_SOURCES


def _evidence_exit(sample, evidence_horizon_seconds=None):
    if evidence_horizon_seconds is None:
        return sample['exit_ts']
    return sample['entry_ts'] + evidence_horizon_seconds


def _non_overlapping_samples(samples, evidence_horizon_seconds=None):
    """Return a maximum chronological set of globally non-overlapping labels.

    The accelerated Remora stream can resolve one H8 paper outcome per 15-minute
    bar.  Those observations are useful training rows, but eight adjacent H8
    labels share most of the same future path.  Promotion evidence therefore
    counts a greedy, globally non-overlapping subset instead of pretending every
    overlapping row is an independent forward result.
    """
    eligible = sorted(
        samples,
        key=lambda sample: (
            _evidence_exit(sample, evidence_horizon_seconds),
            sample['entry_ts'],
        ),
    )
    selected = []
    last_exit = None
    for sample in eligible:
        entry = sample['entry_ts']
        exit_ts = _evidence_exit(sample, evidence_horizon_seconds)
        if last_exit is not None and entry < last_exit:
            continue
        selected.append(sample)
        last_exit = exit_ts
    return selected


def _non_overlapping_count(samples, evidence_horizon_seconds=None):
    return len(_non_overlapping_samples(samples, evidence_horizon_seconds))


def _latest_non_overlapping_samples(samples, evidence_horizon_seconds=None):
    """Return a non-overlapping sequence anchored on the newest evidence."""
    selected = []
    next_entry = None
    for sample in sorted(
            samples,
            key=lambda item: (
                item['entry_ts'],
                _evidence_exit(item, evidence_horizon_seconds),
            ),
            reverse=True):
        exit_ts = _evidence_exit(sample, evidence_horizon_seconds)
        if next_entry is not None and exit_ts > next_entry:
            continue
        selected.append(sample)
        next_entry = sample['entry_ts']
    selected.reverse()
    return selected


def _validation_report(model, samples):
    """Apply one validation contract to any chronological holdout subset."""
    scores = [predict(model, sample['x']) for sample in samples]
    accepted = [sample for sample in samples if assess(model, sample['x'])['accept']]
    probability_only = [
        sample for sample, score in zip(samples, scores) if score >= THRESHOLD
    ]
    base = sum(sample['net_return'] for sample in samples)
    filtered = sum(sample['net_return'] for sample in accepted)
    stressed = [sample['net_return'] - EXTRA_COST_BUFFER for sample in accepted]
    stressed_mean = statistics.mean(stressed) if stressed else 0
    stability_buffer = (
        statistics.pstdev(stressed) / math.sqrt(len(stressed)) if stressed else 0
    )
    gross_win = sum(max(value, 0) for value in stressed)
    gross_loss = -sum(min(value, 0) for value in stressed)
    chunks = [samples[:len(samples)//2], samples[len(samples)//2:]]
    segment_net = [
        sum(
            sample['net_return'] - EXTRA_COST_BUFFER
            for sample in chunk if assess(model, sample['x'])['accept']
        )
        for chunk in chunks
    ]
    brier = (
        sum(
            (score - int(sample['net_return'] > 0)) ** 2
            for score, sample in zip(scores, samples)
        ) / len(samples)
        if samples else None
    )
    baseline_brier = (
        sum(
            (model['base_win_rate'] - int(sample['net_return'] > 0)) ** 2
            for sample in samples
        ) / len(samples)
        if samples else None
    )
    quality_pass = bool(
        len(accepted) >= 10
        and len(samples) - len(accepted) >= 5
        and sum(stressed) > max(0, base)
        and brier is not None
        and baseline_brier is not None
        and brier < baseline_brier
        and stressed_mean > stability_buffer
        and all(value > 0 for value in segment_net)
    )
    return {
        'count': len(samples),
        'accepted': len(accepted),
        'probability_only_accepted': len(probability_only),
        'probability_only_sum_trade_returns': sum(
            sample['net_return'] for sample in probability_only),
        'stressed_sum_trade_returns': sum(stressed),
        'stressed_mean_trade_return': stressed_mean,
        'stability_buffer': stability_buffer,
        'stressed_profit_factor': gross_win / gross_loss if gross_loss else None,
        'chronological_half_net_returns': segment_net,
        'min_net_edge': MIN_NET_EDGE,
        'extra_cost_buffer': EXTRA_COST_BUFFER,
        'baseline_sum_trade_returns': base,
        'filtered_sum_trade_returns': filtered,
        'brier': brier,
        'baseline_brier': baseline_brier,
        'quality_pass': quality_pass,
        'metric_note': (
            'Sum of per-trade net returns, NOT portfolio return. Baseline trades '
            'replayed; skipped-trade reentry effects excluded.'
        ),
    }


def train_candidate(samples, feature_names=FEATURES, *,
                    non_overlapping_validation=False,
                    evidence_horizon_seconds=None,
                    require_forward_quality=False):
    # Purge trades whose entry precedes the last training exit, even across strategies.
    if (evidence_horizon_seconds is not None
            and (isinstance(evidence_horizon_seconds, bool)
                 or not isinstance(evidence_horizon_seconds, (int, float))
                 or not math.isfinite(evidence_horizon_seconds)
                 or evidence_horizon_seconds <= 0)):
        raise ValueError('Evidence horizon must be positive and finite.')
    if require_forward_quality and evidence_horizon_seconds is None:
        raise ValueError('Forward quality requires a fixed evidence horizon.')
    samples = sorted(
        samples,
        key=lambda sample: (
            _evidence_exit(sample, evidence_horizon_seconds),
            sample['entry_ts'],
        ),
    )
    forward_samples = [s for s in samples if is_forward(s)]
    executed_forward_samples = [
        s for s in samples if s.get('source') in EXECUTED_FORWARD_SOURCES
    ]
    forward = len(forward_samples)
    executed_forward = len(executed_forward_samples)
    effective_forward = _non_overlapping_count(
        forward_samples, evidence_horizon_seconds)
    effective_executed_forward = _non_overlapping_count(
        executed_forward_samples, evidence_horizon_seconds)
    result = {'schema':MODEL_SCHEMA, 'status':'collecting', 'sample_count':len(samples), 'forward_count':forward,
              'executed_forward_count':executed_forward,
              'effective_forward_count':effective_forward,
              'effective_executed_forward_count':effective_executed_forward,
              'minimum_samples':MIN_SAMPLES, 'minimum_forward_samples':MIN_FORWARD,
              'threshold':THRESHOLD, 'model':None, 'eligible':False,
              'evidence_horizon_seconds': evidence_horizon_seconds,
              'forward_quality_pass': False,
              'forward_validation': {'count': 0, 'quality_pass': False}}
    if len(samples) < MIN_SAMPLES:
        return result
    rolling_forward = []
    if require_forward_quality:
        rolling_forward = _latest_non_overlapping_samples(
            executed_forward_samples, evidence_horizon_seconds)
    if len(rolling_forward) >= REMORA_FORWARD_VALIDATION_COUNT:
        raw_valid = rolling_forward[-REMORA_FORWARD_VALIDATION_COUNT:]
        holdout_start = min(sample['entry_ts'] for sample in raw_valid)
        train = [
            sample for sample in samples
            if _evidence_exit(sample, evidence_horizon_seconds) <= holdout_start
        ]
        valid = raw_valid
        split_mode = 'latest_executed_forward_holdout'
    else:
        split = int(len(samples)*.7)
        train = samples[:split]
        split_boundary = max(
            _evidence_exit(sample, evidence_horizon_seconds) for sample in train)
        raw_valid = [
            sample for sample in samples[split:]
            if sample['entry_ts'] >= split_boundary
        ]
        valid = raw_valid
        if non_overlapping_validation:
            valid = _non_overlapping_samples(valid, evidence_horizon_seconds)
        split_mode = 'chronological_70_30'
    boundary = (
        max(_evidence_exit(sample, evidence_horizon_seconds) for sample in train)
        if train else None
    )
    result.update({
        'split_mode': split_mode,
        'training_count': len(train),
        'training_forward_count': sum(is_forward(sample) for sample in train),
        'training_executed_forward_count': sum(
            sample.get('source') in EXECUTED_FORWARD_SOURCES for sample in train),
    })
    pending_forward_validation = _non_overlapping_samples(
        [
            sample for sample in raw_valid
            if sample.get('source') in EXECUTED_FORWARD_SOURCES
        ],
        evidence_horizon_seconds,
    )
    result['forward_validation'] = {
        'count': len(pending_forward_validation),
        'quality_pass': False,
    }
    if len(valid)<30 or len({s['net_return']>0 for s in train})<2:
        result['status'] = 'insufficient_validation'
        return result
    model = fit(train, feature_names=feature_names)
    validation = _validation_report(model, valid)
    validation.update({
        'train_last_exit_ts': boundary,
        'validation_first_entry_ts': min(sample['entry_ts'] for sample in valid),
        'split_mode': split_mode,
    })
    passes = validation['quality_pass']
    result.update(status='shadow', model=model, validation=validation)
    validation_forward = [s for s in valid if is_forward(s)]
    result['validation']['forward_count'] = len(validation_forward)
    result['validation']['effective_forward_count'] = _non_overlapping_count(
        validation_forward, evidence_horizon_seconds)
    forward_validation = pending_forward_validation
    forward_report = _validation_report(model, forward_validation)
    forward_report.update({
        'first_entry_ts': (
            min(sample['entry_ts'] for sample in forward_validation)
            if forward_validation else None
        ),
        'last_evidence_exit_ts': (
            max(
                _evidence_exit(sample, evidence_horizon_seconds)
                for sample in forward_validation
            )
            if forward_validation else None
        ),
    })
    result['forward_validation'] = forward_report
    result['forward_quality_pass'] = forward_report['quality_pass']
    result['validation']['effective_executed_forward_count'] = len(
        forward_validation)
    result['eligible'] = (
        passes
        and effective_forward >= MIN_FORWARD
        and result['validation']['effective_forward_count'] >= 20
        and effective_executed_forward >= MIN_FORWARD
        and (
            not require_forward_quality
            or (
                len(forward_validation) >= 20
                and result['forward_quality_pass']
            )
        )
    )
    if result['eligible']:
        result['status'] = 'paper_eligible'
    result['version'] = hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest()[:12]
    return result


def ensure_tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS learning_samples (id TEXT PRIMARY KEY, strategy TEXT, source TEXT, entry_ts REAL, exit_ts REAL, detail TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS learning_models (strategy TEXT PRIMARY KEY, sample_count INTEGER, detail TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS learning_versions (strategy TEXT, version TEXT, detail TEXT, PRIMARY KEY(strategy,version))')
    db.execute('CREATE TABLE IF NOT EXISTS learning_quarantine (id TEXT PRIMARY KEY, quarantined_ts REAL, reason TEXT, detail TEXT)')


def quarantine_overlong_exploration_samples(db, max_holding_seconds, quarantined_ts):
    """Move invalid fixed-horizon labels out of every training query, preserving them."""
    if not math.isfinite(max_holding_seconds) or max_holding_seconds <= 0:
        raise ValueError('Geçersiz keşif etiketi süre sınırı.')
    moved = []
    rows = db.execute(
        'SELECT id,strategy,source,entry_ts,exit_ts,detail FROM learning_samples '
        'WHERE strategy=? AND source=?',
        (EXPLORATION_MODEL, 'paper_exploration_bb15')).fetchall()
    for sample_id, strategy, source, entry_ts, exit_ts, detail in rows:
        sample = json.loads(detail)
        duration = exit_ts-entry_ts
        recorded = sample.get('metadata', {}).get('holding_seconds')
        candidates = [value for value in (duration, recorded)
                      if (not isinstance(value, bool)
                          and isinstance(value, (int, float))
                          and math.isfinite(value))]
        holding = max(candidates) if candidates else float('nan')
        if math.isfinite(holding) and holding <= max_holding_seconds:
            continue
        observed_holding = holding if math.isfinite(holding) else None
        archived = {'strategy': strategy, 'source': source,
                    'entry_ts': entry_ts, 'exit_ts': exit_ts,
                    'sample': sample, 'observed_holding_seconds': observed_holding,
                    'max_holding_seconds': max_holding_seconds}
        db.execute('INSERT OR IGNORE INTO learning_quarantine VALUES (?,?,?,?)',
                   (sample_id, quarantined_ts,
                    'exploration_label_horizon_exceeded',
                    json.dumps(archived, allow_nan=False)))
        db.execute('DELETE FROM learning_samples WHERE id=?', (sample_id,))
        moved.append(sample_id)
    return moved


def add_sample(db, strategy, source, entry, exit, x, net_return, metadata=None):
    sample = {'source':source,'entry_ts':entry,'exit_ts':exit,'x':x,'net_return':net_return}
    if metadata:
        sample['metadata'] = metadata
    if not exit>=entry or not math.isfinite(net_return):
        raise ValueError('Geçersiz eğitim sonucu.')
    key = f'{source}:{strategy}:{entry}'
    db.execute('INSERT OR IGNORE INTO learning_samples VALUES (?,?,?,?,?,?)',
               (key,strategy,source,entry,exit,json.dumps(sample)))


def model_state(db, strategy):
    row = db.execute('SELECT detail FROM learning_models WHERE strategy=?',(strategy,)).fetchone()
    state = (json.loads(row[0]) if row else
             {'status':'collecting','model':None,'eligible':False,
              'sample_count':0,'forward_count':0})
    if 'executed_forward_count' not in state:
        details = db.execute(
            'SELECT detail FROM learning_samples WHERE strategy=?', (strategy,)
        ).fetchall()
        state['executed_forward_count'] = sum(
            json.loads(detail[0]).get('source') in EXECUTED_FORWARD_SOURCES
            for detail in details
        )
    return state


def refresh(db, force=False):
    from strategies import MODEL_KEYS
    with db:
        for name in tuple(MODEL_KEYS.values()) + (EXPLORATION_MODEL, REMORA_MODEL):
            samples = [json.loads(r[0]) for r in db.execute('SELECT detail FROM learning_samples WHERE strategy=? ORDER BY exit_ts',(name,))]
            old = model_state(db,name)
            if not force and old.get('schema') == MODEL_SCHEMA:
                previous_count = old.get('sample_count', 0)
                interval = 1 if name in (EXPLORATION_MODEL, REMORA_MODEL) else 20
                if len(samples) == previous_count or len(samples) < previous_count + interval:
                    continue
            feature_names = REMORA_FEATURES if name == REMORA_MODEL else FEATURES
            result = train_candidate(
                samples,
                feature_names=feature_names,
                non_overlapping_validation=(name == REMORA_MODEL),
                evidence_horizon_seconds=(
                    REMORA_EVIDENCE_HORIZON_SECONDS
                    if name == REMORA_MODEL else None
                ),
                require_forward_quality=(name == REMORA_MODEL),
            )
            if name == EXPLORATION_MODEL:
                result['label_policy'] = 'bollinger_lower_zone_protective_or_15m_timeout'
            elif name == REMORA_MODEL:
                result['label_policy'] = (
                    'remora_h8_historical_seed_plus_pre_registered_forward_paper_probes')
            else:
                result['label_policy'] = 'bollinger_strategy_stop_target_or_signal_exit_15m'
            # Each eligible version controls only its separate paper portfolio.
            # A failed later evaluation puts new entries back into shadow mode.
            encoded = json.dumps(result,allow_nan=False)
            db.execute('INSERT OR REPLACE INTO learning_models VALUES (?,?,?)',(name,len(samples),encoded))
            if 'version' in result:
                db.execute('INSERT OR REPLACE INTO learning_versions VALUES (?,?,?)',(name,result['version'],encoded))


def status(db):
    from strategies import MODEL_KEYS
    result = {}
    for portfolio, model_key in MODEL_KEYS.items():
        state = {k:v for k,v in model_state(db,model_key).items() if k!='model'}
        state['model_key'] = model_key
        result[portfolio] = state
    exploration = {k:v for k,v in model_state(db,EXPLORATION_MODEL).items() if k!='model'}
    exploration['model_key'] = EXPLORATION_MODEL
    result['exploration_15m'] = exploration
    remora = {k:v for k,v in model_state(db,REMORA_MODEL).items() if k!='model'}
    remora['model_key'] = REMORA_MODEL
    result['quant_remora_v5'] = remora
    return result


def seed(db, rows):
    from experiment import simulate
    from strategies import indicators, NAMES, MODEL_KEYS, BAR_SECONDS
    f = indicators(rows)
    indexes = {r['ts']:i for i,r in enumerate(rows)}
    with db:
        for name in NAMES:
            result = simulate(rows,name,100,len(rows))
            for trade in result['trades']:
                # Window-end liquidation is artificial and must not label learning data.
                if trade['exit_reason']=='window_end':
                    continue
                cost = trade['entry_price']*trade['quantity_btc']*1.001
                add_sample(db,MODEL_KEYS[name],'historical_bb15',trade['entry_ts']/1000,
                           trade['exit_ts']/1000+BAR_SECONDS,
                           vector(rows,f,indexes[trade['entry_ts']]),trade['pnl_usd']/cost)
    refresh(db,force=True)
