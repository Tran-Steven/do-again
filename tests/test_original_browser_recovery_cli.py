"""Offline tests for the exact one-time, administrator-only recovery command."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/recover_original_browser_tick.py"
SPEC = importlib.util.spec_from_file_location("original_tick_recovery_cli", SCRIPT)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RecoveryCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.commit = "c" * 40
        self.files = (
            "tools/recover_original_browser_tick.py",
            "src/do_again/supervisor/pre_dispatch_recovery.py",
        )
        for name in self.files:
            file = self.repo / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(("trusted fixture " + name).encode())
        self.expected = {}
        for name in self.files:
            data = (self.repo / name).read_bytes()
            self.expected[name] = hashlib.sha1(
                b"blob " + str(len(data)).encode() + b"\x00" + data
            ).hexdigest()
        def verified_git(*args):
            if args == ("rev-parse", "HEAD"):
                return self.commit
            if len(args) == 2 and args[0] == "rev-parse" and args[1].startswith("HEAD:"):
                return self.expected[args[1][5:]]
            raise AssertionError("unexpected Git command")
        patcher = patch.object(recovery, "REPO", self.repo)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(recovery, "_git", side_effect=verified_git)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_commit_and_exact_tracked_content_are_required(self):
        self.assertEqual(
            recovery._verified_candidate(self.commit),
            self.repo / self.files[1],
        )
        with self.assertRaisesRegex(RuntimeError, "exact source commit"):
            recovery._verified_candidate("not-a-sha")
        with self.assertRaisesRegex(RuntimeError, "approved exact SHA"):
            recovery._verified_candidate("a" * 40)

    def test_tampered_or_aliased_candidate_never_admits_root_recovery(self):
        helper = self.repo / self.files[1]
        helper.write_bytes(b"modified helper")
        with self.assertRaisesRegex(RuntimeError, "committed tree"):
            recovery._verified_candidate(self.commit)
        helper.unlink()
        helper.symlink_to(self.repo / self.files[0])
        with self.assertRaisesRegex(RuntimeError, "aliased"):
            recovery._verified_candidate(self.commit)

    def test_root_entrypoint_refuses_non_mac_and_original_id_mismatch_before_effects(self):
        with patch.object(recovery.sys, "platform", "linux"), \
             patch.object(recovery, "_verified_candidate") as verify:
            with self.assertRaises(SystemExit):
                recovery.run([
                    "--candidate-sha", self.commit, "--request-id", recovery.ORIGINAL_ID,
                    "--apply",
                ])
            verify.assert_not_called()
        with patch.object(recovery.sys, "platform", "darwin"), \
             patch.object(recovery.os, "geteuid", return_value=0, create=True), \
             patch.object(recovery.os, "getuid", return_value=0, create=True), \
             patch.object(recovery, "_verified_candidate") as verify:
            with self.assertRaises(SystemExit):
                recovery.run([
                    "--candidate-sha", self.commit, "--request-id", "other-tick",
                    "--apply",
                ])
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
