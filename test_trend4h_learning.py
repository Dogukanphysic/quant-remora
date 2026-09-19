import tempfile
import unittest
from pathlib import Path
from trend4h_learning import STEP, update, connect, vectors


class LearnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'learn.sqlite3'
        self.rows = [dict(ts=i*STEP,open=100+i*.1,high=102+i*.1,
                          low=99+i*.1,close=101+i*.1,volume=1) for i in range(520)]

    def tearDown(self):
        self.temp.cleanup()

    def test_seed_training_and_dedup(self):
        now = 520*STEP/1000+10
        a = update(self.rows,now,True,self.path)
        b = update(self.rows,now,True,self.path)
        self.assertEqual(a['model_id'],b['model_id'])
        self.assertEqual(a['label_counts'],{'historical':315})
        self.assertLess(a['model']['train_last_label'],a['model']['validation_start'])
        self.assertEqual(a['forward_scored_predictions'],0)

    def test_new_observation_matures_then_retrains(self):
        update(self.rows[:518],518*STEP/1000+1,True,self.path)
        a = update(self.rows[:519],519*STEP/1000+1,False,self.path)
        b = update(self.rows,520*STEP/1000+1,False,self.path)
        self.assertEqual(b['model_id'],a['model_id']+1)
        self.assertEqual(b['forward_scored_predictions'],1)
        self.assertEqual(b['label_counts']['observed'],1)

    def test_unclosed_or_gapped_bar_never_labels_predecessor(self):
        update(self.rows,519*STEP/1000,True,self.path)
        db = connect(self.path)
        self.assertIsNone(db.execute('SELECT y FROM samples WHERE ts=?',(518*STEP,)).fetchone()[0])
        db.close()

    def test_feature_prefix_is_causal(self):
        a,b = vectors(self.rows), vectors(self.rows[:450])
        self.assertEqual({k:a[k] for k in b},b)


if __name__ == '__main__':
    unittest.main()
