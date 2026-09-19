import unittest

from strategy_research import features, signals, simulate, gate


class ResearchTests(unittest.TestCase):
    def scenario(self, cost=0, changes=None, exit_signal=False):
        rows = [dict(ts=i*3600000, open=100., high=101., low=99., close=100., volume=1.)
                for i in range(204)]
        for i, change in (changes or {}).items():
            rows[i].update(change)
        entry, exit_ = [False]*204, [False]*204
        entry[201] = True
        exit_[202] = exit_signal
        return simulate(rows, {'atr': [1.]*204}, entry, exit_, 201, 204, cost, 48)

    def test_signal_fills_next_open(self):
        result = self.scenario()
        self.assertEqual(result['trade_records'][0]['entry_ts'], 202*3600000)

    def test_cost_can_erase_small_gain(self):
        changes = {203: {'close': 100.1}}
        self.assertGreater(self.scenario(changes=changes)['pnl_usd'], 0)
        self.assertLess(self.scenario(.0025, changes)['pnl_usd'], 0)

    def test_double_touch_is_stop_first_and_risk_bounded(self):
        result = self.scenario(changes={202: {'low': 97., 'high': 105.}})
        self.assertEqual(result['trade_records'][0]['reason'], 'stop')
        self.assertAlmostEqual(result['pnl_usd'], -2.5)
        self.assertAlmostEqual(result['max_drawdown'], .0025)

    def test_gap_can_exceed_planned_risk(self):
        result = self.scenario(changes={203: {'open': 95., 'low': 94., 'high': 96., 'close': 95.}})
        self.assertLess(result['pnl_usd'], -2.5)

    def test_open_exit_not_exposed_to_later_low(self):
        result = self.scenario(changes={203: {'low': 1.}}, exit_signal=True)
        self.assertEqual(result['pnl_usd'], 0)
        self.assertLess(result['max_drawdown'], .002)

    def test_signals_cannot_change_with_future_bars(self):
        rows = [dict(ts=i*3600000, open=100+i*.05, high=102+i*.05,
                     low=98+i*.05, close=100+i*.05, volume=1.) for i in range(400)]
        for family in ('trend_hysteresis', 'channel_breakout', 'trend_pullback'):
            for variant in ('balanced', 'patient'):
                full = signals(rows, features(rows), family, variant)
                prefix = signals(rows[:300], features(rows[:300]), family, variant)
                self.assertEqual((full[0][:300], full[1][:300]), prefix)

    def test_empty_evidence_cannot_pass(self):
        self.assertFalse(gate(self.scenario()))


if __name__ == '__main__':
    unittest.main()
