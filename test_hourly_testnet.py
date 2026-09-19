import os
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch, Mock
import switch_testnet_hourly as switch
import binance_testnet_worker as w
from test_binance_testnet_worker import FakeClient, daily_closes, ENABLED_ENV, NOW_MS


class HourlyTests(unittest.TestCase):
    def test_15m_keeps_24h_lookback_and_deduplicates(self):
        step = 900000
        rows = [[i*step,'100','120','99',str(100+i*.1),'1',(i+1)*step-1] for i in range(97)]
        class Market:
            def klines(self,interval,limit,symbol):
                if interval != '15m':
                    raise AssertionError(interval)
                return rows[-limit:]
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,ENABLED_ENV), \
             patch.multiple(w,HOURLY_MODE=True,INTERVAL='15m',LOOKBACK_DAYS=96,DAY_MS=step,
                            MAX_CANDLE_AGE_MS=1500000,MOMENTUM_THRESHOLD=w.Decimal('.002')), \
             patch.object(w,'_now_ms',return_value=97*step+100):
            signal = w._signal(Market())
            self.assertEqual(signal['close_30d'],w.Decimal('100'))
            self.assertEqual(signal['features']['decision_interval'],'15m')
            self.assertIn('return_15m',signal['features'])
            path = Path(folder)/'worker.db'
            client = FakeClient([])
            w.run_once(db_path=path,client=client,market_data_client=Market())
            w.run_once(db_path=path,client=client,market_data_client=Market())
            self.assertEqual(len(client.buy_calls),1)

    def test_switch_never_starts_hourly_with_open_or_pending_position(self):
        for state in ({'tracked_position_qty':'0.001'},
                      {'tracked_position_qty':'0','pending_client_id':'unknown'},
                      {'tracked_position_qty':'0','halted':True}):
            with patch.object(w.execution,'Client',return_value=Mock()), \
                 patch.object(w.execution,'PublicMarketDataClient',return_value=Mock()), \
                 patch.object(w,'validate_bound_account'), patch.object(w,'control'), \
                 patch.object(w,'_control_mutex',side_effect=lambda: nullcontext()), \
                 patch.object(w,'_process_lock',side_effect=lambda _: nullcontext()), \
                 patch.object(w,'run_once',return_value={}), \
                 patch.object(w,'status_snapshot',return_value=state), \
                 patch.object(switch.subprocess,'run') as process:
                with self.assertRaises(RuntimeError):
                    switch.main()
                self.assertEqual(process.call_count,1)  # preflight only, no start

    def test_operator_exit_closes_only_tracked_qty_once(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,ENABLED_ENV), patch.object(w,'_now_ms',return_value=NOW_MS):
            path = Path(folder)/'worker.db'
            client = FakeClient(daily_closes(100,130))
            w.run_once(db_path=path,client=client,market_data_client=client.market_data)
            w.run_once(db_path=path,client=client,market_data_client=client.market_data,exit_only=True)
            w.run_once(db_path=path,client=client,market_data_client=client.market_data,exit_only=True)
            self.assertEqual(len(client.buy_calls),1)
            self.assertEqual(len(client.sell_calls),1)
            self.assertEqual(client.sell_calls[0][0],'0.001')
            with closing(w._connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM worker_learning_daily_labels').fetchone()[0],0)

    def test_hourly_signal_and_buy_do_not_write_daily_labels(self):
        hour = 3600000
        rows = [[i*hour,'100','102','99',str(100+i*.1),'1',(i+1)*hour-1] for i in range(31)]
        class Market:
            def klines(self,interval,limit,symbol):
                if interval != '1h':
                    raise AssertionError(interval)
                return rows[-limit:]
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,ENABLED_ENV), \
             patch.multiple(w,HOURLY_MODE=True,INTERVAL='1h',LOOKBACK_DAYS=24,DAY_MS=hour,
                            MAX_CANDLE_AGE_MS=5400000,MOMENTUM_THRESHOLD=w.Decimal('.002')), \
             patch.object(w,'_now_ms',return_value=31*hour+100):
            path = Path(folder)/'worker.db'
            client = FakeClient([])
            w.run_once(db_path=path,client=client,market_data_client=Market())
            w.run_once(db_path=path,client=client,market_data_client=Market())
            self.assertEqual(len(client.buy_calls),1)
            with closing(w._connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM worker_learning_daily_labels').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT feature_schema FROM worker_decisions').fetchone()[0],'btc_hourly_causal_v1')


if __name__ == '__main__':
    unittest.main()
