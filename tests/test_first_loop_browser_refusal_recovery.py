"""No-effect tests for the second exact, administrator-audited pre-Send recovery."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/recover_first_loop_browser_refusal.py"
SPEC = importlib.util.spec_from_file_location("v1_first_loop_refusal", SCRIPT)
assert SPEC and SPEC.loader
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


class ExactFirstLoopRefusalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.commit = "f" * 40
        rel = "tools/recover_first_loop_browser_refusal.py"
        file = self.home / rel
        file.parent.mkdir(parents=True)
        file.write_bytes(b"exact committed one-time root recovery")
        data = file.read_bytes()
        digest = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\x00" + data).hexdigest()

        def fake_git(*args):
            if args == ("rev-parse", "HEAD"):
                return self.commit
            if args == ("rev-parse", "HEAD:" + rel):
                return digest
            raise AssertionError("unexpected Git command")

        p = patch.object(script, "REPO", self.home)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(script, "_git", side_effect=fake_git)
        p.start()
        self.addCleanup(p.stop)

    def test_source_must_match_explicit_exact_committed_blob(self):
        script._candidate(self.commit)
        with self.assertRaisesRegex(RuntimeError, "Git SHA"):
            script._candidate("short")
        with self.assertRaisesRegex(RuntimeError, "approved exact source"):
            script._candidate("a" * 40)
        path = self.home / "tools/recover_first_loop_browser_refusal.py"
        path.write_bytes(b"modified")
        with self.assertRaisesRegex(RuntimeError, "pinned Git object"):
            script._candidate(self.commit)

    def test_symlink_and_missing_tool_are_rejected(self):
        tool = self.home / "tools/recover_first_loop_browser_refusal.py"
        tool.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing or aliased"):
            script._candidate(self.commit)
        tool.symlink_to(self.home / "tools")
        with self.assertRaisesRegex(RuntimeError, "missing or aliased"):
            script._candidate(self.commit)

    def test_unauthorized_root_request_cannot_enter_recovery_path(self):
        with patch.object(script.sys, "platform", "linux"), \
             patch.object(script, "_candidate") as candidate:
            with self.assertRaises(SystemExit):
                script.run(["--candidate-sha", self.commit,
                            "--request-id", script.ORIGINAL, "--apply"])
            candidate.assert_not_called()
        with patch.object(script.sys, "platform", "darwin"), \
             patch.object(script.os, "getuid", return_value=0, create=True), \
             patch.object(script.os, "geteuid", return_value=0, create=True), \
             patch.object(script, "_candidate") as candidate:
            with self.assertRaises(SystemExit):
                script.run(["--candidate-sha", self.commit,
                            "--request-id", "different-browser-effect", "--apply"])
            candidate.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "native ownership check requires POSIX")
    def test_original_outbox_hash_is_deterministic_and_aliases_fail(self):
        folder = self.home / "browser_outbox"
        folder.mkdir()
        uid = os.getuid()
        for name in ("a", "b"):
            (folder / (name + ".json")).write_text('{"receipt":"fixture"}')
        a = script._fingerprint_outbox(folder, uid)
        b = script._fingerprint_outbox(folder, uid)
        self.assertEqual(a, b)
        self.assertEqual(a["count"], 2)
        (folder / "a.json").write_text('{"receipt":"changed"}')
        self.assertNotEqual(script._fingerprint_outbox(folder, uid)["sha256"], a["sha256"])
        (folder / "b.json").unlink()
        (folder / "b.json").symlink_to(folder / "a.json")
        with self.assertRaisesRegex(RuntimeError, "aliased"):
            script._fingerprint_outbox(folder, uid)


if __name__ == "__main__":
    unittest.main()
