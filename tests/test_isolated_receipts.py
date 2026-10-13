import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.browser import runtime
from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.core.schema import OperatorError, request_fingerprint
from do_again.service.isolated_receipts import report_isolated_receipt


class ReceiptDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "Sonary"
        self.repo.mkdir()
        self.control = self.root / "control"
        self.control.mkdir()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.requests = self.control / "automation" / "do_again" / "requests"
        self.requests.mkdir(parents=True)
        self.rid = "isolated-" + "a" * 24
        self.head = "b" * 40
        self.chat = "https://chatgpt.com/c/01234567-89ab-4cde-8012-0123456789ab"
        self.target = SimpleNamespace(url=self.chat)
        self.patch_bytes = b"synthetic validated patch"
        patchdir = self.state / "isolated-patches"
        patchdir.mkdir()
        (patchdir / ("c" * 24 + ".patch")).write_bytes(self.patch_bytes)
        self.request = {
            "schema_version": 1,
            "request_id": self.rid,
            "operation": "isolated_patch",
            "args": {"manifest": {"expected_head": self.head}},
            "expected": {},
            "limits": {"timeout_seconds": 300},
        }
        (self.requests / (self.rid + ".json")).write_text(json.dumps(self.request))
        self.receipt = {
            "request_id": self.rid,
            "request_fingerprint": request_fingerprint(self.request),
            "operation": "isolated_patch",
            "state": "succeeded",
            "result": {
                "result": {
                    "returncode": 0,
                    "isolated_patch": {
                        "state": "verified_isolated_patch",
                        "source_head": self.head,
                        "patch_file": "c" * 24 + ".patch",
                        "patch_sha256": hashlib.sha256(self.patch_bytes).hexdigest(),
                        "files": ["app.py"],
                        "tested_offline": True,
                        "automatic_merge": False,
                        "production_deployed": False,
                    },
                },
            },
        }
        intents = self.state / "planning-intents"
        intents.mkdir()
        (intents / (self.rid + ".json")).write_text(json.dumps({
            "state": "published",
            "request_id": self.rid,
            "conversation_id": runtime._chat_id(self.chat),
            "source_head": self.head,
        }))
        self.agent = Mock()
        self.agent.state_dir = self.state
        self.agent.requests_dir = self.requests
        self.agent.receipt_payload.return_value = self.receipt
        self.task = {
            "repo": self.repo,
            "control_worktree": self.control,
            "state_dir": self.state,
            "branch": "isolated-control",
            "image": "python@sha256:" + "d" * 64,
            "request_id": self.rid,
        }

    def mocks(self, sender):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch(
            "do_again.service.isolated_receipts.create_isolated_worker",
            return_value=self.agent))
        stack.enter_context(patch(
            "do_again.browser.runtime.project_record",
            return_value={"chat_url": self.chat}))
        stack.enter_context(patch(
            "do_again.browser.runtime.ensure_browser_running",
            return_value={"port": 9224}))
        stack.enter_context(patch(
            "do_again.browser.runtime._find_chatgpt_target",
            return_value=None))
        tab = stack.enter_context(patch(
            "do_again.browser.runtime.cdp.create_target", return_value=self.target))
        stack.enter_context(patch(
            "do_again.browser.runtime.wait_for_authenticated",
            return_value=(self.target, {})))
        send = stack.enter_context(patch(
            "do_again.browser.runtime.send_message", side_effect=sender))
        return send, tab

    def test_sends_exact_success_receipt_once_and_saves_ack(self):
        def answer(target, message, *, before_dispatch, **kwargs):
            self.assertIs(target, self.target)
            payload = json.loads(message)
            self.assertEqual(payload["request_id"], self.rid)
            self.assertEqual(payload["patch_sha256"], self.receipt[
                "result"]["result"]["isolated_patch"]["patch_sha256"])
            self.assertTrue(payload["tested_offline"])
            self.assertFalse(payload["automatic_merge"])
            before_dispatch()
            return {
                "chat_url": self.chat,
                "response": json.dumps({
                    "request_id": self.rid, "acknowledged": True,
                }),
            }
        send, tab = self.mocks(answer)
        result = report_isolated_receipt(**self.task)
        self.assertEqual(result["state"], "acknowledged")
        self.assertEqual(result["receipt_state"], "succeeded")
        journal = json.loads((self.state / "receipt-reports" /
                              (self.rid + ".json")).read_text())
        self.assertEqual(journal["state"], "acknowledged")
        self.assertEqual(len(journal["receipt_sha256"]), 64)
        tab.assert_called_once_with(9224, self.chat, background=True)
        with self.assertRaisesRegex(OperatorError, "already delivered"):
            report_isolated_receipt(**self.task)
        send.assert_called_once()

    def test_waits_for_receipt_without_opening_browser(self):
        self.agent.receipt_payload.return_value = None
        send, _ = self.mocks(Mock())
        result = report_isolated_receipt(**self.task)
        self.assertEqual(result["state"], "awaiting_receipt")
        self.assertFalse((self.state / "receipt-reports").exists())
        send.assert_not_called()

    def test_tampered_receipt_is_rejected_before_model_send(self):
        self.receipt["request_fingerprint"] = "f" * 64
        send, _ = self.mocks(Mock())
        with self.assertRaisesRegex(OperatorError, "does not match"):
            report_isolated_receipt(**self.task)
        send.assert_not_called()

    def test_missing_verified_patch_is_rejected(self):
        self.receipt["result"]["result"]["isolated_patch"]["patch_sha256"] = "0" * 64
        send, _ = self.mocks(Mock())
        with self.assertRaisesRegex(OperatorError, "digest mismatch"):
            report_isolated_receipt(**self.task)
        send.assert_not_called()

    def test_uncertain_send_cannot_be_replayed(self):
        def uncertain(target, message, *, before_dispatch, **kwargs):
            before_dispatch()
            raise BrowserSubmissionUncertain("unknown delivery")
        send, _ = self.mocks(uncertain)
        with self.assertRaises(BrowserSubmissionUncertain):
            report_isolated_receipt(**self.task)
        journal = json.loads((self.state / "receipt-reports" /
                              (self.rid + ".json")).read_text())
        self.assertEqual(journal["state"], "dispatch_started")
        with self.assertRaisesRegex(OperatorError, "uncertain"):
            report_isolated_receipt(**self.task)
        send.assert_called_once()

    def test_changed_chat_binding_is_rejected(self):
        self.chat = "https://chatgpt.com/c/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        send, _ = self.mocks(Mock())
        with self.assertRaisesRegex(OperatorError, "conversation changed"):
            report_isolated_receipt(**self.task)
        send.assert_not_called()

    def test_non_success_receipt_is_reported_as_failure(self):
        self.receipt["state"] = "blocked"
        self.receipt["error"] = "source changed"
        def answer(target, message, *, before_dispatch, **kwargs):
            payload = json.loads(message)
            self.assertEqual(payload["receipt_state"], "blocked")
            self.assertFalse(payload["tested_offline"])
            before_dispatch()
            return {
                "chat_url": self.chat,
                "response": json.dumps({
                    "request_id": self.rid, "acknowledged": True,
                }),
            }
        self.mocks(answer)
        result = report_isolated_receipt(**self.task)
        self.assertEqual(result["receipt_state"], "blocked")
        self.assertEqual(result["state"], "acknowledged")


if __name__ == "__main__":
    unittest.main()
