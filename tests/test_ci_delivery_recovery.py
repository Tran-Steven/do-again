from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.service import liveness


TERMINAL = {
    "state": "terminal",
    "status": "completed",
    "conclusion": "success",
    "head_sha": "a" * 40,
}


class CiUncertainDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.control = self.root / "control"
        self.state = self.root / "state"
        self.state.mkdir()
        (self.repo / "do-again.toml").write_text(
            "[do_again]\ncontinuous = true\nidle_seconds = 60\nrecovery_seconds = 60\n"
        )
        requests = self.control / "automation/do_again/requests"
        requests.mkdir(parents=True)
        receipts = self.control / "automation/do_again/receipts"
        receipts.mkdir(parents=True)
        request = {
            "request_id": "wait-1",
            "continuation": {
                "goal_state": "waiting_for_ci",
                "goal_id": "fix-12",
                "ci": {
                    "repository": "Tran-Steven/do-again",
                    "run_id": 101,
                    "head_sha": "a" * 40,
                },
            },
        }
        (requests / "wait-1.json").write_text(json.dumps(request))
        (receipts / "wait-1.json").write_text(
            json.dumps({"request_id": "wait-1", "state": "succeeded"})
        )

    def mocks(self):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(liveness, "_github_actions_run", return_value=TERMINAL))
        stack.enter_context(patch.object(liveness.browser, "ensure_browser_running", return_value={"port": 9223}))
        stack.enter_context(patch.object(liveness.browser, "project_record", return_value={"chat_url": "https://chatgpt.com/c/owned-project"}))
        stack.enter_context(patch.object(liveness.browser, "_find_chatgpt_target", return_value={"id": "target"}))
        contains = stack.enter_context(patch.object(liveness.browser, "_page_contains", return_value=False))
        send = stack.enter_context(patch.object(liveness.browser, "send_message"))
        return send, contains

    def snapshot(self):
        return json.loads((self.state / "liveness.json").read_text())

    def test_timeout_on_send_does_not_stall_silently_forever(self):
        send, contains = self.mocks()
        send.side_effect = TimeoutError("CDP Runtime.evaluate click timed out")
        with patch.object(liveness.time, "time", return_value=1000.0):
            with self.assertRaises(TimeoutError):
                liveness.check_liveness(self.repo, self.control, self.state)
            first = self.snapshot()
            self.assertEqual(first["ci_delivery_phase"], "uncertain")
            self.assertEqual(first["state"], "ci_delivery_uncertain")
            self.assertTrue(first["ci_delivery_marker"].startswith("DO_AGAIN_CI_GATE_COMPLETE token="))
        # Simulate multiple daemon restarts without a confirmed chat message.
        with patch.object(liveness.time, "time", return_value=1010.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "ci_delivery_uncertain")
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "ci_delivery_uncertain")
        send.assert_called_once()
        with patch.object(liveness.time, "time", return_value=1061.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "stalled_ci_handoff")
        assert "no subsequent goal" in self.snapshot()["ci_error"]
        send.assert_called_once()

    def test_successful_submit_not_automatically_considered_acknowledged(self):
        send, _ = self.mocks()
        with patch.object(liveness.time, "time", return_value=1000.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "recovering")
            row = self.snapshot()
            self.assertEqual(row["ci_delivery_phase"], "submitted_unverified")
        with patch.object(liveness.time, "time", return_value=1020.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "recovering")
        with patch.object(liveness.time, "time", return_value=1061.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "stalled_ci_handoff")
        send.assert_called_once()

    def test_observed_user_message_is_not_model_ack(self):
        send, contains = self.mocks()
        contains.return_value = True
        with patch.object(liveness.time, "time", return_value=1000.0):
            liveness.check_liveness(self.repo, self.control, self.state)
        with patch.object(liveness.time, "time", return_value=1020.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "ci_handoff_observed")
        with patch.object(liveness.time, "time", return_value=1061.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "stalled_ci_handoff")
        send.assert_called_once()

    def test_readonly_probe_timeout_preserves_state_and_escalates(self):
        send, contains = self.mocks()
        send.side_effect = TimeoutError("uncertain submit")
        contains.side_effect = TimeoutError("read-only snapshot timeout")
        with patch.object(liveness.time, "time", return_value=1000.0):
            with self.assertRaises(TimeoutError):
                liveness.check_liveness(self.repo, self.control, self.state)
        with patch.object(liveness.time, "time", return_value=1030.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "ci_delivery_uncertain")
        self.assertEqual(self.snapshot()["ci_probe_error"], "TimeoutError")
        with patch.object(liveness.time, "time", return_value=1061.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "stalled_ci_handoff")
        send.assert_called_once()

    def test_pending_request_prevents_parallel_ci_resume(self):
        send, _ = self.mocks()
        requests = self.control / "automation/do_again/requests"
        (requests / "still-running.json").write_text('{"request_id":"still-running"}')
        with patch.object(liveness, "_github_actions_run", side_effect=AssertionError("should not query CI while running")):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "working")
        send.assert_not_called()

    def test_new_request_goal_state_clears_old_ci_wait_without_duplicate_send(self):
        send, _ = self.mocks()
        with patch.object(liveness.time, "time", return_value=1000.0):
            liveness.check_liveness(self.repo, self.control, self.state)
        request = {
            "request_id": "next",
            "continuation": {"goal_state": "validated_complete", "goal_id": "fix-12"},
        }
        file = self.control / "automation/do_again/requests/next.json"
        file.write_text(json.dumps(request))
        # mtimes are ordered because goal discovery selects most recent.
        os.utime(file, None)
        with patch.object(liveness.time, "time", return_value=1030.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "validated_complete")
        (self.control / "automation/do_again/receipts/next.json").write_text('{"request_id":"next","state":"succeeded"}')
        with patch.object(liveness.time, "time", return_value=1031.0):
            self.assertEqual(liveness.check_liveness(self.repo, self.control, self.state), "validated_complete")
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
