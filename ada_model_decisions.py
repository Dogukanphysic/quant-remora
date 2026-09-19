"""Optional experimental ADA decision reader. No exchange access or activation."""
from contextlib import closing
import hashlib
import json
import math
import sqlite3


def decision(path, bar, now):
    result = dict(owner='ema_atr', model_decisions_requested=True,
                  experimental=True, reason='no_current_prediction')
    close = bar + 900000
    if not 0 <= now * 1000 - close <= 300000:
        result['reason'] = 'outside_decision_window'
        return result
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            row = db.execute('SELECT p.model_id,p.prediction,p.created,m.created,m.last_label,m.value '
                             'FROM predictions p JOIN models m ON m.id=p.model_id WHERE p.ts=?',
                             (bar,)).fetchone()
        if row is None:
            return result
        model = json.loads(row[5])
        digest = model.pop('digest')
        if hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest() != digest:
            raise ValueError('digest_mismatch')
        if model['version'] != 'ridge-next15m-v1' or model['target'] != 'next_15m_close_to_close_net_proxy_not_execution':
            raise ValueError('wrong_model_interval')
        prediction = float(row[1])
        if not all(math.isfinite(float(v)) for v in (prediction,row[2],row[3],row[4],model['train_last_label'])):
            raise ValueError('nonfinite_model_evidence')
        if not (model['train_last_label'] <= row[4] <= close <= row[2]*1000 <= now*1000
                and row[3] <= row[2]):
            raise ValueError('invalid_prediction_timing')
        return dict(owner='learned_model',model_decisions_requested=True,experimental=True,
                    reason='user_opt_in_unproven_model',target_long=prediction > 0,
                    model_id=row[0],model_digest=digest,predicted_net_return=prediction,
                    validation_samples=model.get('validation_samples'),
                    accepted_proxy_samples=model.get('accepted_proxy_samples'),
                    profitability_proven=False)
    except (sqlite3.Error,ValueError,KeyError,TypeError,OSError,OverflowError):
        result['reason'] = 'invalid_or_unavailable_model'
        return result
