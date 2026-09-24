import numpy as np

from ada_bollinger_zone_research import research, zone_signals


def test_zone_is_more_eager_than_exact_touch_without_future_data():
    close = np.asarray([1.0 + (0.01 if i % 2 else -0.01) for i in range(22)])
    prices = {key: close.copy() for key in ('open', 'high', 'low', 'close')}
    prices['low'][20] = 0.982
    exact, _ = zone_signals(prices, 0.0)
    zone, _ = zone_signals(prices, 0.2)
    assert not exact[20]
    assert zone[20]
    assert not zone[:20].any()


def test_research_does_not_authorize_live_execution():
    report = research()
    assert report['independent_forward_evidence'] is False
    assert report['live_authorized'] is False
    assert report['variants']['0.2']['entry_signal_count'] > report['variants']['0']['entry_signal_count']
