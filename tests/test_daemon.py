from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser.errors import BrowserError, BrowserSubmissionUncertain
from do_again.service.daemon import (
    _browser_outbox_dir,
    _drain_browser_outbox,
    _drain_browser_outbox_locked,
    _pending_outbox,
    _queue_receipt,
)


class BrowserOutboxTests(unittest.TestCase):
    def test_dispatch_records_successor_binding_and_survives_restart_without_replay(self):
        from do_again.service import daemon
        from do_again.browser import runtime
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state';repo=Path(tmp)/'repo';repo.mkdir()
            receipt=_queue_receipt(state,{'request_id':'rollover-proof','state':'succeeded'})
            old={'chat_url':'https://chatgpt.com/c/old','binding_generation':'old'}
            new={'chat_url':'https://chatgpt.com/c/new','binding_generation':'new'}
            def send(repo,receipts,*,before_dispatch):
                before_dispatch({'chat_url':new['chat_url'],'binding_identity':runtime.binding_identity(new),
                                 'payload_sha256':'a'*64,'purpose':'receipt_notification','state':'dispatch_started'})
                raise BrowserSubmissionUncertain('injected timeout after gesture')
            with patch.object(daemon,'activate_project'),patch.object(daemon,'ensure_browser_running'),\
                 patch.object(runtime,'project_record',return_value=old),patch.object(daemon,'notify_receipts',side_effect=send) as submit:
                with self.assertRaises(BrowserSubmissionUncertain):_drain_browser_outbox_locked(repo,state)
                evidence=json.loads(daemon._uncertain_delivery_path(state).read_text())
                self.assertEqual(evidence['chat_url'],new['chat_url']);self.assertEqual(evidence['state'],'dispatch_started')
                self.assertEqual(evidence['payload_sha256'],'a'*64)
                with patch.object(runtime,'project_record',return_value=new),patch.object(runtime,'_find_chatgpt_target',return_value=None):
                    with self.assertRaises(BrowserSubmissionUncertain):_drain_browser_outbox_locked(repo,state)
                self.assertEqual(submit.call_count,1);self.assertTrue(receipt.exists())

    def test_same_url_rebinding_defeats_uncertain_reconciliation(self):
        from do_again.service import daemon
        from do_again.browser import runtime
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);repo=state/'repo';repo.mkdir()
            _queue_receipt(state,{'request_id':'generation-proof','state':'succeeded'})
            old={'chat_url':'https://chatgpt.com/c/same','binding_generation':'old'}
            evidence={'request_ids':['generation-proof'],'chat_url':old['chat_url'],
                      'batch_marker':'marker','binding_identity':runtime.binding_identity(old)}
            with patch.object(runtime,'project_record',return_value={**old,'binding_generation':'new'}),\
                 patch.object(daemon,'ensure_browser_running') as session:
                with self.assertRaisesRegex(BrowserSubmissionUncertain,'generation changed'):
                    daemon._reconcile_uncertain_delivery(repo,state,evidence)
                session.assert_not_called()

    def test_queue_receipt_is_durable_and_idempotent_by_request_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first = {"request_id": "req-1", "state": "succeeded"}
            second = {"request_id": "req-1", "state": "failed"}
            path = _queue_receipt(state, first)
            self.assertTrue(path.is_file())
            self.assertEqual(_queue_receipt(state,first),path)
            with self.assertRaises(BrowserSubmissionUncertain):_queue_receipt(state, second)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "succeeded")
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

    def test_busy_batch_keeps_all_receipts_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            first = _queue_receipt(state_dir, {"request_id": "first", "state": "succeeded"})
            second = _queue_receipt(state_dir, {"request_id": "second", "state": "succeeded"})
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with (
                patch("do_again.service.daemon.activate_project"),
                patch("do_again.service.daemon.ensure_browser_running", return_value={"port": 9224}),
                patch("do_again.browser.runtime._find_chatgpt_target", return_value=object()),
                patch("do_again.browser.runtime._page_contains", return_value=True),
                patch("do_again.browser.runtime.receipt_acknowledgment", return_value={"visible": True, "acknowledged": True}),
                patch(
                    "do_again.service.daemon.notify_receipts",
                    side_effect=BrowserError("ChatGPT is still generating; retry delivery later"),
                ),
            ):
                delivered = _drain_browser_outbox_locked(repo, state_dir)

            self.assertEqual(delivered, 0)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            status = json.loads((state_dir / "browser_status.json").read_text())
            self.assertEqual(status["state"], "recovering")
            self.assertEqual(status["pending_receipts"], 2)

    def test_successful_batch_removes_up_to_twenty_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            for index in range(25):
                _queue_receipt(
                    state_dir,
                    {"request_id": f"req-{index:02d}", "state": "succeeded"},
                )
            repo = Path(tmp) / "repo"
            repo.mkdir()

            with (
                patch("do_again.service.daemon.activate_project"),
                patch("do_again.service.daemon.ensure_browser_running", return_value={"port": 9224}),
                patch("do_again.browser.runtime._find_chatgpt_target", return_value=object()),
                patch("do_again.browser.runtime._page_contains", return_value=True),
                patch("do_again.browser.runtime.receipt_acknowledgment", return_value={"visible": True, "acknowledged": True}),
                patch(
                    "do_again.service.daemon.notify_receipts",
                    return_value={"response": "already_delivered"},
                ) as notify,
            ):
                delivered = _drain_browser_outbox_locked(repo, state_dir)

            self.assertEqual(delivered, 20)
            self.assertEqual(len(_pending_outbox(state_dir)), 5)
            self.assertEqual(len(notify.call_args.args[1]), 20)
            status = json.loads((state_dir / "browser_status.json").read_text())
            self.assertEqual(status["state"], "queued")
            self.assertEqual(status["pending_receipts"], 5)

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
                patch("do_again.service.daemon.ensure_browser_running", return_value={"port":9224}),
                patch("do_again.service.daemon.activate_project"),
                patch("do_again.browser.runtime._find_chatgpt_target", return_value=object()),
                patch("do_again.browser.runtime._page_contains", return_value=True),
                patch("do_again.browser.runtime.receipt_acknowledgment", return_value={"visible":True,"acknowledged":True}),
                patch(
                    "do_again.service.daemon.notify_receipts",
                    return_value={"response": "already_delivered"},
                ),
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
