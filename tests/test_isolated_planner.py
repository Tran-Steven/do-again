"""Offline ChatGPT planner and durable dispatch tests. Never contacts a real model."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.browser import runtime
from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.core.schema import OperatorError
from do_again.service.isolated_planner import plan_isolated_request


class IsolatedPlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "Sonary"
        self.repo.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(self.repo), *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        git("config", "core.autocrlf", "false")
        (self.repo / "app.py").write_text("def answer():\n    return 1\n")
        git("add", "app.py")
        git("commit", "-m", "baseline")
        self.head = git("rev-parse", "HEAD")
        self.before = hashlib.sha256((self.repo / "app.py").read_bytes()).hexdigest()
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.bound = "https://chatgpt.com/c/01234567-89ab-4cde-8012-0123456789ab"
        self.target = SimpleNamespace(url=self.bound)
        self.image = "python@sha256:" + "a" * 64
        self.worker = Mock(state_dir=self.state)
        self.worker.receipt_exists.return_value = False
        self.task = {
            "repo": self.repo, "control_worktree": self.root / "control",
            "state_dir": self.state, "branch": "do-again-isolated",
            "image": self.image, "goal": "Return 2 from answer",
            "files": ["app.py"],
            "test_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
        }

    def mocks(self, send):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch(
            "do_again.service.isolated_planner.create_isolated_worker",
            return_value=self.worker))
        stack.enter_context(patch(
            "do_again.browser.runtime.project_record",
            return_value={"chat_url": self.bound}))
        stack.enter_context(patch(
            "do_again.browser.runtime.ensure_browser_running",
            return_value={"port": 9224}))
        stack.enter_context(patch(
            "do_again.browser.runtime._find_chatgpt_target",
            return_value=None))
        create = stack.enter_context(patch(
            "do_again.browser.runtime.cdp.create_target", return_value=self.target))
        stack.enter_context(patch(
            "do_again.browser.runtime.wait_for_authenticated",
            return_value=(self.target, {})))
        sender = stack.enter_context(patch(
            "do_again.browser.runtime.send_message", side_effect=send))
        return sender, create

    def test_planner_auto_restores_only_bound_tab_and_publishes_scoped_git_request(self):
        def fake_send(target, prompt, *, before_dispatch, **kwargs):
            self.assertIs(target, self.target)
            before_dispatch()
            envelope = json.loads(prompt)
            self.assertEqual(envelope["files"][0]["before_sha256"], self.before)
            return {
                "chat_url": self.bound,
                "response": json.dumps({
                    "call_id": envelope["call_id"],
                    "manifest": {
                        "expected_head": self.head,
                        "changes": [{
                            "path": "app.py",
                            "before_sha256": self.before,
                            "content": "def answer():\n    return 2\n",
                        }],
                        "test_command": self.task["test_command"],
                    },
                }),
            }

        sender, create = self.mocks(fake_send)
        result = plan_isolated_request(**self.task)
        self.assertEqual(result["state"], "published")
        self.assertFalse(result["executed"])
        self.assertEqual(result["source_head"], self.head)
        sender.assert_called_once()
        create.assert_called_once_with(9224, self.bound, background=True)
        self.worker.publish_json.assert_called_once()
        args = self.worker.publish_json.call_args.args
        request = args[1]
        self.assertEqual(request["operation"], "isolated_patch")
        self.assertEqual(request["request_id"], result["request_id"])
        self.assertEqual(request["args"]["manifest"]["expected_head"], self.head)
        self.assertEqual(request["args"]["manifest"]["test_command"],
                         self.task["test_command"])
        journal = json.loads((self.state / "planning-intents" /
                              (result["request_id"] + ".json")).read_text())
        self.assertEqual(journal["state"], "published")
        # Identical goal can't be resent even if the prior task completed.
        self.worker.receipt_exists.return_value = True
        with self.assertRaisesRegex(OperatorError, "already attempted"):
            plan_isolated_request(**self.task)
        sender.assert_called_once()

    def test_uncertain_dispatch_blocks_even_a_new_goal(self):
        def uncertain(target, prompt, *, before_dispatch, **kwargs):
            before_dispatch()
            raise BrowserSubmissionUncertain("uncertain browser delivery")
        sender, _ = self.mocks(uncertain)
        with self.assertRaises(BrowserSubmissionUncertain):
            plan_isolated_request(**self.task)
        intent, = list((self.state / "planning-intents").glob("*.json"))
        self.assertEqual(json.loads(intent.read_text())["state"], "dispatch_started")
        with self.assertRaisesRegex(OperatorError, "outcome uncertain"):
            plan_isolated_request(**dict(self.task, goal="Different request"))
        sender.assert_called_once()
        self.worker.publish_json.assert_not_called()

    def test_wrong_path_reply_blocks_git_publication(self):
        def malicious(target, prompt, *, before_dispatch, **kwargs):
            before_dispatch()
            envelope = json.loads(prompt)
            return {"chat_url": self.bound, "response": json.dumps({
                "call_id": envelope["call_id"], "manifest": {
                    "expected_head": self.head,
                    "changes": [{"path": ".env.py",
                                 "before_sha256": self.before, "content": "SECRET"}],
                    "test_command": self.task["test_command"],
                }})}
        self.mocks(malicious)
        with self.assertRaisesRegex(OperatorError, "unapproved source"):
            plan_isolated_request(**self.task)
        self.worker.publish_json.assert_not_called()

    def test_planner_to_git_worker_to_receipt_round_trip(self):
        """No real ChatGPT/Docker; Git remote, claim, patch and receipt are real."""
        from do_again.service.isolated_worker import create_isolated_worker
        remote = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, timeout=30)
        def git(*args):
            return subprocess.run(["git", "-C", str(self.repo), *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git("remote", "add", "origin", str(remote))
        git("push", "origin", "main")
        git("checkout", "-b", "do-again-isolated")
        git("push", "origin", "do-again-isolated")
        git("checkout", "main")
        git("fetch", "origin", "do-again-isolated")
        control = self.root / "control"
        git("worktree", "add", "--detach", str(control), "FETCH_HEAD")
        task = dict(self.task, control_worktree=control)

        def fake_send(target, prompt, *, before_dispatch, **kwargs):
            before_dispatch()
            envelope = json.loads(prompt)
            return {"chat_url": self.bound, "response": json.dumps({
                "call_id": envelope["call_id"],
                "manifest": {
                    "expected_head": self.head,
                    "changes": [{
                        "path": "app.py", "before_sha256": self.before,
                        "content": "def answer():\\n    return 2\\n",
                    }],
                    "test_command": task["test_command"],
                },
            })}
        with patch("do_again.browser.runtime.project_record",
                   return_value={"chat_url": self.bound}), \\
             patch("do_again.browser.runtime.ensure_browser_running",
                   return_value={"port": 9224}), \\
             patch("do_again.browser.runtime._find_chatgpt_target",
                   return_value=self.target), \\
             patch("do_again.browser.runtime.wait_for_authenticated",
                   return_value=(self.target, {})), \\
             patch("do_again.browser.runtime.send_message",
                   side_effect=fake_send) as send, \\
             patch("do_again.service.isolated_cycle._docker_test",
                   return_value=True) as tested:
            plan = plan_isolated_request(**task)
            agent = create_isolated_worker(
                repo=self.repo, control_worktree=control, state_dir=self.state,
                branch=task["branch"], image=self.image)
            self.assertEqual(agent.run(once=True), 0)
            self.assertEqual(agent.run(once=True), 0)
        send.assert_called_once()
        tested.assert_called_once()
        rid = plan["request_id"]
        path = "automation/do_again/receipts/" + rid + ".json"
        receipt = json.loads(git("show", "origin/do-again-isolated:" + path))
        self.assertEqual(receipt["state"], "succeeded", receipt.get("error"))
        self.assertEqual(receipt["result"]["result"]["isolated_patch"]["state"],
                         "verified_isolated_patch")
        self.assertEqual(git("rev-parse", "HEAD"), self.head)
        self.assertEqual((self.repo / "app.py").read_text(),
                         "def answer():\\n    return 1\\n")

    def test_dirty_source_fails_without_opening_browser(self):
        (self.repo / "app.py").write_text("unsaved change")
        with patch("do_again.service.isolated_planner.create_isolated_worker",
                   return_value=self.worker), \
             patch("do_again.browser.runtime.ensure_browser_running") as browser:
            with self.assertRaisesRegex(OperatorError, "clean isolated"):
                plan_isolated_request(**self.task)
        browser.assert_not_called()


class BrowserDispatchHookTests(unittest.TestCase):
    def test_send_message_reserves_durably_before_only_enter(self):
        target = SimpleNamespace(url="https://chatgpt.com/c/01234567-89ab-4cde-8012-0123456789ab")
        events = []
        with patch("do_again.browser.runtime._assistant_snapshot",
                   return_value={"busy": False, "count": 0}), \
             patch("do_again.browser.runtime._context_limit_warning",
                   return_value=""), \
             patch("do_again.browser.runtime.cdp.evaluate",
                   side_effect=["ready", True]), \
             patch("do_again.browser.runtime.cdp.insert_text",
                   side_effect=lambda *_: events.append("insert")), \
             patch("do_again.browser.runtime.cdp.press_enter",
                   side_effect=lambda *_: events.append("enter")), \
             patch("do_again.browser.runtime.time.sleep"):
            result = runtime.send_message(
                target, "hello", wait_for_response=False,
                before_dispatch=lambda: events.append("reserved"))
        self.assertEqual(events, ["insert", "reserved", "enter"])
        self.assertEqual(result["response"], "submitted")

    def test_failed_reservation_prevents_enter(self):
        target = SimpleNamespace(url="https://chatgpt.com/c/01234567-89ab-4cde-8012-0123456789ab")
        with patch("do_again.browser.runtime._assistant_snapshot",
                   return_value={"busy": False, "count": 0}), \
             patch("do_again.browser.runtime._context_limit_warning",
                   return_value=""), \
             patch("do_again.browser.runtime.cdp.evaluate", return_value="ready"), \
             patch("do_again.browser.runtime.cdp.insert_text"), \
             patch("do_again.browser.runtime.cdp.press_enter") as enter, \
             patch("do_again.browser.runtime.time.sleep"):
            with self.assertRaisesRegex(OperatorError, "journal unavailable"):
                runtime.send_message(
                    target, "hello", wait_for_response=False,
                    before_dispatch=lambda: (_ for _ in ()).throw(
                        OperatorError("journal unavailable")))
        enter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
