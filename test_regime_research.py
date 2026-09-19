import unittest
from regime_research import regime_features, learn, filtered_entries
from strategy_research import features


class RegimeTests(unittest.TestCase):
    def test_regime_features_are_prefix_invariant(self):
        rows = [dict(ts=i, open=100+i, high=102+i, low=99+i, close=101+i, volume=1)
                for i in range(400)]
        self.assertEqual(regime_features(rows, features(rows))[:300],
                         regime_features(rows[:300], features(rows[:300])))

    def fixture(self):
        rows = [dict(ts=i) for i in range(400)]
        values = [None]*204 + [('up', .01)]*196
        trades = [dict(entry_ts=i, exit_ts=i+1, pnl=1.) for i in range(210, 234, 2)]
        return rows, values, trades

    def test_training_does_not_use_future_features(self):
        rows, values, trades = self.fixture()
        original = learn(rows, values, trades, 300)
        values[300:] = [('down', 999.)]*100
        self.assertEqual(original, learn(rows, values, trades, 300))
        self.assertEqual(original['allowed'], ['up_low_vol'])

    def test_future_label_rejected(self):
        rows, values, trades = self.fixture()
        trades[0]['exit_ts'] = 300
        with self.assertRaises(ValueError):
            learn(rows, values, trades, 300)

    def test_insufficient_evidence_and_unknown_regime_cannot_enter(self):
        rows, values, trades = self.fixture()
        model = learn(rows, values, trades[:3], 300)
        self.assertEqual(model['allowed'], [])
        self.assertFalse(any(filtered_entries([True]*400, values, model)))
        model['allowed'] = ['up_low_vol']
        self.assertFalse(filtered_entries([True], [None], model)[0])


if __name__ == '__main__':
    unittest.main()
