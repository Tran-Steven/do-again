from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch


@unittest.skipUnless(os.name == "posix", "UNIX domain sockets require POSIX")
class SupervisorReadinessTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "fixture_supervisor_installer",
            Path(__file__).resolve().parents[1] / "tools/macos_supervisor_install.py",
        )
        assert spec and spec.loader
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = "b" * 64
        self.expected = {
            "source_sha": "a" * 40,
            "projects": [{"key": self.key, "source_sha": "c" * 40}],
        }
        self.status = {
            "source_sha": "a" * 40,
            "production_ready": False,
            "operator_intent": "maintenance",
            "authority": {"repo_head": "c" * 40},
            "unresolved_executions": [],
            "inflight_request_ids": [],
            "uid": 402,
        }

    def run_client(self, *, failure_once=False, override=None, peer=0):
        owner = self
        calls = []
        refused = [False]

        class Connection:
            def __init__(self):
                self.path = None
                self.request = None

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def settimeout(self, *_):
                pass

            def connect(self, path):
                self.path = str(path)
                calls.append(self.path)
                if failure_once and not refused[0]:
                    refused[0] = True
                    raise ConnectionRefusedError(111, "refused")

            def sendall(self, raw):
                self.request = json.loads(raw)

            def recv(self, _):
                result = dict(owner.status)
                if self.path.endswith("operator.sock") and override:
                    result.update(override)
                return json.dumps({"ok": True, "result": result}).encode() + b"\\n"

        from do_again.supervisor import macos_execution
        script = self.installer._READINESS_CLIENT
        script = script.replace(
            "Path('/private/var/run/do-again-supervisor')",
            "Path(" + repr(str(self.root)) + ")",
        )
        self.assertNotEqual(script, self.installer._READINESS_CLIENT)
        output = io.StringIO()
        with patch.object(sys, "argv", ["readiness-test", str(self.root), json.dumps(self.expected)]), \
             patch.object(sys, "path", list(sys.path)), \
             patch.object(socket, "socket", side_effect=lambda *_: Connection()), \
             patch.object(macos_execution, "peer_uid", return_value=peer), \
             patch.object(time, "sleep"), redirect_stdout(output):
            try:
                exec(compile(script, "<readiness-test>", "exec"), {})
            except SystemExit as exc:
                return exc.code, calls, output.getvalue()
        return None, calls, output.getvalue()

    def test_authenticates_both_operator_and_project_response(self):
        exit_code, calls, message = self.run_client()
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith(self.key[:24] + ".sock"))
        self.assertTrue(calls[1].endswith("operator.sock"))
        self.assertIn("SUPERVISOR_AUTHENTICATED_READY", message)

    def test_retries_single_startup_refusal(self):
        exit_code, calls, _ = self.run_client(failure_once=True)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(calls), 3)

    def test_rejects_operator_that_disagrees_with_sealed_source(self):
        with self.assertRaisesRegex(RuntimeError, "sealed maintenance authority"):
            self.run_client(override={"source_sha": "f" * 40})

    def test_rejects_operator_that_claims_wrong_project_identity(self):
        with self.assertRaisesRegex(RuntimeError, "identities differ"):
            self.run_client(override={"uid": 999})

    def test_rejects_untrusted_socket_peer(self):
        with self.assertRaisesRegex(RuntimeError, "not served by root"):
            self.run_client(peer=501)

    def test_operator_socket_exposes_status_without_mutation(self):
        from do_again.supervisor import macos_server

        class Exhausted(Exception):
            pass

        class Connection:
            def __init__(self):
                self.sent = None
                self.used = False

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def settimeout(self, *_):
                pass

            def recv(self, _):
                if self.used:
                    return b""
                self.used = True
                return json.dumps({"operation": "status", "project": "key"}).encode() + b"\\n"

            def sendall(self, raw):
                self.sent = json.loads(raw)

        connection = Connection()

        class Listener:
            def __init__(self):
                self.count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def bind(self, path):
                Path(path).touch()

            def listen(self, *_):
                pass

            def accept(self):
                if self.count:
                    raise Exhausted()
                self.count += 1
                return connection, None

        broker = Mock()
        broker.dispatch.return_value = self.status
        with patch.object(macos_server, "SOCKET_ROOT", self.root), \
             patch.object(macos_server.socket, "socket", return_value=Listener()), \
             patch.object(macos_server.os, "chown"), \
             patch.object(macos_server, "peer_uid", return_value=501):
            with self.assertRaises(Exhausted):
                macos_server.serve_operator({"operator_uid": 501, "operator_gid": 20}, {"key": broker})
        self.assertTrue(connection.sent["ok"])
        self.assertEqual(connection.sent["result"], self.status)
        broker.dispatch.assert_called_once_with({"operation": "status"}, 501)
        broker.operator_intent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
