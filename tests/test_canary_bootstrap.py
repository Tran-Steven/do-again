"""Offline contracts for one-shot, headless canary bootstrap. No browser send."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.supervisor.canary_bootstrap import bootstrap_live_canary
from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(os.name == "posix", "canary operator bootstrap uses macOS/POSIX identity")
class CanaryBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.nonce = "e" * 24
        self.url = "https://chatgpt.com/c/" + "b" * 36
        self.output = self.home / "grant.json"
        self.config = {
            "operator_home": str(self.home), "operator_uid": os.getuid(),
            "legacy_authority_path": str(self.home / "authority.sqlite"),
            "production_ready": False,
            "source_sha": "a" * 40,
            "projects": [{"account": "_doagain_da", "repo": str(self.home / "do-again"),
                          "key": "parent", "uid": 401, "gid": 401, "worktree": "/sealed",
                          "source_sha": "f" * 40}],
        }
        self.target = SimpleNamespace(id="new-tab", url="https://chatgpt.com/")
        self.record = {"chat_url": self.url, "binding_generation": "unique-generation"}
        self.browser = SimpleNamespace(
            CHATGPT_URL="https://chatgpt.com/",
            browser_paths=Mock(return_value=SimpleNamespace(
                projects=self.home / "browser" / "projects")),
            ensure_browser_running=Mock(return_value={"port": 9224, "mode": "headless"}),
            wait_for_authenticated=Mock(return_value=(self.target, {})),
            send_message=Mock(),
            register_project=Mock(return_value=self.record),
            binding_identity=Mock(return_value="f" * 64),
        )
        self.cdp = patch("do_again.browser.cdp.create_target", return_value=self.target)
        self.close = patch("do_again.browser.cdp.close_target", return_value=True)
        self.authority = patch("do_again.supervisor.canary_bootstrap.AuthorityRegistry")
        self.create = self.cdp.start()
        self.closed = self.close.start()
        self.registry = self.authority.start()
        self.addCleanup(self.cdp.stop)
        self.addCleanup(self.close.stop)
        self.addCleanup(self.authority.stop)
        self.registry.return_value.status.return_value = {
            "intent": "maintenance", "epoch": 2}
        self.baseline = "c" * 40

    def run_bootstrap(self):
        return bootstrap_live_canary(
            self.config, baseline=self.baseline, nonce=self.nonce,
            output=self.output, browser=self.browser)

    def succeed(self, target, prompt, *, before_dispatch, **kwargs):
        before_dispatch()
        self.assertIn("No development request is authorized", prompt)
        return {"response": "DO_AGAIN_CANARY_CHAT_READY_" + self.nonce,
                "chat_url": self.url}

    def ticket(self):
        return json.loads((self.home / ".do_again" / "canary-bootstrap" /
                           (self.nonce + ".json")).read_text())

    def test_headless_chat_bootstraps_and_seals_private_exact_grant(self):
        self.browser.send_message.side_effect = self.succeed
        result = self.run_bootstrap()
        self.assertFalse(result["production_ready"])
        self.assertEqual(result["chat_url"], self.url)
        grant = json.loads(self.output.read_text())
        self.assertEqual(grant, {
            "nonce": self.nonce, "baseline": self.baseline,
            "parent_epoch": 2, "chat_url": self.url,
            "binding_identity": "f" * 64})
        self.assertEqual(self.ticket()["state"], "grant_sealed")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.create.call_count, 1)
        self.create.assert_called_once_with(9224, "https://chatgpt.com/",
                                            background=True)
        self.browser.register_project.assert_called_once()
        self.assertEqual(self.browser.register_project.call_args.kwargs["chat_url"], self.url)
        self.assertFalse((self.home / ".do_again" / "live-canary" / self.nonce).exists())
        with self.assertRaises(ExecutionBlocked):
            self.run_bootstrap()
        self.browser.send_message.assert_called_once()
        self.closed.assert_not_called()

    def test_uncertain_send_leaves_reserved_identity_and_no_retry(self):
        def fail(target, prompt, *, before_dispatch, **kwargs):
            before_dispatch()
            raise BrowserSubmissionUncertain("lost response")
        self.browser.send_message.side_effect = fail
        with self.assertRaises(BrowserSubmissionUncertain):
            self.run_bootstrap()
        self.assertEqual(self.ticket()["state"], "submission_unresolved")
        self.assertFalse(self.output.exists())
        self.browser.register_project.assert_not_called()
        self.closed.assert_not_called()
        with self.assertRaises(ExecutionBlocked):
            self.run_bootstrap()
        self.assertEqual(self.browser.send_message.call_count, 1)

    def test_bad_acknowledgment_does_not_authorize_grant(self):
        def wrong(target, prompt, *, before_dispatch, **kwargs):
            before_dispatch()
            return {"response": "close but wrong", "chat_url": self.url}
        self.browser.send_message.side_effect = wrong
        with self.assertRaisesRegex(ExecutionBlocked, "exact readiness"):
            self.run_bootstrap()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.ticket()["state"], "submission_unresolved")
        self.browser.register_project.assert_not_called()

    def test_pre_dispatch_auth_failure_consumes_nonce_and_closes_tab(self):
        self.browser.wait_for_authenticated.side_effect = ValueError("challenge")
        with self.assertRaisesRegex(ValueError, "challenge"):
            self.run_bootstrap()
        self.assertEqual(self.ticket()["state"], "reserved")
        self.closed.assert_called_once_with(9224, "new-tab")
        self.browser.send_message.assert_not_called()
        with self.assertRaises(ExecutionBlocked):
            self.run_bootstrap()

    def test_rejects_prior_canary_chat_and_rejects_live_parent(self):
        self.config["live_canary"] = {"chat_url": self.url}
        self.browser.send_message.side_effect = self.succeed
        with self.assertRaisesRegex(ExecutionBlocked, "distinct ChatGPT"):
            self.run_bootstrap()
        self.assertEqual(self.ticket()["state"], "submission_unresolved")
        self.assertFalse(self.output.exists())

    def test_rejects_nonmaintenance_and_production_without_browser_effect(self):
        for change in ("production", "active", "invalid_baseline"):
            with self.subTest(change=change):
                self.browser.ensure_browser_running.reset_mock()
                self.registry.return_value.status.return_value = {
                    "intent": "active" if change == "active" else "maintenance",
                    "epoch": 2}
                self.config["production_ready"] = (change == "production")
                source = "not-a-sha" if change == "invalid_baseline" else self.baseline
                with self.assertRaises(ExecutionBlocked):
                    bootstrap_live_canary(self.config, baseline=source,
                        nonce=self.nonce, output=self.output, browser=self.browser)
                self.browser.ensure_browser_running.assert_not_called()


if __name__ == "__main__":
    unittest.main()
