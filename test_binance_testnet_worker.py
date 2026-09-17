import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import binance_testnet_worker as worker


DAY_MS = 86_400_000
NOW_MS = 200 * DAY_MS + 12 * 60 * 60 * 1000
ENABLED_ENV = {
    "BINANCE_TESTNET_WORKER_ENABLED": "true",
    "BINANCE_ORDER_EXECUTION_ENABLED": "testnet",
    "BINANCE_TESTNET_API_KEY": "test-key",
    "BINANCE_TESTNET_SECRET_KEY": "test-secret",
}


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
    def __init__(self, klines):
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
            "12",
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

    def test_cash_signal_records_once_without_an_order(self):
        client = FakeClient(daily_closes(100, 119))

        result = run_once(db_path=self.db_path, client=client)
        duplicate = run_once(db_path=self.db_path, client=client)

        self.assertEqual(result["action"], "hold_cash")
        self.assertEqual(duplicate["action"], "waiting")
        self.assertEqual(client.buy_calls, [])
        self.assertEqual(client.sell_calls, [])
        status = worker.status_snapshot(self.db_path, Path(self.temp.name) / "lock")
        self.assertEqual(status["latest_decision"]["action"], "hold_cash")

    def test_signal_candles_and_execution_use_separate_clients(self):
        execution_client = FakeClient(daily_closes(100, 119))
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

    def test_default_clients_are_testnet_execution_and_public_market_data(self):
        execution_client = FakeClient(daily_closes(100, 119))
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

        # A new completed candle whose 30-day momentum is no longer above 20%.
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
            worker, "_lock_active", side_effect=[False, True, True, True]
        ), patch.object(worker.subprocess, "Popen", return_value=child), \
                patch.object(worker.time, "sleep"):
            result = worker.control(
                "start", db_path=self.db_path, lock_path=self.lock_path
            )

        self.assertTrue(result["running"])
        self.assertTrue(result["desired_running"])

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
        with patch.object(worker, "_lock_active", side_effect=[False, False, False]), \
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

    def test_control_mutex_rejects_overlapping_control_action(self):
        with worker._control_mutex(self.lock_path), patch.object(
            worker, "STARTUP_WAIT_SECONDS", 0
        ):
            with self.assertRaisesRegex(worker.WorkerHalt, "control action"):
                with worker._control_mutex(self.lock_path):
                    pass

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
        client = FakeClient(daily_closes(100, 119))
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

    def test_reset_atomically_archives_ledger_and_starts_fresh_epoch(self):
        client = FakeClient(daily_closes(100, 125))
        self.assertEqual(
            run_once(db_path=self.db_path, client=client)["action"], "bought"
        )

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
        client = FakeClient(daily_closes(100, 119))
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

    def test_api_cycles_are_at_least_sixty_seconds_apart(self):
        client = FakeClient(daily_closes(100, 119))
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

    def test_both_execution_gates_are_required(self):
        client = FakeClient(daily_closes(100, 125))
        with patch.dict(
            os.environ, {"BINANCE_TESTNET_WORKER_ENABLED": "false"}, clear=False
        ):
            with self.assertRaisesRegex(worker.WorkerHalt, "WORKER_ENABLED"):
                run_once(db_path=self.db_path, client=client)


if __name__ == "__main__":
    unittest.main()

