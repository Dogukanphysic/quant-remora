import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ada_live
from ada_model_decisions import decision


class TimingTests(unittest.TestCase):
    def test_training_delay_and_clock_skew_do_not_reject_new_prediction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'learning.db'
            with patch.object(ada_live,'INTERVAL','15m'), patch.object(ada_live,'STEP_SECONDS',900):
                learner = ada_live.ada_learner()
            rows = [dict(ts=i*900000,open=100+i*.1,high=102+i*.1,
                         low=99+i*.1,close=101+i*.1,volume=1) for i in range(520)]
            now = 520*900+10
            times = iter([now+2,now+3])
            learner.update(rows,now,path=path,clock=lambda:next(times))
            result = decision(path,rows[-1]['ts'],now+4)
            self.assertEqual(result['owner'],'learned_model')
            self.assertEqual(result['target_long'],result['predicted_net_return']>0)
            self.assertEqual(decision(path,rows[-1]['ts'],now+1)['owner'],'ema_atr')
            self.assertEqual(decision(path,rows[-1]['ts'],now+901)['owner'],'ema_atr')
            with sqlite3.connect(path) as db:
                db.execute('UPDATE predictions SET created=?',(now-1,))
            db.close()
            self.assertEqual(decision(path,rows[-1]['ts'],now+4)['owner'],'ema_atr')

    def test_corrupt_model_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'learning.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE models(id,created,last_label,value)')
                db.execute('CREATE TABLE predictions(ts,model_id,prediction,created)')
                db.execute('INSERT INTO models VALUES (1,901,900000,?)',(json.dumps({'digest':'invalid'}),))
                db.execute('INSERT INTO predictions VALUES (0,1,0.1,902)')
            db.close()
            self.assertEqual(decision(path,0,903)['owner'],'ema_atr')


if __name__ == '__main__':
    unittest.main()
