import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from learning import (fit,predict,train_candidate,add_sample,refresh,model_state,
                      vector,assess,is_forward,
                      quarantine_overlong_exploration_samples)
from strategies import indicators, MODEL_KEYS, BAR_MS, BAR_SECONDS
from paper import connect,tick,get


def samples(n=200, source='paper_bb15'):
    return [{'entry_ts':i*10.,'exit_ts':i*10+5.,'source':source,
             'x':[float(i%2)]*8,'net_return':.02 if i%2 else -.01} for i in range(n)]


class LearningTests(unittest.TestCase):
    def test_high_win_probability_can_have_negative_expectancy(self):
        model=fit(samples())
        model.update(mean_win=.001,mean_loss=1.)
        review=assess(model,[1.]*8)
        self.assertGreater(review['score'],.8)
        self.assertLess(review['expected_net_return'],0)
        self.assertFalse(review['accept'])

    def test_spread_can_remove_positive_edge(self):
        model=fit(samples())
        self.assertTrue(assess(model,[1.]*8)['accept'])
        self.assertFalse(assess(model,[1.]*8,spread=.1)['accept'])

    def test_payoff_statistics_do_not_read_validation(self):
        data=samples()
        before=train_candidate(data)['model']
        for s in data[140:]:
            s['net_return']=500
        after=train_candidate(data)['model']
        self.assertEqual(before,after)

    def test_model_learns_from_losses_and_wins(self):
        model=fit(samples())
        self.assertGreater(predict(model,[1.]*8),.8)
        self.assertLess(predict(model,[0.]*8),.2)

    def test_historical_results_cannot_activate_model(self):
        model=train_candidate(samples(source='historical_bb15'))
        self.assertTrue(model['validation']['quality_pass'])
        self.assertFalse(model['eligible'])

    def test_positive_forward_validation_can_qualify(self):
        model=train_candidate(samples())
        self.assertTrue(model['eligible'])
        self.assertGreaterEqual(model['validation']['forward_count'],20)

    def test_exploration_is_forward_but_historical_is_not(self):
        self.assertTrue(is_forward({'source':'paper_bb15'}))
        self.assertTrue(is_forward({'source':'paper_exploration_bb15'}))
        self.assertTrue(is_forward({'source':'paper_remora_probe_h8'}))
        self.assertFalse(is_forward({'source':'paper'}))
        self.assertFalse(is_forward({'source':'historical_bb15'}))
        self.assertFalse(is_forward({'source':'historical_remora_h8'}))
        model=train_candidate(samples(source='paper_exploration_bb15'))
        self.assertEqual(model['forward_count'],200)
        self.assertGreaterEqual(model['validation']['forward_count'],20)

    def test_validation_labels_never_change_fitted_weights(self):
        data=samples()
        before=train_candidate(data)['model']
        for s in data[140:]:
            s['net_return'] *= -1
        after=train_candidate(data)['model']
        self.assertEqual(before,after)

    def test_open_training_trade_purged_from_validation(self):
        data=samples()
        for s in data[140:150]:
            s['entry_ts']=1
        candidate=train_candidate(data)
        self.assertGreater(candidate['validation']['validation_first_entry_ts'],
                           candidate['validation']['train_last_exit_ts'])

    def test_too_few_examples_remain_collecting(self):
        self.assertEqual(train_candidate(samples(50))['status'],'collecting')

    def test_repeated_sample_not_counted_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            db=connect(Path(folder)/'test.db')
            try:
                with db:
                    for _ in range(2):
                        add_sample(db,MODEL_KEYS['trend'],'paper_bb15',10,20,[0.]*8,-.01)
                    add_sample(db,'trend','historical',1,2,[0.]*7,-.01)
                self.assertEqual(db.execute('SELECT count(*) FROM learning_samples').fetchone()[0],2)
                refresh(db)
                self.assertEqual(model_state(db,MODEL_KEYS['trend'])['sample_count'],1)
                self.assertEqual(model_state(db,'trend')['sample_count'],0)
            finally:
                db.close()

    def test_overlong_exploration_sample_is_preserved_outside_training(self):
        with tempfile.TemporaryDirectory() as folder:
            db=connect(Path(folder)/'test.db')
            try:
                with db:
                    add_sample(db, 'bb15_exploration_v1',
                               'paper_exploration_bb15', 10, 5000,
                               [0.]*8, -.01,
                               {'holding_seconds': 4990})
                    moved=quarantine_overlong_exploration_samples(db, 1020, 6000)
                self.assertEqual(len(moved), 1)
                self.assertEqual(db.execute(
                    'SELECT count(*) FROM learning_samples').fetchone()[0], 0)
                row=db.execute(
                    'SELECT reason,detail FROM learning_quarantine').fetchone()
                self.assertEqual(row[0], 'exploration_label_horizon_exceeded')
                import json
                self.assertEqual(json.loads(row[1])['sample']['net_return'], -.01)
                refresh(db, force=True)
                self.assertEqual(model_state(
                    db, 'bb15_exploration_v1')['sample_count'], 0)
            finally:
                db.close()

    @patch('paper.signal',return_value=(True,False))
    def test_paper_trade_result_records_entry_features_only(self,decision):
        with tempfile.TemporaryDirectory() as folder:
            db=connect(Path(folder)/'test.db')
            def step(n,bid):
                rows=[dict(ts=i*BAR_MS,open=100.,high=101.,low=99.,close=100.,volume=10.) for i in range(n)]
                tick(db,{'bid':bid,'ask':bid+.1,'timestamp':n*BAR_SECONDS+10},rows,n*BAR_SECONDS+10)
            try:
                step(200,100.)
                step(201,100.)
                entry_x=get(db,'trend')['position']['learning_x']
                self.assertIsNone(get(db,'learned_trend')['position'])
                self.assertEqual(db.execute('SELECT count(*) FROM learning_samples').fetchone()[0],0)
                step(202,90.)
                import json
                sample=json.loads(db.execute(
                    "SELECT detail FROM learning_samples WHERE strategy=?",
                    (MODEL_KEYS['trend'],)).fetchone()[0])
                self.assertEqual(sample['x'],entry_x)
                self.assertLess(sample['net_return'],0)
                self.assertEqual(sample['source'],'paper_bb15')
            finally:
                db.close()


if __name__=='__main__':
    unittest.main()
