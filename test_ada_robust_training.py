import unittest
import numpy as np
from ada_robust_training import transform, fraction, estimates, portfolio


class RobustTests(unittest.TestCase):
    def test_extremes_clipped_and_raw_preserved(self):
        y = np.array([.001]*100+[-.8,.9])
        raw,_ = transform(y,np.ones(len(y))*.01,'raw')
        self.assertTrue(np.array_equal(raw,y))
        clipped,bounds = transform(y,np.ones(len(y))*.01,'winsor')
        self.assertLess(clipped.max(),.9)
        self.assertGreater(clipped.min(),-.8)
        normalized,_ = transform(y,np.ones(len(y))*.01,'atr_units')
        self.assertEqual(normalized.max(),3)
        self.assertEqual(normalized.min(),-3)

    def test_causal_bounds_and_predictions(self):
        x = {i:[.001]*6+[.01]+[0.]*3 for i in range(1600)}
        labels = {i:(.005 if i%2 else -.003,i+16,'timeout') for i in range(204,1580)}
        before,evidence = estimates(x,labels,1400,1410,'winsor')
        for i in labels:
            if labels[i][1] >= 1400:
                labels[i] = (100.,labels[i][1],'target')
        after,later = estimates(x,labels,1400,1410,'winsor')
        self.assertLess(evidence['last_training_outcome'],1400)
        self.assertEqual(evidence['training_bounds'],later['training_bounds'])
        for i in before:
            self.assertAlmostEqual(before[i],after[i],places=14)

    def test_sizing_and_cash_accounting(self):
        self.assertLessEqual(fraction(.001,.0015,'risk_scaled'),.25)
        self.assertLess(fraction(.1,.0015,'risk_scaled'),fraction(.01,.0015,'risk_scaled'))
        rows = [dict(open=100,low=90,close=110) for _ in range(4)]
        x = {0:[0.]*6+[.01]}
        trades = [dict(index=0,exit_index=1,net_return=.1)]
        full = portfolio(rows,x,trades,.0015,'full')
        scaled = portfolio(rows,x,trades,.0015,'risk_scaled')
        self.assertAlmostEqual(full['net_return'],.1)
        self.assertAlmostEqual(scaled['net_return'],.025)
        self.assertLess(scaled['conservative_drawdown'],full['conservative_drawdown'])
        self.assertEqual(scaled['without_best_trade'],0)
        trades[0]['net_return'] = -.1
        self.assertLess(portfolio(rows,x,trades,.0015,'risk_scaled')['net_return'],0)


if __name__ == '__main__': unittest.main()
