"""No lost browser helper may invent a ChatGPT Send or clear uncertain evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.supervisor import browser_runner
from do_again.core.schema import atomic_json


class BrowserRunnerDeadlineTests(unittest.TestCase):
    REQUEST = "browser-timeout-unique-0001"

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.state=self.root/"state"
        self.repo=self.root/"repo"
        self.repo.mkdir()
        self.state.mkdir()
        self.args=["browser-runner",str(self.repo),str(self.state),
                   str(self.root/"control"),"{}","12345",
                   self.REQUEST,"a"*40,"4"]

    def preparing(self, *, state="preparing", missing_outbox=False):
        value={"schema_version":1,"state":state,
               "request_ids":["example-queue-0001"],
               "batch_marker":"DO_AGAIN_RECEIPTS_READY request_ids=example-queue-0001",
               "binding_identity":"fixture-only","chat_url":"https://chatgpt.com/c/fixture"}
        atomic_json(self.state/"browser_submission_uncertain.json",value)
        if not missing_outbox:
            atomic_json(self.state/"browser_outbox/example-queue-0001.json",
                        {"request_id":"example-queue-0001","kind":"receipt_notification"})
        return value

    def invoke(self, error):
        with patch.object(browser_runner.sys,"argv",self.args), \\
             patch.object(browser_runner,"_tick_with_root_budget",side_effect=error):
            browser_runner.main()

    def test_timeout_before_durable_send_creates_terminal_root_evidence(self):
        self.preparing()
        self.invoke(TimeoutError("fixture deadline"))
        receipt=json.loads((self.state/"browser_tick_receipts"/(self.REQUEST+".json")).read_text())
        self.assertEqual(receipt["request_id"],self.REQUEST)
        self.assertEqual(receipt["source_sha"],"a"*40)
        self.assertEqual(receipt["epoch"],4)
        self.assertEqual(receipt["result"]["state"],"completed")
        self.assertEqual(receipt["result"]["delivered"],0)
        self.assertEqual(receipt["result"]["liveness"],"pre_dispatch_blocked")
        self.assertTrue(receipt["result"]["outbox_preserved"])
        self.assertFalse((self.state/"browser_submission_uncertain.json").exists())
        self.assertTrue((self.state/"browser_outbox/example-queue-0001.json").is_file())

    def test_unknown_browser_error_with_proven_pre_send_is_terminal(self):
        from do_again.browser.errors import BrowserError
        self.preparing()
        self.invoke(BrowserError("fixture UI hydration error"))
        receipt=json.loads((self.state/"browser_tick_receipts"/(self.REQUEST+".json")).read_text())
        self.assertEqual(receipt["result"]["delivered"],0)
        self.assertFalse((self.state/"browser_submission_uncertain.json").exists())

    def test_post_send_or_missing_evidence_stays_uncertain(self):
        from do_again.browser.errors import BrowserError
        for stage in ("dispatch_started","preparing","absent"):
            with self.subTest(stage=stage):
                path=self.state/"browser_submission_uncertain.json"
                if path.exists():path.unlink()
                if stage=="dispatch_started":
                    self.preparing(state=stage)
                if stage=="preparing":
                    self.preparing(missing_outbox=True)
                    (self.state/"browser_outbox/example-queue-0001.json").unlink(missing_ok=True)
                error=BrowserError("unknown after gesture")
                with self.assertRaises(BrowserError):
                    self.invoke(error)
                self.assertFalse((self.state/"browser_tick_receipts"/(self.REQUEST+".json")).exists())
                if stage!="absent":
                    self.assertTrue(path.is_file())

    def test_broken_intent_never_certifies_no_send(self):
        for extra in ({"payload_sha256":"d"*64},{"acknowledgment_token":"token"}):
            with self.subTest(extra=extra):
                value=self.preparing()
                atomic_json(self.state/"browser_submission_uncertain.json",{**value,**extra})
                with self.assertRaises(TimeoutError):
                    self.invoke(TimeoutError("deadline"))
                self.assertFalse((self.state/"browser_tick_receipts"/(self.REQUEST+".json")).exists())


if __name__=="__main__":
    unittest.main()
