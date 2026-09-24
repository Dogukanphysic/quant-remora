"""Offline regressions for signed Futures timestamps and safe read recovery."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import btc_futures_live as f
import btc_futures_testnet as testnet
from test_btc_futures_live import FakeClient


class Clock:
    def __init__(self):
        self.elapsed = 1000.0
        self.wall = 1_800_000_000.0

    def monotonic(self):
        return self.elapsed

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += seconds


class ScriptedOpener:
    """An in-memory exchange: no credentials, sockets or real orders."""

    def __init__(self, clock, *, responses=(), time_delays=()):
        self.clock = clock
        self.responses = deque(responses)
        self.time_delays = deque(time_delays)
        self.requests = []
        self.counts = defaultdict(int)

    def open(self, request, timeout):
        self.requests.append(request)
        route = (request.get_method(), urlsplit(request.full_url).path)
        self.counts[route] += 1
        if route == ("GET", "/fapi/v1/time"):
            delay = self.time_delays.popleft() if self.time_delays else 0.020
            midpoint = self.clock.elapsed + delay / 2
            self.clock.advance(delay)
            server_ms = 1_800_000_000_000 + round((midpoint - 1000) * 1000)
            return io.BytesIO(json.dumps({"serverTime": server_ms}).encode())
        self.clock.advance(0.010)
        reply = self.responses.popleft() if self.responses else []
        if isinstance(reply, int):
            body = json.dumps({
                "code": reply,
                "msg": "unsafe-server-message key=dummy-clock-api-key signature=private",
            }).encode()
            raise HTTPError(request.full_url, 400, "bad request", {}, io.BytesIO(body))
        return io.BytesIO(json.dumps(reply).encode())

    def signed_requests(self):
        return [request for request in self.requests
                if "signature=" in encoded_query(request)]


def encoded_query(request):
    if request.data is not None:
        return request.data.decode()
    return urlsplit(request.full_url).query


def query_values(request):
    return parse_qs(encoded_query(request))


class FuturesClockTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(f.time, "monotonic", self.clock.monotonic))
        self.patches.enter_context(patch.object(f.time, "time", self.clock.time))
        self.addCleanup(self.patches.close)

    def client(self, **kwargs):
        client = object.__new__(f.Client)
        client.key = "dummy-clock-api-key"
        client.secret = "dummy-clock-api-secret"
        client.opener = ScriptedOpener(self.clock, **kwargs)
        return client

    def ledger(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        db = f.connect(Path(directory) / "clock-test.sqlite3")
        self.addCleanup(db.close)
        fake = FakeClient()
        state = f.configure(db, fake, Decimal("50"))
        state["last_poll"] = 1_799_999_000.0
        with db:
            f.write(db, state)
        return db, fake, state

    def check_signature(self, client, request):
        raw = encoded_query(request)
        payload, signature = raw.rsplit("&signature=", 1)
        expected = hmac.new(client.secret.encode(), payload.encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(query_values(request)["recvWindow"], ["5000"])

    def test_signed_get_1021_resyncs_once_with_new_timestamp_and_signature(self):
        client = self.client(responses=[-1021, [{"orderId": 42}]])
        params = {"symbol": "BTCUSDT"}
        reply = client.request("GET", "/fapi/v1/openOrders", params, True)
        self.assertEqual(reply, [{"orderId": 42}])
        signed = client.opener.signed_requests()
        self.assertEqual(len(signed), 2)
        self.assertEqual(client.opener.counts[("GET", "/fapi/v1/time")], 2)
        first, second = map(query_values, signed)
        self.assertGreater(int(second["timestamp"][0]), int(first["timestamp"][0]))
        self.assertNotEqual(first["signature"], second["signature"])
        self.assertEqual(params, {"symbol": "BTCUSDT"})
        for request in signed:
            self.check_signature(client, request)

    def test_repeated_get_1021_is_bounded_and_marked_safe_for_next_poll(self):
        client = self.client(responses=[-1021, -1021, []])
        with self.assertRaises(f.ApiError) as caught:
            client.request("GET", "/fapi/v1/openOrders", {"symbol": "BTCUSDT"}, True)
        self.assertEqual(caught.exception.code, -1021)
        self.assertTrue(caught.exception.retry_safe)
        self.assertEqual(len(client.opener.signed_requests()), 2)
        self.assertEqual(client.opener.counts[("GET", "/fapi/v1/time")], 2)
        self.assertEqual(len(client.opener.responses), 1)

    def test_post_and_delete_1021_never_reissue_mutation(self):
        for method, path in (("POST", "/fapi/v1/order"),
                             ("DELETE", "/fapi/v1/algoOrder")):
            with self.subTest(method=method):
                client = self.client(responses=[-1021, {"orderId": 99}])
                with self.assertRaises(f.ApiError) as caught:
                    client.request(method, path, {"symbol": "BTCUSDT"}, True)
                self.assertEqual(caught.exception.code, -1021)
                self.assertFalse(caught.exception.retry_safe)
                self.assertEqual(client.opener.counts[(method, path)], 1)
                self.assertEqual(len(client.opener.signed_requests()), 1)
                self.assertEqual(len(client.opener.responses), 1)

    def test_non_timestamp_get_error_is_not_retried(self):
        client = self.client(responses=[-2015, []])
        with self.assertRaises(f.ApiError) as caught:
            client.request("GET", "/fapi/v1/openOrders", signed=True)
        self.assertEqual(caught.exception.code, -2015)
        self.assertFalse(caught.exception.retry_safe)
        self.assertEqual(len(client.opener.signed_requests()), 1)

    def test_cached_time_advances_using_monotonic_despite_wall_clock_jump(self):
        client = self.client()
        client.request("GET", "/fapi/v1/openOrders", signed=True)
        self.clock.advance(5)
        self.clock.wall += 10 * 24 * 60 * 60
        client.request("GET", "/fapi/v1/openOrders", signed=True)
        signed = client.opener.signed_requests()
        first, second = map(query_values, signed)
        advance = int(second["timestamp"][0]) - int(first["timestamp"][0])
        self.assertGreaterEqual(advance, 5000)
        self.assertLess(advance, 5100)
        self.assertEqual(client.opener.counts[("GET", "/fapi/v1/time")], 1)
        for request in signed:
            self.check_signature(client, request)

    def test_testnet_subclass_lazily_initializes_clock_and_keeps_testnet_host(self):
        opener = ScriptedOpener(self.clock, responses=[-1021, []])
        dummy_environment = {
            "BTC_FUTURES_TESTNET_AUTHORIZATION": testnet.CONFIRM,
            "BTC_FUTURES_TESTNET_API_KEY": "dummy-clock-api-key",
            "BTC_FUTURES_TESTNET_SECRET_KEY": "dummy-clock-api-secret",
        }
        with patch.dict(f.os.environ, dummy_environment, clear=True), \
                patch.object(f, "build_opener", return_value=opener):
            client = testnet.TestnetClient(enabled=True, environment="testnet")
        self.assertEqual(client.request("GET", "/fapi/v1/openOrders", signed=True), [])
        self.assertEqual(len(opener.signed_requests()), 2)
        for request in opener.requests:
            self.assertEqual(urlsplit(request.full_url).netloc, "testnet.binancefuture.com")
        for request in opener.signed_requests():
            self.check_signature(client, request)

    def test_time_cache_expires_and_is_refreshed_before_signing(self):
        client = self.client()
        client.request("GET", "/fapi/v1/openOrders", signed=True)
        self.clock.advance(61)
        client.request("GET", "/fapi/v1/openOrders", signed=True)
        self.assertEqual(client.opener.counts[("GET", "/fapi/v1/time")], 2)
        signed = client.opener.signed_requests()
        advance = (int(query_values(signed[1])["timestamp"][0]) -
                   int(query_values(signed[0])["timestamp"][0]))
        self.assertGreaterEqual(advance, 61000)
        self.assertLess(advance, 61100)

    def test_slow_time_samples_are_bounded_and_block_order_submission(self):
        client = self.client(time_delays=[1.2, 1.2, 1.2, 0.020])
        with self.assertRaises((f.TransportError, ValueError)):
            client.request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, True)
        self.assertEqual(client.opener.counts[("GET", "/fapi/v1/time")], 3)
        self.assertEqual(client.opener.counts[("POST", "/fapi/v1/order")], 0)
        self.assertEqual(client.opener.signed_requests(), [])

    def test_slow_time_sample_is_replaced_by_fast_sample_before_signing(self):
        client = self.client(responses=[{"orderId": 9}], time_delays=[1.2, 0.020])
        result = client.request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, True)
        self.assertEqual(result, {"orderId": 9})
        self.assertEqual(client.opener.counts[("GET", "/fapi/v1/time")], 2)
        self.assertEqual(client.opener.counts[("POST", "/fapi/v1/order")], 1)
        signed = client.opener.signed_requests()[0]
        server_now = 1_800_000_000_000 + round((self.clock.elapsed - 1000) * 1000)
        self.assertLess(abs(server_now - int(query_values(signed)["timestamp"][0])), 100)

    def test_timestamp_errors_expose_route_but_never_secret_or_remote_message(self):
        client = self.client(responses=[-1021, -1021])
        with self.assertRaises(f.ApiError) as caught:
            client.request("GET", "/fapi/v1/openOrders", {"symbol": "BTCUSDT"}, True)
        message = str(caught.exception)
        self.assertIn("GET /fapi/v1/openOrders", message)
        self.assertIn("-1021", message)
        for forbidden in (client.key, client.secret, "signature=", "timestamp=",
                          "unsafe-server-message", "private", "https://"):
            self.assertNotIn(forbidden, message)

    def test_worker_records_repeated_read_timestamp_error_then_recovers(self):
        transport = self.client(responses=[-1021, -1021])
        try:
            transport.request("GET", "/fapi/v1/openOrders", signed=True)
        except f.ApiError as exc:
            read_failure = exc
        else:
            self.fail("Expected timestamp failure after bounded retry")
        db, fake, state = self.ledger()
        with patch.object(fake, "open_orders", side_effect=read_failure):
            failed, retrying = f.worker_step(db, fake, Decimal("50"))
        self.assertTrue(retrying)
        self.assertEqual(failed["last_poll"], state["last_poll"])
        self.assertEqual(failed["transient_failures"], 1)
        self.assertEqual(failed["last_error"], "retryable_get_timestamp_failure")
        self.assertEqual(failed["last_error_detail"], {
            "type": "ApiError", "http_status": 400, "binance_code": -1021,
            "method": "GET", "path": "/fapi/v1/openOrders",
        })
        self.assertEqual(fake.submitted, [])
        recovered, retrying = f.worker_step(db, fake, Decimal("50"))
        self.assertFalse(retrying)
        self.assertEqual(recovered["transient_failures"], 0)
        self.assertIsNone(recovered["last_error"])
        self.assertIsNone(recovered["last_error_detail"])
        self.assertGreater(recovered["last_poll"], failed["last_poll"])

    def test_mutating_timestamp_failure_escapes_worker_without_retry(self):
        db, fake, state = self.ledger()
        failure = f.ApiError("unsafe remote text", 400, -1021,
                             method="POST", path="/fapi/v1/order")
        with patch.object(f, "tick", side_effect=failure) as tick:
            with self.assertRaises(f.ApiError):
                f.worker_step(db, fake, Decimal("50"))
        tick.assert_called_once()
        self.assertEqual(f.read(db), state)

    def test_fatal_diagnostic_preserves_position_protections_and_poll(self):
        db, fake, state = self.ledger()
        state.update(phase="long", quantity="0.003", entry_price="84270.10",
                     stop_price="83285.20", target_price="86239.80",
                     stop_client_id="qrf-s-test", target_client_id="qrf-t-test",
                     pending_entry={"client_id": "qrf-e-pending", "quantity": "0.003"})
        with db:
            f.write(db, state)
        failure = f.ApiError("unsafe signature=private-key", 400, -1021,
                             method="DELETE", path="/fapi/v1/algoOrder")
        f.record_worker_failure(db, failure, fake.identity)
        failed = f.read(db)
        for key, value in state.items():
            if key not in {"last_error", "last_error_detail", "last_error_at",
                           "worker_stopped_at"}:
                self.assertEqual(failed[key], value, key)
        self.assertEqual(failed["last_error"], "fatal_worker_failure")
        self.assertEqual(failed["last_error_detail"], {
            "type": "ApiError", "http_status": 400, "binance_code": -1021,
            "method": "DELETE", "path": "/fapi/v1/algoOrder",
        })
        self.assertEqual(failed["worker_stopped_at"], self.clock.wall)
        events = db.execute("SELECT kind,payload FROM events WHERE kind='worker_failed'").fetchall()
        self.assertEqual(len(events), 1)
        for content in (json.dumps(failed), str(events)):
            self.assertNotIn("private-key", content)
            self.assertNotIn("signature=", content)

    def test_failure_recorder_leaves_other_account_ledger_untouched(self):
        db, _, state = self.ledger()
        before_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        f.record_worker_failure(db, ValueError("sensitive"), "different-account")
        self.assertEqual(f.read(db), state)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], before_count)

    def test_successful_poll_clears_previous_fatal_diagnostic(self):
        db, fake, _ = self.ledger()
        f.record_worker_failure(db, ValueError("sensitive"), fake.identity)
        state, retrying = f.worker_step(db, fake, Decimal("50"))
        self.assertFalse(retrying)
        self.assertIsNone(state["last_error"])
        self.assertIsNone(state["last_error_detail"])
        self.assertIsNone(state["worker_stopped_at"])
        self.assertGreater(state["last_poll"], 1_799_999_000.0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='worker_recovered'")
                         .fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
