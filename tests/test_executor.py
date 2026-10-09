from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from do_again.core.executor import LocalExecutor
from do_again.core.schema import OperatorError, atomic_json, request_fingerprint, utc_now, validate_request

ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def load_agent_module():
    from do_again.core import agent
    return agent


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        git(self.repo, "branch", "-m", "main")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "automation").mkdir()
        atomic_json(
            self.repo / "automation/direct_chat_chatgpt_control.json",
            {"generation": 10, "state": "stop"},
        )
        atomic_json(
            self.repo / "automation/control_plane_desired.json",
            {
                "generation": 10,
                "mode": "STOP",
                "revision": 20,
                "bundle_sha256": "b" * 64,
                "ownership": {"lease_id": "lease", "epoch": 3},
            },
        )
        (self.repo / "tools").mkdir()
        script = self.repo / "tools/echo_args.py"
        script.write_text(
            "import json,sys\nprint(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "fixture")
        self.head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.state = self.root / "state"
        self.policy = self.root / "policy.json"
        atomic_json(
            self.policy,
            {
                "schema_version": 1,
                "max_request_ttl_seconds": 3600,
                "default_timeout_seconds": 30,
                "absolute_timeout_seconds": 60,
                "max_output_bytes": 16384,
                "max_scratch_script_bytes": 65536,
                "max_inline_artifact_bytes": 450000,
                "max_screenshot_width": 1440,
                "screenshot_jpeg_quality": 55,
                "agent_launchd_label": "io.github.tran-steven.do-again",
                "allowed_operations": [
                    "status",
                    "read_file",
                    "run_tests",
                    "repo_script",
                    "control_plane_launcher",
                    "deploy_cp1_bundle_at_stop",
                    "scratch_script",
                    "self_update",
                    "artifact_get",
                    "artifact_put",
                    "capture_screenshot",
                    "full_process_list",
                    "extended_exec",
                    "process_signal",
                    "launchctl_action",
                ],
                "approved_cwd_roots": ["$REPO"],
                "read_roots": ["$REPO"],
                "repo_script_prefixes": ["tools/", "control_plane/"],
                "allowed_launcher_subcommands": ["session-intake", "session-resolution"],
                "extended_exec_binaries": [
                    "python3",
                    "git",
                    "bash",
                    "zsh",
                    "sh",
                    "find",
                    "sed",
                    "cat",
                ],
                "git_allowed_subcommands": ["status", "show", "diff", "rev-parse"],
                "launchctl_allowed_subcommands": ["print", "kickstart"],
                "launchctl_label_prefixes": ["io.github.tran-steven."],
                "process_signal_allowlist": ["TERM", "KILL"],
                "python_allowed_modules": ["unittest", "py_compile"],
                "hard_denied_binaries": ["sudo", "rm", "dd", "docker"],
            },
        )
        self.executor = LocalExecutor(
            repo=self.repo,
            policy_path=self.policy,
            state_dir=self.state,
        )

    def tearDown(self):
        self.temp.cleanup()

    def request(self, operation: str, **updates):
        now = utc_now()
        value = {
            "schema_version": 1,
            "request_id": "request-12345678",
            "issued_at_utc": now.isoformat(),
            "expires_at_utc": (now + timedelta(minutes=5)).isoformat(),
            "operation": operation,
            "expected": {"generation": 10, "revision": 20, "repo_head": self.head},
            "args": {},
            "limits": {"timeout_seconds": 30},
        }
        value.update(updates)
        return value

    def test_request_ttl_is_bounded(self):
        now = utc_now()
        request = self.request(
            "status",
            issued_at_utc=now.isoformat(),
            expires_at_utc=(now + timedelta(hours=2)).isoformat(),
        )
        with self.assertRaisesRegex(OperatorError, "TTL exceeds"):
            validate_request(request, max_ttl_seconds=3600)

    def test_authority_fence_mismatch_blocks_execution(self):
        request = self.request("status", expected={"generation": 11, "revision": 20})
        with self.assertRaisesRegex(OperatorError, "authority fence mismatch"):
            self.executor.execute(request)

    def test_repo_script_executes_trusted_repository_script(self):
        request = self.request(
            "repo_script",
            args={"path": "tools/echo_args.py", "argv": ["a", "b"]},
        )
        result = self.executor.execute(request)["result"]
        self.assertEqual(result["returncode"], 0)
        self.assertIn('["a", "b"]', result["stdout"])

    def test_future_issued_request_is_rejected(self):
        request = self.request("status")
        request["issued_at_utc"] = (utc_now() + timedelta(days=30)).isoformat()
        request["expires_at_utc"] = (
            utc_now() + timedelta(days=30, minutes=30)
        ).isoformat()
        with self.assertRaisesRegex(OperatorError, "future"):
            validate_request(request, max_ttl_seconds=3600)

    def test_request_paths_are_ordered_by_actual_timestamp(self):
        agent_module = load_agent_module()
        control = self.root / "control-order"
        request_dir = control / "automation/do_again/requests"
        request_dir.mkdir(parents=True)
        (control / "automation/do_again/receipts").mkdir(parents=True)
        first = {
            "request_id": "order-request-a",
            "issued_at_utc": "2026-10-07T10:00:00+02:00",
        }
        second = {
            "request_id": "order-request-b",
            "issued_at_utc": "2026-10-07T09:00:00+00:00",
        }
        atomic_json(request_dir / "order-request-a.json", first)
        atomic_json(request_dir / "order-request-b.json", second)
        agent = agent_module.Agent(
            repo=self.repo,
            control_worktree=control,
            branch="operator-control",
            policy_path=self.policy,
            state_dir=self.root / "order-state",
            executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.root / "order-state"),
        )
        self.assertEqual(
            [path.stem for path in agent.request_paths()],
            ["order-request-a", "order-request-b"],
        )

    def test_malformed_request_remains_observable(self):
        agent_module = load_agent_module()
        control = self.root / "control-invalid"
        request_dir = control / "automation/do_again/requests"
        request_dir.mkdir(parents=True)
        (control / "automation/do_again/receipts").mkdir(parents=True)
        path = request_dir / "malformed-request-0001.json"
        path.write_text("{not json", encoding="utf-8")
        agent = agent_module.Agent(
            repo=self.repo,
            control_worktree=control,
            branch="operator-control",
            policy_path=self.policy,
            state_dir=self.root / "invalid-state",
            executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.root / "invalid-state"),
        )
        self.assertIn(path.resolve(), [value.resolve() for value in agent.request_paths()])
        with patch.object(
            agent,
            "publish_invalid_request",
            return_value=True,
        ) as publish_invalid:
            self.assertTrue(agent.process_path(path))
        publish_invalid.assert_called_once()

    def test_existing_receipt_request_id_reuse_publishes_conflict(self):
        agent_module = load_agent_module()
        control = self.root / "control-reuse"
        request_dir = control / "automation/do_again/requests"
        receipt_dir = control / "automation/do_again/receipts"
        request_dir.mkdir(parents=True)
        receipt_dir.mkdir(parents=True)
        request = self.request("status")
        request_path = request_dir / f"{request['request_id']}.json"
        atomic_json(request_path, request)
        atomic_json(
            receipt_dir / f"{request['request_id']}.json",
            {
                "request_id": request["request_id"],
                "request_fingerprint": "different",
                "state": "succeeded",
            },
        )
        agent = agent_module.Agent(
            repo=self.repo,
            control_worktree=control,
            branch="operator-control",
            policy_path=self.policy,
            state_dir=self.root / "reuse-state",
            executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.root / "reuse-state"),
        )
        conflicts = []
        agent.publish_conflict = lambda **kwargs: (conflicts.append(kwargs) or True)
        self.assertTrue(agent.process_path(request_path))
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["existing_fingerprint"], "different")

    def test_ledger_fingerprint_reuse_publishes_conflict(self):
        agent_module = load_agent_module()
        control = self.root / "control-ledger-reuse"
        request_dir = control / "automation/do_again/requests"
        request_dir.mkdir(parents=True)
        (control / "automation/do_again/receipts").mkdir(parents=True)
        request = self.request("status")
        request_path = request_dir / f"{request['request_id']}.json"
        atomic_json(request_path, request)
        agent = agent_module.Agent(
            repo=self.repo,
            control_worktree=control,
            branch="operator-control",
            policy_path=self.policy,
            state_dir=self.root / "ledger-reuse-state",
            executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.root / "ledger-reuse-state"),
        )
        atomic_json(
            agent.ledger_path(request["request_id"]),
            {
                "state": "terminal",
                "request_fingerprint": "different",
                "receipt": {
                    "request_id": request["request_id"],
                    "request_fingerprint": "different",
                    "state": "succeeded",
                },
            },
        )
        conflicts = []
        agent.publish_conflict = lambda **kwargs: (conflicts.append(kwargs) or True)
        self.assertTrue(agent.process_path(request_path))
        self.assertEqual(len(conflicts), 1)

    def test_same_state_concurrent_agents_execute_once(self):
        agent_module = load_agent_module()
        control = self.root / "control-concurrent"
        request_dir = control / "automation/do_again/requests"
        request_dir.mkdir(parents=True)
        (control / "automation/do_again/receipts").mkdir(parents=True)
        request = self.request("status")
        request_path = request_dir / f"{request['request_id']}.json"
        atomic_json(request_path, request)
        state = self.root / "concurrent-state"
        agents = [
            agent_module.Agent(
                repo=self.repo,
                control_worktree=control,
                branch="operator-control",
                policy_path=self.policy,
                state_dir=state,
                executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=state),
            )
            for _ in range(2)
        ]
        executions = []
        execution_lock = threading.Lock()

        def execute(_request):
            with execution_lock:
                executions.append(1)
            time.sleep(0.2)
            return {"result": {"returncode": 0}}

        for agent in agents:
            agent.executor.execute = execute
            agent.publish_receipt = lambda receipt: None
            agent.publish_status = lambda *args, **kwargs: None
            agent.acquire_remote_claim = lambda request: True

        threads = [
            threading.Thread(target=agent.process_path, args=(request_path,))
            for agent in agents
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(executions), 1)

    def test_scratch_python_executes_and_records_hash(self):
        request = self.request(
            "scratch_script",
            args={
                "language": "python",
                "content": "import sys\nprint('hello', sys.argv[1])\n",
                "argv": ["world"],
            },
        )
        result = self.executor.execute(request)["result"]
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["language"], "python")
        self.assertEqual(len(result["script_sha256"]), 64)
        self.assertEqual(result["script_bytes"], len(request["args"]["content"].encode("utf-8")))
        self.assertIn("hello world", result["stdout"])
        self.assertEqual(Path(result["script_path"]).read_text(), request["args"]["content"])

    def test_scratch_bash_executes(self):
        request = self.request(
            "scratch_script",
            args={
                "language": "bash",
                "content": "printf 'bash:%s\\n' \"$1\"\n",
                "argv": ["ok"],
            },
        )
        if os.name == "nt":
            with self.assertRaisesRegex(OperatorError, "not supported on native Windows"):
                self.executor.execute(request)
            return
        result = self.executor.execute(request)["result"]
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["language"], "bash")
        self.assertIn("bash:ok", result["stdout"])

    def test_scratch_rejects_invalid_language(self):
        request = self.request(
            "scratch_script",
            args={"language": "ruby", "content": "puts 'x'"},
        )
        with self.assertRaisesRegex(OperatorError, "python or bash"):
            self.executor.execute(request)

    def test_scratch_rejects_empty_content(self):
        request = self.request(
            "scratch_script",
            args={"language": "python", "content": ""},
        )
        with self.assertRaisesRegex(OperatorError, "non-empty"):
            self.executor.execute(request)

    def test_scratch_rejects_oversized_content(self):
        self.executor.policy["max_scratch_script_bytes"] = 10
        request = self.request(
            "scratch_script",
            args={"language": "python", "content": "print('way too large')"},
        )
        with self.assertRaisesRegex(OperatorError, "maximum size"):
            self.executor.execute(request)

    def test_artifact_put_and_get_round_trip(self):
        data = b"\x00binary\xffpayload"
        put = self.executor.execute(
            self.request(
                "artifact_put",
                args={
                    "name": "sample.bin",
                    "base64": base64.b64encode(data).decode("ascii"),
                    "mime_type": "application/octet-stream",
                },
            )
        )["result"]
        self.assertEqual(put["size_bytes"], len(data))
        self.assertEqual(Path(put["path"]).read_bytes(), data)
        get = self.executor.execute(
            self.request(
                "artifact_get",
                args={"path": put["path"]},
            )
        )["result"]
        self.assertEqual(base64.b64decode(get["base64"]), data)
        self.assertEqual(get["sha256"], put["sha256"])

    def test_artifact_get_rejects_outside_root(self):
        outside = self.root / "outside.bin"
        outside.write_bytes(b"x")
        with self.assertRaisesRegex(OperatorError, "outside approved roots"):
            self.executor.execute(
                self.request("artifact_get", args={"path": str(outside)})
            )

    def test_artifact_put_rejects_oversize(self):
        self.executor.policy["max_inline_artifact_bytes"] = 3
        with self.assertRaisesRegex(OperatorError, "inline maximum"):
            self.executor.execute(
                self.request(
                    "artifact_put",
                    args={
                        "name": "x.bin",
                        "base64": base64.b64encode(b"1234").decode("ascii"),
                    },
                )
            )

    def test_process_rows_support_commands_with_spaces(self):
        rows = self.executor._process_rows(
            "101 1 00:01 12.5 3.0 2048 python3 worker script.py --flag x\n"
        )
        self.assertEqual(rows[0]["pid"], 101)
        self.assertEqual(rows[0]["rss_kib"], 2048)
        self.assertEqual(rows[0]["command"], "python3 worker script.py --flag x")

    def test_capture_screenshot_packages_compressed_artifact(self):
        request = self.request("capture_screenshot")
        snapshot = self.executor.authority_snapshot()

        def fake_capture(argv, *, timeout, cwd=None, env=None):
            if argv[0] == "/usr/sbin/screencapture":
                Path(argv[-1]).write_bytes(b"raw")
            elif argv[0] == "/usr/bin/sips":
                Path(argv[-1]).write_bytes(b"jpeg-bytes")
            return {
                "argv": argv,
                "cwd": str(cwd or self.repo),
                "returncode": 0,
                "timed_out": False,
                "duration_seconds": 0.01,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

        with patch.object(self.executor, "authority_snapshot", return_value=snapshot), \
             patch.object(self.executor, "_capture", side_effect=fake_capture):
            result = self.executor.execute(request)["result"]
        self.assertEqual(result["source"], "screenshot")
        self.assertEqual(result["mime_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(result["base64"]), b"jpeg-bytes")

    def test_self_update_requires_stop(self):
        atomic_json(
            self.repo / "automation/direct_chat_chatgpt_control.json",
            {"generation": 10, "state": "run"},
        )
        atomic_json(
            self.repo / "automation/control_plane_desired.json",
            {
                "generation": 10,
                "mode": "RUN",
                "revision": 20,
                "bundle_sha256": "b" * 64,
                "ownership": {"lease_id": "lease", "epoch": 3},
            },
        )
        with patch.object(self.executor, "_require_clean_main"):
            with self.assertRaisesRegex(OperatorError, "requires authoritative STOP"):
                self.executor.execute(self.request("self_update"))

    def test_self_update_stale_local_run_requires_explicit_reconcile_mode(self):
        atomic_json(
            self.repo / "automation/direct_chat_chatgpt_control.json",
            {"generation": 10, "state": "run"},
        )
        atomic_json(
            self.repo / "automation/control_plane_desired.json",
            {
                "generation": 10,
                "mode": "RUN",
                "revision": 20,
                "bundle_sha256": "b" * 64,
                "ownership": {"lease_id": "lease", "epoch": 3},
            },
        )
        with patch.object(self.executor, "_require_clean_main"):
            with self.assertRaisesRegex(OperatorError, "requires authoritative STOP"):
                self.executor.execute(self.request("self_update"))

    def test_stale_local_self_update_target_requires_exact_coherent_stop_proof(self):
        before = {
            "generation": 10,
            "control_state": "run",
            "revision": 20,
            "desired_mode": "RUN",
        }
        args = {
            "allow_stale_local_authority_reconcile": True,
            "target_authority": {
                "generation": 11,
                "revision": 21,
                "state": "stop",
                "mode": "STOP",
                "controller_lease_id": "lease",
                "controller_lease_epoch": 3,
                "repo_head": "f" * 40,
            },
        }
        docs = {
            "automation/direct_chat_chatgpt_control.json": {
                "generation": 11,
                "state": "stop",
                "controller_lease_id": "lease",
                "controller_lease_epoch": 3,
            },
            "automation/control_plane_desired.json": {
                "generation": 11,
                "mode": "STOP",
                "revision": 21,
                "ownership": {"lease_id": "lease", "epoch": 3},
            },
            "automation/direct_chat_controller_lease.json": {
                "state": "active",
                "lease_id": "lease",
                "epoch": 3,
            },
        }
        with patch.object(
            self.executor,
            "_commit_json",
            side_effect=lambda commit, path: docs[path],
        ):
            got = self.executor._validate_stale_local_self_update_target(
                target="f" * 40,
                before=before,
                args=args,
            )
        self.assertEqual(got["generation"], 11)
        self.assertEqual(got["revision"], 21)

    def test_stale_local_self_update_rejects_target_run_even_with_claimed_stop(self):
        before = {"generation": 10, "control_state": "run"}
        args = {
            "allow_stale_local_authority_reconcile": True,
            "target_authority": {
                "generation": 11,
                "revision": 21,
                "state": "stop",
                "mode": "STOP",
                "controller_lease_id": "lease",
                "controller_lease_epoch": 3,
                "repo_head": "f" * 40,
            },
        }
        docs = {
            "automation/direct_chat_chatgpt_control.json": {
                "generation": 11,
                "state": "run",
                "controller_lease_id": "lease",
                "controller_lease_epoch": 3,
            },
            "automation/control_plane_desired.json": {
                "generation": 11,
                "mode": "RUN",
                "revision": 21,
                "ownership": {"lease_id": "lease", "epoch": 3},
            },
            "automation/direct_chat_controller_lease.json": {
                "state": "active",
                "lease_id": "lease",
                "epoch": 3,
            },
        }
        with patch.object(
            self.executor,
            "_commit_json",
            side_effect=lambda commit, path: docs[path],
        ):
            with self.assertRaisesRegex(OperatorError, "not coherent authoritative STOP"):
                self.executor._validate_stale_local_self_update_target(
                    target="f" * 40,
                    before=before,
                    args=args,
                )

    def test_extended_exec_rejects_inline_shell(self):
        request = self.request(
            "extended_exec",
            args={"argv": ["bash", "-c", "echo nope"]},
        )
        with self.assertRaisesRegex(OperatorError, "inline shell"):
            self.executor.execute(request)

    def test_extended_exec_rejects_inline_python(self):
        request = self.request(
            "extended_exec",
            args={"argv": ["python3", "-c", "print(1)"]},
        )
        with self.assertRaisesRegex(OperatorError, "inline Python"):
            self.executor.execute(request)

    def test_extended_exec_rejects_denied_binary(self):
        request = self.request(
            "extended_exec",
            args={"argv": ["rm", "-rf", str(self.repo)]},
        )
        with self.assertRaisesRegex(OperatorError, "not allowed"):
            self.executor.execute(request)

    def test_extended_exec_rejects_find_exec_and_delete(self):
        for argv in (
            ["find", str(self.repo), "-delete"],
            ["find", str(self.repo), "-exec", "cat", "{}", ";"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(OperatorError, "find actions"):
                    self.executor.execute(
                        self.request("extended_exec", args={"argv": argv})
                    )

    def test_extended_exec_rejects_sed_in_place(self):
        with self.assertRaisesRegex(OperatorError, "in-place"):
            self.executor.execute(
                self.request(
                    "extended_exec",
                    args={"argv": ["sed", "-i", "", "x", "file"]},
                )
            )

    def test_extended_exec_rejects_file_outside_approved_roots(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaisesRegex(OperatorError, "outside approved roots"):
            self.executor.execute(
                self.request(
                    "extended_exec",
                    args={"argv": ["cat", str(outside)]},
                )
            )

    def test_extended_exec_rejects_destructive_git(self):
        with self.assertRaisesRegex(OperatorError, "subcommand is not allowed"):
            self.executor.execute(
                self.request(
                    "extended_exec",
                    args={"argv": ["git", "reset", "--hard", "HEAD"]},
                )
            )

    def test_session_intake_requires_generation_and_revision_fences(self):
        request = self.request(
            "control_plane_launcher",
            expected={"generation": 10},
            args={"subcommand": "session-intake", "argv": []},
        )
        with self.assertRaisesRegex(OperatorError, "requires generation and revision"):
            self.executor.execute(request)

    def test_session_intake_requires_repo_head_fence(self):
        request = self.request(
            "control_plane_launcher",
            expected={"generation": 10, "revision": 20},
            args={"subcommand": "session-intake", "argv": []},
        )
        with self.assertRaisesRegex(OperatorError, "repo_head"):
            self.executor.execute(request)

    def test_launcher_materializes_inline_json(self):
        home = self.root / "home"
        launcher = home / ".direct_chat_control_plane/launcher.py"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("print('ok')\n", encoding="utf-8")
        request = self.request(
            "control_plane_launcher",
            args={
                "subcommand": "session-intake",
                "argv": ["--engineering-decision-json", "@json:decision"],
                "json_files": {"decision": {"schema_version": 1, "action": "advance"}},
            },
        )
        snapshot = self.executor.authority_snapshot()
        with patch("do_again.core.executor.Path.home", return_value=home), \
             patch.object(self.executor, "authority_snapshot", return_value=snapshot), \
             patch.object(self.executor, "_capture") as capture:
            capture.return_value = {"returncode": 0, "stdout": "", "stderr": ""}
            self.executor.execute(request)
        argv = capture.call_args.args[0]
        decision_path = Path(argv[-1])
        self.assertTrue(decision_path.is_file())
        self.assertEqual(json.loads(decision_path.read_text())["action"], "advance")

    def test_kill_requires_process_identity_guard(self):
        request = self.request(
            "process_signal",
            args={"pid": 99999, "signal": "KILL"},
        )
        snapshot = self.executor.authority_snapshot()
        with patch.object(self.executor, "authority_snapshot", return_value=snapshot), \
             patch.object(
                 self.executor,
                 "_capture",
                 return_value={"returncode": 0, "stdout": "python3 worker.py\n", "stderr": ""},
             ):
            with self.assertRaisesRegex(OperatorError, "expected_command_contains"):
                self.executor.execute(request)

    def test_fast_forward_clean_main_to_origin_reconciles_remote_publication(self):
        calls=[]
        head_reads=0
        def fake_capture(argv, *, timeout, cwd=None, env=None):
            nonlocal head_reads
            calls.append(argv)
            if argv[-3:] == ["fetch","--quiet","origin"] or (len(argv)>=2 and "fetch" in argv):
                return {"returncode":0,"stdout":"","stderr":""}
            if argv[-2:] == ["rev-parse","origin/main"]:
                return {"returncode":0,"stdout":"new-head\n","stderr":""}
            if argv[-2:] == ["rev-parse","HEAD"]:
                head_reads += 1
                return {"returncode":0,"stdout":("old-head\n" if head_reads==1 else "new-head\n"),"stderr":""}
            if "merge-base" in argv:
                return {"returncode":0,"stdout":"","stderr":""}
            if "merge" in argv and "--ff-only" in argv:
                return {"returncode":0,"stdout":"Updating old..new\n","stderr":""}
            return {"returncode":0,"stdout":"","stderr":""}
        with patch.object(self.executor,"_require_clean_main"), \
             patch.object(self.executor,"_capture",side_effect=fake_capture):
            result=self.executor._fast_forward_clean_main_to_origin(30)
        self.assertEqual(result["returncode"],0)
        self.assertEqual(result["stage"],"complete")
        self.assertEqual(result["new_head"],"new-head")
        self.assertTrue(any("merge" in argv and "--ff-only" in argv for argv in calls))

    def test_bundle_deploy_reconciles_local_main_after_successful_retarget(self):
        request=self.request("deploy_cp1_bundle_at_stop")
        snapshot=self.executor.authority_snapshot()
        def fake_capture(argv, *, timeout, cwd=None, env=None):
            if any(str(value).endswith("build_cp1_bundle.py") for value in argv):
                return {"returncode":0,"stdout":"a"*64+"\n","stderr":"","timed_out":False}
            if any(str(value).endswith("retarget_bundle_at_stop.py") for value in argv):
                return {"returncode":0,"stdout":"{}\n","stderr":"","timed_out":False}
            return {"returncode":0,"stdout":"","stderr":"","timed_out":False}
        with patch.object(self.executor,"authority_snapshot",return_value=snapshot), \
             patch.object(self.executor,"_require_clean_main"), \
             patch.object(self.executor,"_capture",side_effect=fake_capture), \
             patch.object(self.executor,"_fast_forward_clean_main_to_origin",return_value={"returncode":0,"stage":"complete","new_head":"new"}) as sync:
            result=self.executor.execute(request)["result"]
        sync.assert_called_once()
        self.assertTrue(result["local_sync_ok"])
        self.assertEqual(result["local_sync"]["new_head"],"new")

    def test_bundle_deploy_requires_stop(self):
        atomic_json(
            self.repo / "automation/direct_chat_chatgpt_control.json",
            {"generation": 10, "state": "run"},
        )
        atomic_json(
            self.repo / "automation/control_plane_desired.json",
            {
                "generation": 10,
                "mode": "RUN",
                "revision": 20,
                "bundle_sha256": "b" * 64,
                "ownership": {"lease_id": "lease", "epoch": 3},
            },
        )
        request = self.request("deploy_cp1_bundle_at_stop")
        with patch.object(self.executor, "_require_clean_main"):
            with self.assertRaisesRegex(OperatorError, "requires authoritative STOP"):
                self.executor.execute(request)

    def test_authority_mutation_rejects_dirty_repository(self):
        (self.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        request = self.request(
            "control_plane_launcher",
            args={"subcommand": "session-intake", "argv": []},
        )
        with self.assertRaisesRegex(OperatorError, "clean repository"):
            self.executor.execute(request)

    def test_successful_self_update_schedules_restart_after_receipt(self):
        agent_module = load_agent_module()
        control = self.root / "control-restart"
        (control / "automation/do_again/requests").mkdir(parents=True)
        (control / "automation/do_again/receipts").mkdir(parents=True)
        request = self.request("self_update")
        request_path = (
            control
            / "automation/do_again/requests"
            / f"{request['request_id']}.json"
        )
        atomic_json(request_path, request)
        agent = agent_module.Agent(
            repo=self.repo,
            control_worktree=control,
            branch="operator-control",
            policy_path=self.policy,
            state_dir=self.root / "agent-restart-state",
            executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.root / "agent-restart-state"),
        )
        payload = {
            "operation": "self_update",
            "request_fingerprint": "x",
            "authority_before": {},
            "authority_after": {},
            "result": {"returncode": 0, "restart_after_receipt": True},
        }
        published = []
        agent.publish_receipt = published.append
        agent.publish_status = lambda *args, **kwargs: None
        with patch.object(agent.executor, "execute", return_value=payload), \
             patch.object(agent, "acquire_remote_claim", return_value=True), \
             patch.object(agent, "schedule_self_restart") as restart:
            self.assertTrue(agent.process_path(request_path))
        self.assertEqual(published[0]["state"], "succeeded")
        restart.assert_called_once_with()

    def test_ambiguous_started_request_is_not_replayed(self):
        agent_module = load_agent_module()
        control = self.root / "control"
        (control / "automation/do_again/requests").mkdir(parents=True)
        (control / "automation/do_again/receipts").mkdir(parents=True)
        request = self.request("status")
        request_path = (
            control
            / "automation/do_again/requests"
            / f"{request['request_id']}.json"
        )
        atomic_json(request_path, request)
        agent = agent_module.Agent(
            repo=self.repo,
            control_worktree=control,
            branch="operator-control",
            policy_path=self.policy,
            state_dir=self.root / "agent-state",
            executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.root / "agent-state"),
        )
        atomic_json(
            agent.ledger_path(request["request_id"]),
            {
                "state": "started",
                "started_at_utc": utc_now().isoformat(),
                "request_fingerprint": request_fingerprint(request),
            },
        )
        receipts = []
        agent.publish_receipt = receipts.append
        with patch.object(agent.executor, "execute") as execute:
            self.assertTrue(agent.process_path(request_path))
        execute.assert_not_called()
        self.assertEqual(receipts[0]["state"], "blocked_ambiguous_replay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
