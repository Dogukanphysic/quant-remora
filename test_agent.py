import unittest
import csv
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from agent import (backtest, validate, describe_tls_reply, public_json, fetch,
                   read_dataset, DATA_FIELDS, DEFAULT_DATA)
from strategies import BAR_SECONDS, BAR_MS


def candles(prices):
    return [dict(ts=i * BAR_MS, open=p, high=p, low=p, close=p, volume=1.0)
            for i, p in enumerate(prices)]


class ResearchTests(unittest.TestCase):
    @patch('agent.time.sleep')
    @patch('agent.time.time', return_value=1002 * BAR_SECONDS)
    @patch('agent.public_json')
    def test_bitstamp_15m_pagination_and_open_candle_exclusion(self, request, clock, sleep):
        def payload(periods):
            return {'data': {'pair': 'BTC/USD', 'ohlc': [
                {'timestamp': str(i * BAR_SECONDS), 'open': '100', 'high': '102',
                 'low': '99', 'close': '101', 'volume': '5'} for i in periods]}}
        request.side_effect = [payload(range(2, 1003)), payload([1])]
        rows = fetch(1001)
        self.assertEqual(len(rows), 1001)
        self.assertEqual(rows[0]['ts'], BAR_MS)
        self.assertEqual(rows[-1]['ts'], 1001 * BAR_MS)
        self.assertEqual(rows[0]['close'], 101)
        self.assertEqual(request.call_args_list[0].args[1]['step'], 900)
        self.assertEqual(request.call_args_list[1].args[1]['end'], 2 * BAR_SECONDS - 1)

    def test_default_download_path_does_not_reuse_hourly_file(self):
        self.assertEqual(DEFAULT_DATA.name, 'bitstamp-btc-usd-15m.csv')

    @patch('agent.public_json', return_value={'data': {'pair': 'BTC/USDT', 'ohlc': []}})
    def test_wrong_quote_currency_rejected(self, request):
        with self.assertRaisesRegex(ValueError, 'BTC/USD'):
            fetch(100)

    def test_csv_source_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=DATA_FIELDS)
                writer.writeheader()
                writer.writerow({**candles([100])[0], 'exchange': 'okx', 'symbol': 'BTC/USDT'})
            with self.assertRaisesRegex(ValueError, 'kaynağı/paritesi'):
                read_dataset(path)

    def test_csv_interval_must_be_15_minutes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hourly.csv'
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=DATA_FIELDS)
                writer.writeheader()
                for row in candles([100, 101]):
                    row['ts'] *= 4
                    writer.writerow({**row, 'exchange': 'bitstamp', 'symbol': 'BTC/USD'})
            with self.assertRaisesRegex(ValueError, '900 saniyelik'):
                read_dataset(path)
            self.assertEqual(len(read_dataset(path, step=3600)), 2)

    def test_filter_redirect_explained(self):
        reply = (b'HTTP/1.1 307 Temporary Redirect\r\n'
                 b'Location: https://guvenliinternet.turktelekom.com.tr/?host=openapi.okx.com\r\n')
        self.assertIn('Türk Telekom', describe_tls_reply(reply))

    def test_unrelated_redirect_not_attributed_to_provider(self):
        reply = b'HTTP/1.1 307 Temporary Redirect\r\nLocation: https://example.com/\r\n'
        self.assertNotIn('Türk Telekom', describe_tls_reply(reply))
        self.assertIn('düz HTTP', describe_tls_reply(reply))

    @patch('agent.inspect_tls_reply', return_value=b'HTTP/1.1 403 Forbidden\r\n')
    @patch('agent.urlopen', side_effect=URLError('[SSL: WRONG_VERSION_NUMBER]'))
    def test_tls_failure_stops_download_with_explanation(self, request, probe):
        with self.assertRaisesRegex(ValueError, 'Veri indirilmedi'):
            public_json('/api/v2/ohlc/btcusd/')
        request.assert_called_once()
        probe.assert_called_once_with('www.bitstamp.net')

    @patch('agent.inspect_tls_reply')
    @patch('agent.urlopen', side_effect=URLError('CERTIFICATE_VERIFY_FAILED'))
    def test_certificate_failure_is_not_retried(self, request, probe):
        with self.assertRaises(URLError):
            public_json('/api/v2/ohlc/btcusd/')
        probe.assert_not_called()

    def test_flat_market_keeps_cash(self):
        result = backtest(candles([100.0] * 10), 2, 3)
        self.assertEqual(result['final_equity_usd'], 1000)
        self.assertEqual(result['trade_count'], 0)

    def test_current_close_cannot_trigger_current_trade(self):
        result = backtest(candles([100, 100, 100, 200, 200]), 1, 3)
        self.assertEqual(result['trades'][0]['ts'], 4 * BAR_MS)

    def test_costs_reduce_flat_execution_equity(self):
        result = backtest(candles([98, 99, 100, 100]), 1, 3)
        expected_quantity = 100 / (100 * 1.0005 * 1.001)
        self.assertAlmostEqual(result['open_quantity_btc'], expected_quantity)
        self.assertAlmostEqual(result['final_equity_usd'],
                               900 + expected_quantity * 100 * 0.9995 * 0.999)

    def test_future_changes_do_not_change_earlier_trades(self):
        a = backtest(candles([100, 101, 102, 103, 104, 105]), 1, 3)
        b = backtest(candles([100, 101, 102, 103, 104, 1]), 1, 3)
        self.assertEqual(a['trades'], b['trades'])

    def test_missing_candle_rejected(self):
        rows = candles([100] * 10)
        del rows[4]
        with self.assertRaises(ValueError):
            validate(rows)

    def test_sale_applies_costs(self):
        result = backtest(candles([98, 99, 100, 100, 90, 90]), 1, 3)
        buy, sell = result['trades']
        self.assertEqual(sell['side'], 'sell')
        self.assertEqual(result['open_quantity_btc'], 0)
        self.assertAlmostEqual(result['final_equity_usd'],
                               900 + buy['quantity'] * sell['price'] * 0.999)


if __name__ == '__main__':
    unittest.main()
