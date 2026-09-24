"""Watch output stays quiet while the separate status artifact stays fresh."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import btc_futures_learning as learner


class LearnerWatchTests(unittest.TestCase):
    def test_watch_refreshes_report_but_prints_only_initial_and_changed_model(self):
        initial = {"status": "trained_proxy_insufficient_evidence", "model_id": "shadow-1",
                   "sample_count": 5, "quarantined_count": 0, "error": None,
                   "last_success_at": 1}
        unchanged = dict(initial, last_success_at=2)
        changed = dict(initial, model_id="shadow-2", sample_count=6, last_success_at=3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "status.json"
            arguments = ["watch", "--source", str(root / "source.sqlite3"),
                         "--db", str(root / "learning.sqlite3"),
                         "--report", str(report_path), "--poll-seconds", "1"]
            with patch.object(learner, "sync", side_effect=[initial, unchanged, changed]) as sync:
                with patch.object(learner.time, "sleep", side_effect=[None, None, KeyboardInterrupt]) as sleep:
                    with patch("builtins.print") as output:
                        with self.assertRaises(KeyboardInterrupt):
                            learner.main(arguments)
            self.assertEqual(sync.call_count, 3)
            self.assertEqual(sleep.call_count, 3)
            printed = [json.loads(call.args[0]) for call in output.call_args_list]
            self.assertEqual(printed, [initial, changed])
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), changed)
            self.assertFalse((root / "source.sqlite3").exists())

    def test_watch_prints_health_failure_and_recovery(self):
        healthy = {"status": "collecting", "model_id": None, "sample_count": 0,
                   "quarantined_count": 0, "error": None}
        failed = dict(healthy, status="source_error", error="source_read_or_schema_error")
        recovered = dict(healthy, quarantined_count=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(learner, "sync", side_effect=[healthy, failed, failed, recovered]):
                with patch.object(learner.time, "sleep", side_effect=[None, None, None, KeyboardInterrupt]):
                    with patch("builtins.print") as output:
                        with self.assertRaises(KeyboardInterrupt):
                            learner.main(["watch", "--source", str(root / "source.sqlite3"),
                                          "--db", str(root / "learning.sqlite3")])
            self.assertEqual([json.loads(call.args[0]) for call in output.call_args_list],
                             [healthy, failed, recovered])

    def test_sync_and_status_still_print_each_requested_report(self):
        report = {"status": "collecting", "sample_count": 0}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ["--source", str(root / "source.sqlite3"),
                     "--db", str(root / "learning.sqlite3")]
            with patch.object(learner, "sync", return_value=report):
                with patch.object(learner, "status", return_value=report):
                    with patch("builtins.print") as output:
                        learner.main(["sync", *paths])
                        learner.main(["status", *paths])
            self.assertEqual([json.loads(call.args[0]) for call in output.call_args_list],
                             [report, report])


if __name__ == "__main__":
    unittest.main()
