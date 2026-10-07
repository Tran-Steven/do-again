from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser.errors import BrowserError
from do_again.service.daemon import (
    _browser_outbox_dir,
    _drain_browser_outbox,
    _drain_browser_outbox_locked,
    _pending_outbox,
    _queue_receipt,
)


class BrowserOutboxTests(unittest.TestCase):
    def test_queue_receipt_is_durable_and_idempotent_by_request_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first = {"request_id": "req-1", "state": "succeeded"}
            second = {"request_id": "req-1", "state": "failed"}
            path = _queue_receipt(state, first)
            self.assertTrue(path.is_file())
            _queue_receipt(state, second)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "failed")
            self.assertEqual(len(_pending_outbox(state)), 1)

    def test_failed_delivery_stays_queued_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            path = _queue_receipt(
                state,
                {"request_id": "req-retry", "state": "succeeded"},
            )
            with (
                patch("do_again.service.daemon.ensure_browser_running"),
                patch("do_again.service.daemon.activate_project"),
                patch(
                    "do_again.service.daemon.notify_receipt",
                    side_effect=RuntimeError("browser crashed"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    _drain_browser_outbox(repo, state)
            self.assertTrue(path.is_file())

    def test_one_blocked_receipt_does_not_stop_later_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            first = _queue_receipt(state_dir, {"request_id": "first", "state": "succeeded"})
            second = _queue_receipt(state_dir, {"request_id": "second", "state": "succeeded"})
            repo = Path(tmp) / "repo"
            repo.mkdir()

            def deliver(_repo, receipt):
                if receipt["request_id"] == "first":
                    raise BrowserError("ChatGPT is still generating; retry delivery later")
                return {"response": "submitted"}

            with (
                patch("do_again.service.daemon.activate_project"),
                patch("do_again.service.daemon.ensure_browser_running"),
                patch("do_again.service.daemon.notify_receipt", side_effect=deliver),
            ):
                delivered = _drain_browser_outbox_locked(repo, state_dir)

            self.assertEqual(delivered, 1)
            self.assertTrue(first.exists())
            self.assertFalse(second.exists())
            status = json.loads((state_dir / "browser_status.json").read_text())
            self.assertEqual(status["state"], "recovering")
            self.assertEqual(status["pending_receipts"], 1)

    def test_nontransient_browser_error_still_aborts_drain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            path = _queue_receipt(state_dir, {"request_id": "hard", "state": "succeeded"})
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with (
                patch("do_again.service.daemon.activate_project"),
                patch("do_again.service.daemon.ensure_browser_running"),
                patch(
                    "do_again.service.daemon.notify_receipt",
                    side_effect=BrowserError("project has no bound automation chat; run do-again setup"),
                ),
            ):
                with self.assertRaises(BrowserError):
                    _drain_browser_outbox_locked(repo, state_dir)

            self.assertTrue(path.exists())

    def test_successful_delivery_removes_outbox_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            repo = Path(tmp) / "repo"
            repo.mkdir()
            path = _queue_receipt(
                state,
                {"request_id": "req-ok", "state": "succeeded"},
            )
            with (
                patch("do_again.service.daemon.ensure_browser_running"),
                patch("do_again.service.daemon.activate_project"),
                patch("do_again.service.daemon.notify_receipt"),
            ):
                delivered = _drain_browser_outbox(repo, state)
            self.assertEqual(delivered, 1)
            self.assertFalse(path.exists())
            self.assertEqual(_pending_outbox(state), [])

    def test_outbox_directory_is_project_runtime_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "project-state"
            self.assertEqual(
                _browser_outbox_dir(state),
                state / "browser_outbox",
            )


if __name__ == "__main__":
    unittest.main()
