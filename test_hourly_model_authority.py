import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from hourly_model_authority import apply,eligible
from trend4h_learning import connect


class AuthorityTests(unittest.TestCase):
    def test_gates(self):
        model = dict(version='ridge-next1h-v1',validation_samples=200,accepted_proxy_samples=20,
                     validation_mse=.1,constant_mse=.2,accepted_mean_net_return=.001)
        self.assertTrue(eligible(model))
        self.assertFalse(eligible(model,'15m'))
        self.assertTrue(eligible(dict(model,version='ridge-next15m-v1'),'15m'))
        for key,value in [('accepted_proxy_samples',0),('validation_mse',.3),
                          ('accepted_mean_net_return',-1),('validation_mse',float('nan'))]:
            self.assertFalse(eligible(dict(model,**{key:value})))

    def test_exact_prediction_digest_and_causality(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'learner.db'
            db = connect(path)
            model = dict(version='ridge-next1h-v1',validation_samples=200,accepted_proxy_samples=20,
                         validation_mse=.1,constant_mse=.2,accepted_mean_net_return=.001)
            model['digest'] = hashlib.sha256(json.dumps(model,sort_keys=True).encode()).hexdigest()
            db.execute('INSERT INTO models VALUES (1,0,3600000,?)',(json.dumps(model),))
            db.execute('INSERT INTO predictions VALUES (0,1,-.01,3600)')
            db.commit()
            signal = dict(candle_close_ms=3599999,target_long=True,features={})
            decision = apply(signal,path)
            self.assertFalse(decision['target_long'])
            self.assertEqual(decision['features']['decision_owner'],'learned_model')
            self.assertTrue(apply(dict(signal,candle_close_ms=7199999),path)['target_long'])
            db.execute('UPDATE models SET last_label=3600001'); db.commit()
            self.assertTrue(apply(signal,path)['target_long'])
            db.execute('UPDATE models SET last_label=3600000,value=?',(json.dumps(dict(model,version='tampered')),)); db.commit()
            self.assertTrue(apply(signal,path)['target_long'])
            db.close()


if __name__ == '__main__':
    unittest.main()
