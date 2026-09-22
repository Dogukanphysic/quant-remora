"""Explicit Testnet-only scheduled exploration; never a profitability gate."""
import math


def apply(signal, *, environment, interval, now_ms):
    if environment != 'binance_spot_testnet' or interval != '15m':
        raise ValueError('Exploration is restricted to 15m Spot Testnet')
    close = int(signal['candle_close_ms'])+1
    if close % 900000 or now_ms < close:
        raise ValueError('Exploration requires an aligned closed candle')
    result = dict(signal)
    result['features'] = dict(signal['features'])
    if now_ms-close > 300000:
        result['target_long'] = False
        result['features'].update(decision_owner='testnet_exploration',experimental=True,
                                  exploration_reason='waiting_for_fresh_candle',profitability_proven=False)
        return result
    slot = (close//900000)%4
    prediction = signal['features'].get('predicted_net_return')
    valid = isinstance(prediction,(float,int)) and math.isfinite(prediction)
    # Once each hour take a small experimental position even without model edge.
    # Next slot may hold if a validated model estimate clears a relaxed threshold.
    # Last two slots are always cash, so no prolonged model-driven hold.
    result['target_long'] = slot == 0 or (slot == 1 and valid and prediction > -.0025)
    result['feature_schema'] = 'btc_15m_testnet_scheduled_exploration_v1'
    result['features'].update(decision_owner='testnet_exploration',
        exploration_slot=slot,exploration_entry=slot==0,
        exploration_threshold=-.0025,experimental=True,profitability_proven=False,
        previous_target_long=bool(signal['target_long']),
        exploration_reason='scheduled_probe' if slot==0 else 'model_hold' if result['target_long'] else 'scheduled_cash')
    return result
