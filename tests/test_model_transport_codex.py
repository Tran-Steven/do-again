"""Offline regressions for the opt-in Codex transport. Never invokes Codex."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again import model_transport as transport


class CodexTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.binary = self.home / "codex"
        self.binary.touch()
        self.capacity_guard = patch("do_again.model_quota.require_model_capacity", return_value={"allowed": True})
        self.capacity_guard.start()
        self.addCleanup(self.capacity_guard.stop)
        self.schema = {
            "type": "object",
            "properties": {"reply": {"type": "string", "maxLength": 120}},
            "required": ["reply"],
            "additionalProperties": False,
        }

    def test_no_authorization_means_no_login_or_model_process(self):
        with patch.object(transport, "login_ready") as login, patch.object(
                transport.subprocess, "run") as run:
            with self.assertRaisesRegex(transport.CodexTransportBlocked, "explicit"):
                transport.generate_structured("test", self.schema, binary=self.binary, home=self.home)
        login.assert_not_called()
        run.assert_not_called()

    def test_discovers_isolated_npm_cli_without_global_path(self):
        root = self.home / ".do_again" / "codex-tools"
        binary = root / "node_modules" / ".bin" / "codex"
        binary.parent.mkdir(parents=True)
        binary.touch()
        with patch.object(transport.Path, "home", return_value=self.home), patch.object(
                transport.shutil, "which", return_value=None):
            self.assertEqual(transport.discover_codex().resolve(), binary.resolve())

    def test_rejects_escaped_isolated_binary_symlink(self):
        root = self.home / ".do_again" / "codex-tools"
        binary = root / "node_modules" / ".bin" / "codex"
        binary.parent.mkdir(parents=True)
        binary.symlink_to(self.binary)
        with patch.object(transport.Path, "home", return_value=self.home), patch.object(
                transport.shutil, "which", return_value=None):
            self.assertIsNone(transport.discover_codex())

    def test_unavailable_binary_is_not_a_browser_fallback(self):
        with patch.object(transport, "discover_codex", return_value=None), patch.object(
                transport.subprocess, "run") as run:
            with self.assertRaisesRegex(transport.CodexTransportBlocked, "not installed"):
                transport.generate_structured("test", self.schema, home=self.home, allow_model_call=True)
        run.assert_not_called()

    def test_login_status_never_exposes_original_session(self):
        with patch.object(transport.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["codex", "login", "status"], 0, "Logged in using ChatGPT", "")) as run:
            self.assertTrue(transport.login_ready(self.binary, home=self.home))
        argv = run.call_args.args[0]
        self.assertEqual(argv, [str(self.binary), "login", "status"])
        self.assertNotIn("OPENAI_API_KEY", run.call_args.kwargs["env"])
        self.assertNotIn("GITHUB_TOKEN", run.call_args.kwargs["env"])

    def test_unverified_login_returns_false(self):
        with patch.object(transport.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "Not logged in", "")):
            self.assertFalse(transport.login_ready(self.binary, home=self.home))

    def test_single_sandboxed_structured_call(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            output = Path(args[args.index("--output-last-message") + 1])
            output.write_text(json.dumps({"reply": "ready"}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch.object(transport, "login_ready", return_value=True), patch.object(
                transport.subprocess, "run", side_effect=fake_run):
            answer = transport.generate_structured(
                "Respond with ready", self.schema, binary=self.binary, home=self.home,
                allow_model_call=True, model="gpt-5.6", timeout_seconds=60)
        self.assertEqual(answer, {"reply": "ready"})
        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args[:4], [str(self.binary), "--ask-for-approval", "never", "exec"])
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(args[args.index("--ask-for-approval") + 1], "never")
        self.assertIn("--skip-git-repo-check", args)
        self.assertNotIn("--full-auto", args)
        self.assertEqual(args[-1], "-")
        self.assertEqual(kwargs["input"], "Respond with ready")
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
        self.assertFalse(Path(kwargs["cwd"]).exists())

    def test_invalid_schema_is_blocked_before_auth_or_execution(self):
        for schema in ({"type": "object", "properties": {}, "required": [],
                        "additionalProperties": False},
                       dict(self.schema, additionalProperties=True),
                       {"type": "array", "items": {"type": "string"}}):
            with self.subTest(schema=schema), patch.object(transport, "login_ready") as login:
                with self.assertRaises(transport.CodexTransportBlocked):
                    transport.generate_structured("test", schema, binary=self.binary,
                                                  home=self.home, allow_model_call=True)
                login.assert_not_called()

    def test_model_parameter_does_not_accept_flag_injection(self):
        with patch.object(transport, "login_ready") as login:
            with self.assertRaisesRegex(transport.CodexTransportBlocked, "invalid fixed model"):
                transport.generate_structured("test", self.schema, binary=self.binary,
                    home=self.home, allow_model_call=True, model="gpt --full-auto")
        login.assert_not_called()

    def test_invalid_output_never_claims_success(self):
        def fake_run(args, **kwargs):
            target = Path(args[args.index("--output-last-message") + 1])
            target.write_text(json.dumps({"reply": 12}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch.object(transport, "login_ready", return_value=True), patch.object(
                transport.subprocess, "run", side_effect=fake_run) as run:
            with self.assertRaises(transport.CodexTransportUncertain):
                transport.generate_structured("test", self.schema, binary=self.binary,
                                                  home=self.home, allow_model_call=True)
        run.assert_called_once()

    def test_timeout_is_uncertain_and_never_retried(self):
        with patch.object(transport, "login_ready", return_value=True), patch.object(
                transport.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 60)) as run:
            with self.assertRaisesRegex(transport.CodexTransportUncertain, "do not retry"):
                transport.generate_structured("test", self.schema, binary=self.binary,
                    home=self.home, allow_model_call=True, timeout_seconds=60)
        run.assert_called_once()

    def test_sensitive_codex_errors_are_redacted_to_safe_categories(self):
        from do_again.model_transport import _safe_codex_failure_category
        token = "shh-not-a-real-token"
        for stderr, expected in (
            ("401 access token " + token, "authentication"),
            ("quota exceeded; secret=" + token, "quota_or_rate_limit"),
            ("Connection refused at secret=" + token, "connectivity"),
            ("unknown option --flag: " + token, "argument_validation"),
            ("random provider details; secret=" + token, "unclassified"),
        ):
            with self.subTest(category=expected):
                category = _safe_codex_failure_category("", stderr)
                self.assertEqual(category, expected)
                self.assertNotIn(token, category)

    def test_nonzero_result_categorized_without_leaking_provider_output(self):
        with patch.object(transport, "login_ready", return_value=True), patch.object(
                transport.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["codex"], 1, "", "rate limit; private-auth-stuff")) as run:
            with self.assertRaises(transport.CodexTransportUncertain) as raised:
                transport.generate_structured("test", self.schema, binary=self.binary,
                                             home=self.home, allow_model_call=True)
        self.assertIn("quota_or_rate_limit", str(raised.exception))
        self.assertNotIn("private-auth-stuff", str(raised.exception))
        run.assert_called_once()

    def test_exhausted_account_blocks_before_model_launch_without_retry(self):
        from do_again.model_quota import CodexQuotaUnavailable
        with patch.object(transport, "login_ready", return_value=True), patch(
                "do_again.model_quota.require_model_capacity",
                side_effect=CodexQuotaUnavailable("weekly usage exhausted; no model call attempted")
        ) as quota, patch.object(transport.subprocess, "run") as run:
            with self.assertRaisesRegex(transport.CodexTransportBlocked, "weekly usage exhausted"):
                transport.generate_structured(
                    "test", self.schema, binary=self.binary, home=self.home, allow_model_call=True)
        quota.assert_called_once_with(self.binary, self.home.resolve())
        run.assert_not_called()

    def test_nonzero_return_is_uncertain_and_never_retried(self):
        with patch.object(transport, "login_ready", return_value=True), patch.object(
                transport.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["codex"], 1, "private message", "secret")) as run:
            with self.assertRaisesRegex(transport.CodexTransportUncertain, "do not retry"):
                transport.generate_structured("test", self.schema, binary=self.binary,
                    home=self.home, allow_model_call=True)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
