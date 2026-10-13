import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.browser import runtime
from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.core.schema import OperatorError
from do_again.service.isolated_flow import run_isolated_once


class IsolatedFullCycleTests(unittest.TestCase):
    def fixture(self, name):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo = root / name
        repo.mkdir()
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True, capture_output=True, text=True, timeout=30
            ).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "core.autocrlf", "false")
        (repo / "app.py").write_text("def value():\n    return 1\n")
        git("add", "app.py")
        git("commit", "-m", "baseline")
        head = git("rev-parse", "HEAD")
        old = hashlib.sha256((repo / "app.py").read_bytes()).hexdigest()
        remote = root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, timeout=30)
        git("remote", "add", "origin", str(remote))
        git("push", "origin", "main")
        git("checkout", "-b", "isolated-control")
        git("push", "origin", "isolated-control")
        git("checkout", "main")
        git("fetch", "origin", "isolated-control")
        control = root / "control"
        git("worktree", "add", "--detach", str(control), "FETCH_HEAD")
        state = root / "state"
        state.mkdir(mode=0o700)
        chat = "https://chatgpt.com/c/01234567-89ab-4cde-8012-0123456789ab"
        task = {
            "repo": repo, "control_worktree": control,
            "state_dir": state, "branch": "isolated-control",
            "image": "python@sha256:" + "a" * 64,
            "goal": "Update " + name + " synthetic fixture",
            "files": ["app.py"],
            "test_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
        }
        return task, git, head, old, chat

    def browser(self, chat, callback):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        target = SimpleNamespace(url=chat)
        stack.enter_context(patch(
            "do_again.browser.runtime.project_record",
            return_value={"chat_url": chat}))
        stack.enter_context(patch(
            "do_again.browser.runtime.ensure_browser_running",
            return_value={"port": 9224}))
        stack.enter_context(patch(
            "do_again.browser.runtime._find_chatgpt_target",
            return_value=target))
        stack.enter_context(patch(
            "do_again.browser.runtime.wait_for_authenticated",
            return_value=(target, {})))
        sent = stack.enter_context(patch(
            "do_again.browser.runtime.send_message", side_effect=callback))
        tested = stack.enter_context(patch(
            "do_again.service.isolated_cycle._docker_test", return_value=True))
        return sent, tested

    def reply(self, task, head, before_sha, chat, *, failure=None):
        def answer(target, message, *, before_dispatch, **kwargs):
            body = json.loads(message)
            before_dispatch()
            if "goal" in body:
                if failure == "plan":
                    raise BrowserSubmissionUncertain("planning send uncertain")
                return {
                    "chat_url": chat,
                    "response": json.dumps({
                        "call_id": body["call_id"],
                        "manifest": {
                            "expected_head": head,
                            "changes": [{
                                "path": "app.py",
                                "before_sha256": before_sha,
                                "content": "def value():\n    return 2\n",
                            }],
                            "test_command": task["test_command"],
                        },
                    }),
                }
            if failure == "report":
                raise BrowserSubmissionUncertain("report send uncertain")
            self.assertEqual(body["receipt_state"], "succeeded")
            self.assertTrue(body["tested_offline"])
            return {
                "chat_url": chat,
                "response": json.dumps({
                    "request_id": body["request_id"],
                    "acknowledged": True,
                }),
            }
        return answer

    def test_two_project_fixtures_complete_independent_cycles(self):
        for name in ("Sonary", "jobpipe"):
            with self.subTest(project=name):
                task, git, head, before, chat = self.fixture(name)
                sent, tested = self.browser(
                    chat, self.reply(task, head, before, chat))
                result = run_isolated_once(**task)
                self.assertEqual(result["state"], "completed")
                self.assertTrue(result["chatgpt_acknowledged"])
                self.assertFalse(result["planning_reused"])
                self.assertFalse(result["production_deployed"])
                self.assertFalse(result["automatic_merge"])
                self.assertEqual(git("rev-parse", "HEAD"), head)
                self.assertEqual(
                    (task["repo"] / "app.py").read_text(),
                    "def value():\n    return 1\n")
                receipt_path = (
                    "automation/do_again/receipts/" + result["request_id"] + ".json")
                git("fetch", "origin", "isolated-control")
                receipt = json.loads(git("show", "FETCH_HEAD:" + receipt_path))
                self.assertEqual(receipt["state"], "succeeded")
                self.assertEqual(
                    receipt["result"]["result"]["isolated_patch"]["state"],
                    "verified_isolated_patch")
                self.assertEqual(sent.call_count, 2)
                tested.assert_called_once()
                resumed = run_isolated_once(**task)
                self.assertEqual(resumed["state"], "completed")
                self.assertTrue(resumed["planning_reused"])
                self.assertEqual(sent.call_count, 2)
                tested.assert_called_once()

    def test_uncertain_plan_cannot_restart(self):
        task, git, head, before, chat = self.fixture("Sonary")
        sent, tested = self.browser(
            chat, self.reply(task, head, before, chat, failure="plan"))
        with self.assertRaises(BrowserSubmissionUncertain):
            run_isolated_once(**task)
        with self.assertRaisesRegex(OperatorError, "incomplete or uncertain"):
            run_isolated_once(**task)
        sent.assert_called_once()
        tested.assert_not_called()

    def test_uncertain_receipt_cannot_resend(self):
        task, git, head, before, chat = self.fixture("jobpipe")
        sent, tested = self.browser(
            chat, self.reply(task, head, before, chat, failure="report"))
        with self.assertRaises(BrowserSubmissionUncertain):
            run_isolated_once(**task)
        with self.assertRaisesRegex(OperatorError, "receipt delivery outcome is uncertain"):
            run_isolated_once(**task)
        self.assertEqual(sent.call_count, 2)
        tested.assert_called_once()


if __name__ == "__main__":
    unittest.main()
