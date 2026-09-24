import numpy as np

from ada_bb_execution_audit import atr14, entry_variants, simulate


def test_atr_and_signals_use_only_historical_prefix():
    n = 180
    close = np.asarray([100 + .04*i + 3*np.sin(i*.12) for i in range(n)])
    prices = dict(open=close, high=close+.7, low=close-.7, close=close)
    short = {k:v[:140] for k,v in prices.items()}
    np.testing.assert_allclose(atr14(short), atr14(prices)[:140], equal_nan=True)
    short_entries, short_exit = entry_variants(short)
    full_entries, full_exit = entry_variants(prices)
    for name in short_entries:
        np.testing.assert_array_equal(short_entries[name], full_entries[name][:140])
    np.testing.assert_array_equal(short_exit, full_exit[:140])


def test_entry_uses_next_open_and_same_bar_stop_wins_ambiguous_bar():
    prices = dict(open=np.asarray([100.,100.,100.]),
                  high=np.asarray([100.,105.,100.]),
                  low=np.asarray([100.,97.,100.]),
                  close=np.asarray([100.,100.,100.]))
    result = simulate(prices, np.asarray([True,False,False]),
                      np.asarray([False,False,False]),
                      np.asarray([1.,1.,1.]), 0, 3, fee=.01)
    expected_cash = 1000 * (1-.01) / 100 * 98 * (1-.01)
    assert result['trades'] == 1
    assert result['exits']['stop'] == 1
    assert abs(result['return_pct'] - (expected_cash/1000-1)*100) < 1e-9
