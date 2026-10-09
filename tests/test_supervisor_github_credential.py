from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.macos_server import ProjectBroker


class OperatorGitHubCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.intent = "maintenance"
        self.broker = SimpleNamespace(
            state=self.path,
            project=SimpleNamespace(key="project", repo=Path("/synthetic/project")),
            config={"projects": [{"key": "project", "github_repository": "Tran-Steven/do-again"}]},
            registry=SimpleNamespace(status=lambda repo: {"intent": self.intent}),
            admission=nullcontext,
        )
        self.token = "testCredentialOnly" * 3

    def enroll(self):
        return ProjectBroker.operator_github_token(self.broker, self.token)

    def test_creates_private_root_owned_compatible_credential(self):
        result = self.enroll()
        target = self.path / "github-token"
        self.assertEqual(result, {"registered": True, "repository": "Tran-Steven/do-again"})
        self.assertNotIn(self.token, str(result))
        self.assertEqual(target.read_text(), self.token)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertFalse((self.path / "github-token.pending").exists())

    def test_reenrollment_atomically_replaces_token(self):
        self.enroll()
        self.token = "anotherPrivateCredential" * 2
        self.enroll()
        self.assertEqual((self.path / "github-token").read_text(), self.token)
        self.assertEqual((self.path / "github-token").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.path / "github-token.pending").exists())

    def test_maintenance_required_before_file_creation(self):
        self.intent = "active"
        with self.assertRaises(ExecutionBlocked):
            self.enroll()
        self.assertFalse((self.path / "github-token").exists())

    def test_alias_target_never_replaced(self):
        (self.path / "original").write_text("unchanged")
        (self.path / "github-token").symlink_to(self.path / "original")
        with self.assertRaises(ExecutionBlocked):
            self.enroll()
        self.assertEqual((self.path / "original").read_text(), "unchanged")
        self.assertFalse((self.path / "github-token.pending").exists())

    def test_preexisting_pending_file_is_not_overwritten(self):
        pending = self.path / "github-token.pending"
        pending.write_text("unrelated")
        with self.assertRaises(FileExistsError):
            self.enroll()
        self.assertEqual(pending.read_text(), "unrelated")
        self.assertFalse((self.path / "github-token").exists())

    def test_existing_hardlink_target_never_replaced(self):
        original = self.path / "original"
        original.write_text("unchanged")
        os.link(original, self.path / "github-token")
        with self.assertRaises(ExecutionBlocked):
            self.enroll()
        self.assertEqual(original.read_text(), "unchanged")
        self.assertFalse((self.path / "github-token.pending").exists())


if __name__ == "__main__":
    unittest.main()
