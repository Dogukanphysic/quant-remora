from pathlib import Path
import tempfile
import unittest

import btc_futures_testnet as t


class FuturesTestnetLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = t.connect_learning(Path(self.temp.name) / "learning.sqlite3")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_environment_hosts_are_pinned(self):
        self.assertEqual(t.BASES["testnet"], "https://testnet.binancefuture.com")
        self.assertEqual(t.BASES["demo"], "https://demo-fapi.binance.com")

    def test_round_trip_becomes_learning_label(self):
        before = {"phase": "cash"}
        opened = {"phase": "long", "wallet_usdt": "1000", "last_bar": 900000,
                  "decision": {"bar": 900000, "entry_regime": "reclaim", "rsi": 42}}
        t.sync_learning(self.db, before, opened, 1)
        closed = dict(opened, phase="cash", wallet_usdt="1005")
        t.sync_learning(self.db, opened, closed, 2)
        status = t.learning_snapshot(self.db)
        self.assertEqual(status["closed_labels"], 1)
        self.assertEqual(status["regimes"]["reclaim"]["wins"], 1)
        self.assertEqual(status["regimes"]["reclaim"]["mean_pnl_usdt"], 5.0)

    def test_negative_regime_is_filtered_after_three_labels_except_exploration(self):
        for index in range(3):
            self.db.execute("""INSERT INTO samples(entry_bar,regime,rsi,
                entry_wallet_usdt,close_wallet_usdt,pnl_usdt,label,opened_ts,closed_ts)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                            (index, "reclaim", 40, "100", "99", "-1", 0, 1, 2))
        self.db.commit()
        status = t.learning_snapshot(self.db)
        allowed, reason = t.adaptive_permission(status, "reclaim", 900_000)
        self.assertFalse(allowed)
        self.assertEqual(reason, "negative_regime_filtered")
        allowed, reason = t.adaptive_permission(status, "reclaim", 4_500_000)
        self.assertTrue(allowed)
        self.assertEqual(reason, "controlled_exploration")


if __name__ == "__main__":
    unittest.main()
