from ada_band_diagnostic import STEP_MS, analyze, closed_candles


def test_uses_only_closed_contiguous_candles():
    raw = [[i * STEP_MS, '1', '1.1', '0.9', '1', '0', (i + 1) * STEP_MS - 1]
           for i in range(22)]
    rows = closed_candles(raw, 21 * STEP_MS)
    assert len(rows) == 21
    assert rows[-1]['ts'] == 20 * STEP_MS


def test_counts_touch_after_strategy_transition_without_orders():
    rows = [{'ts': i * STEP_MS, 'high': 1.02 if i % 2 else 0.98,
             'low': 1.02 if i % 2 else 0.98,
             'close': 1.02 if i % 2 else 0.98}
            for i in range(24)]
    rows[21]['low'] = 0.9
    rows[22]['high'] = 1.1
    state = {'strategy_mode': 'bollinger_touch_15m_v1',
             'strategy_changed_at': 21 * STEP_MS / 1000 + 10,
             'phase': 'cash', 'stopped': False, 'halted': None,
             'last_poll': 24 * STEP_MS / 1000 - 60,
             'last_bar': 23 * STEP_MS}
    report = analyze(state, rows, 24 * STEP_MS)
    assert report['eligible_closed_candles_in_window'] == 3
    assert report['lower_touch_count'] == 1
    assert report['upper_touch_count'] == 1
    assert report['orders_submitted'] == 0
