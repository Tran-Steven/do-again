from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.browser import runtime as browser
from do_again.service import daemon


class DurableBrowserUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "Sonary"
        self.repo.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()

    def _mocks(self):
        from contextlib import ExitStack

        st = ExitStack()
        self.addCleanup(st.close)
        st.enter_context(patch.object(daemon, "activate_project"))
        st.enter_context(patch.object(daemon, "ensure_browser_running", return_value={"port": 9224}))
        st.enter_context(patch.object(browser, "project_record", return_value={"chat_url": "https://chatgpt.com/c/bound"}))
        st.enter_context(patch.object(browser, "_find_chatgpt_target", return_value=object()))
        contain = st.enter_context(patch.object(browser, "_page_contains", return_value=False))
        st.enter_context(patch.object(browser, "_assistant_snapshot", return_value={"busy": True, "latest": ""}))
        notify = st.enter_context(patch.object(daemon, "notify_receipts", side_effect=BrowserSubmissionUncertain("unknown after click")))
        return contain, notify

    def _enqueue(self, id):
        return daemon._queue_receipt(self.state, {"request_id": id, "state": "succeeded"})

    def _uncertain(self):
        return json.loads(daemon._uncertain_delivery_path(self.state).read_text())

    def test_timeout_persists_exact_batch_and_never_replays(self):
        a = self._enqueue("req-a")
        b = self._enqueue("req-b")
        contain, notify = self._mocks()
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo, self.state)
        self.assertEqual(notify.call_count, 1)
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        saved = self._uncertain()
        self.assertEqual(saved["request_ids"], ["req-a", "req-b"])
        self.assertEqual(saved["chat_url"], "https://chatgpt.com/c/bound")
        self.assertEqual(saved["batch_marker"], "DO_AGAIN_RECEIPTS_READY request_ids=req-a,req-b")
        for _ in range(12):
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo, self.state)
        self.assertEqual(notify.call_count, 1)
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())
        self.assertEqual(contain.call_count, 12)

    def test_reconciled_marker_clears_only_old_batch_and_resumes_later(self):
        a = self._enqueue("req-a")
        contain, notify = self._mocks()
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo, self.state)
        b = self._enqueue("req-b")
        contain.return_value = True
        self.assertEqual(daemon._drain_browser_outbox_locked(self.repo, self.state), 1)
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        self.assertFalse(daemon._uncertain_delivery_path(self.state).exists())
        self.assertEqual(notify.call_count, 1)
        saved=json.loads((self.state/"browser_status.json").read_text())
        self.assertEqual(saved["state"], "queued")
        notify.side_effect = None
        notify.return_value = {"response":"already_delivered"}
        self.assertEqual(daemon._drain_browser_outbox_locked(self.repo, self.state), 1)
        self.assertFalse(b.exists())

    def test_restart_and_chat_mismatch_halt_before_any_resend(self):
        path = self._enqueue("req-1")
        contain, notify = self._mocks()
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo, self.state)
        with patch.object(browser, "project_record", return_value={"chat_url": "https://chatgpt.com/c/not-bound"}):
            with self.assertRaisesRegex(BrowserSubmissionUncertain, "different conversation"):
                daemon._drain_browser_outbox_locked(self.repo, self.state)
        self.assertTrue(path.exists())
        self.assertEqual(notify.call_count, 1)
        contain.assert_not_called()

    def test_unacknowledged_after_deadline_creates_durable_local_incident_once(self):
        self._enqueue("req-blocked")
        contain, notify = self._mocks()
        with patch.object(daemon.time, "time", return_value=1000):
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo, self.state)
        with patch.object(daemon.time, "time", return_value=1400):
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo, self.state)
        incident = self.state / "incidents/browser-submission-unacknowledged.json"
        self.assertTrue(incident.is_file())
        self.assertEqual(json.loads(incident.read_text())["request_ids"], ["req-blocked"])
        before = incident.stat().st_mtime_ns
        with patch.object(daemon.time, "time", return_value=2000):
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo, self.state)
        self.assertEqual(incident.stat().st_mtime_ns, before)
        notify.assert_called_once()

    def test_clicked_without_chat_ack_is_not_considered_delivered(self):
        a=self._enqueue("req-submitted")
        contain,notify=self._mocks()
        notify.side_effect=None
        notify.return_value={"response":"submitted"}
        with self.assertRaisesRegex(BrowserSubmissionUncertain,"not yet verified"):
            daemon._drain_browser_outbox_locked(self.repo,self.state)
        self.assertTrue(a.exists())
        self.assertTrue(daemon._uncertain_delivery_path(self.state).exists())
        self.assertEqual(notify.call_count,1)
        contain.return_value=True
        self.assertEqual(daemon._drain_browser_outbox_locked(self.repo,self.state),1)
        self.assertFalse(a.exists())
        notify.assert_called_once()

    def test_safe_restart_after_crash_before_first_send_never_replays(self):
        a=self._enqueue("req-crashed")
        contain,notify=self._mocks()
        # Simulate process crash after the intent was durably recorded but
        # before the first browser send, by writing the exact prepared state.
        daemon.atomic_json(daemon._uncertain_delivery_path(self.state),{
            "schema_version":1,
            "request_ids":["req-crashed"],
            "batch_marker":"DO_AGAIN_RECEIPTS_READY request_ids=req-crashed",
            "chat_url":"https://chatgpt.com/c/bound",
            "first_seen_epoch":1000,
            "state":"preparing"
        })
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo,self.state)
        self.assertTrue(a.exists())
        notify.assert_not_called()

    def test_old_preparing_batch_sends_one_read_only_ack_and_never_replays_original(self):
        original = self._enqueue("jobpipe-complete")
        contain, notify = self._mocks()
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo,self.state)
        notify.assert_called_once()
        snap={"busy":False,"latest":""}
        with patch.object(browser,"_assistant_snapshot",return_value=snap), patch.object(
            browser,"send_message",return_value={"response":"submitted"}
        ) as ack_send:
            with patch.object(daemon.time,"time",return_value=1400):
                state=self._uncertain()
                state["first_seen_epoch"]=1000
                daemon.atomic_json(daemon._uncertain_delivery_path(self.state),state)
                with self.assertRaises(BrowserSubmissionUncertain):
                    daemon._drain_browser_outbox_locked(self.repo,self.state)
                ack_send.assert_called_once()
                prompt=ack_send.call_args.args[1]
                self.assertIn("READ-ONLY DELIVERY RECONCILIATION",prompt)
                self.assertIn("Do not repeat any",prompt)
                token=self._uncertain()["ack_probe_token"]
                self.assertIn(token,prompt)
                with self.assertRaises(BrowserSubmissionUncertain):
                    daemon._drain_browser_outbox_locked(self.repo,self.state)
                ack_send.assert_called_once()
                self.assertTrue(original.exists())
                snap["latest"]=token
                self.assertEqual(daemon._drain_browser_outbox_locked(self.repo,self.state),1)
                self.assertFalse(original.exists())
                self.assertFalse(daemon._uncertain_delivery_path(self.state).exists())
                notify.assert_called_once()

    def test_acknowledgment_timeout_survives_restart_without_resend(self):
        self._enqueue("jobpipe-done")
        _, notify=self._mocks()
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo,self.state)
        state=self._uncertain()
        state["first_seen_epoch"]=1000
        daemon.atomic_json(daemon._uncertain_delivery_path(self.state),state)
        with patch.object(browser,"_assistant_snapshot",return_value={"busy":False,"latest":""}),patch.object(
            browser,"send_message",side_effect=TimeoutError("CDP uncertain")
        ) as send, patch.object(daemon.time,"time",return_value=1400):
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo,self.state)
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo,self.state)
        send.assert_called_once()
        notify.assert_called_once()
        self.assertTrue(self._uncertain()["ack_probe_token"])
        self.assertTrue((self.state/"incidents/browser-submission-unacknowledged.json").exists())

    def test_no_browser_message_cannot_clear_outbox(self):
        self._enqueue("req-1")
        self._mocks()
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo, self.state)
        with self.assertRaises(BrowserSubmissionUncertain):
            daemon._drain_browser_outbox_locked(self.repo, self.state)
        self.assertEqual(len(daemon._pending_outbox(self.state)), 1)


if __name__ == "__main__":
    unittest.main()
