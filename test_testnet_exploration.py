import unittest
from testnet_exploration import apply


class ExplorationTests(unittest.TestCase):
    def signal(self,slot,prediction=None):
        close=(400+slot)*900000
        return dict(candle_close_ms=close-1,target_long=False,
                    features={} if prediction is None else {'predicted_net_return':prediction})

    def run_signal(self,slot,prediction=None):
        s=self.signal(slot,prediction)
        return apply(s,environment='binance_spot_testnet',interval='15m',now_ms=s['candle_close_ms']+1001)

    def test_scheduled_entry_without_positive_prediction(self):
        self.assertTrue(self.run_signal(0,-.01)['target_long'])
        self.assertFalse(self.run_signal(1,-.01)['target_long'])
        self.assertTrue(self.run_signal(1,-.002)['target_long'])

    def test_forced_cash_slots_even_for_positive_model(self):
        for slot in (2,3):
            self.assertFalse(self.run_signal(slot,.1)['target_long'])

    def test_no_mainnet_or_other_interval(self):
        for env,frame in [('binance_spot_mainnet','15m'),('binance_spot_testnet','1h')]:
            with self.assertRaises(ValueError):
                apply(self.signal(0),environment=env,interval=frame,now_ms=400*900000)

    def test_stale_and_source_unchanged(self):
        s=self.signal(0)
        stale=apply(s,environment='binance_spot_testnet',interval='15m',now_ms=400*900000+300001)
        self.assertFalse(stale['target_long'])
        self.run_signal(0)
        self.assertEqual(s['features'],{})


if __name__ == '__main__': unittest.main()
