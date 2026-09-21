import unittest
from ada_regime_training import regime, training_indices, estimates, simulate, passed


class RegimeTests(unittest.TestCase):
    def test_regime_direction(self):
        self.assertEqual(regime([0,0,0,0,.01,.02]),'up')
        self.assertEqual(regime([0,0,0,0,-.01,-.02]),'down')
        self.assertEqual(regime([0,0,0,0,.01,-.02]),'range')

    def test_purge_and_future_labels_invariance(self):
        x = {i:[i*.00001,0.,0.,0.,.01,.02] for i in range(1600)}
        labels = {i:(i*.000001,i+16,'timeout') for i in range(204,1580)}
        before,evidence = estimates(x,labels,1400,1500,'ridge')
        trees_before,_ = estimates(x,labels,1400,1500,'regime_trees')
        self.assertLess(evidence['last_training_outcome'],1400)
        for i in labels:
            if labels[i][1] >= 1400:
                labels[i] = (-.99,labels[i][1],'stop')
        after,_ = estimates(x,labels,1400,1500,'ridge')
        self.assertEqual(before,after)
        trees_after,_ = estimates(x,labels,1400,1500,'regime_trees')
        for i in trees_before:
            self.assertAlmostEqual(trees_before[i],trees_after[i],places=14)
        self.assertTrue(all(i>=40000-180*96 for i in training_indices(
            {i:(0,i+16,'timeout') for i in range(200,41000)},40000)))

    def test_boundary_does_not_use_realized_exit_and_no_overlap(self):
        rows = [dict(open=100,high=103,low=99,close=101) for _ in range(30)]
        labels = {i:(.02,i+4,'target') for i in range(20)}
        pred = {i:.03 for i in range(20)}
        result = simulate(rows,labels,pred,4,0,20,.0015)
        self.assertEqual(result['trades'],4)  # decision indices 0,5,10,15
        changed = dict(labels)
        for i in range(16,20):
            changed[i] = (1.,i,'target')
        self.assertEqual(result,simulate(rows,changed,pred,4,0,20,.0015))

    def test_cash_is_not_success(self):
        empty = dict(trades=0,net_return=0.,conservative_drawdown=0.,profit_factor=None)
        self.assertFalse(passed([dict(results=[empty,empty])]*3))
        rows = [dict(open=100,low=99,close=100) for _ in range(30)]
        result = simulate(rows,{},dict.fromkeys(range(20),-.01),4,0,20,.0015)
        self.assertEqual(result['trades'],0)
        self.assertEqual(result['net_return'],0)


if __name__ == '__main__': unittest.main()
