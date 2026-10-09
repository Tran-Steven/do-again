from __future__ import annotations

from do_again.core.executor import LocalExecutor

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.agent import Agent
from do_again.core.schema import OperatorError, atomic_json


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "Linux/macOS guarded git lock recovery")
class StaleGitLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Lock Test")
        git(self.repo, "config", "user.email", "lock@example.invalid")
        (self.repo / "a.txt").write_text("base\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-m", "base")
        self.control = root / "control"
        git(self.repo, "worktree", "add", "-b", "operator-control", str(self.control))
        self.policy = root / "policy.json"
        atomic_json(self.policy, {"schema_version": 1})
        self.agent = Agent(repo=self.repo, control_worktree=self.control, branch="operator-control",
                           policy_path=self.policy, state_dir=root/"state", executor=LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=root/"state"), )
        self.lock = Path(git(self.control, "rev-parse", "--git-path", "index.lock")).resolve()
        (self.control / "pending.txt").write_text("pending\n")

    def make_lock(self, age=650, content=b""):
        self.lock.write_bytes(content)
        t = time.time() - age
        os.utime(self.lock, (t, t))

    def mocked_checks(self, *, owner=False, scoped=False):
        original = subprocess.run
        def invoke(args, *a, **kw):
            if args[:2] == ["lsof", "-n"]:
                return subprocess.CompletedProcess(args, 0 if owner else 1,
                                                  "git 123" if owner else "", "")
            if args[:2] == ["ps", "-axo"]:
                return subprocess.CompletedProcess(args, 0, "123 1 /usr/bin/git -C "+str(self.agent.control_worktree)+" commit -m a\n" if scoped else "123 1 /usr/bin/git -C /tmp/other status\n", "")
            return original(args, *a, **kw)
        return patch("do_again.core.agent.subprocess.run", side_effect=invoke)

    def test_old_unowned_worktree_index_lock_is_removed_and_command_retried_once(self):
        self.make_lock()
        with patch("do_again.core.agent.shutil.which", return_value="/usr/sbin/lsof"), self.mocked_checks():
            self.agent.require_git("add", "pending.txt")
        self.assertFalse(self.lock.exists())
        self.assertIn("pending.txt", git(self.control, "diff", "--cached", "--name-only"))
        evidence = self.agent.state_dir/"stale_git_lock_recovery.json"
        self.assertTrue(evidence.exists())
        self.assertIn("orphaned_control_index_lock", evidence.read_text())

    def test_new_lock_is_never_deleted(self):
        self.make_lock(age=20)
        with patch("do_again.core.agent.shutil.which", return_value="/usr/sbin/lsof"), self.mocked_checks():
            with self.assertRaises(OperatorError):
                self.agent.require_git("add", "pending.txt")
        self.assertTrue(self.lock.exists())

    def test_owned_lock_is_never_deleted(self):
        self.make_lock()
        with patch("do_again.core.agent.shutil.which", return_value="/usr/sbin/lsof"), self.mocked_checks(owner=True):
            with self.assertRaises(OperatorError):
                self.agent.require_git("add", "pending.txt")
        self.assertTrue(self.lock.exists())

    def test_git_activity_in_same_worktree_prevents_deletion(self):
        self.make_lock()
        with patch("do_again.core.agent.shutil.which", return_value="/usr/sbin/lsof"), self.mocked_checks(scoped=True):
            with self.assertRaises(OperatorError):
                self.agent.require_git("add", "pending.txt")
        self.assertTrue(self.lock.exists())

    def test_nonempty_lock_is_never_deleted(self):
        self.make_lock(content=b"active data")
        with patch("do_again.core.agent.shutil.which", return_value="/usr/sbin/lsof"), self.mocked_checks():
            with self.assertRaises(OperatorError):
                self.agent.require_git("add", "pending.txt")
        self.assertTrue(self.lock.exists())

    def test_unrelated_git_errors_do_not_delete_lock(self):
        self.make_lock()
        with patch("do_again.core.agent.shutil.which", return_value="/usr/sbin/lsof"), self.mocked_checks():
            with self.assertRaises(OperatorError):
                self.agent.require_git("rev-parse", "ref-does-not-exist")
        self.assertTrue(self.lock.exists())

    def test_unknown_ownership_checker_fails_closed(self):
        self.make_lock()
        with patch("do_again.core.agent.shutil.which", return_value=None):
            with self.assertRaises(OperatorError):
                self.agent.require_git("add", "pending.txt")
        self.assertTrue(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
