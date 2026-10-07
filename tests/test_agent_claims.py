from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from do_again.core.agent import Agent
from do_again.core.schema import atomic_json, utc_now


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        text=True,
        capture_output=True,
    )


class DistributedClaimTests(unittest.TestCase):
    def test_two_control_worktrees_execute_one_request_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.invalid")
            (repo / "README.md").write_text("test\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")

            bare = root / "control.git"
            subprocess.run(
                ["git", "init", "--bare", str(bare)],
                check=True,
                text=True,
                capture_output=True,
            )
            seed = root / "seed"
            seed.mkdir()
            git(seed, "init", "-b", "operator-control")
            git(seed, "config", "user.name", "Test")
            git(seed, "config", "user.email", "test@example.invalid")
            request_dir = seed / "automation/do_again/requests"
            request_dir.mkdir(parents=True)
            now = utc_now()
            request = {
                "schema_version": 1,
                "request_id": "distributed-claim-0001",
                "operation": "status",
                "issued_at_utc": now.isoformat(),
                "expires_at_utc": (now + timedelta(minutes=20)).isoformat(),
                "args": {},
                "expected": {},
                "limits": {},
            }
            request_path = request_dir / "distributed-claim-0001.json"
            atomic_json(request_path, request)
            git(seed, "add", ".")
            git(seed, "commit", "-m", "request")
            git(seed, "remote", "add", "origin", str(bare))
            git(seed, "push", "-u", "origin", "operator-control")

            controls = []
            for name in ("a", "b"):
                control = root / f"control-{name}"
                subprocess.run(
                    [
                        "git",
                        "clone",
                        "--branch",
                        "operator-control",
                        str(bare),
                        str(control),
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                )
                git(control, "config", "user.name", "Test")
                git(control, "config", "user.email", "test@example.invalid")
                controls.append(control)

            policy = root / "policy.json"
            atomic_json(
                policy,
                {
                    "schema_version": 1,
                    "max_request_ttl_seconds": 3600,
                    "max_future_skew_seconds": 300,
                    "allowed_operations": ["status"],
                    "approved_cwd_roots": [str(repo)],
                    "read_roots": [str(repo)],
                },
            )
            agents = [
                Agent(
                    repo=repo,
                    control_worktree=control,
                    branch="operator-control",
                    remote="origin",
                    policy_path=policy,
                    state_dir=root / f"state-{index}",
                )
                for index, control in enumerate(controls)
            ]

            executions: list[int] = []
            execution_lock = threading.Lock()

            def execute(_request):
                with execution_lock:
                    executions.append(1)
                time.sleep(0.25)
                return {"result": {"returncode": 0}}

            for agent in agents:
                agent.executor.execute = execute
                agent.publish_receipt = lambda receipt: None
                agent.publish_status = lambda *args, **kwargs: None

            threads = [
                threading.Thread(
                    target=agent.process_path,
                    args=(
                        control
                        / "automation/do_again/requests/distributed-claim-0001.json",
                    ),
                )
                for agent, control in zip(agents, controls)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(executions, [1])
            claim = subprocess.run(
                [
                    "git",
                    f"--git-dir={bare}",
                    "show",
                    "operator-control:automation/do_again/claims/distributed-claim-0001.json",
                ],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            value = json.loads(claim)
            self.assertEqual(
                value["request_fingerprint"],
                agents[0].claim_payload("distributed-claim-0001")[
                    "request_fingerprint"
                ]
                if agents[0].claim_payload("distributed-claim-0001")
                else agents[1].claim_payload("distributed-claim-0001")[
                    "request_fingerprint"
                ],
            )


if __name__ == "__main__":
    unittest.main()
