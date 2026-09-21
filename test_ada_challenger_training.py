import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from ada_challenger_training import dataset, evaluate, train, STEP


class ChallengerTests(unittest.TestCase):
    def source(self, path):
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE samples(ts INTEGER,x TEXT,y REAL,label_end INTEGER)')
            for i in range(700):
                ts = (i + (i >= 300))*STEP
                db.execute('INSERT INTO samples VALUES(?,?,?,?)',
                           (ts, json.dumps([.001]*6), 1.004*.9975/1.0025-1, ts+2*STEP))

    def test_gap_purge_and_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'source.db'
            self.source(path)
            before = path.read_bytes()
            rows, x, labels = dataset(path)
            self.assertNotIn(299, labels[4])
            for h in labels:
                _, result = evaluate(rows, x, labels[h], h, 420, 560)
                self.assertLess(result['train_last_label'], result['first_test_decision'])
                self.assertLess(result['test_last_label'], rows[560][0]+STEP)
            report, artifact = train(path)
            self.assertFalse(report['execution_eligible'])
            self.assertFalse(artifact['automatic_activation'])
            self.assertEqual(path.read_bytes(), before)

    def test_future_outcomes_do_not_change_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'source.db'
            self.source(path)
            rows, x, labels = dataset(path)
            original, _ = evaluate(rows, x, labels[8], 8, 420, 560)
            changed = dict(labels[8])
            for i in changed:
                if i >= 420:
                    changed[i] = -.5
            later, _ = evaluate(rows, x, changed, 8, 420, 560)
            self.assertEqual(original, later)


if __name__ == '__main__':
    unittest.main()
