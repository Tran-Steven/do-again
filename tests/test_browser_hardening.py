from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.browser import cdp
from do_again.browser import runtime as browser
from do_again.service import daemon


class BrowserHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"DO_AGAIN_HOME": str(self.root / "home")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.target = cdp.Target("project", "https://chatgpt.com/c/project", "", "ws://127.0.0.1/project")

    def bind(self):
        return browser.register_project(self.repo, remote_url="https://github.com/example/repo.git", control_branch="operator-control", chat_url=self.target.url)

    def test_missing_bound_tab_never_reuses_other_project(self):
        other = cdp.Target("other", "https://chatgpt.com/c/other", "", "ws://127.0.0.1/other")
        with patch.object(cdp, "targets", return_value=[other]):
            self.assertIsNone(browser._find_chatgpt_target(9223, self.target.url))

    def test_new_target_auth_is_verified_by_id_even_with_same_root_url(self):
        first = cdp.Target("first", browser.CHATGPT_URL, "", "ws://127.0.0.1/first")
        second = cdp.Target("second", browser.CHATGPT_URL, "", "ws://127.0.0.1/second")
        with patch.object(cdp, "targets", return_value=[first, second]), patch.object(browser, "_prompt_probe", return_value={"prompt": True}) as probe:
            target, _ = browser.wait_for_authenticated(9223, target=second)
        self.assertEqual(target.id, "second")
        probe.assert_called_once_with(second)

    def test_guest_composer_is_not_accepted_as_authenticated(self):
        with patch.object(cdp, "targets", return_value=[self.target]), patch.object(browser, "_prompt_probe", return_value={"prompt": True, "login": True}), patch.object(browser.time, "sleep"):
            with self.assertRaises(browser.BrowserAuthRequired):
                browser.wait_for_authenticated(9223, target=self.target, timeout=0.001)

    def test_incomplete_page_load_is_retryable_without_latching_auth_failure(self):
        with patch.object(cdp, "targets", return_value=[self.target]), patch.object(browser, "_prompt_probe", return_value={"prompt": False, "login": False, "challenge": False}), patch.object(browser.time, "sleep"):
            with self.assertRaises(browser.BrowserError) as error:
                browser.wait_for_authenticated(9223, target=self.target, timeout=0.001)
        self.assertNotIsInstance(error.exception, browser.BrowserAuthRequired)

    def test_occupied_port_is_not_reused_with_stale_state(self):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            occupied = sock.getsockname()[1]
            browser.save_state({"pid": os.getpid(), "port": occupied})
            self.assertNotEqual(browser._select_port({"port": occupied}), occupied)

    def test_stale_pid_and_unrelated_cdp_are_never_closed(self):
        browser.save_state({"pid": os.getpid(), "port": 9223, "profile_dir": str(browser.browser_paths().profile), "browser_websocket_url": "ws://127.0.0.1:9223/devtools/browser/old"})
        with patch.object(browser, "_owns_process", return_value=False), patch.object(cdp, "browser_websocket_url", return_value="ws://127.0.0.1:9223/devtools/browser/other"), patch.object(cdp, "close_browser") as close, patch.object(browser.os, "kill") as kill:
            browser.stop_browser(force=True)
        close.assert_not_called()
        kill.assert_not_called()

    def test_reused_pid_without_dedicated_profile_is_not_owned(self):
        state = {"pid": os.getpid(), "port": 9223, "profile_dir": str(browser.browser_paths().profile)}
        with patch.object(browser.subprocess, "check_output", return_value="unrelated-process --remote-debugging-port=9223"):
            self.assertFalse(browser._owns_process(state))

    def test_browser_startup_failure_terminates_spawned_process(self):
        process = Mock(pid=123, returncode=None)
        process.poll.return_value = None
        binary = self.root / "chrome"
        binary.touch()
        with patch.object(browser, "discover_browser", return_value=binary), patch.object(browser, "_select_port", return_value=9223), patch.object(browser.subprocess, "Popen", return_value=process), patch.object(browser, "_wait_endpoint", side_effect=browser.BrowserError("failed")):
            with self.assertRaises(browser.BrowserError):
                browser.launch_browser("headless")
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)

    def test_live_long_running_lock_is_not_stolen(self):
        path = self.root / "lock"
        path.mkdir()
        (path / "owner.json").write_text(json.dumps({"pid": os.getpid()}))
        os.utime(path, (0, 0))
        with self.assertRaises(browser.BrowserError):
            with browser._file_lock(path, timeout=0):
                self.fail("stole a live lock")
        self.assertTrue(path.is_dir())

    def test_new_lock_without_owner_has_creation_grace(self):
        path = self.root / "lock"
        path.mkdir()
        with self.assertRaises(browser.BrowserError):
            with browser._file_lock(path, timeout=0):
                self.fail("stole an initializing lock")

    def test_dead_owner_lock_is_recovered(self):
        path = self.root / "lock"
        path.mkdir()
        (path / "owner.json").write_text(json.dumps({"pid": 999999999}))
        with browser._file_lock(path, timeout=0.1):
            self.assertEqual(json.loads((path / "owner.json").read_text())["pid"], os.getpid())
        self.assertFalse(path.exists())

    def test_two_threads_serialize_shared_browser_launch(self):
        running = threading.Event()
        launches = []
        barrier = threading.Barrier(2)
        errors = []

        def status(**kwargs):
            return {"running": running.is_set(), "port": 9223, "mode": "headless"}

        def launch(*args, **kwargs):
            launches.append(1)
            running.set()
            return status()

        def worker():
            try:
                barrier.wait(timeout=5)
                browser.ensure_browser_running(verify_auth=False)
            except Exception as exc:
                errors.append(exc)

        with patch.object(browser, "browser_status", side_effect=status), patch.object(browser, "launch_browser", side_effect=launch):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(launches), 1)

    def test_auth_failure_latches_until_interactive_setup(self):
        config = browser.load_config()
        config.update(authenticated=True, preferred_mode="auto", resolved_mode="headless")
        browser.save_config(config)
        with patch.object(browser, "browser_status", return_value={"running": True, "port": 9223, "mode": "headless"}), patch.object(browser, "wait_for_authenticated", side_effect=browser.BrowserAuthRequired("expired")), patch.object(browser, "stop_browser"), patch.object(browser, "launch_browser", return_value={"port": 9223}):
            with self.assertRaises(browser.BrowserAuthRequired):
                browser.ensure_browser_running()
        self.assertTrue(browser.load_config()["auth_required"])
        with patch.object(browser, "launch_browser") as launch:
            with self.assertRaises(browser.BrowserAuthRequired):
                browser.ensure_browser_running()
        launch.assert_not_called()

    def test_repeated_setup_uses_authenticated_runtime_without_visible_launch(self):
        config = browser.load_config()
        config["authenticated"] = True
        browser.save_config(config)
        with patch.object(browser, "discover_browser", return_value=self.root / "chrome"), patch.object(browser, "ensure_browser_running", return_value={"mode": "headless"}), patch.object(browser, "launch_browser") as launch:
            self.assertEqual(browser.setup_browser(run_iteration_test=False)["mode"], "headless")
        launch.assert_not_called()

    def test_transient_self_test_error_does_not_reopen_visible_login(self):
        config = browser.load_config()
        config["authenticated"] = True
        browser.save_config(config)
        with patch.object(browser, "discover_browser", return_value=self.root / "chrome"), patch.object(browser, "ensure_browser_running", return_value={"mode": "headless"}), patch.object(browser, "browser_self_test", side_effect=browser.BrowserError("network timeout")), patch.object(browser, "launch_browser") as launch:
            with self.assertRaises(browser.BrowserError):
                browser.setup_browser()
        launch.assert_not_called()

    def test_busy_conversation_keeps_receipt_pending_without_changing_composer(self):
        with patch.object(browser, "_assistant_snapshot", return_value={"busy": True}), patch.object(cdp, "insert_text") as insert:
            with self.assertRaises(browser.BrowserError):
                browser.send_message(self.target, "message")
        insert.assert_not_called()

    def test_failed_rollover_preserves_old_chat_binding(self):
        record = self.bind()
        new = cdp.Target("new", browser.CHATGPT_URL, "", "ws://127.0.0.1/new")
        with patch.object(cdp, "evaluate", return_value=[]), patch.object(cdp, "create_target", return_value=new), patch.object(browser, "wait_for_authenticated", return_value=(new, {})), patch.object(browser, "send_message", return_value={"response": "unready", "chat_url": "https://chatgpt.com/c/new"}), patch.object(cdp, "close_target") as close:
            with self.assertRaises(browser.BrowserError):
                browser._rollover_project_chat(self.repo, record, port=9223, old_target=self.target)
        self.assertEqual(browser.project_record(self.repo)["chat_url"], self.target.url)
        close.assert_called_once_with(9223, "new")

    def test_missing_chat_does_not_drop_durable_receipt(self):
        state = self.root / "state"
        path = daemon._queue_receipt(state, {"request_id": "req-missing"})
        with patch.object(daemon, "ensure_browser_running"):
            with self.assertRaises(browser.BrowserError):
                daemon._drain_browser_outbox(self.repo, state)
        self.assertTrue(path.exists())

    def test_receipt_filename_cannot_escape_outbox(self):
        for request_id in ["../outside", "..", "/absolute", "a/b", "a\\b"]:
            with self.subTest(request_id=request_id), self.assertRaises(browser.BrowserError):
                daemon._queue_receipt(self.root, {"request_id": request_id})

    def test_lease_updates_preserve_rollover_binding(self):
        self.bind()
        browser.activate_project(self.repo)
        browser.register_project(self.repo, chat_url="https://chatgpt.com/c/rolled")
        browser.deactivate_project(self.repo)
        self.assertEqual(browser.project_record(self.repo)["chat_url"], "https://chatgpt.com/c/rolled")
        self.assertEqual(browser.active_projects(), [])

    def test_two_delivery_workers_send_each_receipt_once(self):
        state = self.root / "state"
        daemon._queue_receipt(state, {"request_id": "req-concurrent"})
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                daemon._drain_browser_outbox(self.repo, state)
            except Exception as exc:
                errors.append(exc)

        with patch.object(daemon, "activate_project"), patch.object(daemon, "ensure_browser_running"), patch.object(daemon, "notify_receipt") as send:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        send.assert_called_once()
        self.assertEqual(daemon._pending_outbox(state), [])

    def test_daemon_executes_local_agent_when_browser_requires_auth(self):
        args = ["--repo", str(self.repo), "--control-worktree", str(self.root / "control"), "--policy", str(self.root / "policy.json"), "--state-dir", str(self.root / "state"), "--once"]
        with patch.object(daemon, "runtime_layout", return_value=Mock(browser_enabled=True)), patch.object(daemon, "Agent") as agent, patch.object(daemon, "activate_project"), patch.object(daemon, "deactivate_project"), patch.object(daemon, "stop_if_unused"), patch.object(daemon, "ensure_browser_running", side_effect=browser.BrowserAuthRequired("expired")), patch.object(daemon.signal, "signal"):
            agent.return_value.run.return_value = 0
            self.assertEqual(daemon.main(args), 0)
        agent.return_value.run.assert_called_once_with(once=True)
        state = json.loads((self.root / "state/browser_status.json").read_text())
        self.assertEqual(state["state"], "auth_required")

    def test_context_limit_after_submission_rolls_over_once(self):
        self.bind()
        new = cdp.Target("new", "https://chatgpt.com/c/new", "", "ws://127.0.0.1/new")
        with patch.object(browser, "ensure_browser_running", return_value={"port": 9223}), patch.object(browser, "_find_chatgpt_target", return_value=self.target), patch.object(browser, "wait_for_authenticated", return_value=(self.target, {})), patch.object(browser, "_page_contains", return_value=False), patch.object(browser, "_assistant_snapshot", return_value={"busy": False}), patch.object(browser, "_context_limit_warning", side_effect=["", "maximum length"]), patch.object(browser, "_rollover_project_chat", return_value=(new, {})) as rollover, patch.object(browser, "send_message", side_effect=[browser.BrowserError("limit"), {"response": "continued"}]) as send:
            self.assertEqual(browser.notify_receipt(self.repo, {"request_id": "req-after"})["response"], "continued")
        rollover.assert_called_once()
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[0], new)


if __name__ == "__main__":
    unittest.main()
