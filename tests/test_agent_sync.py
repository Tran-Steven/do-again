from __future__ import annotations

from do_again.core.executor import LocalExecutor

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.agent import Agent
from do_again.core.schema import atomic_json


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class AgentSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.seed = root / "seed"
        self.seed.mkdir()
        git(self.seed, "init", "-b", "operator-control")
        git(self.seed, "config", "user.name", "Test")
        git(self.seed, "config", "user.email", "test@example.invalid")
        (self.seed / "README.md").write_text("initial\n", encoding="utf-8")
        git(self.seed, "add", "README.md")
        git(self.seed, "commit", "-m", "initial")
        self.bare = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.bare)], check=True, capture_output=True)
        git(self.seed, "remote", "add", "relay", str(self.bare))
        git(self.seed, "push", "relay", "operator-control")
        self.control = root / "control"
        subprocess.run(
            ["git", "clone", "-o", "relay", "--branch", "operator-control", str(self.bare), str(self.control)],
            check=True,
            capture_output=True,
        )
        git(self.control, "config", "user.name", "Test")
        git(self.control, "config", "user.email", "test@example.invalid")
        self.policy = root / "policy.json"
        atomic_json(self.policy, {"schema_version": 1})
        self.agent = Agent(
            repo=self.seed,
            control_worktree=self.control,
            branch="operator-control",
            remote="relay",
            policy_path=self.policy,
            state_dir=root / "state",
            executor=LocalExecutor(repo=self.seed, policy_path=self.policy, state_dir=root / "state"),
        )

    def commands(self, spy):
        return [call.args[0] for call in spy.call_args_list]

    def test_idle_sync_skips_fetch_and_push(self):
        with patch.object(self.agent, "git", wraps=self.agent.git) as spy:
            self.agent.sync()
        commands = self.commands(spy)
        self.assertIn("ls-remote", commands)
        self.assertNotIn("fetch", commands)
        self.assertNotIn("push", commands)

    def test_remote_changes_fetch_without_noop_push(self):
        (self.seed / "update.txt").write_text("remote update\n", encoding="utf-8")
        git(self.seed, "add", "update.txt")
        git(self.seed, "commit", "-m", "remote update")
        git(self.seed, "push", "relay", "operator-control")
        with patch.object(self.agent, "git", wraps=self.agent.git) as spy:
            self.agent.sync()
        commands = self.commands(spy)
        self.assertIn("fetch", commands)
        self.assertNotIn("push", commands)
        self.assertEqual((self.control / "update.txt").read_text(), "remote update\n")

    def test_unpushed_local_commit_is_published(self):
        (self.control / "local.txt").write_text("local update\n", encoding="utf-8")
        git(self.control, "add", "local.txt")
        git(self.control, "commit", "-m", "local update")
        with patch.object(self.agent, "git", wraps=self.agent.git) as spy:
            self.agent.sync()
        self.assertIn("push", self.commands(spy))
        self.assertEqual(
            subprocess.run(
                ["git", f"--git-dir={self.bare}", "show", "operator-control:local.txt"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout,
            "local update\n",
        )

    def test_publish_uses_configured_remote(self):
        self.agent.publish_json(Path("automation/do_again/agent_status.json"), {"state": "ready"}, "ready")
        result = subprocess.run(
            ["git", f"--git-dir={self.bare}", "show", "operator-control:automation/do_again/agent_status.json"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn('"state": "ready"', result.stdout)


if __name__ == "__main__":
    unittest.main()
