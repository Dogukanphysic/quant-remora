import json
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from ada_edge_diagnostics import attribution, fee_audit, spread_samples


class EdgeTests(unittest.TestCase):
    def test_public_spread_sample_and_invalid_book(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'spread.json'
            blob = json.dumps(dict(symbol='ADAUSDT',bidPrice='100',askPrice='100.04')).encode()
            with patch('ada_edge_diagnostics.urllib.request.urlopen',return_value=io.BytesIO(blob)) as fetch:
                with patch('builtins.print'):
                    spread_samples(path,count=1)
                self.assertIn('data-api.binance.vision/api/v3/ticker/bookTicker',fetch.call_args.args[0])
            self.assertAlmostEqual(json.loads(path.read_text())['median_spread_bps'],4)
            invalid = json.dumps(dict(symbol='ADAUSDT',bidPrice='100',askPrice='99')).encode()
            with patch('ada_edge_diagnostics.urllib.request.urlopen',return_value=io.BytesIO(invalid)):
                with self.assertRaises(ValueError):
                    spread_samples(path,count=1)

    def test_winner_dependence_and_cost_erasure(self):
        trades = [dict(net_return=.3,gross_return=.31,reason='target'),
                  dict(net_return=-.02,gross_return=-.01,reason='stop'),
                  dict(net_return=-.001,gross_return=.001,reason='timeout')]
        result = attribution(trades)
        self.assertGreater(result['net_return'],0)
        self.assertLess(result['without_best_trade'],0)
        self.assertEqual(result['gross_positive_but_net_negative'],1)
        self.assertEqual(attribution([])['trades'],0)

    def test_fee_conversion_and_readonly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'ledger.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE orders(response TEXT,applied INTEGER)')
                for asset,commission in [('USDT','.01'),('ADA','.1'),('BNB','.001')]:
                    response = dict(side='BUY',fills=[dict(qty='100',price='.1',
                                        commission=commission,commissionAsset=asset)])
                    db.execute('INSERT INTO orders VALUES(?,1)',(json.dumps(response),))
                db.execute('INSERT INTO orders VALUES(?,1)',(json.dumps(dict(kind='recovery')),))
            db.close()
            before = path.read_bytes()
            report = fee_audit(path)
            self.assertEqual(report['filled_orders'],2)
            self.assertEqual(report['unsupported_fee_orders'],1)
            for row in report['rates']:
                self.assertAlmostEqual(row['rate'],.001)
            self.assertEqual(path.read_bytes(),before)


if __name__ == '__main__': unittest.main()
