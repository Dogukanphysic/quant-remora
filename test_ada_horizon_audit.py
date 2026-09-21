import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from ada_horizon_audit import audit, STEP


class AuditTests(unittest.TestCase):
    def test_causal_purge_and_read_only_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.db'
            db=sqlite3.connect(path)
            db.execute('CREATE TABLE samples(ts INTEGER,x TEXT,y REAL,label_end INTEGER)')
            # Stable positive gross returns, with a gap in the middle.
            for i in range(520):
                t=i if i<250 else i+1
                db.execute('INSERT INTO samples VALUES (?,?,?,?)',
                           (t*STEP,json.dumps([.004]*6),1.004*(1-.0025)/(1+.0025)-1,(t+2)*STEP))
            db.commit(); db.close()
            before=path.read_bytes()
            report=audit(path)
            self.assertFalse(report['execution_enabled'])
            self.assertEqual(len(report['results']),12)
            for row in report['results']:
                self.assertLess(row['train_last_label_end'],row['first_test_decision'])
            self.assertEqual(path.read_bytes(),before)
            low=next(r for r in report['results'] if r['horizon_bars']==1 and r['cost_per_side']==.001)
            high=next(r for r in report['results'] if r['horizon_bars']==1 and r['cost_per_side']==.0025)
            self.assertGreater(low['simulated_round_trips'],0)
            self.assertEqual(high['simulated_round_trips'],0)


if __name__=='__main__': unittest.main()
