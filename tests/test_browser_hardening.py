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

    def test_headless_launch_failure_automatically_uses_background(self):
        with patch.object(browser, "browser_status", side_effect=[{"running": False}, {"running": True, "port": 9223, "mode": "background"}]), patch.object(browser, "stop_browser"), patch.object(browser, "launch_browser", side_effect=[browser.BrowserError("headless startup failed"), {"port": 9223}]) as launch:
            status = browser.ensure_browser_running(verify_auth=False)
        self.assertEqual(status["mode"], "background")
        self.assertEqual([call.args[0] for call in launch.call_args_list], ["headless", "background"])
        self.assertEqual(browser.load_config()["resolved_mode"], "background")

    def test_explicit_mode_change_restarts_an_existing_browser(self):
        config = browser.load_config()
        config["preferred_mode"] = "background"
        browser.save_config(config)
        with patch.object(browser, "browser_status", side_effect=[{"running": True, "port": 9223, "mode": "headless"}, {"running": True, "port": 9223, "mode": "background"}]), patch.object(browser, "stop_browser") as stop, patch.object(browser, "launch_browser") as launch:
            self.assertEqual(browser.ensure_browser_running(verify_auth=False)["mode"], "background")
        stop.assert_called_once_with(force=True)
        self.assertEqual(launch.call_args.args[0], "background")

    def test_background_network_failure_does_not_mark_auth_expired(self):
        config = browser.load_config()
        config.update(authenticated=True, resolved_mode="background")
        browser.save_config(config)
        with patch.object(browser, "browser_status", return_value={"running": True, "port": 9223, "mode": "background"}), patch.object(browser, "wait_for_authenticated", side_effect=browser.BrowserError("network unavailable")):
            with self.assertRaises(browser.BrowserError):
                browser.ensure_browser_running()
        self.assertTrue(browser.load_config()["authenticated"])
        self.assertFalse(browser.load_config().get("auth_required", False))

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

    def test_assistant_snapshot_strips_chatgpt_presentation_prefix(self):
        payload = {
            "count": 1,
            "latest": "ChatGPT said:\n\nDO_AGAIN_BROWSER_OK",
            "busy": False,
            "url": self.target.url,
        }
        with patch.object(cdp, "evaluate", return_value=payload):
            snapshot = browser._assistant_snapshot(self.target)
        self.assertEqual(snapshot["latest"], "DO_AGAIN_BROWSER_OK")

    def test_assistant_text_does_not_strip_inline_chatgpt_said_content(self):
        value = "ChatGPT said: this is actual content"
        self.assertEqual(browser._normalize_assistant_text(value), value)

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

    def test_rollover_transport_failure_closes_unbound_target(self):
        record = self.bind()
        new = cdp.Target("new", browser.CHATGPT_URL, "", "ws://127.0.0.1/new")
        with patch.object(cdp, "evaluate", return_value=[]), patch.object(cdp, "create_target", return_value=new), patch.object(browser, "wait_for_authenticated", return_value=(new, {})), patch.object(browser, "send_message", side_effect=browser.BrowserError("connection lost")), patch.object(cdp, "close_target") as close:
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

        with patch.object(daemon, "activate_project"), patch.object(daemon, "ensure_browser_running"), patch.object(daemon, "notify_receipts", return_value={"response":"already_delivered"}) as send:
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


    def test_rollover_failed_ack_preserves_binding_and_persists_failed_transaction(self):
        record = self.bind()
        new = cdp.Target("new", browser.CHATGPT_URL, "", "ws://127.0.0.1/new")
        with patch.object(browser, "_build_rollover_checkpoint", return_value={
            "handoff_token": "fixedtoken", "project": "repo", "completed_request_ids": [],
            "pending_request_ids": [], "receipts": {}, "next_action": "continue"
        }), patch.object(browser.secrets, "token_hex", return_value="fixedtoken"), patch.object(
            cdp, "create_target", return_value=new
        ), patch.object(
            browser, "wait_for_authenticated", return_value=(new, {})
        ), patch.object(
            browser, "send_message",
            return_value={"response": "wrong marker", "chat_url": "https://chatgpt.com/c/new"}
        ), patch.object(cdp, "close_target") as close:
            with self.assertRaisesRegex(browser.BrowserError, "handoff token"):
                browser._rollover_project_chat(self.repo, record, port=9223, old_target=self.target)
        self.assertEqual(browser.project_record(self.repo)["chat_url"], self.target.url)
        tx = browser._read_json(browser._rollover_transaction_path(self.repo))
        self.assertEqual(tx["state"], "failed")
        self.assertEqual(tx["predecessor_chat_url"], self.target.url)
        close.assert_called_once_with(9223, "new")

    def test_successful_rollover_records_owned_successor_before_binding(self):
        record = self.bind()
        browser._record_owned_chat(self.repo, self.target.url, created_reason="bootstrap")
        new = cdp.Target("new", browser.CHATGPT_URL, "", "ws://127.0.0.1/new")
        checkpoint = {
            "handoff_token": "fixedtoken", "project": "repo", "completed_request_ids": [],
            "pending_request_ids": [], "receipts": {}, "next_action": "continue"
        }
        with patch.object(browser, "_build_rollover_checkpoint", return_value=checkpoint), patch.object(
            browser.secrets, "token_hex", return_value="fixedtoken"
        ), patch.object(cdp, "create_target", return_value=new), patch.object(
            browser, "wait_for_authenticated", return_value=(new, {})
        ), patch.object(
            browser, "send_message",
            return_value={
                "response": "DO_AGAIN_HANDOFF_READY fixedtoken",
                "chat_url": "https://chatgpt.com/c/new"
            }
        ), patch.object(browser, "process_archive_queue", return_value={"items": []}), patch.object(cdp, "close_target"):
            _, updated = browser._rollover_project_chat(
                self.repo, record, port=9223, old_target=self.target
            )
        self.assertEqual(updated["chat_url"], "https://chatgpt.com/c/new")
        tx = browser._read_json(browser._rollover_transaction_path(self.repo))
        self.assertEqual(tx["state"], "bound")
        self.assertEqual(tx["archive_state"], "pending")
        registry = browser._read_json(browser._owned_chat_registry_path(self.repo))
        successor = next(row for row in registry["chats"] if row["chat_id"] == "new")
        self.assertEqual(successor["handoff_token"], "fixedtoken")

    def test_rollover_pressure_uses_configured_character_threshold(self):
        with patch.object(browser, "_context_limit_warning", return_value=""), patch.object(
            browser, "load_config", return_value={"rollover_char_threshold": 50000}
        ), patch.object(browser, "_conversation_pressure_chars", return_value=50000):
            self.assertTrue(browser._rollover_needed(self.target))
        with patch.object(browser, "_context_limit_warning", return_value=""), patch.object(
            browser, "load_config", return_value={"rollover_char_threshold": 50000}
        ), patch.object(browser, "_conversation_pressure_chars", return_value=49999):
            self.assertFalse(browser._rollover_needed(self.target))

    def test_checkpoint_is_grounded_in_control_requests_and_receipt_hashes(self):
        home = self.root / "home"
        with patch.dict(os.environ, {"DO_AGAIN_HOME": str(home)}):
            control = home / "projects" / browser._project_key(self.repo) / "control"
            requests = control / "automation/do_again/requests"
            receipts = control / "automation/do_again/receipts"
            requests.mkdir(parents=True)
            receipts.mkdir(parents=True)
            (requests / "request-one.json").write_text("{}")
            (requests / "request-two.json").write_text("{}")
            (receipts / "request-one.json").write_text(
                json.dumps({"request_id": "request-one", "state": "succeeded"})
            )
            record = browser.register_project(
                self.repo,
                remote_url="https://github.com/example/repo.git",
                control_branch="operator-control",
                chat_url=self.target.url,
            )
            cp = browser._build_rollover_checkpoint(
                self.repo, record, token="checkpoint-token"
            )
        self.assertEqual(cp["completed_request_ids"], ["request-one"])
        self.assertEqual(cp["pending_request_ids"], ["request-two"])
        self.assertEqual(cp["receipts"]["request-one"]["state"], "succeeded")
        self.assertEqual(len(cp["receipts"]["request-one"]["sha256"]), 64)




    def test_rollover_requires_exact_handoff_response_not_containment(self):
        record = self.bind()
        new = cdp.Target("new", browser.CHATGPT_URL, "", "ws://127.0.0.1/new")
        checkpoint = {
            "handoff_token": "fixedtoken", "project": "repo", "completed_request_ids": [],
            "pending_request_ids": [], "receipts": {}, "next_action": "continue"
        }
        with patch.object(browser, "_build_rollover_checkpoint", return_value=checkpoint), patch.object(
            browser.secrets, "token_hex", return_value="fixedtoken"
        ), patch.object(cdp, "create_target", return_value=new), patch.object(
            browser, "wait_for_authenticated", return_value=(new, {})
        ), patch.object(
            browser, "send_message",
            return_value={
                "response": "prefix DO_AGAIN_HANDOFF_READY fixedtoken suffix",
                "chat_url": "https://chatgpt.com/c/new"
            }
        ), patch.object(cdp, "close_target"):
            with self.assertRaisesRegex(browser.BrowserError, "handoff token"):
                browser._rollover_project_chat(self.repo, record, port=9223, old_target=self.target)

    def test_rollover_prompt_has_only_handoff_readiness_instruction(self):
        record = self.bind()
        new = cdp.Target("new", browser.CHATGPT_URL, "", "ws://127.0.0.1/new")
        checkpoint = {
            "handoff_token": "fixedtoken", "project": "repo", "completed_request_ids": [],
            "pending_request_ids": [], "receipts": {}, "next_action": "continue"
        }
        captured = {}
        def send(target, prompt, **kwargs):
            captured["prompt"] = prompt
            return {"response": "DO_AGAIN_HANDOFF_READY fixedtoken", "chat_url": "https://chatgpt.com/c/new"}
        with patch.object(browser, "_build_rollover_checkpoint", return_value=checkpoint), patch.object(
            browser.secrets, "token_hex", return_value="fixedtoken"
        ), patch.object(cdp, "create_target", return_value=new), patch.object(
            browser, "wait_for_authenticated", return_value=(new, {})
        ), patch.object(browser, "send_message", side_effect=send), patch.object(
            browser, "process_archive_queue", return_value={"items": []}
        ), patch.object(cdp, "close_target"):
            browser._rollover_project_chat(self.repo, record, port=9223, old_target=self.target)
        self.assertNotIn("Reply exactly: DO_AGAIN_PROJECT_READY", captured["prompt"])
        self.assertIn("reply exactly: DO_AGAIN_HANDOFF_READY fixedtoken", captured["prompt"])

    def test_checkpoint_transfer_is_bounded_but_durable_checkpoint_retains_full_history(self):
        checkpoint = {
            "schema_version": 1,
            "handoff_token": "t",
            "project": "repo",
            "completed_request_ids": [f"done-{i}" for i in range(100)],
            "pending_request_ids": [f"pending-{i}" for i in range(75)],
            "receipts": {f"done-{i}": {"state": "succeeded", "sha256": str(i)} for i in range(100)},
            "unresolved_issues": [{"request_id": f"bad-{i}", "state": "failed"} for i in range(30)],
            "durable_checkpoint_path": "/tmp/full.json",
        }
        view = browser._checkpoint_transfer_view(checkpoint)
        self.assertEqual(len(view["completed_request_ids"]), 40)
        self.assertEqual(view["completed_request_count"], 100)
        self.assertEqual(len(view["pending_request_ids"]), 40)
        self.assertEqual(view["pending_request_count"], 75)
        self.assertEqual(len(view["unresolved_issues"]), 20)
        self.assertEqual(view["durable_checkpoint_path"], "/tmp/full.json")

    def test_rollover_archive_failure_does_not_undo_successful_binding(self):
        record = self.bind()
        browser._record_owned_chat(self.repo, self.target.url, created_reason="bootstrap")
        new = cdp.Target("new", browser.CHATGPT_URL, "", "ws://127.0.0.1/new")
        checkpoint = {
            "handoff_token": "fixedtoken", "project": "repo", "completed_request_ids": [],
            "pending_request_ids": [], "receipts": {}, "next_action": "continue"
        }
        with patch.object(browser, "_build_rollover_checkpoint", return_value=checkpoint), patch.object(
            browser.secrets, "token_hex", return_value="fixedtoken"
        ), patch.object(cdp, "create_target", return_value=new), patch.object(
            browser, "wait_for_authenticated", return_value=(new, {})
        ), patch.object(
            browser, "send_message",
            return_value={"response": "DO_AGAIN_HANDOFF_READY fixedtoken", "chat_url": "https://chatgpt.com/c/new"}
        ), patch.object(browser, "queue_verified_archives"), patch.object(
            browser, "process_archive_queue", side_effect=browser.BrowserError("archive ui changed")
        ), patch.object(cdp, "close_target"):
            _, updated = browser._rollover_project_chat(self.repo, record, port=9223, old_target=self.target)
        self.assertEqual(updated["chat_url"], "https://chatgpt.com/c/new")
        tx = browser._read_json(browser._rollover_transaction_path(self.repo))
        self.assertEqual(tx["state"], "bound")
        self.assertEqual(tx["archive_state"], "retry")
        self.assertIn("archive ui changed", tx["archive_error"])

    def test_historical_verification_provenance_is_persisted(self):
        chat_id = "historical-owned"
        with patch.object(browser, "_all_bound_chat_ids", return_value=set()), patch.object(
            browser, "_candidate_receipt_evidence", return_value=[{"receipt": "x"}]
        ), patch.object(browser, "ensure_browser_running", return_value={"port": 9223}), patch.object(
            browser, "_verify_historical_chat_ownership", return_value={"owned": True}
        ):
            result = browser.verify_candidate_chat(self.repo, chat_id)
        self.assertTrue(result["verified"])
        registry = browser._read_json(browser._owned_chat_registry_path(self.repo))
        row = next(row for row in registry["chats"] if row["chat_id"] == chat_id)
        self.assertEqual(row["verified_from"], "bootstrap_markers_and_receipts")
        self.assertTrue(row["verified_utc"])

    def test_historical_rollover_bootstrap_markers_count_as_project_ownership(self):
        record = self.bind()
        fragments = [
            "Do Again automation workspace bootstrap.",
            f"Project: {self.repo.name}",
            f"Repository: {record['remote_url']}",
            "Control branch: operator-control",
            "DO_AGAIN_HANDOFF_READY abcdef",
        ]
        with patch.object(browser, "_page_contains", side_effect=lambda target, marker: any(marker in fragment for fragment in fragments)):
            self.assertTrue(browser._chat_has_bootstrap_markers(
                self.target,
                repo=self.repo,
                remote_url=record["remote_url"],
                control_branch="operator-control",
            ))
        fragments[2] = "Repository: https://github.com/another/repo.git"
        with patch.object(browser, "_page_contains", side_effect=lambda target, marker: any(marker in fragment for fragment in fragments)):
            self.assertFalse(browser._chat_has_bootstrap_markers(
                self.target,
                repo=self.repo,
                remote_url=record["remote_url"],
                control_branch="operator-control",
            ))

    def test_historical_chat_candidates_are_discovered_but_not_automatically_archived(self):
        record = self.bind()
        record["previous_chat_url"] = "https://chatgpt.com/c/previous"
        browser._atomic_json(browser._project_record_path(self.repo), record)
        browser._atomic_json(
            browser._rollover_transaction_path(self.repo),
            {"predecessor_chat_url": "https://chatgpt.com/c/rollover"},
        )
        browser._atomic_json(
            browser._checkpoint_path(self.repo, "old"),
            {"repo": str(self.repo.resolve()), "active_chat_url": "https://chatgpt.com/c/checkpoint"},
        )
        browser._atomic_json(
            browser._checkpoint_path(self.repo, "unrelated"),
            {"repo": "/other/repo", "active_chat_url": "https://chatgpt.com/c/stranger"},
        )
        with patch.object(browser, "_candidate_receipt_evidence", return_value=[]):
            inventory = browser.chat_cleanup_inventory(self.repo)
            cleanup = browser.queue_verified_archives(self.repo, apply=True)
        candidates = {row["chat_id"]: row for row in inventory["candidates"]}
        self.assertEqual(set(candidates), {"previous", "rollover", "checkpoint"})
        self.assertTrue(all(not row["eligible"] for row in candidates.values()))
        self.assertTrue(all(row["reason"] == "needs_content_verification" for row in candidates.values()))
        self.assertEqual(cleanup["queued_chat_ids"], [])
        self.assertEqual(cleanup["queue"]["items"], [])

    def test_invalid_historical_checkpoint_does_not_block_cleanup_inventory(self):
        record = self.bind()
        record["previous_chat_url"] = "https://chatgpt.com/c/prior"
        browser._atomic_json(browser._project_record_path(self.repo), record)
        damaged = browser._checkpoint_path(self.repo, "damaged")
        damaged.parent.mkdir(parents=True, exist_ok=True)
        damaged.write_text("invalid-json", encoding="utf-8")
        with patch.object(browser, "_candidate_receipt_evidence", return_value=[]):
            inventory = browser.chat_cleanup_inventory(self.repo)
        self.assertEqual([row["chat_id"] for row in inventory["candidates"]], ["prior"])

    def test_historical_checkpoint_can_be_verified_without_receipt_ids(self):
        self.bind()
        browser._atomic_json(
            browser._checkpoint_path(self.repo, "verified"),
            {"repo": str(self.repo.resolve()), "active_chat_url": "https://chatgpt.com/c/historical"},
        )
        with patch.object(browser, "_candidate_receipt_evidence", return_value=[]), patch.object(
            browser, "ensure_browser_running", return_value={"port": 9223}
        ), patch.object(
            browser, "_verify_historical_chat_ownership", return_value={"owned": True}
        ):
            result = browser.verify_candidate_chat(self.repo, "historical")
        self.assertTrue(result["verified"])
        self.assertEqual(result["reason"], "bootstrap_markers_and_history")
        self.assertEqual(result["historical_evidence"], ["checkpoint:verified.json"])
        registry = browser._read_json(browser._owned_chat_registry_path(self.repo))
        self.assertEqual(registry["chats"][0]["verified_from"], "bootstrap_markers_and_history")

    def test_historical_reference_is_not_enough_without_bootstrap_markers(self):
        self.bind()
        browser._atomic_json(
            browser._rollover_transaction_path(self.repo),
            {"predecessor_chat_url": "https://chatgpt.com/c/unverified"},
        )
        with patch.object(browser, "_candidate_receipt_evidence", return_value=[]), patch.object(
            browser, "ensure_browser_running", return_value={"port": 9223}
        ), patch.object(
            browser, "_verify_historical_chat_ownership", return_value={"owned": False}
        ):
            result = browser.verify_candidate_chat(self.repo, "unverified")
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "bootstrap_markers_missing")
        self.assertEqual(browser._read_json(browser._owned_chat_registry_path(self.repo)), {})

    def test_cleanup_inventory_excludes_active_bound_and_unverified_candidates(self):
        record = self.bind()
        browser._record_owned_chat(
            self.repo, self.target.url, created_reason="bootstrap"
        )
        other = "11111111-2222-3333-4444-555555555555"
        with patch.object(browser, "_candidate_receipt_evidence", return_value=[{"receipt": "x.json"}]):
            inventory = browser.chat_cleanup_inventory(
                self.repo, candidate_ids=[other]
            )
        owned = next(row for row in inventory["candidates"] if row["chat_id"] == "project")
        candidate = next(row for row in inventory["candidates"] if row["chat_id"] == other)
        self.assertTrue(owned["ownership_verified"])
        self.assertTrue(owned["active_bound"])
        self.assertFalse(owned["eligible"])
        self.assertFalse(candidate["ownership_verified"])
        self.assertFalse(candidate["eligible"])
        self.assertEqual(candidate["reason"], "needs_content_verification")

    def test_cleanup_dry_run_never_writes_queue(self):
        self.bind()
        old = "https://chatgpt.com/c/old-owned"
        browser._record_owned_chat(self.repo, old, created_reason="rollover")
        result = browser.queue_verified_archives(self.repo, apply=False)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["eligible_chat_ids"], ["old-owned"])
        self.assertEqual(result["queued_chat_ids"], [])
        self.assertFalse(browser._archive_queue_path(self.repo).exists())

    def test_cleanup_apply_is_idempotent_for_verified_owned_inactive_chat(self):
        self.bind()
        old = "https://chatgpt.com/c/old-owned"
        browser._record_owned_chat(self.repo, old, created_reason="rollover")
        first = browser.queue_verified_archives(self.repo, apply=True)
        second = browser.queue_verified_archives(self.repo, apply=True)
        self.assertEqual(first["queued_chat_ids"], ["old-owned"])
        self.assertEqual(second["queued_chat_ids"], [])
        queue = browser.archive_queue_status(self.repo)
        self.assertEqual(len(queue["items"]), 1)
        self.assertEqual(queue["items"][0]["state"], "pending")



    def test_verify_candidate_rejects_active_chat_before_browser_open(self):
        self.bind()
        with patch.object(browser, "ensure_browser_running") as ensure:
            result = browser.verify_candidate_chat(self.repo, "project")
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "currently_bound")
        ensure.assert_not_called()

    def test_verify_candidate_requires_receipt_corroboration(self):
        with patch.object(browser, "_candidate_receipt_evidence", return_value=[]), patch.object(
            browser, "ensure_browser_running"
        ) as ensure:
            result = browser.verify_candidate_chat(self.repo, "historical")
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "no_corroborating_receipts_or_history")
        ensure.assert_not_called()

    def test_archive_queue_blocks_unowned_and_active_without_ui_mutation(self):
        self.bind()
        queue_path = browser._archive_queue_path(self.repo)
        browser._atomic_json(queue_path, {
            "schema_version": 1,
            "items": [
                {"chat_id": "project", "chat_url": self.target.url, "state": "pending", "attempts": 0},
                {"chat_id": "stranger", "chat_url": "https://chatgpt.com/c/stranger", "state": "pending", "attempts": 0},
            ],
        })
        browser._record_owned_chat(self.repo, self.target.url, created_reason="bootstrap")
        with patch.object(browser, "ensure_browser_running", return_value={"port": 9223}), patch.object(
            cdp, "create_target"
        ) as create:
            result = browser.process_archive_queue(self.repo)
        states = {row["chat_id"]: row["state"] for row in result["items"]}
        self.assertEqual(states["project"], "blocked_active")
        self.assertEqual(states["stranger"], "blocked_unowned")
        create.assert_not_called()

    def test_archive_failure_is_retryable_and_duplicate_archived_is_idempotent(self):
        self.bind()
        old_url = "https://chatgpt.com/c/old-owned"
        browser._record_owned_chat(self.repo, old_url, created_reason="rollover")
        browser.queue_verified_archives(self.repo, apply=True)
        target = cdp.Target("old", old_url, "", "ws://127.0.0.1/old")
        with patch.object(browser, "ensure_browser_running", return_value={"port": 9223}), patch.object(
            cdp, "create_target", return_value=target
        ), patch.object(browser, "wait_for_authenticated", return_value=(target, {})), patch.object(
            browser, "_archive_chat_via_ui", side_effect=browser.BrowserError("ui changed")
        ), patch.object(cdp, "close_target"):
            failed = browser.process_archive_queue(self.repo)
        self.assertEqual(failed["items"][0]["state"], "retry")
        self.assertEqual(failed["items"][0]["attempts"], 1)

        with patch.object(browser, "ensure_browser_running", return_value={"port": 9223}), patch.object(
            cdp, "create_target", return_value=target
        ), patch.object(browser, "wait_for_authenticated", return_value=(target, {})), patch.object(
            browser, "_archive_chat_via_ui", return_value="clicked_archive"
        ) as archive, patch.object(cdp, "close_target"):
            succeeded = browser.process_archive_queue(self.repo)
        self.assertEqual(succeeded["items"][0]["state"], "archived")
        self.assertEqual(succeeded["items"][0]["attempts"], 2)
        self.assertEqual(archive.call_count, 1)

        with patch.object(browser, "ensure_browser_running", return_value={"port": 9223}), patch.object(
            cdp, "create_target"
        ) as create:
            again = browser.process_archive_queue(self.repo)
        self.assertEqual(again["items"][0]["state"], "archived")
        create.assert_not_called()



if __name__ == "__main__":
    unittest.main()
