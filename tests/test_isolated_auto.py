import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.core.schema import OperatorError
from do_again.service.isolated_auto import run_isolated_auto


class BoundedAutoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source = root / "Sonary"
        self.source.mkdir()
        self.state = root / "private-state"
        self.state.mkdir(mode=0o700)
        def git(cwd, *args):
            return subprocess.run(
                ["git", "-C", str(cwd), *args],
                check=True, capture_output=True, text=True, timeout=30
            ).stdout.strip()
        self.git = git
        git(self.source, "init", "-b", "main")
        git(self.source, "config", "user.name", "Fixture")
        git(self.source, "config", "user.email", "fixture@example.invalid")
        git(self.source, "config", "core.autocrlf", "false")
        (self.source / "app.py").write_text("def value():\n    return 1\n")
        git(self.source, "add", "app.py")
        git(self.source, "commit", "-m", "baseline")
        self.original_head = git(self.source, "rev-parse", "HEAD")
        remote = root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True, capture_output=True, timeout=30)
        git(self.source, "remote", "add", "origin", str(remote))
        git(self.source, "push", "origin", "main")
        git(self.source, "checkout", "-b", "isolated-control")
        git(self.source, "push", "origin", "isolated-control")
        git(self.source, "checkout", "main")
        git(self.source, "fetch", "origin", "isolated-control")
        self.control = root / "control"
        git(self.source, "worktree", "add", "--detach",
            str(self.control), "FETCH_HEAD")
        self.chat = "https://chatgpt.com/c/01234567-89ab-4cde-8012-0123456789ab"
        self.target = SimpleNamespace(url=self.chat)
        self.task = {
            "source_repo": self.source,
            "control_worktree": self.control,
            "state_dir": self.state,
            "branch": "isolated-control",
            "image": "python@sha256:" + "a" * 64,
            "goal": "Increment the validated synthetic value",
            "files": ["app.py"],
            "test_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
            "cycles": 2,
        }

    def sender(self, *, uncertain_second_report=False):
        received_reports = 0
        def answer(target, message, *, before_dispatch, **kwargs):
            nonlocal received_reports
            self.assertIs(target, self.target)
            body = json.loads(message)
            before_dispatch()
            if "goal" in body:
                self.assertEqual(body["goal"], self.task["goal"])
                source = body["files"][0]
                current = int(source["content"].split("return ", 1)[1].strip())
                return {
                    "chat_url": self.chat,
                    "response": json.dumps({
                        "call_id": body["call_id"],
                        "manifest": {
                            "expected_head": body["source_head"],
                            "changes": [{
                                "path": "app.py",
                                "before_sha256": source["before_sha256"],
                                "content": "def value():\n    return " + str(current + 1) + "\n",
                            }],
                            "test_command": self.task["test_command"],
                        },
                    }),
                }
            received_reports += 1
            self.assertEqual(body["receipt_state"], "succeeded")
            self.assertTrue(body["tested_offline"])
            if uncertain_second_report and received_reports == 2:
                raise BrowserSubmissionUncertain("receipt dispatch outcome uncertain")
            return {
                "chat_url": self.chat,
                "response": json.dumps({
                    "request_id": body["request_id"], "acknowledged": True,
                }),
            }
        return answer

    def browser(self, sender):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        def lookup(repo):
            self.assertEqual(Path(repo).resolve(), self.source.resolve())
            return {"chat_url": self.chat}
        stack.enter_context(patch(
            "do_again.browser.runtime.project_record", side_effect=lookup))
        stack.enter_context(patch(
            "do_again.browser.runtime.ensure_browser_running",
            return_value={"port": 9224}))
        stack.enter_context(patch(
            "do_again.browser.runtime._find_chatgpt_target",
            return_value=self.target))
        stack.enter_context(patch(
            "do_again.browser.runtime.wait_for_authenticated",
            return_value=(self.target, {})))
        sent = stack.enter_context(patch(
            "do_again.browser.runtime.send_message", side_effect=sender))
        tested = stack.enter_context(patch(
            "do_again.service.isolated_cycle._docker_test",
            return_value=True))
        return sent, tested

    def test_two_consecutive_cycles_advance_only_private_source_and_resume_idempotently(self):
        sent, tested = self.browser(self.sender())
        result = run_isolated_auto(**self.task)
        self.assertEqual(result["state"], "completed_bounded_cycles")
        self.assertEqual(result["completed_cycles"], 2)
        self.assertEqual(len(result["commits"]), 2)
        self.assertEqual(sent.call_count, 4)
        self.assertEqual(tested.call_count, 2)
        self.assertEqual(
            (self.state / "isolated-source" / "app.py").read_text(),
            "def value():\n    return 3\n")
        self.assertEqual(
            (self.source / "app.py").read_text(),
            "def value():\n    return 1\n")
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.original_head)
        ledger = json.loads((self.state / "isolated-auto.json").read_text())
        self.assertEqual(ledger["state"], "completed")
        self.assertEqual(ledger["completed_cycles"], 2)
        repeat = run_isolated_auto(**self.task)
        self.assertEqual(repeat["completed_cycles"], 2)
        self.assertEqual(sent.call_count, 4)
        self.assertEqual(tested.call_count, 2)

    def test_uncertain_second_receipt_blocks_resume_without_any_new_send(self):
        sent, tested = self.browser(self.sender(uncertain_second_report=True))
        with self.assertRaises(BrowserSubmissionUncertain):
            run_isolated_auto(**self.task)
        self.assertEqual(sent.call_count, 4)
        self.assertEqual(tested.call_count, 2)
        self.assertEqual(
            (self.state / "isolated-source" / "app.py").read_text(),
            "def value():\n    return 2\n")
        with self.assertRaisesRegex(OperatorError, "previous receipt delivery outcome is uncertain"):
            run_isolated_auto(**self.task)
        self.assertEqual(sent.call_count, 4)
        self.assertEqual(tested.call_count, 2)
        self.assertEqual(
            (self.source / "app.py").read_text(),
            "def value():\n    return 1\n")

    def test_goal_complete_stops_without_unnecessary_second_patch(self):
        ordinary = self.sender()
        def complete_after_first_change(target, message, *, before_dispatch, **kwargs):
            body = json.loads(message)
            if "goal" in body and "return 2" in body["files"][0]["content"]:
                before_dispatch()
                return {
                    "chat_url": self.chat,
                    "response": json.dumps({
                        "call_id": body["call_id"],
                        "status": "goal_complete",
                    }),
                }
            return ordinary(target, message, before_dispatch=before_dispatch, **kwargs)

        sent, tested = self.browser(complete_after_first_change)
        result = run_isolated_auto(**self.task)
        self.assertEqual(result["state"], "model_declared_complete")
        self.assertEqual(result["completed_cycles"], 1)
        self.assertEqual(sent.call_count, 3)
        tested.assert_called_once()
        self.assertEqual(
            (self.state / "isolated-source" / "app.py").read_text(),
            "def value():\n    return 2\n")
        self.assertEqual(
            (self.source / "app.py").read_text(),
            "def value():\n    return 1\n")
        ledger = json.loads((self.state / "isolated-auto.json").read_text())
        self.assertEqual(ledger["state"], "model_declared_complete")
        repeat = run_isolated_auto(**self.task)
        self.assertEqual(repeat["state"], "model_declared_complete")
        self.assertEqual(sent.call_count, 3)
        tested.assert_called_once()

    def test_invalid_cycle_budget_rejected_before_source_changes(self):
        for count in (0, 11, True):
            with self.subTest(cycles=count):
                with self.assertRaisesRegex(OperatorError, "1 to 10"):
                    run_isolated_auto(**dict(self.task, cycles=count))
        self.assertFalse((self.state / "isolated-source").exists())


if __name__ == "__main__":
    unittest.main()
