import numpy as np

from dual_market_strategy_audit import indicators, simulate


def test_order_fills_at_next_open_and_charges_both_sides():
    prices = dict(open=np.asarray([100., 110., 120., 130.]),
                  close=np.asarray([100., 110., 120., 130.]))
    result = simulate(prices, np.asarray([True,False,False,False]),
                      np.asarray([False,False,True,False]),0,4,fee=.01)
    expected = 1000*(1-.01)/110*130*(1-.01)
    assert abs(result['return_pct']-(expected/1000-1)*100) < 1e-9
    assert result['trades'] == 1


def test_indicator_prefix_is_causal():
    close = np.asarray([100+i*.05+2*np.sin(i*.3) for i in range(200)])
    prices = dict(close=close)
    short = indicators(dict(close=close[:130]))
    full = indicators(prices)
    for before, after in zip(short, full):
        np.testing.assert_allclose(before, after[:130], equal_nan=True)
