import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_DOWN, getcontext, setcontext
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import binance_testnet_worker as worker


DAY_MS = 86_400_000
NOW_MS = 200 * DAY_MS + 12 * 60 * 60 * 1000
TEST_API_KEY_A = "A1b2C3d4" * 8
TEST_API_KEY_B = "Z9y8X7w6" * 8
ENABLED_ENV = {
    "BINANCE_TESTNET_WORKER_ENABLED": "true",
    "BINANCE_ORDER_EXECUTION_ENABLED": "testnet",
    "BINANCE_TESTNET_API_KEY": TEST_API_KEY_A,
    "BINANCE_TESTNET_SECRET_KEY": "test-secret",
}


def definitive_order_not_found():
    return worker.execution.BinanceOrderNotFoundError(
        'Binance HTTP 400: {"code":-2013,"msg":"Order does not exist."}',
        method="GET",
        path="/v3/order",
        http_status=400,
        api_code=-2013,
        signed=True,
    )


class Rules:
    min_notional = "5"
    step_size = "0.000001"
    min_qty = "0.000001"


def daily_closes(first, last, *, final_day=200):
    first = float(first)
    last = float(last)
    values = [first + (last - first) * index / 30 for index in range(31)]
    candles = []
    for index, close in enumerate(values):
        day = final_day - 30 + index
        close_time = day * DAY_MS - 1
        text = format(close, ".8f")
        candles.append(
            [(day - 1) * DAY_MS, text, text, text, text, "1", close_time]
        )
    return candles


class FakeMarketData:
    def __init__(self, klines):
        self.daily = klines
        self.kline_calls = 0

    def klines(self, interval="1d", limit=32, symbol=worker.SYMBOL):
        self.kline_calls += 1
        if interval != "1d" or symbol != worker.SYMBOL:
            raise AssertionError("unexpected public market-data request")
        return self.daily[-limit:]


class FakeClient:
    def __init__(self, klines, *, api_key=TEST_API_KEY_A):
        self.api_key = api_key
        self.market_data = FakeMarketData(klines)
        self.buy_calls = []
        self.sell_calls = []
        self.lookup_calls = []
        self.orders = {}
        self.buy_error = None
        self.lookup_error = None
        self.query_omit_fills = False
        self.open = []
        self.open_orders_error = None
        self.open_order_calls = 0
        self.free_usdt = "10000"
        self.free_btc = "100"
        self.buy_quantity = "0.001"
        self.buy_commission = "0"
        self.buy_commission_asset = "BTC"
        self.sell_commission = "0.1"
        self.sell_commission_asset = "USDT"
        self.sell_quote = "12"

    def klines(self, interval="1d", limit=32, symbol=worker.SYMBOL):
        raise AssertionError("Testnet execution client must not supply signal candles")

    @property
    def daily(self):
        return self.market_data.daily

    @daily.setter
    def daily(self, value):
        self.market_data.daily = value

    @property
    def kline_calls(self):
        return self.market_data.kline_calls

    def symbol_rules(self, symbol=worker.SYMBOL):
        return Rules()

    def book_ticker(self, symbol=worker.SYMBOL):
        return {"symbol": symbol, "bidPrice": "100000", "askPrice": "100001"}

    def open_orders(self, symbol=worker.SYMBOL):
        self.open_order_calls += 1
        if self.open_orders_error is not None:
            raise self.open_orders_error
        return list(self.open)

    def account(self):
        return {
            "balances": [
                {"asset": "USDT", "free": self.free_usdt, "locked": "0"},
                {"asset": "BTC", "free": self.free_btc, "locked": "0"},
            ]
        }
    def order_by_client_id(self, client_id, symbol=worker.SYMBOL):
        self.lookup_calls.append((client_id, symbol))
        if self.lookup_error is not None:
            raise self.lookup_error
        if client_id not in self.orders:
            raise LookupError("order not found")
        result = dict(self.orders[client_id])
        if self.query_omit_fills:
            result.pop("fills", None)
        return result

    def reconcile_filled_order(self, client_id, symbol=worker.SYMBOL):
        return dict(self.orders[client_id])

    def place_market_buy(
        self, quote_usdt, symbol=worker.SYMBOL, client_order_id=None
    ):
        self.buy_calls.append((str(quote_usdt), symbol, client_order_id))
        if self.buy_error is not None:
            raise self.buy_error
        order = self.filled_order(
            client_order_id,
            "BUY",
            self.buy_quantity,
            "10",
            commission=self.buy_commission,
            commission_asset=self.buy_commission_asset,
        )
        self.orders[client_order_id] = order
        return dict(order)

    def place_market_sell(
        self, quantity, symbol=worker.SYMBOL, client_order_id=None
    ):
        self.sell_calls.append((str(quantity), symbol, client_order_id))
        order = self.filled_order(
            client_order_id,
            "SELL",
            str(quantity),
            self.sell_quote,
            commission=self.sell_commission,
            commission_asset=self.sell_commission_asset,
        )
        self.orders[client_order_id] = order
        return dict(order)

    @staticmethod
    def filled_order(
        client_id, side, quantity, quote, *, commission="0", commission_asset="BTC"
    ):
        return {
            "symbol": worker.SYMBOL,
            "orderId": 123,
            "clientOrderId": client_id,
            "status": "FILLED",
            "side": side,
            "executedQty": quantity,
            "cummulativeQuoteQty": quote,
            "fills": [
                {
                    "qty": quantity,
                    "commission": commission,
                    "commissionAsset": commission_asset,
                }
            ],
        }


def run_once(*, db_path, client, market_data_client=None, **kwargs):
    return worker.run_once(
        db_path=db_path,
        client=client,
        market_data_client=(market_data_client or client.market_data),
        **kwargs,
    )


class BinanceTestnetWorkerTests(unittest.TestCase):
    def setUp(self):
        # Baseline fixtures must not inherit the operator's active sizing override.
        sizing = patch.object(worker, "ENTRY_QUOTE_USDT", worker.Decimal("10"))
        sizing.start()
        self.addCleanup(sizing.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "worker.sqlite3"
        self.lock_path = Path(self.temp.name) / "worker.lock"
        self.env = patch.dict(os.environ, ENABLED_ENV, clear=False)
        self.env.start()
        self.clock = patch.object(worker, "_now_ms", return_value=NOW_MS)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.env.stop()
        self.temp.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows detached-process regression")
    def test_pid_alive_supports_detached_windows_worker(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
        )
        try:
            self.assertTrue(worker._pid_alive(child.pid))
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertFalse(worker._pid_alive(child.pid))

    def test_cash_signal_records_once_without_an_order(self):
        client = FakeClient(daily_closes(100, 109))

        result = run_once(db_path=self.db_path, client=client)
        duplicate = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "hold_cash")
        self.assertEqual(duplicate["action"], "waiting")
        self.assertEqual(client.buy_calls, [])
        self.assertEqual(client.sell_calls, [])
        status = worker.status_snapshot(self.db_path, Path(self.temp.name) / "lock")
        self.assertEqual(status["latest_decision"]["action"], "hold_cash")
        self.assertEqual(status["momentum_threshold"], "0.10")
        self.assertTrue(status["policy_match"])

    def test_signal_persists_versioned_causal_daily_features(self):
        client = FakeClient(daily_closes(100, 109))

        result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "hold_cash")
        with worker.closing(worker._connect(self.db_path)) as db:
            row = db.execute(
                "SELECT feature_schema, feature_json FROM worker_decisions"
            ).fetchone()
        features = json.loads(row["feature_json"])
        self.assertEqual(row["feature_schema"], "btc_daily_causal_v1")
        self.assertEqual(
            set(features),
            {
                "close",
                "return_1d",
                "return_7d",
                "momentum_30d",
                "realized_volatility_20d",
            },
        )
        self.assertEqual(features["close"], "109.00000000")
        self.assertEqual(features["momentum_30d"], "0.09")

    def test_signal_is_independent_of_process_decimal_context(self):
        candles = daily_closes(100, 110)
        near_threshold = "110.0000000000000000000000000000000000000001"
        for index in (1, 2, 3, 4):
            candles[-1][index] = near_threshold
        market = FakeMarketData(candles)
        baseline = worker._signal(market)
        baseline_db = Path(self.temp.name) / "signal-context-a.sqlite3"
        baseline_action = run_once(
            db_path=baseline_db, client=FakeClient(candles)
        )
        original = getcontext().copy()
        try:
            getcontext().prec = 6
            getcontext().rounding = ROUND_DOWN
            changed = worker._signal(market)
            changed_db = Path(self.temp.name) / "signal-context-b.sqlite3"
            changed_action = run_once(
                db_path=changed_db, client=FakeClient(candles)
            )
        finally:
            setcontext(original)

        self.assertEqual(changed, baseline)
        self.assertEqual(changed_action, baseline_action)
        self.assertEqual(baseline_action["action"], "bought")
        with worker.closing(worker._connect(baseline_db)) as first, \
                worker.closing(worker._connect(changed_db)) as second:
            first_features = first.execute(
                "SELECT feature_json FROM worker_decisions"
            ).fetchone()[0]
            second_features = second.execute(
                "SELECT feature_json FROM worker_decisions"
            ).fetchone()[0]
        self.assertEqual(second_features, first_features)
        self.assertTrue(baseline["target_long"])
        self.assertGreater(baseline["momentum"], worker.MOMENTUM_THRESHOLD)

    def test_consecutive_daily_decisions_seal_once_and_gap_is_quarantined(self):
        client = FakeClient(daily_closes(100, 109, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "hold_cash"
        )

        client.daily = daily_closes(101, 110, final_day=201)
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            self.assertEqual(
                run_once(db_path=self.db_path, client=client)["action"], "hold_cash"
            )
            self.assertEqual(
                run_once(db_path=self.db_path, client=client)["action"], "waiting"
            )

        client.daily = daily_closes(102, 111, final_day=203)
        with patch.object(worker, "_now_ms", return_value=NOW_MS + 3 * DAY_MS):
            self.assertEqual(
                run_once(db_path=self.db_path, client=client)["action"], "hold_cash"
            )

        with worker.closing(worker._connect(self.db_path)) as db:
            labels = db.execute(
                "SELECT sample_json FROM worker_learning_daily_labels"
            ).fetchall()
            gaps = db.execute(
                "SELECT reason FROM worker_learning_daily_gaps"
            ).fetchall()
        self.assertEqual(len(labels), 1)
        sample = json.loads(labels[0]["sample_json"])
        self.assertEqual(
            sample["label_available_ts"] - sample["decision_ts"], DAY_MS
        )
        self.assertEqual(
            [row["reason"] for row in gaps], ["non_contiguous_daily_horizon"]
        )

    def test_failed_daily_label_capture_stays_pending_until_exact_replay(self):
        client = FakeClient(daily_closes(100, 109, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "hold_cash"
        )
        worker._refresh_learning_nonfatal(self.db_path)

        client.daily = daily_closes(100, 109, final_day=201)
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS), \
                patch.object(
                    worker.learning_store,
                    "capture_daily_label",
                    side_effect=RuntimeError("temporary label capture outage"),
                ):
            decision = run_once(db_path=self.db_path, client=client)
            still_blocked = worker._refresh_learning_nonfatal(self.db_path)

        self.assertEqual(decision["action"], "hold_cash")
        self.assertEqual(
            still_blocked["status"], "blocked_by_unresolved_learning_outbox"
        )
        self.assertFalse(still_blocked["proposal_ready_for_review"])
        self.assertEqual(
            still_blocked["evidence"]["unresolved_learning_outbox"], 1
        )
        self.assertEqual(
            still_blocked["evidence"]["source_learning_revision"],
            still_blocked["evidence"]["ingested_source_revision"],
        )
        raw = worker.sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                raw.execute("SELECT COUNT(*) FROM worker_decisions").fetchone()[0],
                2,
            )
            self.assertEqual(
                raw.execute(
                    "SELECT COUNT(*) FROM worker_learning_daily_labels"
                ).fetchone()[0],
                0,
            )
        finally:
            raw.close()

        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            worker._prepare_learning_before_decision(self.db_path)
            with worker.closing(worker._connect_read_only(self.db_path)) as db:
                label = db.execute(
                    """SELECT decision_candle_close_ms, label_candle_close_ms
                       FROM worker_learning_daily_labels"""
                ).fetchone()
                pending = worker._unresolved_learning_outbox_count(db)
        self.assertEqual(
            tuple(label),
            (200 * DAY_MS - 1, 201 * DAY_MS - 1),
        )
        self.assertEqual(pending, 0)
        caught_up_after_replay = worker.learning_snapshot(self.db_path)
        self.assertNotEqual(
            caught_up_after_replay["status"],
            "blocked_by_unresolved_learning_outbox",
        )
        self.assertEqual(
            caught_up_after_replay["evidence"]["source_learning_revision"],
            caught_up_after_replay["evidence"]["ingested_source_revision"],
        )
        recovered = worker._refresh_learning_nonfatal(self.db_path)
        self.assertNotEqual(
            recovered["status"], "blocked_by_unresolved_learning_outbox"
        )
        self.assertEqual(recovered["evidence"]["unresolved_learning_outbox"], 0)
        self.assertEqual(
            recovered["evidence"]["source_learning_revision"],
            recovered["evidence"]["ingested_source_revision"],
        )

    def test_pending_candidate_transition_defers_capture_without_source_write(self):
        client = FakeClient(daily_closes(100, 109, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "hold_cash"
        )
        transition = {
            "schema": worker.learning_store.SCHEMA_VERSION,
            "kind": "testnet_candidate_source_transition",
            "retiring_candidate_sha256": None,
            "replacement_candidate_sha256": "f" * 64,
            "break_gap_id": None,
            "break_gap_sha256": None,
            "boundary_sample_sha256": None,
            "boundary_decision_ts": None,
        }
        worker.learning_store._stage_source_candidate_transition(
            self.db_path, transition, NOW_MS + 1
        )
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            signal = worker._signal(
                FakeMarketData(daily_closes(100, 109, final_day=201))
            )
            with worker.closing(worker._connect(self.db_path)) as db, patch.object(
                worker.learning_store,
                "capture_daily_label",
                side_effect=AssertionError("capture reached stale source binding"),
            ) as capture:
                persisted = worker._persist_decision(
                    db,
                    signal,
                    "hold_cash",
                    None,
                    learning_refresh_healthy=True,
                )
                pending = db.execute(
                    """SELECT operation, resolved_ms
                       FROM worker_learning_outbox
                       WHERE operation='capture_daily_label'"""
                ).fetchone()
                transition_count = int(db.execute(
                    """SELECT COUNT(*)
                       FROM worker_learning_pending_candidate_transition"""
                ).fetchone()[0])

        self.assertTrue(persisted)
        capture.assert_not_called()
        self.assertEqual(tuple(pending), ("capture_daily_label", None))
        self.assertEqual(transition_count, 1)

    def test_outbox_replay_stops_at_first_failed_causal_event(self):
        client = FakeClient(daily_closes(100, 109, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "hold_cash"
        )
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            signal_201 = worker._signal(
                FakeMarketData(daily_closes(100, 109, final_day=201))
            )
        with patch.object(worker, "_now_ms", return_value=NOW_MS + 2 * DAY_MS):
            signal_202 = worker._signal(
                FakeMarketData(daily_closes(100, 109, final_day=202))
            )
        with worker.closing(worker._connect(self.db_path)) as db, \
                patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS), \
                patch.object(
                    worker.learning_store,
                    "capture_daily_label",
                    side_effect=RuntimeError("capture outage"),
                ):
            self.assertTrue(worker._persist_decision(
                db, signal_201, "hold_cash", None
            ))
            self.assertTrue(worker._persist_decision(
                db, signal_202, "hold_cash", None
            ))

        real_capture = worker.learning_store.capture_daily_label
        calls = []

        def fail_first(*args, **kwargs):
            calls.append(int(args[1]["candle_close_ms"]))
            if len(calls) == 1:
                raise RuntimeError("first replay still unavailable")
            return real_capture(*args, **kwargs)

        with patch.object(worker, "_now_ms", return_value=NOW_MS + 2 * DAY_MS), \
                patch.object(
                    worker.learning_store,
                    "capture_daily_label",
                    side_effect=fail_first,
                ), worker.closing(worker._connect(self.db_path)) as db:
            worker._replay_learning_outbox(db)
            attempts_after_failure = [
                int(row[0])
                for row in db.execute(
                    """SELECT attempt_count FROM worker_learning_outbox
                       WHERE resolved_ms IS NULL ORDER BY rowid"""
                )
            ]
            labels_after_failure = db.execute(
                "SELECT COUNT(*) FROM worker_learning_daily_labels"
            ).fetchone()[0]
            gaps_after_failure = db.execute(
                "SELECT COUNT(*) FROM worker_learning_daily_gaps"
            ).fetchone()[0]
            repaired = worker._replay_learning_outbox(db)
            labels = [
                tuple(row)
                for row in db.execute(
                    """SELECT decision_candle_close_ms, label_candle_close_ms
                       FROM worker_learning_daily_labels
                       ORDER BY decision_candle_close_ms"""
                )
            ]
            unresolved = worker._unresolved_learning_outbox_count(db)

        self.assertEqual(attempts_after_failure, [2, 1])
        self.assertEqual(labels_after_failure, 0)
        self.assertEqual(gaps_after_failure, 0)
        self.assertEqual(repaired, 2)
        self.assertEqual(
            labels,
            [
                (200 * DAY_MS - 1, 201 * DAY_MS - 1),
                (201 * DAY_MS - 1, 202 * DAY_MS - 1),
            ],
        )
        self.assertEqual(unresolved, 0)
        self.assertEqual(calls, [201 * DAY_MS - 1] * 2 + [202 * DAY_MS - 1])

    def test_policy_config_rejects_execution_scope_or_trained_rule_changes(self):
        mutations = (
            ("environment", "https://api.binance.com"),
            ("real_money_eligible", True),
            ("policy_id", "btc_daily_momentum_30d_t05_testnet_v1"),
            ("threshold", "0.05"),
            ("quote_usdt", "25"),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                value = json.loads(json.dumps(worker.POLICY_CONFIG))
                if field == "threshold":
                    value["rule"][field] = replacement
                elif field == "quote_usdt":
                    value["order"][field] = replacement
                else:
                    value[field] = replacement
                path = Path(self.temp.name) / f"invalid-{field}.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    worker._load_policy_config(path)

    def test_ten_percent_threshold_is_strict(self):
        exactly_ten = FakeClient(daily_closes(100, 110))
        above_ten = FakeClient(daily_closes(100, 110.01))

        self.assertEqual(
            run_once(db_path=self.db_path, client=exactly_ten)["action"],
            "hold_cash",
        )
        second_db = Path(self.temp.name) / "above-threshold.sqlite3"
        self.assertEqual(
            run_once(db_path=second_db, client=above_ten)["action"],
            "bought",
        )

    def test_ledger_refuses_policy_spec_mismatch(self):
        with worker.closing(worker._connect(self.db_path)) as db:
            db.execute(
                "UPDATE worker_state SET policy_spec_hash=? WHERE singleton=1",
                ("0" * 64,),
            )
        with self.assertRaisesRegex(worker.WorkerHalt, "does not match"):
            worker._connect(self.db_path)

    def test_legacy_worker_state_schema_adds_nullable_account_binding(self):
        with worker.closing(worker._connect(self.db_path)):
            pass
        with worker.closing(worker.sqlite3.connect(self.db_path)) as db:
            db.execute(
                "ALTER TABLE worker_state DROP COLUMN api_key_fingerprint_sha256"
            )

        with worker.closing(worker._connect(self.db_path)) as db:
            columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(worker_state)")
            }
            fingerprint = db.execute(
                "SELECT api_key_fingerprint_sha256 FROM worker_state WHERE singleton=1"
            ).fetchone()[0]

        self.assertIn("api_key_fingerprint_sha256", columns)
        self.assertIsNone(fingerprint)

    def test_signal_candles_and_execution_use_separate_clients(self):
        execution_client = FakeClient(daily_closes(100, 109))
        public_market = execution_client.market_data

        result = run_once(
            db_path=self.db_path,
            client=execution_client,
            market_data_client=public_market,
        )

        self.assertEqual(result["action"], "hold_cash")
        self.assertEqual(public_market.kline_calls, 1)
        self.assertEqual(execution_client.open_order_calls, 1)
        self.assertFalse(hasattr(public_market, "place_market_buy"))

    def test_first_signed_use_binds_account_without_status_leak(self):
        client = FakeClient(daily_closes(100, 109))

        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "hold_cash"
        )

        expected = worker.hashlib.sha256(TEST_API_KEY_A.encode("ascii")).hexdigest()
        with worker.closing(worker._connect(self.db_path)) as db:
            stored = db.execute(
                "SELECT api_key_fingerprint_sha256 FROM worker_state WHERE singleton=1"
            ).fetchone()[0]
        self.assertEqual(stored, expected)
        status = worker.status_snapshot(self.db_path, self.lock_path)
        rendered = json.dumps(status, sort_keys=True)
        self.assertTrue(status["account_bound"])
        self.assertNotIn("api_key_fingerprint_sha256", status)
        self.assertNotIn(expected, rendered)
        self.assertNotIn(TEST_API_KEY_A, rendered)

        other = FakeClient(daily_closes(100, 109), api_key=TEST_API_KEY_B)
        with self.assertRaisesRegex(worker.AccountBindingError, "does not match"):
            run_once(db_path=self.db_path, client=other)

    def test_default_clients_are_testnet_execution_and_public_market_data(self):
        execution_client = FakeClient(daily_closes(100, 109))
        public_market = execution_client.market_data
        with patch.object(
            worker.execution, "Client", return_value=execution_client
        ) as execution_factory, patch.object(
            worker.execution,
            "PublicMarketDataClient",
            return_value=public_market,
        ) as market_factory:
            result = worker.run_once(db_path=self.db_path)

        self.assertEqual(result["action"], "hold_cash")
        execution_factory.assert_called_once_with()
        market_factory.assert_called_once_with()
        self.assertEqual(public_market.kline_calls, 1)

    def test_buy_transition_uses_fixed_ten_usdt_and_is_not_duplicated(self):
        client = FakeClient(daily_closes(100, 121))

        bought = run_once(db_path=self.db_path, client=client)
        waiting = run_once(db_path=self.db_path, client=client)

        self.assertEqual(bought["action"], "bought")
        self.assertEqual(waiting["action"], "waiting")
        self.assertEqual(len(client.buy_calls), 1)
        self.assertEqual(client.buy_calls[0][0], "10")
        self.assertTrue(client.buy_calls[0][2].startswith("qr-b-"))
        status = worker.status_snapshot(self.db_path, Path(self.temp.name) / "lock")
        self.assertEqual(status["position"], "long")
        self.assertEqual(status["filled_orders"], 1)

    def test_restart_reconciles_pending_client_id_without_second_post(self):
        first = FakeClient(daily_closes(100, 125))
        first.buy_error = TimeoutError("response lost")
        first.lookup_error = LookupError("temporarily not found")

        ambiguous = run_once(db_path=self.db_path, client=first)
        self.assertEqual(ambiguous["action"], "halted")
        self.assertEqual(len(first.buy_calls), 1)
        client_id = first.buy_calls[0][2]

        restarted = FakeClient(daily_closes(100, 125))
        restarted.orders[client_id] = restarted.filled_order(
            client_id, "BUY", "0.001", "10"
        )
        restarted.query_omit_fills = True
        reconciled = run_once(db_path=self.db_path, client=restarted)

        self.assertEqual(reconciled["action"], "bought")
        self.assertEqual(restarted.buy_calls, [])
        self.assertEqual(restarted.lookup_calls, [(client_id, worker.SYMBOL)])
        status = worker.status_snapshot(self.db_path, Path(self.temp.name) / "lock")
        self.assertFalse(status["halted"])
        self.assertIsNone(status["pending_client_id"])
        self.assertEqual(status["filled_orders"], 1)

    def test_sell_transition_sells_only_tracked_worker_quantity(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        bought = run_once(db_path=self.db_path, client=client)
        self.assertEqual(bought["action"], "bought")

        # A new completed candle whose 30-day momentum is no longer above 10%.
        client.daily = daily_closes(120, 100, final_day=201)
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            sold = run_once(db_path=self.db_path, client=client)

        self.assertEqual(sold["action"], "sold")
        self.assertEqual(len(client.sell_calls), 1)
        self.assertEqual(client.sell_calls[0][0], "0.001")
        self.assertTrue(client.sell_calls[0][2].startswith("qr-s-"))
        status = worker.status_snapshot(self.db_path, Path(self.temp.name) / "lock")
        self.assertEqual(status["position"], "cash")
        self.assertEqual(status["tracked_position_qty"], "0.000")
        self.assertEqual(status["filled_orders"], 2)
        self.assertEqual(status["realized_pnl_usdt"], "1.9")
        self.assertEqual(status["completed_round_trips"], 1)
        self.assertTrue(status["pnl_complete"])
        with worker.closing(worker._connect(self.db_path)) as db:
            evidence = db.execute(
                """SELECT status, exact_pnl, realized_pnl_usdt, net_return
                   FROM worker_learning_round_trips
                   ORDER BY created_ms, status"""
            ).fetchall()
        self.assertEqual([row["status"] for row in evidence], ["open", "closed"])
        self.assertEqual(evidence[1]["exact_pnl"], 1)
        self.assertEqual(evidence[1]["realized_pnl_usdt"], "1.9")
        self.assertEqual(evidence[1]["net_return"], "0.19")

    def test_failed_managed_buy_learning_event_replays_before_legacy_adoption(self):
        warmup = FakeClient(daily_closes(100, 109, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=warmup)["action"], "hold_cash"
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            db.execute(
                """UPDATE worker_learning_registrations
                   SET true_forward_decision_created_cutoff_ms=?,
                       freeze_cutoff_candle_ms=?, frozen_candidate_json='{}',
                       frozen_candidate_sha256=?
                   WHERE policy=? AND model_version=?""",
                (
                    NOW_MS - 1,
                    NOW_MS - DAY_MS,
                    "f" * 64,
                    worker.POLICY,
                    worker.POLICY_SPEC_HASH,
                ),
            )

        client = FakeClient(daily_closes(100, 125, final_day=201))
        healthy = {
            "status": "collecting",
            "refresh_health": {
                "healthy": True,
                "last_attempt_failed": False,
            },
        }
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS), \
                patch.object(
                    worker,
                    "_prepare_learning_before_decision",
                    return_value=healthy,
                ), patch.object(
            worker.learning_store,
            "open_round_trip",
            side_effect=RuntimeError("temporary source write outage"),
        ):
            bought = run_once(db_path=self.db_path, client=client)

        self.assertEqual(bought["action"], "bought")
        raw = worker.sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                raw.execute(
                    """SELECT COUNT(*) FROM worker_learning_outbox
                       WHERE resolved_ms IS NULL"""
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                raw.execute(
                    "SELECT COUNT(*) FROM worker_learning_round_trips"
                ).fetchone()[0],
                0,
            )
        finally:
            raw.close()

        with worker.closing(worker._connect(self.db_path)) as db:
            first_replay = worker._replay_learning_outbox(db)
            repaired = db.execute(
                """SELECT source_kind, entry_candidate_bound
                   FROM worker_learning_round_trips WHERE status='open'"""
            ).fetchone()
            pending = worker._unresolved_learning_outbox_count(db)
            second_replay = worker._replay_learning_outbox(db)
            record_count = db.execute(
                "SELECT COUNT(*) FROM worker_learning_round_trips"
            ).fetchone()[0]

        self.assertEqual(repaired["source_kind"], "managed_buy_fill")
        self.assertEqual(repaired["entry_candidate_bound"], 1)
        self.assertEqual(first_replay, 1)
        self.assertEqual(pending, 0)
        self.assertEqual(second_replay, 0)
        self.assertEqual(record_count, 1)

    def test_failed_losing_close_learning_event_is_durably_replayed(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        client.daily = daily_closes(120, 100, final_day=201)
        client.sell_quote = "8"
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS), \
                patch.object(
                    worker.learning_store,
                    "close_round_trip",
                    side_effect=RuntimeError("temporary close evidence outage"),
                ):
            sold = run_once(db_path=self.db_path, client=client)

        self.assertEqual(sold["action"], "sold")
        self.assertEqual(sold["realized_pnl_usdt"], "-2.1")
        raw = worker.sqlite3.connect(self.db_path)
        try:
            outbox = raw.execute(
                """SELECT operation, resolved_ms FROM worker_learning_outbox
                   WHERE operation='close_round_trip'"""
            ).fetchone()
            statuses = raw.execute(
                """SELECT status FROM worker_learning_round_trips
                   ORDER BY created_ms, status"""
            ).fetchall()
        finally:
            raw.close()
        self.assertEqual(outbox, ("close_round_trip", None))
        self.assertEqual([row[0] for row in statuses], ["open"])

        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            worker._prepare_learning_before_decision(self.db_path)
            with worker.closing(worker._connect_read_only(self.db_path)) as db:
                repaired = db.execute(
                    """SELECT status, exact_pnl, realized_pnl_usdt
                       FROM worker_learning_round_trips
                       WHERE status='closed'"""
                ).fetchone()
                pending = worker._unresolved_learning_outbox_count(db)

        self.assertEqual(repaired["status"], "closed")
        self.assertEqual(repaired["exact_pnl"], 1)
        self.assertEqual(repaired["realized_pnl_usdt"], "-2.1")
        self.assertEqual(pending, 0)

    def test_existing_managed_position_is_adopted_without_inventing_features(self):
        client = FakeClient(daily_closes(100, 125))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            db.execute("DELETE FROM worker_learning_round_trips")
            db.execute(
                "UPDATE worker_decisions SET feature_schema=NULL, feature_json=NULL"
            )

        worker._prepare_learning_before_decision(self.db_path)
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            adopted = db.execute(
                """SELECT status, source_kind, entry_feature_status,
                          entry_feature_json
                   FROM worker_learning_round_trips"""
            ).fetchone()
        self.assertEqual(adopted["status"], "open")
        self.assertEqual(adopted["source_kind"], "legacy_open_position_adopted")
        self.assertEqual(
            adopted["entry_feature_status"], "diagnostic_missing_legacy_feature"
        )
        self.assertIsNone(adopted["entry_feature_json"])

    def test_btc_sell_commission_reduces_tracked_quantity_and_cost(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        client.buy_quantity = "0.0011"
        client.buy_commission = "0.0000005"
        client.buy_commission_asset = "BTC"
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        self.assertEqual(
            worker.status_snapshot(self.db_path, self.lock_path)[
                "tracked_position_qty"
            ],
            "0.0010995",
        )

        client.sell_commission = "0.0000005"
        client.sell_commission_asset = "BTC"
        client.daily = daily_closes(120, 100, final_day=201)
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "sold")
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertEqual(status["tracked_position_qty"], "0.0000000")
        self.assertEqual(status["tracked_position_cost_usdt"], "0")
        self.assertEqual(status["realized_pnl_usdt"], "2")
        self.assertTrue(status["pnl_complete"])

    def test_fill_accounting_is_independent_of_process_decimal_context(self):
        def execute_round_trip(db_path):
            client = FakeClient(daily_closes(100, 125, final_day=200))
            client.buy_quantity = "0.001999999"
            buy = run_once(db_path=db_path, client=client)
            client.daily = daily_closes(120, 100, final_day=201)
            with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
                sell = run_once(db_path=db_path, client=client)
            status = worker.status_snapshot(db_path, self.lock_path)
            with worker.closing(worker._connect(db_path)) as db:
                sell_row = dict(db.execute(
                    """SELECT executed_qty, realized_pnl_usdt
                       FROM worker_order_intents WHERE side='SELL'"""
                ).fetchone())
            return buy, sell, {
                "tracked_position_qty": status["tracked_position_qty"],
                "tracked_position_cost_usdt": status[
                    "tracked_position_cost_usdt"
                ],
                "realized_pnl_usdt": status["realized_pnl_usdt"],
                "completed_round_trips": status["completed_round_trips"],
                "sell_requested_qty": client.sell_calls[0][0],
                "sell_row": sell_row,
            }

        baseline = execute_round_trip(Path(self.temp.name) / "decimal-a.sqlite3")
        original = getcontext().copy()
        try:
            getcontext().prec = 3
            getcontext().rounding = ROUND_DOWN
            changed = execute_round_trip(
                Path(self.temp.name) / "decimal-b.sqlite3"
            )
        finally:
            setcontext(original)

        self.assertEqual(changed, baseline)
        self.assertEqual(baseline[2]["sell_requested_qty"], "0.001999")

    def test_third_asset_commission_persists_and_blocks_exact_pnl_claim(self):
        client = FakeClient(daily_closes(100, 125))
        client.buy_commission = "0.01"
        client.buy_commission_asset = "BNB"

        result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "halted")
        self.assertEqual(result["execution_action"], "bought")
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertTrue(status["halted"])
        self.assertFalse(status["pnl_complete"])
        self.assertIn("BNB=0.01", status["pnl_incomplete_reason"])
        self.assertEqual(status["realized_pnl_usdt"], "0")
        with worker.closing(worker._connect(self.db_path)) as db:
            stored = db.execute(
                "SELECT commission_by_asset FROM worker_order_intents"
            ).fetchone()[0]
        self.assertEqual(stored, '{"BNB":"0.01"}')

    def test_mismatched_pending_order_halts_instead_of_posting(self):
        first = FakeClient(daily_closes(100, 125))
        first.buy_error = TimeoutError("response lost")
        first.lookup_error = LookupError("temporarily not found")
        run_once(db_path=self.db_path, client=first)
        client_id = first.buy_calls[0][2]

        restarted = FakeClient(daily_closes(100, 125))
        wrong = restarted.filled_order(client_id, "BUY", "0.001", "10")
        wrong["symbol"] = "ETHUSDT"
        restarted.orders[client_id] = wrong
        result = run_once(db_path=self.db_path, client=restarted)

        self.assertEqual(result["action"], "halted")
        self.assertIn("symbol mismatch", result["reason"])
        self.assertEqual(restarted.buy_calls, [])

    def test_untracked_open_order_halts_before_market_data_evaluation(self):
        client = FakeClient(daily_closes(100, 125))
        client.open = [{"symbol": worker.SYMBOL, "clientOrderId": "manual-order"}]

        result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "halted")
        self.assertIn("Untracked", result["reason"])
        self.assertEqual(client.kline_calls, 0)
        self.assertEqual(client.buy_calls, [])

    def test_buy_halts_when_free_usdt_is_insufficient(self):
        client = FakeClient(daily_closes(100, 125))
        client.free_usdt = "9.99"

        result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "halted")
        self.assertIn("Insufficient free Testnet USDT", result["reason"])
        self.assertEqual(client.buy_calls, [])

    def test_sell_halts_when_free_btc_is_insufficient(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        client.daily = daily_closes(120, 100, final_day=201)
        client.free_btc = "0"

        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "halted")
        self.assertIn("Insufficient free Testnet BTC", result["reason"])
        self.assertEqual(client.sell_calls, [])

    def test_daily_window_must_be_contiguous_and_fresh(self):
        gapped = daily_closes(100, 125)
        for candle in gapped[15:]:
            candle[0] += DAY_MS
            candle[6] += DAY_MS
        with self.assertRaisesRegex(worker.WorkerHalt, "exactly contiguous"):
            run_once(db_path=self.db_path, client=FakeClient(gapped))

        stale = daily_closes(100, 125, final_day=198)
        with self.assertRaisesRegex(worker.WorkerHalt, "stale"):
            run_once(
                db_path=Path(self.temp.name) / "stale.sqlite3",
                client=FakeClient(stale),
            )

    def test_terminal_order_with_partial_fill_stays_pending_and_halts(self):
        first = FakeClient(daily_closes(100, 125))
        first.buy_error = TimeoutError("response lost")
        first.lookup_error = LookupError("temporarily not found")
        run_once(db_path=self.db_path, client=first)
        client_id = first.buy_calls[0][2]

        restarted = FakeClient(daily_closes(100, 125))
        restarted.orders[client_id] = {
            "symbol": worker.SYMBOL,
            "orderId": 77,
            "clientOrderId": client_id,
            "status": "CANCELED",
            "side": "BUY",
            "executedQty": "0.0005",
            "cummulativeQuoteQty": "5",
        }
        result = run_once(db_path=self.db_path, client=restarted)

        self.assertEqual(result["action"], "halted")
        self.assertIn("partial execution", result["reason"])
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertEqual(status["pending_client_id"], client_id)
        self.assertTrue(status["halted"])

    def test_start_allows_halted_pending_reconciliation_and_waits_for_lock(self):
        first = FakeClient(daily_closes(100, 125))
        first.buy_error = TimeoutError("response lost")
        first.lookup_error = LookupError("temporarily not found")
        run_once(db_path=self.db_path, client=first)

        child = Mock()
        child.poll.return_value = None
        with patch.object(
            worker,
            "_lock_active",
            side_effect=[False, False, True, True, True, True, True],
        ), patch.object(worker.subprocess, "Popen", return_value=child), \
                patch.object(worker.time, "sleep"):
            result = worker.control(
                "start", db_path=self.db_path, lock_path=self.lock_path
            )

        self.assertTrue(result["running"])
        self.assertTrue(result["desired_running"])

    def test_unhealthy_learning_defers_pending_fill_evidence_after_preparation(self):
        first = FakeClient(daily_closes(100, 125))
        first.buy_error = TimeoutError("response lost")
        first.lookup_error = LookupError("temporarily unavailable")
        self.assertEqual(
            run_once(db_path=self.db_path, client=first)["action"], "halted"
        )
        client_id = first.buy_calls[0][2]
        with worker.closing(worker._connect(self.db_path)) as db:
            worker.learning_store.mark_refresh_failure(
                db, "candidate transition recovery unavailable", NOW_MS + 1
            )

        restarted = FakeClient(daily_closes(100, 125))
        restarted.orders[client_id] = restarted.filled_order(
            client_id, "BUY", "0.001", "10"
        )
        events = []
        real_lookup = restarted.order_by_client_id

        def lookup_after_prepare(*args, **kwargs):
            events.append("reconcile")
            return real_lookup(*args, **kwargs)

        unhealthy = {
            "status": "learning_refresh_unhealthy",
            "refresh_health": {
                "healthy": False,
                "last_attempt_failed": True,
            },
        }

        def prepare(_db_path):
            events.append("prepare")
            return unhealthy

        restarted.order_by_client_id = lookup_after_prepare
        with patch.object(
            worker, "_prepare_learning_before_decision", side_effect=prepare
        ):
            reconciled = run_once(db_path=self.db_path, client=restarted)

        self.assertEqual(reconciled["action"], "bought")
        self.assertEqual(events[:2], ["prepare", "reconcile"])
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            pending = db.execute(
                """SELECT operation, resolved_ms
                   FROM worker_learning_outbox
                   WHERE operation='open_round_trip'"""
            ).fetchone()
            evidence_count = int(db.execute(
                "SELECT COUNT(*) FROM worker_learning_round_trips"
            ).fetchone()[0])
        self.assertEqual(tuple(pending), ("open_round_trip", None))
        self.assertEqual(evidence_count, 0)

    def test_start_failure_clears_desired_running(self):
        with patch.object(worker, "_lock_active", return_value=False), \
                patch.object(worker.subprocess, "Popen", side_effect=OSError("boom")):
            with self.assertRaisesRegex(worker.WorkerHalt, "Unable to start"):
                worker.control("start", db_path=self.db_path, lock_path=self.lock_path)

        self.assertFalse(worker.status_snapshot(self.db_path, self.lock_path)[
            "desired_running"
        ])

    def test_child_exit_during_startup_clears_desired_running(self):
        child = Mock()
        child.poll.return_value = 2
        with patch.object(
            worker,
            "_lock_active",
            side_effect=[False, False, False, False, False, False, False],
        ), \
                patch.object(worker.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(worker.WorkerHalt, "did not remain running"):
                worker.control("start", db_path=self.db_path, lock_path=self.lock_path)

        self.assertFalse(worker.status_snapshot(self.db_path, self.lock_path)[
            "desired_running"
        ])

    def test_start_rejects_already_running_worker_environment_replacement(self):
        with patch.object(worker, "_lock_active", return_value=True), \
                patch.object(worker.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(worker.WorkerHalt, "already running"):
                worker.control("start", db_path=self.db_path, lock_path=self.lock_path)
        popen.assert_not_called()

    def test_start_rejects_api_key_from_another_bound_ledger(self):
        account_a = FakeClient(daily_closes(100, 109), api_key=TEST_API_KEY_A)
        run_once(db_path=self.db_path, client=account_a)
        account_b = FakeClient(daily_closes(100, 109), api_key=TEST_API_KEY_B)

        with patch.object(worker, "_lock_active", return_value=False), \
                patch.object(worker.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(
                    worker.AccountBindingError, "does not match"
                ):
            worker.control(
                "start",
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account_b,
            )

        popen.assert_not_called()
        self.assertFalse(
            worker.status_snapshot(self.db_path, self.lock_path)["desired_running"]
        )

    def test_bound_account_check_matches_without_mutating_ledger(self):
        account_a = FakeClient(daily_closes(100, 109), api_key=TEST_API_KEY_A)
        run_once(db_path=self.db_path, client=account_a)

        result = worker.validate_bound_account(
            db_path=self.db_path, client=account_a
        )

        self.assertTrue(result["validated"])
        self.assertTrue(result["account_bound"])
        self.assertTrue(result["api_key_matches_ledger"])
        account_b = FakeClient(daily_closes(100, 109), api_key=TEST_API_KEY_B)
        with self.assertRaisesRegex(worker.AccountBindingError, "does not match"):
            worker.validate_bound_account(
                db_path=self.db_path, client=account_b
            )

    def test_learning_maintenance_lease_requires_stopped_worker_and_holds_locks(self):
        with worker.closing(worker._connect(self.db_path)):
            pass
        control_path = worker._control_lock_path(self.lock_path)
        account_path = worker._account_lock_path(self.lock_path)

        with worker.learning_maintenance_lease(
            db_path=self.db_path, lock_path=self.lock_path
        ) as status:
            self.assertFalse(status["running"])
            self.assertTrue(control_path.exists())
            self.assertTrue(account_path.exists())

        self.assertTrue(control_path.exists())
        self.assertTrue(account_path.exists())
        self.assertFalse(worker._lock_active(control_path))
        self.assertFalse(worker._lock_active(account_path))
        worker._set_desired(self.db_path, True)
        with self.assertRaisesRegex(worker.WorkerHalt, "fully stopped"):
            with worker.learning_maintenance_lease(
                db_path=self.db_path, lock_path=self.lock_path
            ):
                pass

    def test_learning_upgrade_lease_restores_running_intent_after_success(self):
        account = FakeClient(daily_closes(100, 109))
        run_once(db_path=self.db_path, client=account)
        worker._set_desired(self.db_path, True)

        def desired_worker_lock(path):
            if Path(path).resolve() != self.lock_path.resolve():
                return False
            with worker.closing(worker._connect_read_only(self.db_path)) as db:
                return bool(worker._state(db)["desired_running"])

        recoveries = []

        def recover(action, **kwargs):
            self.assertEqual(action, "recover")
            recoveries.append(action)
            worker._set_desired(self.db_path, True)
            return worker.status_snapshot(self.db_path, self.lock_path)

        with patch.object(
            worker, "_lock_active", side_effect=desired_worker_lock
        ), patch.object(worker, "_control_locked", side_effect=recover):
            with worker.learning_upgrade_lease(
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account,
            ) as lease:
                self.assertTrue(lease["restart_required"])
                with worker.closing(worker._connect_read_only(self.db_path)) as db:
                    self.assertFalse(worker._state(db)["desired_running"])

        self.assertEqual(recoveries, ["recover"])
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            self.assertTrue(worker._state(db)["desired_running"])

    def test_learning_upgrade_lease_preserves_initial_stopped_state(self):
        account = FakeClient(daily_closes(100, 109))
        run_once(db_path=self.db_path, client=account)
        with patch.object(worker, "_control_locked") as control_locked:
            with worker.learning_upgrade_lease(
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account,
            ) as lease:
                self.assertFalse(lease["restart_required"])
        control_locked.assert_not_called()
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            self.assertFalse(worker._state(db)["desired_running"])

    def test_learning_upgrade_lease_restores_intent_after_body_error(self):
        account = FakeClient(daily_closes(100, 109))
        run_once(db_path=self.db_path, client=account)
        worker._set_desired(self.db_path, True)

        def desired_worker_lock(path):
            if Path(path).resolve() != self.lock_path.resolve():
                return False
            with worker.closing(worker._connect_read_only(self.db_path)) as db:
                return bool(worker._state(db)["desired_running"])

        def recover(action, **kwargs):
            worker._set_desired(self.db_path, True)
            return worker.status_snapshot(self.db_path, self.lock_path)

        with patch.object(
            worker, "_lock_active", side_effect=desired_worker_lock
        ), patch.object(worker, "_control_locked", side_effect=recover), \
                self.assertRaisesRegex(RuntimeError, "seed failed"):
            with worker.learning_upgrade_lease(
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account,
            ):
                raise RuntimeError("seed failed")

        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            self.assertTrue(worker._state(db)["desired_running"])

    def test_learning_upgrade_lease_checks_gate_and_signed_auth_before_stop(self):
        account = FakeClient(daily_closes(100, 109))
        run_once(db_path=self.db_path, client=account)
        worker._set_desired(self.db_path, True)

        def desired_worker_lock(path):
            if Path(path).resolve() != self.lock_path.resolve():
                return False
            with worker.closing(worker._connect_read_only(self.db_path)) as db:
                return bool(worker._state(db)["desired_running"])

        with patch.object(
            worker, "_lock_active", side_effect=desired_worker_lock
        ), patch.dict(
            os.environ, {"BINANCE_TESTNET_WORKER_ENABLED": "false"}, clear=False
        ), self.assertRaisesRegex(worker.WorkerHalt, "fail-closed"):
            with worker.learning_upgrade_lease(
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account,
            ):
                pass
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            self.assertTrue(worker._state(db)["desired_running"])

        with patch.object(
            worker, "_lock_active", side_effect=desired_worker_lock
        ), patch.object(account, "account", side_effect=RuntimeError("bad secret")), \
                self.assertRaisesRegex(worker.WorkerHalt, "signed account proof"):
            with worker.learning_upgrade_lease(
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account,
            ):
                pass
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            self.assertTrue(worker._state(db)["desired_running"])

    def test_concurrent_stop_wins_after_atomic_learning_upgrade(self):
        account = FakeClient(daily_closes(100, 109))
        run_once(db_path=self.db_path, client=account)
        worker._set_desired(self.db_path, True)

        def desired_worker_lock(path):
            if Path(path).resolve() != self.lock_path.resolve():
                return False
            with worker.closing(worker._connect_read_only(self.db_path)) as db:
                return bool(worker._state(db)["desired_running"])

        original_control_locked = worker._control_locked

        def recover(action, **kwargs):
            if action == "recover":
                worker._set_desired(self.db_path, True)
                return worker.status_snapshot(self.db_path, self.lock_path)
            return original_control_locked(action, **kwargs)

        with patch.object(
            worker, "_lock_active", side_effect=desired_worker_lock
        ), patch.object(worker, "_control_locked", side_effect=recover):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with worker.learning_upgrade_lease(
                    db_path=self.db_path,
                    lock_path=self.lock_path,
                    client=account,
                ):
                    later_stop = pool.submit(
                        worker.control,
                        "stop",
                        db_path=self.db_path,
                        lock_path=self.lock_path,
                    )
                    time.sleep(0.1)
                    self.assertFalse(later_stop.done())
                later_stop.result(timeout=5)

        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            self.assertFalse(worker._state(db)["desired_running"])

    def test_process_lock_has_exactly_one_concurrent_owner(self):
        lock_path = Path(self.temp.name) / "contended.lock"
        barrier = threading.Barrier(2)

        def contend():
            barrier.wait()
            try:
                with worker._process_lock(lock_path):
                    time.sleep(0.2)
                    return "acquired"
            except worker.WorkerHalt:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: contend(), range(2)))

        self.assertEqual(sorted(outcomes), ["acquired", "blocked"])
        self.assertTrue(lock_path.exists())
        self.assertFalse(worker._lock_active(lock_path))

    def test_advisory_lock_honors_live_legacy_pid_metadata(self):
        lock_path = Path(self.temp.name) / "legacy.lock"
        child_code = (
            "import json,os,sys,time; "
            "path=sys.argv[1]; "
            "fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY); "
            "os.write(fd,json.dumps({'pid':os.getpid(),'token':'legacy',"
            "'started_ms':1}).encode('utf-8')); "
            "os.close(fd); print('ready',flush=True); time.sleep(30)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(lock_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertTrue(worker._lock_active(lock_path))
            with self.assertRaisesRegex(worker.WorkerHalt, "already running"):
                with worker._process_lock(lock_path):
                    pass
        finally:
            child.terminate()
            child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

        self.assertFalse(worker._lock_active(lock_path))
        with worker._process_lock(lock_path):
            self.assertTrue(worker._lock_active(lock_path))

    def test_recover_rearms_running_worker_without_spawning_duplicate(self):
        account = FakeClient(daily_closes(100, 109), api_key=TEST_API_KEY_A)
        run_once(db_path=self.db_path, client=account)
        worker._set_desired(self.db_path, False)
        account_lock = worker._account_lock_path(self.lock_path)

        with worker._process_lock(account_lock), worker._process_lock(
            self.lock_path
        ), patch.object(worker.subprocess, "Popen") as popen:
            recovered = worker.control(
                "recover",
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account,
            )

        self.assertTrue(recovered["running"])
        self.assertTrue(
            worker.status_snapshot(self.db_path, self.lock_path)["desired_running"]
        )
        popen.assert_not_called()
        worker._set_desired(self.db_path, False)

    def test_start_rejects_policy_config_changed_after_import(self):
        changed = json.loads(json.dumps(worker.POLICY_CONFIG))
        changed["model_version"] = "f" * 64
        with patch.object(worker, "_load_policy_config", return_value=changed), \
                patch.object(worker.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(worker.WorkerHalt, "changed after"):
                worker.control(
                    "start", db_path=self.db_path, lock_path=self.lock_path
                )
        popen.assert_not_called()

    def test_control_mutex_rejects_overlapping_control_action(self):
        with worker._control_mutex(self.lock_path), patch.object(
            worker, "STARTUP_WAIT_SECONDS", 0
        ):
            with self.assertRaisesRegex(worker.WorkerHalt, "control action"):
                with worker._control_mutex(self.lock_path):
                    pass

    def test_default_control_mutex_path_is_stable_across_policy_ledgers(self):
        self.assertEqual(
            worker._control_lock_path(worker.LOCK_PATH),
            worker.CONTROL_LOCK_PATH,
        )
        custom = Path(self.temp.name) / "another-policy.lock"
        self.assertNotEqual(worker._control_lock_path(custom), worker.CONTROL_LOCK_PATH)

    def test_default_account_lease_is_stable_across_policy_ledgers(self):
        self.assertEqual(
            worker._account_lock_path(worker.LOCK_PATH),
            worker.ACCOUNT_LOCK_PATH,
        )
        old_policy = worker.STATE_DIR / "binance-testnet-old-policy.lock"
        self.assertEqual(worker._account_lock_path(old_policy), worker.ACCOUNT_LOCK_PATH)
        custom = Path(self.temp.name) / "another-policy.lock"
        self.assertNotEqual(worker._account_lock_path(custom), worker.ACCOUNT_LOCK_PATH)

    def test_learning_aggregate_path_is_unique_to_each_source_ledger(self):
        first = Path(self.temp.name) / "first.sqlite3"
        second = Path(self.temp.name) / "second.sqlite3"

        self.assertEqual(
            worker._learning_db_path(first),
            first.with_name("first-online-learning.sqlite3").resolve(),
        )
        self.assertEqual(
            worker._learning_db_path(second),
            second.with_name("second-online-learning.sqlite3").resolve(),
        )
        self.assertNotEqual(
            worker._learning_db_path(first), worker._learning_db_path(second)
        )
        self.assertEqual(
            worker.LEARNING_DB_PATH,
            worker.DB_PATH.with_name(
                f"{worker.DB_PATH.stem}-online-learning.sqlite3"
            ),
        )

    def test_start_refuses_account_lease_owned_by_another_policy(self):
        account_lock = worker._account_lock_path(self.lock_path)
        with worker._process_lock(account_lock), patch.object(
            worker.subprocess, "Popen"
        ) as popen:
            with self.assertRaisesRegex(worker.WorkerHalt, "account-wide"):
                worker.control(
                    "start", db_path=self.db_path, lock_path=self.lock_path
                )
        popen.assert_not_called()

    def test_once_is_not_a_public_cli_action(self):
        with self.assertRaises(SystemExit) as raised:
            worker._main(["once"])
        self.assertEqual(raised.exception.code, 2)

    def test_stop_waits_for_worker_lock_release(self):
        worker._set_desired(self.db_path, True)
        with patch.object(
            worker, "_lock_active", side_effect=[True, False, False, False]
        ), patch.object(worker.time, "sleep") as sleep:
            result = worker.control(
                "stop", db_path=self.db_path, lock_path=self.lock_path
            )

        self.assertFalse(result["running"])
        self.assertFalse(result["desired_running"])
        sleep.assert_called_once_with(0.05)

    def test_reset_requires_independent_gate_and_refuses_open_orders(self):
        client = FakeClient(daily_closes(100, 109))
        with self.assertRaisesRegex(worker.WorkerHalt, "RESET_ENABLED"):
            worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

        client.open = [{"symbol": worker.SYMBOL, "clientOrderId": "manual"}]
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ), self.assertRaisesRegex(worker.WorkerHalt, "open orders"):
            worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

    def test_reset_refuses_desired_running_and_pending_intent(self):
        client = FakeClient(daily_closes(100, 125))
        worker._set_desired(self.db_path, True)
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ), self.assertRaisesRegex(worker.WorkerHalt, "desired_running"):
            worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

        worker._set_desired(self.db_path, False)
        client.buy_error = TimeoutError("response lost")
        client.lookup_error = LookupError("not found")
        run_once(db_path=self.db_path, client=client)
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ), self.assertRaisesRegex(worker.WorkerHalt, "pending"):
            worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

    def test_reset_allows_a_flat_local_ledger_without_origin_lookup(self):
        client = FakeClient(daily_closes(100, 109))
        client.lookup_error = AssertionError("flat reset must not query an origin")

        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ):
            result = worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

        self.assertEqual(result["position"], "cash")
        self.assertEqual(result["reset_epoch_id"], 1)
        self.assertEqual(client.lookup_calls, [])

    def test_reset_atomically_archives_ledger_and_starts_fresh_epoch(self):
        client = FakeClient(daily_closes(100, 125))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        # Spot Testnet may periodically delete its order epoch. Only Binance's
        # structured GET /v3/order -2013 response authorizes discarding a local
        # tracked position during reset.
        client.lookup_error = definitive_order_not_found()

        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ):
            result = worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

        self.assertEqual(result["reset_epoch_id"], 1)
        self.assertEqual(result["archived_decisions"], 1)
        self.assertEqual(result["archived_orders"], 1)
        self.assertEqual(result["position"], "cash")
        self.assertEqual(result["intents"], 0)
        self.assertEqual(result["archived_epochs"], 1)
        with worker.closing(worker._connect(self.db_path)) as db:
            epoch = db.execute(
                "SELECT state_json, decision_count, order_count FROM worker_epochs"
            ).fetchone()
            archived_order = db.execute(
                "SELECT record_json FROM worker_epoch_order_intents"
            ).fetchone()
            active_counts = db.execute(
                "SELECT (SELECT COUNT(*) FROM worker_decisions), "
                "(SELECT COUNT(*) FROM worker_order_intents)"
            ).fetchone()
        self.assertIn('"position_qty":"0.001"', epoch[0])
        self.assertIn('"account_bound":true', epoch[0])
        self.assertNotIn("api_key_fingerprint", epoch[0])
        self.assertEqual((epoch[1], epoch[2]), (1, 1))
        self.assertIn('"client_id":"qr-b-', archived_order[0])
        self.assertEqual(tuple(active_counts), (0, 0))

        # Later resets append epochs and retain every prior audit record.
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ):
            second = worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )
        self.assertEqual(second["reset_epoch_id"], 2)
        self.assertEqual(second["archived_epochs"], 2)
        with worker.closing(worker._connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM worker_epochs").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM worker_epoch_order_intents"
                ).fetchone()[0],
                1,
            )

    def test_reset_preserves_last_candle_and_prevents_client_id_reuse(self):
        client = FakeClient(daily_closes(100, 125))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        first_client_id = client.buy_calls[0][2]
        client.lookup_error = definitive_order_not_found()
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ):
            reset = worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

        self.assertEqual(reset["position"], "cash")
        resumed = FakeClient(daily_closes(100, 125))
        same_candle = run_once(db_path=self.db_path, client=resumed)
        self.assertEqual(same_candle["action"], "waiting")
        self.assertEqual(resumed.buy_calls, [])
        self.assertEqual(reset["last_candle_close_ms"], same_candle["candle_close_ms"])
        with worker.closing(worker._connect(self.db_path)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM worker_order_intents").fetchone()[0],
                0,
            )
            archived = db.execute(
                "SELECT client_id FROM worker_epoch_order_intents"
            ).fetchone()[0]
        self.assertEqual(archived, first_client_id)

    def test_failed_reset_quarantine_is_durable_and_blocks_archive(self):
        client = FakeClient(daily_closes(100, 125))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        client.lookup_error = definitive_order_not_found()

        with patch.object(
            worker.learning_store,
            "quarantine_open_round_trips",
            side_effect=RuntimeError("temporary quarantine outage"),
        ):
            with self.assertRaisesRegex(worker.WorkerHalt, "deferred"):
                worker._archive_epoch_and_reset(self.db_path, client)
            with self.assertRaisesRegex(worker.WorkerHalt, "unresolved"):
                worker._archive_epoch_and_reset(self.db_path, client)
            learning = worker.learning_snapshot(self.db_path)

        self.assertEqual(
            learning["status"], "blocked_by_unresolved_learning_outbox"
        )
        self.assertFalse(learning["proposal_ready_for_review"])
        self.assertEqual(learning["evidence"]["unresolved_learning_outbox"], 1)
        raw = worker.sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                raw.execute("SELECT COUNT(*) FROM worker_epochs").fetchone()[0], 0
            )
            self.assertEqual(
                raw.execute("SELECT COUNT(*) FROM worker_order_intents").fetchone()[0],
                1,
            )
            self.assertEqual(
                raw.execute(
                    """SELECT COUNT(*) FROM worker_learning_outbox
                       WHERE resolved_ms IS NULL"""
                ).fetchone()[0],
                1,
            )
        finally:
            raw.close()

        worker._prepare_learning_before_decision(self.db_path)
        repaired = worker._archive_epoch_and_reset(self.db_path, client)
        self.assertEqual(repaired["reset_epoch_id"], 1)
        self.assertEqual(repaired["archived_orders"], 1)

    def test_direct_reset_refuses_to_erase_position_when_origin_still_exists(self):
        client = FakeClient(daily_closes(100, 125))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )

        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ), self.assertRaisesRegex(worker.WorkerHalt, "still exists"):
            worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )
        with self.assertRaisesRegex(worker.WorkerHalt, "still exists"):
            worker._archive_epoch_and_reset(self.db_path, client)

        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertEqual(status["position"], "long")
        self.assertEqual(status["intents"], 1)
        self.assertEqual(status["archived_epochs"], 0)

    def test_reset_rejects_ambiguous_origin_lookup_and_preserves_ledger(self):
        client = FakeClient(daily_closes(100, 125))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )

        for lookup_error in (
            LookupError("not found"),
            ValueError("unstructured -2013 text"),
            worker.execution.BinanceTransportError("connection lost"),
            worker.execution.BinanceOrderNotFoundError(
                "wrong endpoint",
                method="GET",
                path="/v3/account",
                http_status=400,
                api_code=-2013,
                signed=True,
            ),
        ):
            with self.subTest(error=type(lookup_error).__name__):
                client.lookup_error = lookup_error
                with patch.dict(
                    os.environ,
                    {"BINANCE_TESTNET_RESET_ENABLED": "reset"},
                    clear=False,
                ), self.assertRaisesRegex(worker.WorkerHalt, "could not prove"):
                    worker.control(
                        "reset",
                        db_path=self.db_path,
                        lock_path=self.lock_path,
                        client=client,
                    )
                status = worker.status_snapshot(self.db_path, self.lock_path)
                self.assertEqual(status["position"], "long")
                self.assertEqual(status["intents"], 1)
                self.assertEqual(status["archived_epochs"], 0)

    def test_other_api_key_cannot_turn_minus_2013_into_reset_authority(self):
        account_a = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_A)
        self.assertEqual(
            run_once(db_path=self.db_path, client=account_a)["action"], "bought"
        )
        account_b = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_B)
        account_b.lookup_error = definitive_order_not_found()

        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ), self.assertRaisesRegex(worker.AccountBindingError, "does not match"):
            worker.control(
                "reset",
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account_b,
            )

        self.assertEqual(account_b.lookup_calls, [])
        preserved = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertEqual(preserved["position"], "long")
        self.assertEqual(preserved["intents"], 1)
        self.assertEqual(preserved["archived_epochs"], 0)
        self.assertTrue(preserved["account_bound"])

        account_a.lookup_error = definitive_order_not_found()
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ):
            reset = worker.control(
                "reset",
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=account_a,
            )
        self.assertEqual(reset["position"], "cash")
        self.assertEqual(reset["reset_epoch_id"], 1)
        self.assertTrue(reset["account_bound"])

    def test_legacy_unbound_open_position_requires_present_exact_origin_to_bind(self):
        account_a = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_A)
        self.assertEqual(
            run_once(db_path=self.db_path, client=account_a)["action"], "bought"
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            db.execute(
                "UPDATE worker_state SET api_key_fingerprint_sha256=NULL "
                "WHERE singleton=1"
            )

        account_b = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_B)
        account_b.lookup_error = definitive_order_not_found()
        with self.assertRaisesRegex(
            worker.AccountBindingError, "could not be proven"
        ):
            run_once(db_path=self.db_path, client=account_b)
        self.assertFalse(
            worker.status_snapshot(self.db_path, self.lock_path)["account_bound"]
        )

        resumed_a = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_A)
        resumed_a.orders.update(account_a.orders)
        result = run_once(db_path=self.db_path, client=resumed_a)
        self.assertEqual(result["action"], "waiting")
        self.assertTrue(
            worker.status_snapshot(self.db_path, self.lock_path)["account_bound"]
        )

    def test_legacy_unbound_pending_order_requires_exact_remote_proof(self):
        first = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_A)
        first.buy_error = TimeoutError("response lost")
        first.lookup_error = LookupError("temporarily not found")
        self.assertEqual(
            run_once(db_path=self.db_path, client=first)["action"], "halted"
        )
        client_id = first.buy_calls[0][2]
        with worker.closing(worker._connect(self.db_path)) as db:
            db.execute(
                "UPDATE worker_state SET api_key_fingerprint_sha256=NULL "
                "WHERE singleton=1"
            )

        wrong = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_B)
        wrong.lookup_error = definitive_order_not_found()
        with self.assertRaisesRegex(
            worker.AccountBindingError, "could not be proven"
        ):
            run_once(db_path=self.db_path, client=wrong)
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertFalse(status["account_bound"])
        self.assertEqual(status["pending_client_id"], client_id)

        resumed = FakeClient(daily_closes(100, 125), api_key=TEST_API_KEY_A)
        resumed.orders[client_id] = resumed.filled_order(
            client_id, "BUY", "0.001", "10"
        )
        reconciled = run_once(db_path=self.db_path, client=resumed)
        self.assertEqual(reconciled["action"], "bought")
        self.assertTrue(
            worker.status_snapshot(self.db_path, self.lock_path)["account_bound"]
        )

    def test_legacy_flat_history_cannot_bind_from_generic_other_account(self):
        account_a = FakeClient(
            daily_closes(100, 125, final_day=200), api_key=TEST_API_KEY_A
        )
        self.assertEqual(
            run_once(db_path=self.db_path, client=account_a)["action"], "bought"
        )
        account_a.daily = daily_closes(120, 100, final_day=201)
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            self.assertEqual(
                run_once(db_path=self.db_path, client=account_a)["action"], "sold"
            )
        with worker.closing(worker._connect(self.db_path)) as db:
            db.execute(
                "UPDATE worker_state SET api_key_fingerprint_sha256=NULL "
                "WHERE singleton=1"
            )

        account_b = FakeClient(
            daily_closes(120, 100, final_day=201), api_key=TEST_API_KEY_B
        )
        account_b.lookup_error = definitive_order_not_found()
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS), \
                self.assertRaisesRegex(
                    worker.AccountBindingError, "could not be proven"
                ):
            run_once(db_path=self.db_path, client=account_b)
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertFalse(status["account_bound"])
        self.assertEqual(status["completed_round_trips"], 1)
        self.assertEqual(status["intents"], 2)

        resumed_a = FakeClient(
            daily_closes(120, 100, final_day=201), api_key=TEST_API_KEY_A
        )
        resumed_a.orders.update(account_a.orders)
        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            result = run_once(db_path=self.db_path, client=resumed_a)
        self.assertEqual(result["action"], "waiting")
        self.assertTrue(
            worker.status_snapshot(self.db_path, self.lock_path)["account_bound"]
        )

    def test_reset_refuses_active_account_wide_execution_lease(self):
        client = FakeClient(daily_closes(100, 109))
        account_lock = worker._account_lock_path(self.lock_path)

        def active_only(path):
            return Path(path) == account_lock

        with patch.dict(
            os.environ, {"BINANCE_TESTNET_RESET_ENABLED": "reset"}, clear=False
        ), patch.object(worker, "_lock_active", side_effect=active_only), \
                self.assertRaisesRegex(worker.WorkerHalt, "account-wide"):
            worker.control(
                "reset", db_path=self.db_path, lock_path=self.lock_path, client=client
            )

        self.assertEqual(client.open_order_calls, 0)

    def test_stop_between_intent_and_post_aborts_without_order(self):
        client = FakeClient(daily_closes(100, 125))
        worker._set_desired(self.db_path, True)
        original_persist = worker._persist_decision

        def persist_then_stop(*args, **kwargs):
            claimed = original_persist(*args, **kwargs)
            worker._set_desired(self.db_path, False)
            return claimed

        with patch.object(worker, "_persist_decision", side_effect=persist_then_stop):
            result = run_once(
                db_path=self.db_path, client=client, enforce_desired=True
            )

        self.assertEqual(result["action"], "stopped")
        self.assertEqual(client.buy_calls, [])
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertIsNone(status["pending_client_id"])
        self.assertFalse(status["desired_running"])

    def test_read_outage_retries_without_permanent_halt(self):
        client = FakeClient(daily_closes(100, 109))
        client.open_orders_error = worker.execution.BinanceTransportError("vpn down")

        retry = run_once(db_path=self.db_path, client=client)

        self.assertEqual(retry["action"], "retry")
        self.assertEqual(retry["backoff_seconds"], 60)
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertFalse(status["halted"])
        self.assertEqual(status["transient_failures"], 1)
        self.assertIn("vpn down", status["last_error"])

        client.open_orders_error = None
        recovered = run_once(db_path=self.db_path, client=client)
        self.assertEqual(recovered["action"], "hold_cash")
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertEqual(status["transient_failures"], 0)
        self.assertIsNone(status["last_error"])

        client.open_orders_error = worker.execution.BinanceTransportError("brief")
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "retry"
        )
        client.open_orders_error = None
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "waiting"
        )
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertEqual(status["transient_failures"], 0)

    def test_missing_remote_buy_origin_halts_before_selling_prefunded_btc(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        client.orders.clear()  # Simulate Binance Spot Testnet's periodic reset.
        client.lookup_error = definitive_order_not_found()
        client.daily = daily_closes(120, 100, final_day=201)

        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "halted")
        self.assertIn("Testnet reset is suspected", result["reason"])
        self.assertEqual(client.sell_calls, [])

    def test_position_origin_transport_outage_retries_without_selling(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )
        client.lookup_error = worker.execution.BinanceTransportError("offline")
        client.daily = daily_closes(120, 100, final_day=201)

        with patch.object(worker, "_now_ms", return_value=NOW_MS + DAY_MS):
            result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "retry")
        self.assertFalse(worker.status_snapshot(self.db_path, self.lock_path)["halted"])
        self.assertEqual(client.sell_calls, [])

    def test_clock_skew_halt_recovery_preserves_and_reproves_open_position(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        bought = run_once(db_path=self.db_path, client=client)
        self.assertEqual(bought["action"], "bought")
        reason = (
            f"Worker BUY origin {bought['client_id']} lookup failed without definitive "
            "Testnet-reset evidence (BinanceAPIError: Binance HTTP 400: "
            '{"code":-1021,"msg":"Timestamp for this request is outside of the '
            'recvWindow."}).'
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            worker._halt(db, reason)

        before = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertTrue(before["halted"])
        self.assertEqual(before["tracked_position_qty"], "0.001")
        self.assertTrue(
            worker._recover_timestamp_halt(
                self.db_path, self.lock_path, client
            )
        )

        after = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertFalse(after["halted"])
        self.assertIsNone(after["halt_reason"])
        self.assertIsNone(after["last_error"])
        self.assertEqual(after["tracked_position_qty"], "0.001")
        self.assertEqual(after["intents"], before["intents"])
        self.assertEqual(after["filled_orders"], before["filled_orders"])
        self.assertIn((bought["client_id"], worker.SYMBOL), client.lookup_calls)

    def test_start_recovers_only_proven_clock_skew_read_halt(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        bought = run_once(db_path=self.db_path, client=client)
        reason = (
            f"Worker BUY origin {bought['client_id']} lookup failed without definitive "
            "Testnet-reset evidence (BinanceAPIError: Binance HTTP 400: "
            '{"code":-1021,"msg":"Timestamp for this request is outside of the '
            'recvWindow."}).'
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            worker._halt(db, reason)

        child = Mock()
        child.poll.return_value = None
        with patch.object(
            worker,
            "_lock_active",
            side_effect=[False, False, True, True, True, True, True],
        ), patch.object(worker.subprocess, "Popen", return_value=child), \
                patch.object(worker.time, "sleep"):
            result = worker.control(
                "start",
                db_path=self.db_path,
                lock_path=self.lock_path,
                client=client,
            )

        self.assertTrue(result["running"])
        self.assertTrue(result["desired_running"])
        self.assertFalse(result["halted"])
        self.assertEqual(result["tracked_position_qty"], "0.001")
        self.assertEqual(client.buy_calls, [("10", worker.SYMBOL, bought["client_id"])])

    def test_clock_skew_halt_recovery_keeps_halt_on_origin_mismatch(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        bought = run_once(db_path=self.db_path, client=client)
        self.assertEqual(bought["action"], "bought")
        reason = (
            f"Worker BUY origin {bought['client_id']} lookup failed without definitive "
            "Testnet-reset evidence (BinanceAPIError: Binance HTTP 400: "
            '{"code":-1021,"msg":"Timestamp for this request is outside of the '
            'recvWindow."}).'
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            worker._halt(db, reason)
        client.orders[bought["client_id"]]["executedQty"] = "0.002"

        with self.assertRaisesRegex(worker.WorkerHalt, "no longer matches"):
            worker._recover_timestamp_halt(
                self.db_path, self.lock_path, client
            )

        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertTrue(status["halted"])
        self.assertEqual(status["halt_reason"], reason)
        self.assertEqual(status["tracked_position_qty"], "0.001")

    def test_clock_skew_halt_recovery_requires_free_tracked_btc(self):
        client = FakeClient(daily_closes(100, 125, final_day=200))
        bought = run_once(db_path=self.db_path, client=client)
        reason = (
            f"Worker BUY origin {bought['client_id']} lookup failed without definitive "
            "Testnet-reset evidence (BinanceAPIError: Binance HTTP 400: "
            '{"code":-1021,"msg":"Timestamp for this request is outside of the '
            'recvWindow."}).'
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            worker._halt(db, reason)
        client.free_btc = "0"

        with self.assertRaisesRegex(worker.WorkerHalt, "enough free BTC"):
            worker._recover_timestamp_halt(
                self.db_path, self.lock_path, client
            )

        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertTrue(status["halted"])
        self.assertEqual(status["halt_reason"], reason)
        self.assertEqual(status["tracked_position_qty"], "0.001")

    def test_foreground_restart_reconciles_halted_pending_before_exit(self):
        first = FakeClient(daily_closes(100, 125))
        first.buy_error = TimeoutError("response lost")
        first.lookup_error = LookupError("temporarily not found")
        run_once(db_path=self.db_path, client=first)
        client_id = first.buy_calls[0][2]

        restarted = FakeClient(daily_closes(100, 125))
        restarted.orders[client_id] = restarted.filled_order(
            client_id, "BUY", "0.001", "10"
        )
        restarted.query_omit_fills = True
        worker._set_desired(self.db_path, True)
        real_run_once = worker.run_once

        def reconcile_then_stop(**kwargs):
            result = real_run_once(**kwargs)
            worker._set_desired(self.db_path, False)
            return result

        with patch.object(worker, "run_once", side_effect=reconcile_then_stop):
            worker.run_forever(
                db_path=self.db_path,
                lock_path=self.lock_path,
                poll_seconds=0.1,
                client=restarted,
                market_data_client=restarted.market_data,
            )

        self.assertEqual(restarted.buy_calls, [])
        self.assertEqual(restarted.lookup_calls, [(client_id, worker.SYMBOL)])
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertFalse(status["halted"])
        self.assertIsNone(status["pending_client_id"])

    def test_learning_refresh_failure_never_halts_execution_worker(self):
        client = FakeClient(daily_closes(100, 109))
        worker._set_desired(self.db_path, True)
        real_run_once = worker.run_once

        def decide_then_stop(**kwargs):
            result = real_run_once(**kwargs)
            worker._set_desired(self.db_path, False)
            return result

        with patch.object(worker, "run_once", side_effect=decide_then_stop), \
                patch.object(
                    worker.learning_store,
                    "refresh",
                    side_effect=RuntimeError("learner unavailable"),
                ):
            result = worker.run_forever(
                db_path=self.db_path,
                lock_path=self.lock_path,
                poll_seconds=0.1,
                client=client,
                market_data_client=client.market_data,
            )

        self.assertFalse(result["halted"])
        self.assertEqual(result["position"], "cash")
        with worker.closing(worker._connect(self.db_path)) as db:
            errors = db.execute(
                "SELECT message FROM worker_learning_source_errors"
            ).fetchall()
        self.assertEqual(errors, [])
        learning = worker.learning_snapshot(self.db_path)
        self.assertEqual(learning["status"], "learning_refresh_unhealthy")
        self.assertFalse(learning["proposal_ready_for_review"])
        self.assertTrue(learning["refresh_health"]["last_attempt_failed"])

    def test_successful_learning_refresh_clears_transient_health_failure(self):
        with patch.object(
            worker.learning_store,
            "refresh",
            side_effect=worker.sqlite3.OperationalError("database is busy"),
        ):
            failed = worker._refresh_learning_nonfatal(self.db_path)

        self.assertEqual(failed["status"], "learning_refresh_unhealthy")
        self.assertFalse(failed["proposal_ready_for_review"])
        pure_failed = worker.learning_snapshot(self.db_path)
        self.assertTrue(
            pure_failed["refresh_health"]["last_attempt_failed"]
        )
        with worker.closing(worker._connect(self.db_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM worker_learning_source_errors"
                ).fetchone()[0],
                0,
            )

        recovered = worker._refresh_learning_nonfatal(self.db_path)
        self.assertFalse(
            recovered["refresh_health"]["last_attempt_failed"]
        )
        self.assertTrue(recovered["refresh_health"]["healthy"])
        self.assertNotEqual(recovered["status"], "learning_refresh_unhealthy")
        pure_recovered = worker.learning_snapshot(self.db_path)
        self.assertEqual(
            pure_recovered["refresh_health"], recovered["refresh_health"]
        )

    def test_learning_integrity_failure_is_persistent_safety_evidence(self):
        with patch.object(
            worker.learning_store,
            "refresh",
            side_effect=worker.learning_store.LearningIntegrityError(
                "source seal changed"
            ),
        ):
            failed = worker._refresh_learning_nonfatal(self.db_path)

        self.assertIn("source seal changed", failed["last_refresh_error"])
        with worker.closing(worker._connect(self.db_path)) as db:
            errors = db.execute(
                "SELECT message FROM worker_learning_source_errors"
            ).fetchall()
        self.assertEqual(len(errors), 1)
        self.assertIn("LearningIntegrityError", errors[0]["message"])

        blocked = worker.learning_snapshot(self.db_path, refresh=True)
        self.assertEqual(blocked["status"], "blocked_by_safety_violation")
        self.assertEqual(blocked["evidence"]["safety_violations"], 1)
        self.assertFalse(blocked["proposal_ready_for_review"])

    def test_refresh_integrity_error_and_health_marker_commit_atomically(self):
        with worker.closing(worker._connect(self.db_path)) as db:
            revision_before = int(db.execute(
                """SELECT learning_revision FROM worker_learning_meta
                   WHERE singleton=1"""
            ).fetchone()[0])

        with patch.object(
            worker.learning_store,
            "refresh",
            side_effect=worker.learning_store.LearningIntegrityError(
                "source seal changed"
            ),
        ), patch.object(
            worker.learning_store,
            "mark_refresh_failure",
            side_effect=RuntimeError("health write failed"),
        ):
            returned = worker._refresh_learning_nonfatal(self.db_path)

        self.assertEqual(returned["status"], "learning_refresh_unhealthy")
        self.assertFalse(returned["proposal_ready_for_review"])
        with worker.closing(worker._connect(self.db_path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM worker_learning_source_errors"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                int(db.execute(
                    """SELECT learning_revision FROM worker_learning_meta
                       WHERE singleton=1"""
                ).fetchone()[0]),
                revision_before,
            )
            self.assertEqual(
                int(db.execute(
                    """SELECT last_attempt_failed
                       FROM worker_learning_refresh_health WHERE singleton=1"""
                ).fetchone()[0]),
                0,
            )

    def test_learning_recovery_precedes_first_execution_cycle_after_lock(self):
        client = FakeClient(daily_closes(100, 109))
        worker._set_desired(self.db_path, True)
        events = []
        real_run_once = worker.run_once

        def refresh(db_path):
            events.append("refresh")
            self.assertTrue(worker._lock_active(self.lock_path))
            self.assertTrue(
                worker._lock_active(worker._account_lock_path(self.lock_path))
            )
            return {"status": "collecting"}

        def decide_then_stop(**kwargs):
            events.append("run_once")
            result = real_run_once(**kwargs)
            worker._set_desired(self.db_path, False)
            return result

        with patch.object(
            worker, "_refresh_learning_nonfatal", side_effect=refresh
        ), patch.object(worker, "run_once", side_effect=decide_then_stop):
            result = worker.run_forever(
                db_path=self.db_path,
                lock_path=self.lock_path,
                poll_seconds=0.1,
                client=client,
                market_data_client=client.market_data,
            )

        self.assertFalse(result["halted"])
        self.assertGreaterEqual(events.count("refresh"), 2)
        self.assertEqual(events[:2], ["refresh", "run_once"])

    def test_learning_schema_failure_never_blocks_execution_cycle(self):
        client = FakeClient(daily_closes(100, 109))

        with patch.object(
            worker.learning_store,
            "ensure_source_schema",
            side_effect=RuntimeError("learning schema unavailable"),
        ):
            result = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "hold_cash")
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertFalse(status["halted"])
        self.assertEqual(status["latest_decision"]["action"], "hold_cash")
        self.assertEqual(client.buy_calls, [])
        self.assertEqual(client.sell_calls, [])

    def test_learning_source_sidecar_persists_only_integrity_failures(self):
        transient_db = Path(self.temp.name) / "learning-transient.sqlite3"
        with patch.object(
            worker.learning_store,
            "capture_daily_label",
            side_effect=RuntimeError("temporary learner failure"),
        ):
            transient = run_once(
                db_path=transient_db,
                client=FakeClient(daily_closes(100, 109)),
            )
        self.assertEqual(transient["action"], "hold_cash")
        with worker.closing(worker._connect(transient_db)) as db:
            transient_errors = db.execute(
                "SELECT message FROM worker_learning_source_errors"
            ).fetchall()
        self.assertEqual(transient_errors, [])

        integrity_db = Path(self.temp.name) / "learning-integrity.sqlite3"
        with patch.object(
            worker.learning_store,
            "capture_daily_label",
            side_effect=worker.learning_store.LearningIntegrityError(
                "daily evidence changed"
            ),
        ):
            integrity = run_once(
                db_path=integrity_db,
                client=FakeClient(daily_closes(100, 109)),
            )
        self.assertEqual(integrity["action"], "hold_cash")
        with worker.closing(worker._connect(integrity_db)) as db:
            integrity_errors = db.execute(
                "SELECT message FROM worker_learning_source_errors"
            ).fetchall()
        self.assertEqual(len(integrity_errors), 1)
        self.assertIn("LearningIntegrityError", integrity_errors[0]["message"])

    def test_learning_status_is_proposal_only_and_starts_collecting(self):
        client = FakeClient(daily_closes(100, 109))
        run_once(db_path=self.db_path, client=client)
        config_before = worker.POLICY_CONFIG_PATH.read_bytes()

        status = worker.learning_snapshot(self.db_path)

        self.assertEqual(worker.POLICY_CONFIG_PATH.read_bytes(), config_before)
        self.assertEqual(status["mode"], "proposal_only_manual_review_required")
        self.assertFalse(status["proposal_ready_for_review"])
        self.assertFalse(status["automatic_activation_enabled"])
        self.assertFalse(status["writes_active_config"])
        self.assertEqual(status["evidence"]["finalized_daily_labels"], 0)
        self.assertEqual(status["cadence"]["first_training_at_daily_labels"], 60)

    def test_learning_status_default_is_strictly_read_only(self):
        with worker.closing(worker._connect(self.db_path)):
            pass
        aggregate_path = worker._learning_db_path(self.db_path)
        self.assertFalse(aggregate_path.exists())
        before = self.db_path.read_bytes()

        with patch.object(
            worker,
            "_connect",
            side_effect=AssertionError("read-only status must not initialize schema"),
        ):
            status = worker.learning_snapshot(self.db_path)

        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertFalse(aggregate_path.exists())
        self.assertEqual(status["mode"], "proposal_only_manual_review_required")
        self.assertFalse(status["proposal_ready_for_review"])

    def test_learning_status_cli_explicitly_disables_refresh(self):
        import agent

        with patch.object(
            sys, "argv", ["agent.py", "binance-testnet-learning-status"]
        ), patch.object(
            worker, "learning_snapshot", return_value={"status": "collecting"}
        ) as snapshot, patch("builtins.print"):
            agent.main()

        snapshot.assert_called_once_with(refresh=False)

    def test_bound_account_cli_uses_read_only_ledger_assertion(self):
        import agent

        expected = {
            "validated": True,
            "account_bound": True,
            "api_key_matches_ledger": True,
        }
        with patch.object(
            sys, "argv", ["agent.py", "binance-testnet-bound-account-check"]
        ), patch.object(
            worker, "validate_bound_account", return_value=expected
        ) as validate, patch("builtins.print") as printer:
            agent.main()

        validate.assert_called_once_with()
        self.assertEqual(json.loads(printer.call_args.args[0]), expected)

    def test_learning_seed_cli_uses_companion_manifest_then_refreshes(self):
        import agent
        import binance_testnet_learning_seed as seed_builder
        import testnet_learning_store as learning_store

        data_path = Path(self.temp.name) / "spot.csv"
        data_path.write_text("test fixture", encoding="utf-8")
        manifest_path = data_path.with_suffix(data_path.suffix + ".manifest.json")
        manifest_path.write_text("{}", encoding="utf-8")
        manifest = {"immutable_sha256": "a" * 64, "samples": []}
        seed_result = {"seed_id": "history:test", "sample_count": 60}
        learning_result = {
            "status": "candidate_frozen_awaiting_true_forward",
            "provenance_valid": True,
            "refresh_health": {
                "healthy": True,
                "last_attempt_failed": False,
            },
            "historical_development_labels": 60,
        }
        calls = []

        def build(*args, **kwargs):
            calls.append("build")
            return manifest

        def persist(*args, **kwargs):
            calls.append("seed")
            return seed_result

        def snapshot(*args, **kwargs):
            calls.append("snapshot")
            return learning_result

        with patch.object(
            sys,
            "argv",
            [
                "agent.py",
                "binance-testnet-seed-learning",
                "--data",
                str(data_path),
            ],
        ), patch.object(
            seed_builder, "build_seed_manifest", side_effect=build
        ) as builder, patch.object(
            learning_store, "seed_historical_development", side_effect=persist
        ) as store, patch.object(
            worker, "learning_snapshot", side_effect=snapshot
        ) as learning_snapshot, patch.object(
            worker,
            "status_snapshot",
            return_value={"running": False, "desired_running": False},
        ) as worker_status, patch.object(
            worker, "learning_maintenance_lease"
        ) as maintenance_lease, patch.object(
            worker, "control"
        ) as control, patch("builtins.print") as printer:
            agent.main()

        self.assertEqual(calls, ["build", "seed", "snapshot"])
        builder.assert_called_once_with(
            data_path,
            source_manifest_path=manifest_path,
            interval=None,
            sample_limit=None,
        )
        store.assert_called_once_with(
            source_db_path=worker.DB_PATH,
            seed_manifest=manifest,
            learning_db_path=worker.LEARNING_DB_PATH,
        )
        learning_snapshot.assert_called_once_with(refresh=True)
        worker_status.assert_called_once_with()
        maintenance_lease.assert_called_once_with()
        control.assert_not_called()
        self.assertEqual(
            json.loads(printer.call_args.args[0]),
            {"seed": seed_result, "learning": learning_result},
        )

    def test_learning_upgrade_cli_uses_atomic_upgrade_lease(self):
        import agent
        import binance_testnet_learning_seed as seed_builder
        import testnet_learning_store as learning_store

        data_path = Path(self.temp.name) / "spot.csv"
        data_path.write_text("test fixture", encoding="utf-8")
        manifest = {"immutable_sha256": "a" * 64, "samples": []}
        seed_result = {"seed_id": "history:test", "sample_count": 60}
        learning_result = {
            "status": "no_viable_challenger",
            "provenance_valid": True,
            "refresh_health": {"healthy": True, "last_attempt_failed": False},
        }
        maintenance_result = {
            "initial": {"running": True, "desired_running": True},
            "stopped": {"running": False, "desired_running": False},
            "restart_required": True,
        }
        final_worker = {"running": True, "desired_running": True}

        with patch.object(
            sys,
            "argv",
            [
                "agent.py",
                "binance-testnet-upgrade-learning",
                "--data",
                str(data_path),
            ],
        ), patch.object(
            seed_builder, "build_seed_manifest", return_value=manifest
        ) as builder, patch.object(
            learning_store,
            "seed_historical_development",
            return_value=seed_result,
        ) as persist, patch.object(
            worker, "learning_snapshot", return_value=learning_result
        ) as snapshot, patch.object(
            worker, "learning_upgrade_lease"
        ) as upgrade_lease, patch.object(
            worker, "learning_maintenance_lease"
        ) as maintenance_lease, patch.object(
            worker, "status_snapshot", return_value=final_worker
        ) as worker_status, patch("builtins.print") as printer:
            upgrade_lease.return_value.__enter__.return_value = maintenance_result
            agent.main()

        builder.assert_called_once_with(
            data_path,
            source_manifest_path=None,
            interval=None,
            sample_limit=None,
        )
        persist.assert_called_once_with(
            source_db_path=worker.DB_PATH,
            seed_manifest=manifest,
            learning_db_path=worker.LEARNING_DB_PATH,
        )
        snapshot.assert_called_once_with(refresh=True)
        upgrade_lease.assert_called_once_with()
        maintenance_lease.assert_not_called()
        worker_status.assert_called_once_with()
        self.assertEqual(
            json.loads(printer.call_args.args[0]),
            {
                "seed": seed_result,
                "learning": learning_result,
                "maintenance": maintenance_result,
                "worker": final_worker,
            },
        )

    def test_learning_seed_cli_rejects_nonpositive_sample_count(self):
        import agent
        import binance_testnet_learning_seed as seed_builder

        with patch.object(
            sys,
            "argv",
            [
                "agent.py",
                "binance-testnet-seed-learning",
                "--data",
                "spot.csv",
                "--samples",
                "0",
            ],
        ), patch.object(seed_builder, "build_seed_manifest") as builder, \
                self.assertRaises(SystemExit):
            agent.main()

        builder.assert_not_called()

    def test_learning_seed_cli_requires_stopped_worker(self):
        import agent
        import binance_testnet_learning_seed as seed_builder
        import testnet_learning_store as learning_store

        with patch.object(
            sys,
            "argv",
            [
                "agent.py",
                "binance-testnet-seed-learning",
                "--data",
                "spot.csv",
            ],
        ), patch.object(
            worker,
            "status_snapshot",
            return_value={"running": True, "desired_running": True},
        ), patch.object(
            seed_builder, "build_seed_manifest"
        ) as builder, patch.object(
            learning_store, "seed_historical_development"
        ) as store, self.assertRaisesRegex(ValueError, "tamamen durmuşken"):
            agent.main()

        builder.assert_not_called()
        store.assert_not_called()

    def test_learning_seed_validate_only_never_touches_worker_or_store(self):
        import agent
        import binance_testnet_learning_seed as seed_builder
        import testnet_learning_store as learning_store

        data_path = Path(self.temp.name) / "spot.csv"
        data_path.write_text("fixture", encoding="utf-8")
        manifest = {
            "sample_count": 60,
            "first_decision_ts": 1,
            "last_label_available_ts": 2,
            "last_close": "100",
            "raw_dataset_sha256": "a" * 64,
            "immutable_sha256": "b" * 64,
            "evidence_role": "development_only_not_forward_or_execution_evidence",
        }
        with patch.object(
            sys,
            "argv",
            [
                "agent.py",
                "binance-testnet-seed-learning",
                "--data",
                str(data_path),
                "--interval",
                "15m",
                "--validate-only",
            ],
        ), patch.object(
            seed_builder, "build_seed_manifest", return_value=manifest
        ) as builder, patch.object(
            learning_store, "seed_historical_development"
        ) as store, patch.object(
            worker, "status_snapshot"
        ) as status, patch.object(
            worker, "learning_snapshot"
        ) as learning, patch("builtins.print") as printer:
            agent.main()

        builder.assert_called_once_with(
            data_path,
            source_manifest_path=None,
            interval="15m",
            sample_limit=None,
        )
        store.assert_not_called()
        status.assert_not_called()
        learning.assert_not_called()
        output = json.loads(printer.call_args.args[0])
        self.assertTrue(output["valid"])
        self.assertFalse(output["mutation_performed"])

    def test_learning_seed_cli_surfaces_nonfatal_refresh_failure(self):
        import agent
        import binance_testnet_learning_seed as seed_builder
        import testnet_learning_store as learning_store

        data_path = Path(self.temp.name) / "spot.csv"
        data_path.write_text("fixture", encoding="utf-8")
        with patch.object(
            sys,
            "argv",
            ["agent.py", "binance-testnet-seed-learning", "--data", str(data_path)],
        ), patch.object(
            seed_builder,
            "build_seed_manifest",
            return_value={"immutable_sha256": "a" * 64},
        ), patch.object(
            learning_store,
            "seed_historical_development",
            return_value={"seed_id": "history:test"},
        ), patch.object(
            worker,
            "status_snapshot",
            return_value={"running": False, "desired_running": False},
        ), patch.object(
            worker, "learning_maintenance_lease"
        ), patch.object(
            worker,
            "learning_snapshot",
            return_value={
                "status": "learning_refresh_unhealthy",
                "provenance_valid": False,
                "refresh_health": {"healthy": False, "last_attempt_failed": True},
                "last_error": "refresh failed",
            },
        ), self.assertRaisesRegex(ValueError, "yenilemesi doğrulanamadı"):
            agent.main()

    def test_api_cycles_are_at_least_sixty_seconds_apart(self):
        client = FakeClient(daily_closes(100, 109))
        worker._set_desired(self.db_path, True)
        clock = [0.0]
        waits = []
        calls = []
        real_run_once = worker.run_once

        class FakeEvent:
            def wait(self, seconds):
                waits.append(seconds)
                clock[0] += seconds

        def measured_run_once(**kwargs):
            calls.append(clock[0])
            result = real_run_once(**kwargs)
            if len(calls) == 2:
                worker._set_desired(self.db_path, False)
            return result

        with patch.object(worker.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(worker.threading, "Event", return_value=FakeEvent()), \
                patch.object(worker, "run_once", side_effect=measured_run_once):
            worker.run_forever(
                db_path=self.db_path,
                lock_path=self.lock_path,
                poll_seconds=5,
                api_poll_seconds=1,
                client=client,
                market_data_client=client.market_data,
            )

        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1] - calls[0], 60)
        self.assertLessEqual(max(waits), 5)

    def test_status_names_environment_flags_as_caller_environment(self):
        status = worker.status_snapshot(self.db_path, self.lock_path)
        self.assertEqual(status["market_data_source"], "binance_public_spot")
        self.assertEqual(
            status["execution_environment"], "binance_spot_testnet"
        )
        self.assertTrue(status["caller_env_credentials_present"])
        self.assertTrue(status["caller_env_worker_enabled"])
        self.assertTrue(status["caller_env_execution_enabled"])
        self.assertNotIn("credentials_present", status)

    def test_status_is_read_only_and_does_not_initialize_missing_ledger(self):
        self.assertFalse(self.db_path.exists())

        status = worker.status_snapshot(self.db_path, self.lock_path)

        self.assertEqual(status["status"], "worker_status_unavailable")
        self.assertFalse(status["status_available"])
        self.assertFalse(status["execution_state_known"])
        self.assertEqual(status["position"], "unknown")
        self.assertFalse(self.db_path.exists())

    def test_status_does_not_replay_or_adopt_learning_evidence(self):
        with worker.closing(worker._connect(self.db_path)) as db:
            event_id = worker._enqueue_learning_outbox(
                db,
                "capture_daily_label",
                {
                    "candle_close_ms": NOW_MS,
                    "policy": worker.POLICY,
                    "model_version": worker.POLICY_SPEC_HASH,
                },
                RuntimeError("capture unavailable"),
                NOW_MS,
            )

        with patch.object(
            worker, "_connect", side_effect=AssertionError("writer opened")
        ), patch.object(worker, "_replay_learning_outbox") as replay, patch.object(
            worker.learning_store, "adopt_open_round_trip"
        ) as adopt:
            status = worker.status_snapshot(self.db_path, self.lock_path)

        self.assertTrue(status["status_available"])
        self.assertEqual(status["unresolved_learning_outbox"], 1)
        replay.assert_not_called()
        adopt.assert_not_called()
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            pending = db.execute(
                """SELECT attempt_count, resolved_ms, last_error
                   FROM worker_learning_outbox WHERE event_id=?""",
                (event_id,),
            ).fetchone()
        self.assertEqual(int(pending["attempt_count"]), 1)
        self.assertIsNone(pending["resolved_ms"])
        self.assertEqual(
            pending["last_error"],
            "RuntimeError: learning sidecar operation failed",
        )

    def test_status_reads_one_consistent_sqlite_snapshot(self):
        with worker.closing(worker._connect(self.db_path)):
            pass
        real_state = worker._state

        def read_state_then_commit_new_epoch(db):
            self.assertTrue(db.in_transaction)
            row = real_state(db)
            with worker.closing(worker.sqlite3.connect(self.db_path)) as writer:
                writer.execute(
                    """INSERT INTO worker_epochs
                       (archived_ms, reason, state_json, decision_count,
                        order_count) VALUES (?, ?, ?, 0, 0)""",
                    (NOW_MS, "concurrent test epoch", "{}"),
                )
                writer.commit()
            return row

        with patch.object(worker, "_state", side_effect=read_state_then_commit_new_epoch):
            status = worker.status_snapshot(self.db_path, self.lock_path)

        self.assertTrue(status["status_available"])
        self.assertEqual(status["archived_epochs"], 0)
        with worker.closing(worker._connect_read_only(self.db_path)) as db:
            self.assertEqual(
                int(db.execute("SELECT COUNT(*) FROM worker_epochs").fetchone()[0]),
                1,
            )

    def test_both_execution_gates_are_required(self):
        client = FakeClient(daily_closes(100, 125))
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_WORKER_ENABLED": "false"}, clear=False
        ):
            with self.assertRaisesRegex(worker.WorkerHalt, "WORKER_ENABLED"):
                run_once(db_path=self.db_path, client=client)


if __name__ == "__main__":
    unittest.main()

