from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import atomic_json
from do_again.service.session_summary import (
    build_summary, render_summary, save_summary, session_start,
)


class LocalSessionRecapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.control = root / "control"
        self.control.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.layout = SimpleNamespace(repo=self.repo, control_worktree=self.control,
                                      state_dir=self.state, browser_enabled=False)
        self.now = datetime(2026, 10, 8, 5, 0, tzinfo=timezone.utc)
        self.since = self.now - timedelta(hours=2)
        self.requests = self.control / "automation/do_again/requests"
        self.receipts = self.control / "automation/do_again/receipts"
        self.requests.mkdir(parents=True)
        self.receipts.mkdir(parents=True)

    def write_request(self, rid, *, state=None, at=None, raw_output=None):
        timestamp = at or self.now - timedelta(minutes=20)
        atomic_json(self.requests / f"{rid}.json", {
            "request_id": rid, "issued_at_utc": timestamp.isoformat(),
            "operation": "status",
            "args": {"contains_private_data": "DO_NOT_PRINT_THIS_SECRET"},
        })
        if state:
            atomic_json(self.receipts / f"{rid}.json", {
                "request_id": rid, "state": state,
                "finished_at_utc": timestamp.isoformat(),
                "result": {"stdout": raw_output or "SHOULD_NOT_EXPOSE_PRIVATE_STDOUT"},
            })

    def test_digest_is_grounded_and_does_not_leak_request_output(self):
        self.write_request("jobpipe-p1-answer-fanout-test-01", state="succeeded")
        self.write_request("jobpipe-p1-failed-execution-02", state="failed")
        self.write_request("jobpipe-p1-unfinished-task-03")
        atomic_json(self.state / "liveness.json", {
            "state": "stalled", "issue_number": 21,
        })
        atomic_json(self.state / "browser_status.json", {
            "state": "submission_uncertain", "pending_receipts": 2,
        })
        report = build_summary(self.layout, since=self.since, now=self.now)
        self.assertEqual(report["totals"], {
            "succeeded": 1, "failed_or_blocked": 1, "pending": 1,
        })
        body = render_summary(report)
        self.assertIn("Work recap", body)
        self.assertIn("Completed requests", body)
        self.assertIn("Remaining / needs attention", body)
        self.assertIn("Watchdog incident: #21", body)
        self.assertIn("2 receipts need delivery/reconciliation", body)
        self.assertNotIn("DO_NOT_PRINT_THIS_SECRET", body)
        self.assertNotIn("SHOULD_NOT_EXPOSE_PRIVATE_STDOUT", body)

    def test_only_period_receipts_count_but_old_pending_remains_visible(self):
        self.write_request("old-success-01", state="succeeded",
                           at=self.since - timedelta(days=1))
        self.write_request("old-pending-02", at=self.since - timedelta(days=1))
        report = build_summary(self.layout, since=self.since, now=self.now)
        self.assertEqual(report["totals"]["succeeded"], 0)
        self.assertEqual(report["totals"]["pending"], 1)
        self.assertEqual(report["recent_pending"][0]["request_id"], "old-pending-02")

    def test_no_receipt_never_claims_feature_shipped(self):
        self.write_request("feature-in-progress-01")
        report = build_summary(self.layout, since=self.since, now=self.now)
        digest = render_summary(report)
        self.assertEqual(report["totals"]["succeeded"], 0)
        self.assertIn("Pending request: feature-in-progress-01", digest)
        self.assertIn("Neither alone proves features deployed", digest)

    def test_session_start_uses_agent_status_and_falls_back(self):
        self.assertEqual(session_start(self.layout, now=self.now),
                         self.now - timedelta(hours=24))
        path = self.control / "automation/do_again/agent_status.json"
        atomic_json(path, {"started_at_utc": self.since.isoformat()})
        self.assertEqual(session_start(self.layout, now=self.now), self.since)
        atomic_json(path, {"started_at_utc": "not-a-timestamp"})
        self.assertEqual(session_start(self.layout, now=self.now),
                         self.now - timedelta(hours=24))

    def test_local_reports_are_written_as_markdown_and_machine_readable_json(self):
        self.write_request("done-thing-01", state="succeeded")
        report = build_summary(self.layout, since=self.since, now=self.now)
        rendered = render_summary(report)
        path = save_summary(self.layout, report, rendered)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(), rendered)
        self.assertEqual((self.state/"session_reports/latest.md").read_text(), rendered)
        self.assertEqual(json.loads((self.state/"session_reports/latest.json").read_text())["totals"]["succeeded"], 1)
        self.assertFalse((self.repo/"session_reports").exists())
        self.assertFalse((self.control/"session_reports").exists())

    def test_git_commits_are_labeled_without_claiming_agent_ownership(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.repo), *args],
                                  check=True, capture_output=True, text=True)
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Test")
        (self.repo/"app.txt").write_text("a")
        git("add", "app.txt")
        git("commit", "-m", "Fix audio playback")
        report = build_summary(self.layout, since=self.now-timedelta(days=2),
                               now=self.now+timedelta(days=2))
        self.assertIn("Fix audio playback", report["recent_project_commits"])
        self.assertIn("may include non-Do Again changes", render_summary(report))

    def test_stop_prints_and_saves_report_without_invoking_chatgpt(self):
        from do_again import cli
        self.write_request("done-local-request-01", state="succeeded")
        output = io.StringIO()
        self.layout.browser_enabled = False
        with patch.object(cli, "find_repo", return_value=self.repo),              patch.object(cli, "runtime_layout", return_value=self.layout),              patch.object(cli, "stop_service", return_value={"running":False, "repo":str(self.repo)}) as stop,              redirect_stdout(output):
            result = cli._service_action("stop", str(self.repo))
        self.assertEqual(result, 0)
        self.assertIn("Saved recap:", output.getvalue())
        self.assertIn("Work recap", output.getvalue())
        self.assertTrue((self.state/"session_reports/latest.md").is_file())
        stop.assert_called_once()

    def test_on_demand_cli_summary_json_does_not_stop_or_send_messages(self):
        from do_again import cli
        self.write_request("done-local-request-01", state="succeeded")
        output = io.StringIO()
        with patch.object(cli, "find_repo", return_value=self.repo),              patch.object(cli, "runtime_layout", return_value=self.layout),              patch.object(cli, "stop_service") as stop,              redirect_stdout(output):
            self.assertEqual(cli.project_summary(str(self.repo), hours=48.0, json_output=True), 0)
        parsed = json.loads(output.getvalue())
        self.assertIn("recent_completed", parsed)
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
