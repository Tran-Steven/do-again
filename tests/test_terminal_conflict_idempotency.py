from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from do_again.core.agent import Agent
from do_again.core.schema import atomic_json, request_fingerprint


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class TerminalConflictRegressionTests(unittest.TestCase):
    def test_failed_request_changed_after_head_advance_emits_one_conflict_in_200_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "project"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Fixture")
            git(repo, "config", "user.email", "fixture@example.invalid")
            (repo / "app.txt").write_text("before\n")
            git(repo, "add", "app.txt")
            git(repo, "commit", "-m", "initial")
            old_head = git(repo, "rev-parse", "HEAD")

            remote = root / "control.git"
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True, capture_output=True, text=True,
            )
            control = root / "control"
            control.mkdir()
            git(control, "init", "-b", "operator-control")
            git(control, "config", "user.name", "Fixture")
            git(control, "config", "user.email", "fixture@example.invalid")
            requests = control / "automation/do_again/requests"
            receipts = control / "automation/do_again/receipts"
            requests.mkdir(parents=True)
            receipts.mkdir(parents=True)
            request = {
                "request_id": "sonary-failed-old-request",
                "operation": "status",
                "args": {},
                "expected": {"repo_head": old_head},
                "limits": {},
            }
            old_fingerprint = request_fingerprint(request)
            path = requests / "sonary-failed-old-request.json"
            atomic_json(path, request)
            atomic_json(
                receipts / path.name,
                {
                    "request_id": request["request_id"],
                    "state": "failed",
                    "request_fingerprint": old_fingerprint,
                    "result": {"returncode": 1},
                },
            )
            git(control, "add", ".")
            git(control, "commit", "-m", "failed receipt")
            git(control, "remote", "add", "origin", str(remote))
            git(control, "push", "-u", "origin", "operator-control")

            # Subsequent legitimate success on the project, plus an old request
            # inadvertently edited in place, reproduces Sonary's actual issue.
            (repo / "app.txt").write_text("fixed\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "successful audio fix")
            updated_head = git(repo, "rev-parse", "HEAD")
            self.assertNotEqual(old_head, updated_head)
            request["args"] = {"changed": True}
            atomic_json(path, request)
            git(control, "add", ".")
            git(control, "commit", "-m", "unexpected reuse of prior request id")
            git(control, "push", "origin", "operator-control")
            initial_commit = git(control, "rev-list", "--count", "HEAD")

            policy = root / "policy.json"
            atomic_json(policy, {"schema_version": 1, "allowed_operations": ["status"]})
            state = root / "state"
            def new_agent():
                return Agent(
                    repo=repo, control_worktree=control, branch="operator-control",
                    policy_path=policy, state_dir=state,
                )

            agent = new_agent()
            observed = [agent.process_path(path) for _ in range(100)]
            self.assertTrue(observed[0])
            self.assertEqual(sum(bool(x) for x in observed), 1)
            second_agent = new_agent()
            observed_after_restart = [second_agent.process_path(path) for _ in range(100)]
            self.assertEqual(sum(bool(x) for x in observed_after_restart), 0)
            self.assertEqual(
                int(git(control, "rev-list", "--count", "HEAD")), int(initial_commit) + 1
            )
            conflicts = list((control / "automation/do_again/conflicts").rglob("*.json"))
            self.assertEqual(len(conflicts), 1)
            saved = json.loads(conflicts[0].read_text())
            self.assertEqual(saved["request_fingerprint"], request_fingerprint(request))
            self.assertEqual(saved["existing_fingerprint"], old_fingerprint)
            self.assertEqual(
                json.loads((receipts / path.name).read_text())["state"], "failed"
            )
            self.assertEqual(git(repo, "rev-parse", "HEAD"), updated_head)
            incident = json.loads((state / "stale_request_blocked.json").read_text())
            self.assertEqual(incident["request_id"], request["request_id"])

    def test_changed_conflict_cause_republishes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            policy = root / "policy.json"
            atomic_json(policy, {"schema_version": 1})
            agent = Agent(
                repo=repo, control_worktree=root / "control", branch="operator-control",
                policy_path=policy, state_dir=root / "state",
            )
            control = agent.control_worktree
            control.mkdir()
            publishes = []
            agent.sync = lambda: None
            def publish(relative, payload, message):
                publishes.append(payload)
                atomic_json(control / relative, payload)
            agent.publish_json = publish
            request = {"request_id": "request-123", "operation": "status"}
            self.assertTrue(agent.publish_conflict(request=request, reason="one", existing_fingerprint="old"))
            self.assertFalse(agent.publish_conflict(request=request, reason="one", existing_fingerprint="old"))
            self.assertTrue(agent.publish_conflict(request=request, reason="two", existing_fingerprint="old"))
            self.assertFalse(agent.publish_conflict(request=request, reason="two", existing_fingerprint="old"))
            self.assertEqual(len(publishes), 2)


if __name__ == "__main__":
    unittest.main()
