from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.service import liveness


class LivenessDecisionTests(unittest.TestCase):
    def test_idle_receipt_triggers_bounded_recovery_then_stall(self):
        base = {"receipt_id": "r1", "progress_at": 100.0, "attempts": 0}
        result, action = liveness._decision(
            base, receipt_id="r1", receipt_time=100, pending=False,
            busy=False, now=200, idle_seconds=60, recovery_seconds=60,
        )
        self.assertEqual(action, "resume")
        first = {**result, "attempts": 1, "last_resume_at": 201}
        _, action = liveness._decision(
            first, receipt_id="r1", receipt_time=100, pending=False,
            busy=False, now=240, idle_seconds=60, recovery_seconds=60,
        )
        self.assertEqual(action, "wait")
        second, action = liveness._decision(
            first, receipt_id="r1", receipt_time=100, pending=False,
            busy=False, now=262, idle_seconds=60, recovery_seconds=60,
        )
        self.assertEqual(action, "resume")
        second.update(attempts=2, last_resume_at=263)
        _, action = liveness._decision(
            second, receipt_id="r1", receipt_time=100, pending=False,
            busy=False, now=280, idle_seconds=60, recovery_seconds=60,
        )
        self.assertEqual(action, "wait")
        stalled, action = liveness._decision(
            second, receipt_id="r1", receipt_time=100, pending=False,
            busy=False, now=324, idle_seconds=60, recovery_seconds=60,
        )
        self.assertEqual(action, "report")
        self.assertEqual(stalled["state"], "stalled")

    def test_stuck_generation_is_reported_without_prompting(self):
        old = {"receipt_id": "r1", "progress_at": 100.0, "busy_since": 100.0}
        value, action = liveness._decision(
            old, receipt_id="r1", receipt_time=100.0, pending=False,
            busy=True, now=2000.0, idle_seconds=60, recovery_seconds=60,
        )
        self.assertEqual(value["state"], "stalled_generating")
        self.assertEqual(action, "report_busy")

    def test_progress_resets_retry_budget(self):
        old = {"receipt_id": "r1", "progress_at": 100, "attempts": 2, "last_resume_at": 180}
        value, action = liveness._decision(
            old, receipt_id="r2", receipt_time=220, pending=False,
            busy=False, now=240, idle_seconds=60, recovery_seconds=60,
        )
        self.assertEqual(action, "wait")
        self.assertEqual(value["attempts"], 0)
        self.assertEqual(value["state"], "idle_grace")

    def test_pending_or_generating_never_prompts(self):
        old = {"receipt_id": "r1", "progress_at": 100, "attempts": 1}
        for pending, busy in ((True, False), (False, True)):
            _, action = liveness._decision(
                old, receipt_id="r1", receipt_time=100, pending=pending,
                busy=busy, now=500, idle_seconds=60, recovery_seconds=60,
            )
            self.assertEqual(action, "wait")


class LivenessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.control = root / "control"
        self.control.mkdir()
        self.receipts = self.control / "automation/do_again/receipts"
        self.receipts.mkdir(parents=True)
        receipt = self.receipts / "r1.json"
        receipt.write_text('{"request_id": "r1"}', encoding="utf-8")
        os.utime(receipt, (time.time() - 3600, time.time() - 3600))
        (self.repo / "do-again.toml").write_text(
            "[do_again]\ncontinuous = true\nidle_seconds = 60\nrecovery_seconds = 60\n",
            encoding="utf-8",
        )
        self.target = Mock()
        self.browser_patches = [
            patch.object(liveness.browser, "ensure_browser_running", return_value={"port": 9223}),
            patch.object(liveness.browser, "project_record", return_value={"chat_url": "https://chatgpt.com/c/project"}),
            patch.object(liveness.browser, "_find_chatgpt_target", return_value=self.target),
            patch.object(liveness.browser, "_assistant_snapshot", return_value={"busy": False}),
            patch.object(liveness.browser, "_context_limit_warning", return_value=""),
            patch.object(liveness.browser, "send_message"),
        ]

    def _mock_browser(self):
        stack = __import__("contextlib").ExitStack()
        self.addCleanup(stack.close)
        return [stack.enter_context(p) for p in self.browser_patches]

    def test_continuation_is_one_shot_across_ticks_and_restarts(self):
        mocks = self._mock_browser()
        send = mocks[-1]
        self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "recovering")
        send.assert_called_once()
        self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "recovering")
        send.assert_called_once()
        record = json.loads((self.state / "liveness.json").read_text())
        self.assertEqual(record["attempts"], 1)

    def test_busy_chat_does_not_receive_prompt(self):
        mocks = self._mock_browser()
        mocks[3].return_value = {"busy": True}
        self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "generating")
        mocks[-1].assert_not_called()

    def test_unfinished_request_prevents_prompt(self):
        self._mock_browser()
        requests = self.control / "automation/do_again/requests"
        requests.mkdir(parents=True)
        (requests / "r2.json").write_text("{}")
        self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "working")

    def test_new_receipt_resets_stall(self):
        mocks = self._mock_browser()
        liveness.check_liveness(self.repo, self.control, self.state)
        receipt = self.receipts / "r2.json"
        receipt.write_text('{"request_id": "r2"}')
        self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "idle_grace")
        state = json.loads((self.state / "liveness.json").read_text())
        self.assertEqual(state["attempts"], 0)
        mocks[-1].assert_called_once()

    def test_stale_unfinished_request_is_classified_as_stall(self):
        self._mock_browser()
        requests = self.control / "automation/do_again/requests"
        requests.mkdir(parents=True)
        path = requests / "r2.json"
        path.write_text("{}")
        os.utime(path, (time.time() - 7500, time.time() - 7500))
        self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "stalled_pending")

    def test_continuous_disabled_never_opens_browser(self):
        (self.repo / "do-again.toml").write_text("[do_again]\ncontinuous = false\n")
        with patch.object(liveness.browser, "ensure_browser_running") as browser:
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "disabled")
            browser.assert_not_called()

    def test_send_exception_keeps_durable_at_most_once_budget(self):
        mocks = self._mock_browser()
        mocks[-1].side_effect = RuntimeError("transport uncertain")
        with self.assertRaisesRegex(RuntimeError, "transport uncertain"):
            liveness.check_liveness(self.repo, self.control, self.state)
        value = json.loads((self.state / "liveness.json").read_text())
        self.assertEqual(value["attempts"], 1)
        mocks[-1].side_effect = None
        liveness.check_liveness(self.repo, self.control, self.state)
        mocks[-1].assert_called_once()

    def test_unavailable_issue_cli_is_recorded_without_crash(self):
        config = {"report_stalls": True, "issue_repo": "Tran-Steven/do-again"}
        with patch.object(liveness.subprocess, "run", side_effect=FileNotFoundError("missing")):
            value = liveness._report_stall(self.repo, config, {})
        self.assertEqual(value["issue_report"], "unavailable")
        self.assertGreater(value["next_report_at"], time.time())

    def test_issue_reporting_deduplicates_existing_issue(self):
        config = {"report_stalls": True, "issue_repo": "Tran-Steven/do-again"}
        existing = Mock(returncode=0, stdout=json.dumps([
            {"number": 42, "title": "[do-again] No-progress after bounded continuation (repo)"}
        ]))
        with patch.object(liveness.subprocess, "run", return_value=existing) as run:
            result = liveness._report_stall(self.repo, config, {})
        self.assertEqual(result["issue_number"], 42)
        self.assertEqual(result["issue_report"], "existing")
        run.assert_called_once()
        self.assertNotIn(str(self.repo.parent), str(run.call_args))

    def test_issue_reporting_creates_sanitized_issue_once(self):
        config = {"report_stalls": True, "issue_repo": "Tran-Steven/do-again"}
        listed = Mock(returncode=0, stdout="[]")
        created = Mock(returncode=0, stdout="https://github.com/Tran-Steven/do-again/issues/11\n")
        with patch.object(liveness.subprocess, "run", side_effect=[listed, created]) as run:
            value = liveness._report_stall(self.repo, config, {})
        self.assertEqual(value["issue_number"], 11)
        self.assertEqual(value["issue_report"], "created")
        self.assertEqual(run.call_count, 2)
        self.assertIn("No candidate records", str(run.call_args))
        self.assertNotIn(str(self.repo.parent), str(run.call_args))
        with patch.object(liveness.subprocess, "run") as run_again:
            liveness._report_stall(self.repo, config, value)
        run_again.assert_not_called()


if __name__ == "__main__":
    unittest.main()
