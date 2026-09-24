import numpy as np

from genetic_confluence_research import choose, condition_library


def test_each_condition_is_unchanged_when_future_candles_are_appended():
    n = 240
    close = np.asarray([100 + i * .03 + 3 * np.sin(i * .17) for i in range(n)])
    prices = dict(close=close, low=close - .5, high=close + .5)
    short = condition_library({key: value[:180] for key, value in prices.items()})
    full = condition_library(prices)
    for short_group, full_group in zip(short[:3], full[:3]):
        for name in short_group:
            np.testing.assert_array_equal(short_group[name], full_group[name][:180])
    np.testing.assert_array_equal(short[3], full[3][:180])


def test_selection_uses_only_passed_training_folds():
    def fold(return_pct, trades=10, max_drawdown_pct=10):
        return dict(return_pct=return_pct, trades=trades,
                    max_drawdown_pct=max_drawdown_pct)

    train = {
        'steady': [fold(3), fold(2)],
        'one_lucky_fold': [fold(100), fold(-3)],
        'too_few_trades': [fold(50, trades=2), fold(50)],
        'too_much_drawdown': [fold(60, max_drawdown_pct=50), fold(60)],
    }
    assert choose(train) == 'steady'
