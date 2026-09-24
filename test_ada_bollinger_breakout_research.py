import numpy as np

from ada_bollinger_breakout_research import breakout_signals, research


def test_breakout_requires_closed_price_above_prior_band():
    close = np.asarray([1.0 + (0.01 if i % 2 else -0.01) for i in range(22)])
    close[20] = 1.04
    prices = {key: close.copy() for key in ('open', 'high', 'low', 'close')}
    entries, exits = breakout_signals(prices)
    assert entries['upper_close'][20]
    assert entries['first_upper_close'][20]
    assert not entries['upper_close'][:20].any()
    assert len(exits['middle_close']) == len(close)


def test_breakout_research_cannot_enable_live():
    report = research()
    assert report['independent_forward_evidence'] is False
    assert report['live_authorized'] is False
    assert len(report['variants']) == 8
