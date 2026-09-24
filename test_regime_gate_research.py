import numpy as np

from dual_market_strategy_audit import simulate
from regime_gate_research import profitable_window, shadow_equity


def test_shadow_state_and_gate_do_not_change_when_future_candles_arrive():
    n = 100
    close = np.asarray([100 + .1 * i + 3 * np.sin(i * .23) for i in range(n)])
    prices = dict(open=close + .03, close=close)
    buy = np.asarray([i % 11 == 1 for i in range(n)])
    sell = np.asarray([i % 11 == 5 for i in range(n)])
    short = shadow_equity({k: v[:70] for k, v in prices.items()},
                          buy[:70], sell[:70])
    full = shadow_equity(prices, buy, sell)
    np.testing.assert_allclose(short, full[:70])
    np.testing.assert_array_equal(profitable_window(short, 10),
                                  profitable_window(full, 10)[:70])


def test_negative_observed_window_flattens_at_next_open():
    equity = np.asarray([1000., 1020., 990., 1010.])
    gate = profitable_window(equity, 1)
    np.testing.assert_array_equal(gate, [False, True, False, True])
    prices = dict(open=np.asarray([100., 100., 90., 80.]),
                  close=np.asarray([100., 100., 90., 80.]))
    entry = np.asarray([False, True, False, False]) & gate
    leave = ~gate
    result = simulate(prices, entry, leave, 0, 4, fee=0)
    assert result['trades'] == 1
    assert result['return_pct'] < 0  # open 2 entry, open 3 exit
