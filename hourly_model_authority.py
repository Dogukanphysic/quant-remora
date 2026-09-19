"""User-authorized Testnet-only model overlay; no exchange client or order API."""
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from strategy_research import ROOT


def eligible(model, frame='1h'):
    try:
        values = [float(model[k]) for k in ('validation_mse','constant_mse','accepted_mean_net_return')]
        return (all(math.isfinite(x) for x in values)
                and model['version']==f'ridge-next{frame}-v1'
                and model['validation_samples'] >= 200
                and model['accepted_proxy_samples'] >= 20
                and 0 <= values[0] < values[1] and values[2] > 0)
    except (KeyError,TypeError,ValueError):
        return False


def apply(signal, path=None):
    frame = signal['features'].get('decision_interval','1h')
    if frame not in ('1h','15m'):
        return signal
    path = path or ROOT/f'state/testnet{frame}-learning.sqlite3'
    result = dict(signal)
    result['features'] = dict(signal['features'])
    result['features']['automatic_authority'] = 'testnet_validation_gated_v1'
    result['features']['decision_owner'] = 'momentum_fallback'
    # Prediction must have been registered on this exact decision candle.
    timestamp = int(signal['candle_close_ms'])+1-({'1h':3600000,'15m':900000}[frame])
    try:
        with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro',uri=True)) as db:
            row = db.execute('SELECT p.model_id,p.prediction,m.last_label,m.value FROM predictions p JOIN models m ON m.id=p.model_id WHERE p.ts=?',(timestamp,)).fetchone()
        if not row:
            return result
        model = json.loads(row[3])
        digest = model.pop('digest')
        if hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest() != digest:
            return result
        if row[2] > int(signal['candle_close_ms'])+1 or not eligible(model,frame):
            return result
        prediction = float(row[1])
        if not math.isfinite(prediction):
            return result
        result['target_long'] = prediction > 0
        result['feature_schema'] = 'btc_15m_auto_ridge_v1' if frame == '15m' else 'btc_hourly_auto_ridge_v1'
        result['features'].update(decision_owner='learned_model',model_id=row[0],
                                  model_digest=digest,predicted_net_return=prediction)
    except (sqlite3.Error,ValueError,KeyError,TypeError,OSError):
        pass
    return result
