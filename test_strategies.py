import unittest
from unittest.mock import patch
from strategies import (indicators, signal, Risk, size_position, BAR_SECONDS, BAR_MS,
                        BAR_LABEL, POLICY_ID, MODEL_KEYS)
from experiment import simulate, run


def rows(n=100):
    return [dict(ts=i*BAR_MS, open=100., close=100., high=101., low=99., volume=10.)
            for i in range(n)]


class StrategyTests(unittest.TestCase):
    def test_policy_constants_and_storage_mapping(self):
        self.assertEqual((BAR_SECONDS, BAR_MS, BAR_LABEL, POLICY_ID),
                         (900, 900_000, '15M', 'bollinger_15m_v1'))
        self.assertEqual(MODEL_KEYS, {
            'trend': 'bb15_midtrend_v1',
            'breakout': 'bb15_breakout_v1',
            'reversion': 'bb15_reentry_v1',
        })

    def test_rsi_flat_and_monotonic(self):
        self.assertEqual(indicators(rows())['rsi'][14], 50)
        data = rows()
        for i, r in enumerate(data):
            r.update(open=100+i, close=100+i, high=101+i, low=99+i)
        self.assertEqual(indicators(data)['rsi'][14], 100)
        self.assertEqual(indicators(list(reversed(data)))['rsi'][14], 0)

    def test_atr_wilder_seed_and_update(self):
        data = rows()
        data[14]['high'] = 115
        atr = indicators(data)['atr']
        self.assertEqual(atr[13], 2)
        self.assertAlmostEqual(atr[14], (26+16)/14)

    def test_bollinger_uses_population_standard_deviation(self):
        data = rows(20)
        for i, row in enumerate(data, 1):
            row.update(open=float(i), close=float(i), high=float(i)+1, low=max(.1, i-1.))
        result = indicators(data)
        deviation = (33.25 ** .5)
        self.assertAlmostEqual(result['bb_middle'][19], 10.5)
        self.assertAlmostEqual(result['bb_upper'][19], 10.5 + 2*deviation)
        self.assertAlmostEqual(result['bb_lower'][19], 10.5 - 2*deviation)
        self.assertAlmostEqual(result['percent_b'][19],
                               (20-(10.5-2*deviation))/(4*deviation))
        self.assertAlmostEqual(result['bandwidth'][19], 4*deviation/10.5)

    def test_flat_bollinger_percent_b_is_neutral(self):
        result = indicators(rows(20))
        self.assertEqual(result['percent_b'][19], .5)
        self.assertEqual(result['bandwidth'][19], 0)

    def test_signal_does_not_read_current_or_future(self):
        data = rows()
        before = {k: signal(data, indicators(data), 60, k) for k in ('trend','breakout','reversion')}
        for r in data[60:]:
            r.update(open=1000, close=1000, high=2000, low=1, volume=99999)
        after = {k: signal(data, indicators(data), 60, k) for k in before}
        self.assertEqual(before, after)

    def test_bollinger_signal_rules(self):
        data = rows()
        f = indicators(data)
        cases = {
            'trend': {'previous_close': 99, 'close': 101, 'previous_middle': 100,
                      'middle': 100.5, 'previous_upper': 102, 'upper': 102,
                      'previous_lower': 98, 'lower': 98},
            'breakout': {'previous_close': 100, 'close': 103, 'previous_middle': 100,
                         'middle': 101, 'previous_upper': 102, 'upper': 102.5,
                         'previous_lower': 98, 'lower': 99},
            'reversion': {'previous_close': 98, 'close': 99.5, 'previous_middle': 100,
                          'middle': 100, 'previous_upper': 102, 'upper': 102,
                          'previous_lower': 99, 'lower': 99},
        }
        j = 60
        for name, values in cases.items():
            with self.subTest(strategy=name):
                data[j-1]['close'] = values['previous_close']
                data[j]['close'] = values['close']
                f['bb_middle'][j-1] = values['previous_middle']
                f['bb_middle'][j] = values['middle']
                f['bb_upper'][j-1] = values['previous_upper']
                f['bb_upper'][j] = values['upper']
                f['bb_lower'][j-1] = values['previous_lower']
                f['bb_lower'][j] = values['lower']
                self.assertEqual(signal(data, f, j+1, name), (True, False))

    def test_bollinger_exit_rules(self):
        data = rows()
        f = indicators(data)
        j = 60
        f['bb_middle'][j] = 100
        data[j]['close'] = 99
        self.assertTrue(signal(data, f, j+1, 'trend')[1])
        self.assertTrue(signal(data, f, j+1, 'breakout')[1])
        data[j]['close'] = 100
        self.assertTrue(signal(data, f, j+1, 'reversion')[1])

    def test_risk_budget_and_cap_include_costs(self):
        risk = Risk()
        for atr in (0.01, 2, 20):
            p = size_position(1000, 100, atr, risk)
            loss = p['cost'] - p['quantity']*p['stop']*(1-risk.slippage)*(1-risk.fee)
            self.assertLessEqual(loss, 1+1e-8)
            self.assertLessEqual(p['cost'], 50+1e-8)

    @patch('experiment.signal', return_value=(True, False))
    def test_stop_wins_when_both_levels_touched(self, decision):
        data = rows()
        data[60].update(low=90, high=120)
        result = simulate(data, 'trend', 60, 61)
        self.assertEqual(result['trades'][0]['exit_reason'], 'stop')
        self.assertLess(result['final_equity_usd'], 1000)

    @patch('experiment.signal', return_value=(True, False))
    def test_gap_stop_fills_at_open(self, decision):
        data = rows()
        data[61].update(open=80, close=80, high=81, low=79)
        result = simulate(data, 'trend', 60, 62)
        trade = result['trades'][0]
        self.assertEqual(trade['exit_reason'], 'stop_gap')
        self.assertAlmostEqual(trade['exit_price'], 80*(1-Risk().slippage))

    @patch('experiment.signal', return_value=(True, False))
    def test_zero_volume_no_fill(self, decision):
        data = rows()
        data[60]['volume'] = 0
        result = simulate(data, 'trend', 60, 61)
        self.assertEqual(result['completed_trades'], 0)
        self.assertEqual(result['final_equity_usd'], 1000)

    @patch('experiment.signal', return_value=(True, False))
    def test_final_equity_reconciles_closed_trades(self, decision):
        result = simulate(rows(), 'trend', 60, 65)
        self.assertEqual(result['open_quantity_btc'], 0)
        self.assertAlmostEqual(result['final_equity_usd'],
                               1000+sum(t['pnl_usd'] for t in result['trades']))

    def test_training_selection_ignores_test_prices(self):
        data = rows(1500)
        first = run(data)['folds'][0]
        for i, r in enumerate(data[600:]):
            r.update(open=100+i, close=100+i, high=101+i, low=99+i)
        second = run(data)['folds'][0]
        self.assertEqual(first['training'], second['training'])
        self.assertEqual(first['selected_from_training'], second['selected_from_training'])
        self.assertEqual(first['selected_from_training'], 'cash')


if __name__ == '__main__':
    unittest.main()
