import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from do_again.core.schema import OperatorError, utc_now
from do_again.service.isolated_sandbox import (
    advance_isolated_sandbox, create_isolated_sandbox,
)
from do_again.service.isolated_worker import create_isolated_worker


class IsolatedSandboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.root = root
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
        (self.source / ".gitignore").write_text("app.py\n")
        (self.source / "app.py").write_text("def value():\n    return 1\n")
        git(self.source, "add", ".gitignore")
        git(self.source, "add", "-f", "app.py")
        git(self.source, "commit", "-m", "tracked ignored fixture")
        self.source_head = git(self.source, "rev-parse", "HEAD")
        self.sandbox = create_isolated_sandbox(
            source_repo=self.source, state_dir=self.state)
        self.workspace = Path(self.sandbox["workspace"])
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
        self.agent = create_isolated_worker(
            repo=self.workspace, control_worktree=self.control,
            state_dir=self.state, branch="isolated-control",
            image="python@sha256:" + "a" * 64)
        self.request_id = "isolated-" + "1" * 24

    def produce_receipt(self):
        before = hashlib.sha256((self.workspace / "app.py").read_bytes()).hexdigest()
        issued = utc_now()
        manifest = {
            "expected_head": self.git(self.workspace, "rev-parse", "HEAD"),
            "changes": [{
                "path": "app.py",
                "before_sha256": before,
                "content": "def value():\n    return 2\n",
            }],
            "test_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
        }
        request = {
            "schema_version": 1,
            "request_id": self.request_id,
            "operation": "isolated_patch",
            "issued_at_utc": issued.isoformat(),
            "expires_at_utc": (issued + timedelta(minutes=30)).isoformat(),
            "args": {"manifest": manifest},
            "expected": {},
            "limits": {"timeout_seconds": 300},
        }
        self.agent.publish_json(
            Path("automation/do_again/requests") / (self.request_id + ".json"),
            request, "synthetic isolated request")
        with patch("do_again.service.isolated_cycle._docker_test",
                   return_value=True) as tested:
            self.assertEqual(self.agent.run(once=True), 0)
        tested.assert_called_once()
        receipt = self.agent.receipt_payload(self.request_id)
        self.assertEqual(receipt["state"], "succeeded", receipt.get("error"))
        return receipt

    def acknowledge(self, receipt, *, digest=None):
        digest = digest or hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        folder = self.state / "receipt-reports"
        folder.mkdir(mode=0o700, exist_ok=True)
        (folder / (self.request_id + ".json")).write_text(json.dumps({
            "state": "acknowledged",
            "request_id": self.request_id,
            "receipt_sha256": digest,
        }))

    def test_create_private_snapshot_retains_tracked_ignored_files(self):
        self.assertEqual(self.sandbox["state"], "created")
        self.assertTrue((self.workspace / "app.py").is_file())
        self.assertFalse((self.workspace / ".git" / "config").read_text().find(
            "origin.git") >= 0)
        self.assertEqual(
            (self.source / "app.py").read_text(), "def value():\n    return 1\n")
        existing = create_isolated_sandbox(
            source_repo=self.source, state_dir=self.state)
        self.assertEqual(existing["state"], "existing")
        self.assertEqual(existing["head"], self.sandbox["head"])

    def test_advance_only_verified_acknowledged_patch_without_touching_original(self):
        receipt = self.produce_receipt()
        self.acknowledge(receipt)
        result = advance_isolated_sandbox(self.agent, request_id=self.request_id)
        self.assertEqual(result["state"], "advanced")
        self.assertEqual(
            (self.workspace / "app.py").read_text(),
            "def value():\n    return 2\n")
        self.assertEqual(
            (self.source / "app.py").read_text(),
            "def value():\n    return 1\n")
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.source_head)
        self.assertFalse(self.git(self.workspace, "status", "--porcelain"))
        twice = advance_isolated_sandbox(self.agent, request_id=self.request_id)
        self.assertEqual(twice["state"], "already_advanced")
        self.assertEqual(twice["sandbox_commit"], result["sandbox_commit"])

    def test_refuses_unacknowledged_receipts(self):
        self.produce_receipt()
        with self.assertRaisesRegex(OperatorError, "has not acknowledged"):
            advance_isolated_sandbox(self.agent, request_id=self.request_id)
        self.assertEqual(
            (self.workspace / "app.py").read_text(),
            "def value():\n    return 1\n")

    def test_refuses_receipt_digest_mismatch(self):
        receipt = self.produce_receipt()
        self.acknowledge(receipt, digest="0" * 64)
        with self.assertRaisesRegex(OperatorError, "does not attest"):
            advance_isolated_sandbox(self.agent, request_id=self.request_id)
        self.assertEqual(
            (self.workspace / "app.py").read_text(),
            "def value():\n    return 1\n")

    def test_refuses_dirty_isolated_workspace(self):
        receipt = self.produce_receipt()
        self.acknowledge(receipt)
        (self.workspace / "app.py").write_text("uncommitted\n")
        with self.assertRaisesRegex(OperatorError, "dirty"):
            advance_isolated_sandbox(self.agent, request_id=self.request_id)
        self.assertEqual(
            (self.source / "app.py").read_text(),
            "def value():\n    return 1\n")


if __name__ == "__main__":
    unittest.main()
