"""Offline connection diagnostics and fail-closed Futures recovery regressions."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from decimal import Decimal
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import btc_futures_live as f


TIME_URL = "https://fapi.binance.com/fapi/v1/time"
PRIMARY_IP_URL = "https://checkip.amazonaws.com/"
SECONDARY_IP_URL = "https://api.ipify.org"
UNSAFE_TEXT = "unsafe-remote-body signature=private-test-secret"


class PublicNetworkCheckTests(unittest.TestCase):
    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        # Even accidental use of authenticated or ledger paths must fail offline.
        self.client_init = self.guards.enter_context(patch.object(
            f.Client, "__init__", side_effect=AssertionError("Client constructed")))
        self.connect = self.guards.enter_context(patch.object(
            f, "connect", side_effect=AssertionError("Ledger opened")))
        self.sqlite_connect = self.guards.enter_context(patch.object(
            f.sqlite3, "connect", side_effect=AssertionError("SQLite opened")))
        self.guards.enter_context(patch.dict(f.os.environ, {
            "BTC_FUTURES_MAINNET_API_KEY": "unused-network-check-key",
            "BTC_FUTURES_MAINNET_SECRET_KEY": "unused-network-check-secret",
        }, clear=True))
        self.requests = []

    def public_opener(self, *, primary=b"8.8.8.8\n", secondary=b"8.8.8.8",
                      time_reply=None):
        replies = {
            TIME_URL: (b'{"serverTime":1800000000000}' if time_reply is None
                       else time_reply),
            PRIMARY_IP_URL: primary,
            SECONDARY_IP_URL: secondary,
        }

        def open_response(request, *args, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            method = "GET" if isinstance(request, str) else request.get_method()
            self.requests.append((method, url, request))
            self.assertIn(url, replies, "Unexpected network destination")
            self.assertEqual(method, "GET")
            if not isinstance(request, str):
                self.assertIsNone(request.data)
                headers = {key.lower(): value for key, value in request.header_items()}
                self.assertNotIn("x-mbx-apikey", headers)
                self.assertNotIn("authorization", headers)
            self.assertFalse(urlsplit(url).query)
            reply = replies[url]
            if isinstance(reply, Exception):
                raise reply
            response = io.BytesIO(reply)
            response.status = 200
            return response

        return Mock(open=Mock(side_effect=open_response))

    def check_with(self, **kwargs):
        opener = self.public_opener(**kwargs)
        with patch.object(f, "build_opener", return_value=opener):
            report = f.network_check()
        return report

    def assert_read_only_report(self, report):
        self.assertEqual(report["orders_submitted"], 0)
        for flag in ("ledger_modified", "worker_started", "credentials_used",
                     "authentication_checked"):
            self.assertIs(report[flag], False, flag)
        self.client_init.assert_not_called()
        self.connect.assert_not_called()
        self.sqlite_connect.assert_not_called()
        rendered = json.dumps(report)
        for forbidden in (UNSAFE_TEXT, "unused-network-check-key",
                          "unused-network-check-secret", "signature="):
            self.assertNotIn(forbidden, rendered)

    def test_success_checks_only_public_endpoints_without_authentication(self):
        report = self.check_with()
        self.assert_read_only_report(report)
        self.assertIs(report["ok"], True)
        self.assertEqual(report["public_ip"], "8.8.8.8")
        self.assertIs(report["public_ip_consistent"], True)
        self.assertEqual(set(report["checks"]), {
            "futures_public_time", "public_ip_primary", "public_ip_secondary"})
        self.assertTrue(all(check["ok"] for check in report["checks"].values()))
        self.assertCountEqual([url for _, url, _ in self.requests],
                              [TIME_URL, PRIMARY_IP_URL, SECONDARY_IP_URL])

    def test_disagreeing_ip_services_do_not_claim_a_confirmed_public_ip(self):
        report = self.check_with(secondary=b"1.1.1.1")
        self.assert_read_only_report(report)
        self.assertIs(report["ok"], False)
        self.assertIs(report["public_ip_consistent"], False)
        self.assertIsNone(report["public_ip"])

    def test_failed_public_endpoint_is_reported_without_remote_exception_text(self):
        report = self.check_with(time_reply=URLError(UNSAFE_TEXT))
        self.assert_read_only_report(report)
        self.assertIs(report["ok"], False)
        self.assertIs(report["checks"]["futures_public_time"]["ok"], False)

    def test_invalid_ip_response_is_not_echoed_or_treated_as_confirmed(self):
        report = self.check_with(primary=UNSAFE_TEXT.encode())
        self.assert_read_only_report(report)
        self.assertIs(report["ok"], False)
        self.assertIs(report["checks"]["public_ip_primary"]["ok"], False)
        self.assertIs(report["public_ip_consistent"], False)
        self.assertIsNone(report["public_ip"])

    def test_cli_network_check_needs_no_margin_credentials_or_ledger(self):
        opener = self.public_opener()
        output = io.StringIO()
        with patch.object(f.sys, "argv", ["btc_futures_live.py", "network-check"]), \
                patch.object(f, "build_opener", return_value=opener), \
                patch.object(f, "positive", side_effect=AssertionError("Margin checked")), \
                redirect_stdout(output):
            result = f.main()
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assert_read_only_report(report)
        self.assertIs(report["ok"], True)


class FuturesAuthenticationAndClockFailureTests(unittest.TestCase):
    def client(self):
        client = object.__new__(f.Client)
        client.key = "dummy-connection-api-key"
        client.secret = "dummy-connection-api-secret"
        client.opener = Mock()
        return client

    def authentication_error(self, *, retry_safe=False):
        return f.ApiError(UNSAFE_TEXT, 401, -2015, method="GET",
                          path="/fapi/v1/positionSide/dual", retry_safe=retry_safe)

    def test_authentication_diagnostic_is_actionable_and_sanitized(self):
        detail = f.error_details(self.authentication_error())
        self.assertEqual(detail, {
            "type": "ApiError", "http_status": 401, "binance_code": -2015,
            "method": "GET", "path": "/fapi/v1/positionSide/dual",
            "category": "authentication",
            "operator_action": "check_same_api_key_ip_allowlist_and_futures_permission",
        })
        self.assertNotIn(UNSAFE_TEXT, json.dumps(detail))
        self.assertNotIn("signature=", json.dumps(detail))

    def test_401_2015_is_never_retried_even_on_a_signed_get(self):
        client = self.client()
        body = json.dumps({"code": -2015, "msg": UNSAFE_TEXT}).encode()
        client.opener.open.side_effect = HTTPError(
            TIME_URL + "?signature=private-test-secret", 401, UNSAFE_TEXT,
            {}, io.BytesIO(body))
        with patch.object(client, "_signed_timestamp", return_value=1800000000000):
            with self.assertRaises(f.ApiError) as caught:
                client.request("GET", "/fapi/v1/positionSide/dual", signed=True)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.code, -2015)
        self.assertFalse(caught.exception.retry_safe)
        client.opener.open.assert_called_once()
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urlsplit(request.full_url).path, "/fapi/v1/positionSide/dual")
        for forbidden in (UNSAFE_TEXT, client.key, client.secret, "signature="):
            self.assertNotIn(forbidden, str(caught.exception))

    def test_worker_does_not_retry_authentication_or_rewrite_ledger(self):
        failure = self.authentication_error(retry_safe=True)
        self.assertFalse(failure.retry_safe)
        with patch.object(f, "tick", side_effect=failure) as tick, \
                patch.object(f, "read", side_effect=AssertionError("Ledger read")) as read, \
                patch.object(f, "write", side_effect=AssertionError("Ledger write")) as write:
            with self.assertRaises(f.ApiError) as caught:
                f.worker_step(object(), object(), Decimal("50"))
        self.assertIs(caught.exception, failure)
        tick.assert_called_once()
        read.assert_not_called()
        write.assert_not_called()

    def test_pre_sign_clock_failure_never_sends_or_retries_a_mutation(self):
        for method, path in (("POST", "/fapi/v1/order"),
                             ("DELETE", "/fapi/v1/algoOrder")):
            with self.subTest(method=method):
                client = self.client()
                client.opener.open.side_effect = URLError(UNSAFE_TEXT)
                with self.assertRaises(f.TransportError) as caught:
                    client.request(method, path, {"symbol": "BTCUSDT"}, signed=True)
                error = caught.exception
                self.assertFalse(error.retry_safe)
                self.assertEqual((error.method, error.path), (method, path))
                self.assertIs(error.request_sent, False)
                self.assertNotIn(UNSAFE_TEXT, str(error))
                self.assertNotIn("signature=", str(error))
                client.opener.open.assert_called_once()
                request = client.opener.open.call_args.args[0]
                self.assertEqual(request.get_method(), "GET")
                self.assertEqual(request.full_url, TIME_URL)
                with patch.object(f, "tick", side_effect=error) as tick, \
                        patch.object(f, "read", side_effect=AssertionError("Ledger read")):
                    with self.assertRaises(f.TransportError):
                        f.worker_step(object(), client, Decimal("50"))
                tick.assert_called_once()

    def test_pre_sign_clock_failure_for_a_get_remains_retry_safe(self):
        client = self.client()
        client.opener.open.side_effect = URLError(UNSAFE_TEXT)
        with self.assertRaises(f.TransportError) as caught:
            client.request("GET", "/fapi/v1/positionSide/dual", signed=True)
        self.assertTrue(caught.exception.retry_safe)
        self.assertIs(caught.exception.request_sent, False)
        client.opener.open.assert_called_once()
        self.assertEqual(client.opener.open.call_args.args[0].full_url, TIME_URL)


if __name__ == "__main__":
    unittest.main()
