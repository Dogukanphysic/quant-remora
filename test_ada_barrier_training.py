import unittest
import numpy as np
from ada_barrier_training import outcome, train_before


class BarrierTests(unittest.TestCase):
    def test_both_touched_stop_first_and_next_open(self):
        rows = [dict(open=80,high=90,low=70,close=85),
                dict(open=100,high=105,low=97,close=101)]
        gross, end, reason = outcome(rows,0,1,1)
        self.assertAlmostEqual(gross,-.015)
        self.assertEqual((end,reason),(1,'stop'))

    def test_gap_stop_and_timeout(self):
        rows = [dict(open=100,high=100,low=100,close=100),
                dict(open=100,high=100.5,low=99.5,close=100),
                dict(open=95,high=101,low=94,close=100)]
        self.assertEqual(outcome(rows,0,2,1)[2],'stop')
        self.assertAlmostEqual(outcome(rows,0,2,1)[0],-.05)
        self.assertEqual(outcome(rows,0,1,1)[2],'timeout')

    def test_future_labels_do_not_enter_training(self):
        x = {i:[float(i),1.] for i in range(20)}
        labels = {i:(i*.001,i+4,'target') for i in range(20)}
        first, last = train_before([],x,labels,15)
        self.assertLess(last,15)
        for i in labels:
            if labels[i][1] >= 15:
                labels[i]=(-.9,labels[i][1],'stop')
        second, _ = train_before([],x,labels,15)
        self.assertEqual(first,second)


if __name__ == '__main__': unittest.main()
