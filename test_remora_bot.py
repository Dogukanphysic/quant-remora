import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from remora_bot import bot, exchange, learner, model_v2, strategy
from remora_bot.exchange import ApiError, TransportError
import unified_futures_research as research

STEP = strategy.STEP_MS


def make_bars(n, seed=1, start=1_600_000_000_000 // STEP * STEP):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return [strategy.Bar(start + i * STEP, c, c * 1.004, c * 0.996, c, 1.0) for i, c in enumerate(close)]


class StrategyParity(unittest.TestCase):
    def test_bot_signal_matches_research_state_machine(self):
        bars = make_bars(1500)
        frame = pd.DataFrame([b.__dict__ for b in bars])
        pos = research.signal(frame, 4, "donchian", dict(mode="long_only", n=100))
        phase = 0
        for i in range(strategy.required_bars(), len(bars)):
            s = strategy.evaluate(bars[: i + 1])
            if phase == 1 and s.breakdown:
                phase = 0
            elif phase == 0 and s.breakout:
                phase = 1
            self.assertEqual(phase, pos[i], i)

    def test_rejects_gaps(self):
        bars = make_bars(300)
        del bars[150]
        with self.assertRaises(ValueError):
            strategy.evaluate(bars)

    def test_stop_never_farther_than_25_percent(self):
        self.assertEqual(strategy.protective_stop(Decimal("100"), 50.0), Decimal("75.00"))
        self.assertEqual(strategy.protective_stop(Decimal("100"), 90.0), Decimal("90.0"))


class LearnerCausality(unittest.TestCase):
    def test_future_bars_do_not_change_features_or_prediction(self):
        bars = make_bars(1200)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            a, b = learner.connect(Path(d) / "a.db"), learner.connect(Path(d) / "b.db")
            now = bars[900].ts + STEP
            learner.ingest(a, "BTCUSDT", bars[:901], now, "t")
            learner.ingest(b, "BTCUSDT", bars, now, "t")      # future rows are ignored by time
            for db in (a, b):
                learner.train(db, now)
            pa = learner.register_prediction(a, "BTCUSDT", bars[900].ts, now)
            pb = learner.register_prediction(b, "BTCUSDT", bars[900].ts, now)
            self.assertIsNotNone(pa)
            self.assertAlmostEqual(pa, pb)
            self.assertIsNone(a.execute("SELECT y FROM samples WHERE ts=?", (bars[900].ts,)).fetchone()[0])

    def test_gate_requires_forward_evidence(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = learner.connect(Path(d) / "a.db")
            self.assertFalse(learner.gate(db)["authority"])

    def test_live_gate_alone_cannot_grant_authority(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = learner.connect(Path(d) / "a.db")
            passing = dict(authority=True, reason="forward_gate_passed", forward_scored=100)
            original = learner._live_gate
            learner._live_gate = lambda _db: dict(passing)
            try:
                failed = Path(d) / "wf.json"
                failed.write_text(json.dumps(dict(authority=False, reason="walk_forward_gate_failed")))
                self.assertFalse(learner.gate(db, failed)["authority"])
                self.assertFalse(learner.gate(db, Path(d) / "missing.json")["authority"])
                ok = Path(d) / "ok.json"
                ok.write_text(json.dumps(dict(authority=True, reason="walk_forward_gate_passed")))
                self.assertTrue(learner.gate(db, ok)["authority"])
            finally:
                learner._live_gate = original


class FakeClient:
    environment, identity = "fake", "id"

    def __init__(self):
        self.qty = {s: Decimal(0) for s in bot.SYMBOLS}
        self.orders, self.stops, self.posts = {}, {}, []
        self.fail_next_post = None

    def rules(self, s):
        return dict(step=Decimal("0.001"), min_qty=Decimal("0.001"), tick=Decimal("0.1"), min_notional=Decimal("5"))

    def mark(self, s):
        return Decimal("100")

    def one_way_mode(self):
        return True

    def wallet(self):
        return Decimal("5000"), Decimal("5000")

    def position(self, s):
        return self.qty[s], Decimal("100")

    def open_orders(self, s):
        return [cid for cid, sym in self.stops.items() if sym == s]

    def order(self, s, cid):
        if cid not in self.orders:
            raise ApiError("not found", 400, -2013)
        return self.orders[cid]

    def configure(self, s, lev):
        pass

    def market_order(self, s, side, qty, cid, reduce_only):
        self.posts.append(cid)
        if self.fail_next_post:
            exc, self.fail_next_post = self.fail_next_post, None
            if isinstance(exc, TransportError):
                self._fill(s, side, qty, cid)      # accepted by exchange, response lost
            raise exc
        return self._fill(s, side, qty, cid)

    def _fill(self, s, side, qty, cid):
        self.qty[s] += qty if side == "BUY" else -qty
        self.orders[cid] = dict(status="FILLED", executedQty=str(qty), avgPrice="100")
        return self.orders[cid]

    def place_stop(self, s, qty, trigger, cid):
        self.stops[cid] = s

    def cancel_stop(self, cid):
        self.stops.pop(cid, None)


class FakeMarket:
    def __init__(self, bars):
        self.bars = bars

    def closed_bars(self, s, limit=1000):
        return self.bars

    def funding(self, s, limit=1000):
        idx = pd.to_datetime([b.ts for b in self.bars[::2]], unit="ms", utc=True)
        return pd.Series(0.0001, index=idx)


def breakout_bars():
    bars = make_bars(400, seed=3)
    last = bars[-1]
    hi = max(b.high for b in bars[-101:-1])
    bars[-1] = strategy.Bar(last.ts, last.open, hi * 1.06, last.low, hi * 1.05, 1.0)
    return bars


class BotFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = bot.connect(Path(self.tmp.name) / "l.db")
        self.ldb = learner.connect(Path(self.tmp.name) / "learn.db")
        self.client = FakeClient()
        self.bars = breakout_bars()
        self.now = self.bars[-1].ts + STEP + 60_000

    def tearDown(self):
        self.db.close()
        self.ldb.close()
        self.tmp.cleanup()

    def tick(self, now=None):
        return bot.tick(self.db, self.ldb, self.client, FakeMarket(self.bars), now or self.now)

    def test_breakout_enters_and_places_protective_stop(self):
        result = self.tick()
        self.assertEqual(result["BTCUSDT"], "enter")
        pos = self.db.execute("SELECT * FROM positions WHERE symbol='BTCUSDT'").fetchone()
        self.assertEqual(pos["phase"], "long")
        self.assertIn(pos["stop_id"], self.client.stops)
        self.assertEqual(self.tick()["BTCUSDT"], "waiting")          # same bar never re-enters

    def test_ambiguous_post_is_reconciled_not_reposted(self):
        self.client.fail_next_post = TransportError("timeout")
        self.tick()
        self.assertEqual(len([p for p in self.client.posts if p.startswith("rmb-btc-e-")]), 1)
        pos = self.db.execute("SELECT phase FROM positions WHERE symbol='BTCUSDT'").fetchone()
        self.assertEqual(pos["phase"], "long")

    def test_untracked_exchange_position_halts(self):
        self.client.qty["ETHUSDT"] = Decimal("1")
        with self.assertRaises(bot.Halt):
            self.tick()

    def test_missing_stop_halts(self):
        self.tick()
        self.client.stops.clear()
        with self.assertRaises(bot.Halt):
            self.tick(self.now + STEP)

    def test_exchange_stop_closes_trade(self):
        self.tick()
        self.client.qty["BTCUSDT"] = Decimal(0)
        self.client.stops = {k: v for k, v in self.client.stops.items() if v != "BTCUSDT"}
        self.tick(self.now + 1000)
        trade = self.db.execute("SELECT exit_reason FROM trades").fetchone()
        self.assertEqual(trade["exit_reason"], "exchange_stop")

    def test_drawdown_blocks_new_entries(self):
        self.db.execute("UPDATE bot SET peak_wallet='6000'")
        self.db.commit()
        result = self.tick()
        self.assertTrue(result["BTCUSDT"].startswith("entries_blocked"))
        self.assertEqual(self.client.posts, [])

    def test_rejected_key_does_not_bind_ledger(self):
        def reject():
            raise ApiError("HTTP 401", 401, -2015)
        self.client.one_way_mode = reject
        with self.assertRaises(ApiError):
            bot.startup(self.db, self.client)
        self.assertIsNone(self.db.execute("SELECT identity FROM bot").fetchone()["identity"])

    def test_client_ids_are_deterministic_and_short(self):
        a = bot.client_id("BTCUSDT", 1_700_000_000_000, "e")
        self.assertEqual(a, bot.client_id("BTCUSDT", 1_700_000_000_000, "e"))
        self.assertLessEqual(len(a), 36)


class ModelMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = bot.connect(Path(self.tmp.name) / "l.db")
        self.ldb = learner.connect(Path(self.tmp.name) / "learn.db")
        self.mdb = model_v2.connect(Path(self.tmp.name) / "m.db")
        self.client = FakeClient()
        self.bars = make_bars(400, seed=5)            # no Donchian breakout needed in model mode
        self.now = self.bars[-1].ts + STEP + 60_000
        self.original = model_v2.update
        self.pred = {"BTCUSDT": 0.01, "ETHUSDT": -0.01}
        model_v2.update = lambda *a, **k: dict(self.pred)

    def tearDown(self):
        model_v2.update = self.original
        self.tmp.cleanup()

    def tick(self, now):
        return bot.tick(self.db, self.ldb, self.client, FakeMarket(self.bars), now, model_db=self.mdb)

    def test_positive_prediction_enters_negative_stays_cash(self):
        result = self.tick(self.now)
        self.assertEqual(result, {"BTCUSDT": "enter", "ETHUSDT": "model_predicts_non_positive"})
        pos = self.db.execute("SELECT stop_price, entry_price FROM positions WHERE symbol='BTCUSDT'").fetchone()
        distance = 1 - Decimal(pos["stop_price"]) / Decimal(pos["entry_price"])
        self.assertTrue(Decimal("0.03") <= distance <= Decimal("0.15") + Decimal("0.001"))
        owner = json.loads(self.db.execute("SELECT signal FROM decisions WHERE action='enter'").fetchone()[0])
        self.assertEqual(owner["decision_owner"], "experimental_model_v2")
        self.assertFalse(owner["profitability_proven"])

    def test_non_positive_prediction_exits(self):
        self.tick(self.now)
        self.bars = self.bars + [strategy.Bar(self.bars[-1].ts + STEP, *([self.bars[-1].close] * 4), 1.0)]
        self.pred = {"BTCUSDT": -0.001, "ETHUSDT": -0.01}
        result = self.tick(self.now + STEP)
        self.assertEqual(result["BTCUSDT"], "exit")
        self.assertEqual(self.client.qty["BTCUSDT"], 0)
        self.assertNotIn(True, [v == "BTCUSDT" for v in self.client.stops.values()])

    def test_missing_prediction_never_trades(self):
        self.pred = {}
        result = self.tick(self.now)
        self.assertEqual(set(result.values()), {"no_model_prediction"})
        self.assertEqual(self.client.posts, [])


class ModelV2Features(unittest.TestCase):
    def test_future_bars_do_not_change_past_features(self):
        bars = model_v2.bars_frame(make_bars(700, seed=9))
        other = model_v2.bars_frame(make_bars(700, seed=10))["close"]
        funding = pd.Series(0.0001, index=bars.index[::2])
        full = model_v2.feature_frame(bars, other, funding, "BTCUSDT")
        cut = model_v2.feature_frame(bars.iloc[:600], other.iloc[:600], funding[funding.index < bars.index[600]],
                                     "BTCUSDT")
        pd.testing.assert_frame_equal(full.loc[cut.index, model_v2.FEATURES], cut[model_v2.FEATURES])
        self.assertTrue(np.isnan(cut["y"].iloc[-1]))      # label needs future bars


class ModelV2Training(unittest.TestCase):
    def test_ingest_stores_epoch_ms_and_trains_then_predicts(self):
        btc, eth = make_bars(1200, seed=11), make_bars(1200, seed=12)
        now = btc[-1].ts + STEP + 60_000
        funding = {s: pd.Series(0.0001, index=pd.to_datetime([b.ts for b in btc[::2]], unit="ms", utc=True))
                   for s in bot.SYMBOLS}
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db = model_v2.connect(Path(d) / "m.db")
            preds = model_v2.update(db, {"BTCUSDT": btc, "ETHUSDT": eth}, funding, now)
            ts = db.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
            self.assertEqual(ts, btc[-1].ts)
            self.assertIsNotNone(db.execute("SELECT id FROM models").fetchone())
            self.assertTrue(all(isinstance(v, float) for v in preds.values()))


def make_bars_h(n, hours, seed=1):
    step = hours * 3600 * 1000
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, n)))
    start = 1_600_000_000_000 // step * step
    return [strategy.Bar(start + i * step, c, c * 1.002, c * 0.998, c, 1.0) for i, c in enumerate(close)]


class HourlyInterval(unittest.TestCase):
    def setUp(self):
        bot.configure_interval(1)

    def tearDown(self):
        bot.configure_interval(4)

    def test_configure_scales_day_windows_and_keeps_4h_unchanged(self):
        self.assertEqual((strategy.INTERVAL, strategy.STEP_MS, strategy.VOL_BARS), ("1h", 3_600_000, 720))
        self.assertEqual(model_v2.FEATURES[0], "r_1h")
        bot.configure_interval(4)
        self.assertEqual((strategy.VOL_BARS, strategy.BARS_PER_YEAR), (180, 2190))
        self.assertEqual(model_v2.FEATURES[0], "r_4h")
        self.assertEqual(model_v2.VERSION, "pooled-ridge-next24h-v2")

    def test_hourly_features_are_causal(self):
        bars = model_v2.bars_frame(make_bars_h(2000, 1, seed=3))
        other = model_v2.bars_frame(make_bars_h(2000, 1, seed=4))["close"]
        funding = pd.Series(0.0001, index=bars.index[::8])
        full = model_v2.feature_frame(bars, other, funding, "ETHUSDT")
        cut = model_v2.feature_frame(bars.iloc[:1500], other.iloc[:1500],
                                     funding[funding.index < bars.index[1500]], "ETHUSDT")
        rows = cut.dropna(subset=model_v2.FEATURES).index
        self.assertGreater(len(rows), 500)
        pd.testing.assert_frame_equal(full.loc[rows, model_v2.FEATURES], cut.loc[rows, model_v2.FEATURES])

    def test_hourly_model_mode_trades_and_never_feeds_4h_learner(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db, ldb = bot.connect(Path(d) / "l.db"), learner.connect(Path(d) / "learn.db")
            mdb = model_v2.connect(Path(d) / "m.db")
            bars = make_bars_h(800, 1, seed=6)
            now = bars[-1].ts + 3_600_000 + 60_000
            original = model_v2.update
            model_v2.update = lambda *a, **k: {"BTCUSDT": 0.002, "ETHUSDT": -0.001}
            try:
                result = bot.tick(db, ldb, FakeClient(), FakeMarket(bars), now, model_db=mdb)
            finally:
                model_v2.update = original
            self.assertEqual(result, {"BTCUSDT": "enter", "ETHUSDT": "model_predicts_non_positive"})
            self.assertEqual(ldb.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)
            signal = json.loads(db.execute("SELECT signal FROM decisions WHERE action='enter'").fetchone()[0])
            self.assertEqual(signal["interval"], "1h")


class TestnetOnly(unittest.TestCase):
    def test_no_mainnet_host_for_signed_requests(self):
        self.assertEqual(set(exchange.TEST_HOSTS.values()),
                         {"https://testnet.binancefuture.com", "https://demo-fapi.binance.com"})
        self.assertNotIn(exchange.PUBLIC_BASE, exchange.TEST_HOSTS.values())
        source = Path(exchange.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count('"https://fapi.binance.com"'), 1)     # public market data only
        self.assertNotIn("ALLOW_MAINNET", source)

    def test_unknown_test_environment_refused(self):
        import os
        os.environ.update({exchange.KEY_ENV: "k", exchange.SECRET_ENV: "s", exchange.HOST_ENV: "mainnet"})
        try:
            with self.assertRaises(ValueError):
                exchange.TestnetClient()
        finally:
            for k in (exchange.KEY_ENV, exchange.SECRET_ENV, exchange.HOST_ENV):
                os.environ.pop(k, None)

    def test_unlisted_operations_refused(self):
        client = exchange.TestnetClient.__new__(exchange.TestnetClient)
        with self.assertRaises(ValueError):
            client._send("POST", "/fapi/v1/listenKey", {}, False)


if __name__ == "__main__":
    unittest.main()
