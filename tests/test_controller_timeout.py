from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.browser import cdp
from do_again.browser import runtime as browser
from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.service import daemon


class ControllerTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.target = cdp.Target("test-id", "https://chatgpt.com/c/test", "test", "ws://127.0.0.1:9223/devtools/page/test")

    def test_socket_timeout_is_typed_and_websocket_is_closed(self):
        ws = Mock()
        ws.recv_text.side_effect = TimeoutError("timed out")
        with patch.object(cdp, "WebSocket", return_value=ws):
            with self.assertRaisesRegex(cdp.CdpTimeoutError, "Runtime.evaluate response timed out"):
                cdp.call(self.target.websocket_url, "Runtime.evaluate", timeout=1)
        ws.close.assert_called_once()

    def test_websocket_constructor_timeout_is_typed(self):
        with patch.object(cdp, "WebSocket", side_effect=TimeoutError("timed out")):
            with self.assertRaises(cdp.CdpTimeoutError):
                cdp.call(self.target.websocket_url, "Runtime.evaluate", timeout=1)

    def test_after_submit_timeout_is_uncertain_not_retried(self):
        with (
            patch.object(browser, "_assistant_snapshot", return_value={"busy": False}),
            patch.object(browser, "_context_limit_warning", return_value=""),
            patch.object(cdp, "evaluate", side_effect=["ready", cdp.CdpTimeoutError("CDP Runtime.evaluate response timed out")]) as evaluate,
            patch.object(cdp, "insert_text") as insert,
            patch.object(cdp, "click_send") as enter,
            patch.object(browser.time, "sleep"),
        ):
            with self.assertRaisesRegex(BrowserSubmissionUncertain, "outcome is uncertain"):
                browser.send_message(self.target, "DO_AGAIN_TEST id=unique", wait_for_response=False)
        insert.assert_called_once()
        enter.assert_called_once()
        self.assertEqual(evaluate.call_count, 2)  # No duplicate click after an uncertain submit.

    def test_former_fallback_click_is_never_attempted(self):
        # The former click fallback is unreachable even when the composer
        # never clears; only the original Enter may have caused an effect.
        outcomes = ["ready"] + [False] * 20 + [cdp.CdpTimeoutError("CDP Runtime.evaluate response timed out")]
        with (
            patch.object(browser, "_assistant_snapshot", return_value={"busy": False}),
            patch.object(browser, "_context_limit_warning", return_value=""),
            patch.object(cdp, "evaluate", side_effect=outcomes) as evaluate,
            patch.object(cdp, "insert_text") as insert,
            patch.object(cdp, "click_send") as enter,
            patch.object(browser.time, "sleep"),
        ):
            with self.assertRaises(BrowserSubmissionUncertain):
                browser.send_message(self.target, "DO_AGAIN_TEST id=clicked", wait_for_response=False)
        insert.assert_called_once()
        enter.assert_called_once()
        self.assertEqual(evaluate.call_count, 21)

    def test_enter_then_no_send_button_is_uncertain(self):
        with (
            patch.object(browser,"_assistant_snapshot",return_value={"busy":False}),
            patch.object(browser,"_context_limit_warning",return_value=""),
            patch.object(cdp,"evaluate",side_effect=["ready"]+[False]*20+["no_button"]) as evaluate,
            patch.object(cdp,"insert_text") as insert,
            patch.object(cdp,"click_send") as enter,
            patch.object(browser.time,"sleep"),
        ):
            with self.assertRaisesRegex(BrowserSubmissionUncertain,"delivery outcome must be reconciled"):
                browser.send_message(self.target,"DO_AGAIN_RECEIPT_NOT_CONFIRMED",wait_for_response=False)
        self.assertEqual(evaluate.call_count,21)
        insert.assert_called_once()
        enter.assert_called_once()

    def test_readonly_snapshot_timeout_never_assumes_submission(self):
        with patch.object(browser, "_assistant_snapshot", side_effect=cdp.CdpTimeoutError("CDP timeout")):
            with self.assertRaises(cdp.CdpTimeoutError):
                browser.send_message(self.target, "DO_AGAIN_TEST", wait_for_response=False)

    def test_outbox_retained_when_submission_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            repo = Path(tmp) / "repo"
            repo.mkdir()
            queued = daemon._queue_receipt(state, {"request_id": "uncertain-1", "state": "succeeded"})
            with (
                patch.object(daemon, "activate_project"),
                patch.object(daemon, "ensure_browser_running"),
                patch.object(daemon, "notify_receipts", side_effect=BrowserSubmissionUncertain("uncertain")),
            ):
                with self.assertRaises(BrowserSubmissionUncertain):
                    daemon._drain_browser_outbox_locked(repo, state)
            self.assertTrue(queued.is_file())
            self.assertEqual(len(daemon._pending_outbox(state)), 1)

    def test_daemon_surfaces_uncertain_and_unresponsive_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            for exc, state in [
                (BrowserSubmissionUncertain("submit outcome unknown"), "submission_uncertain"),
                (cdp.CdpTimeoutError("CDP Runtime.evaluate timed out"), "browser_unresponsive"),
            ]:
                stop = Mock()
                stop.is_set.side_effect = [False, True]
                work = Mock()
                with patch.object(daemon, "_drain_browser_outbox", side_effect=exc):
                    daemon._browser_monitor(repo=repo, state_dir=Path(tmp), stop_event=stop, work_event=work)
                saved = json.loads((Path(tmp) / "browser_status.json").read_text())
                self.assertEqual(saved["state"], state)
                self.assertIn("timed out" if state == "browser_unresponsive" else "unknown", saved["error"])
                work.wait.assert_called_once_with(15.0)


if __name__ == "__main__":
    unittest.main()
