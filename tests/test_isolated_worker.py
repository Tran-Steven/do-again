"""Two-project Git-backed patch worker integration; no production repos or credentials."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from do_again.core.schema import OperatorError, request_fingerprint, utc_now
from do_again.service.isolated_worker import IsolatedPatchExecutor, create_isolated_worker


class IsolatedWorkerIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = "python@sha256:" + "a" * 64

    @staticmethod
    def git(repo, *args):
        proc = subprocess.run(["git", "-C", str(repo), *args], text=True,
                              capture_output=True, check=True, timeout=30)
        return proc.stdout.strip()

    def project(self, name):
        source = self.root / name
        source.mkdir()
        self.git(source, "init", "-b", "main")
        self.git(source, "config", "user.name", "Fixture")
        self.git(source, "config", "user.email", "fixture@example.invalid")
        self.git(source, "config", "core.autocrlf", "false")
        (source / "app.py").write_text("def answer():\n    return 1\n")
        self.git(source, "add", "app.py")
        self.git(source, "commit", "-m", "source baseline")
        baseline = self.git(source, "rev-parse", "HEAD")
        remote = self.root / (name + ".git")
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       capture_output=True, timeout=30)
        self.git(source, "remote", "add", "origin", str(remote))
        self.git(source, "push", "origin", "main")

        # The isolated worker never shares the privileged operator-control
        # branch or edits the live source branch.
        self.git(source, "checkout", "-b", "do-again-isolated")
        now = utc_now()
        rid = "isolated-" + name.lower() + "-000001"
        manifest = {
            "expected_head": baseline,
            "changes": [{
                "path": "app.py",
                "before_sha256": hashlib.sha256(
                    (source / "app.py").read_bytes()).hexdigest(),
                "content": "def answer():\n    return 2\n",
            }],
            "test_command": ["python", "-m", "unittest", "discover"],
        }
        request = {
            "schema_version": 1, "request_id": rid,
            "operation": "isolated_patch", "args": {"manifest": manifest},
            "expected": {}, "limits": {"timeout_seconds": 180},
            "issued_at_utc": now.isoformat(),
            "expires_at_utc": (now + timedelta(minutes=10)).isoformat(),
        }
        path = source / "automation/do_again/requests" / (rid + ".json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(request))
        self.git(source, "add", "automation")
        self.git(source, "commit", "-m", "queue isolated patch task")
        self.git(source, "push", "origin", "do-again-isolated")
        self.git(source, "checkout", "main")
        control = self.root / (name + "-control")
        self.git(source, "fetch", "origin", "do-again-isolated")
        self.git(source, "worktree", "add", "--detach", str(control), "FETCH_HEAD")
        return source, control, baseline, rid, request

    def test_sonary_and_jobpipe_have_independent_exact_once_receipts(self):
        projects = [self.project("Sonary"), self.project("jobpipe")]
        with patch("do_again.service.isolated_cycle._docker_test",
                   return_value=True) as tested:
            for source, control, baseline, rid, request in projects:
                agent = create_isolated_worker(
                    repo=source, control_worktree=control,
                    state_dir=self.root / (source.name + "-state"),
                    branch="do-again-isolated", image=self.image)
                self.assertEqual(agent.run(once=True), 0)
                receipt_path = control / "automation/do_again/receipts" / (rid + ".json")
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["state"], "succeeded", receipt.get("error"))
                self.assertEqual(receipt["request_fingerprint"],
                                 request_fingerprint(request))
                self.assertEqual(receipt["result"]["result"]["returncode"], 0)
                self.assertEqual(receipt["result"]["result"]["isolated_patch"]["state"],
                                 "verified_isolated_patch")
                self.assertEqual(self.git(source, "rev-parse", "HEAD"), baseline)
                self.assertEqual((source / "app.py").read_text(),
                                 "def answer():\n    return 1\n")
                self.assertTrue((agent.state_dir / "ledger" / (rid + ".json")).is_file())
                # Rerunning with the same state/remote receipt cannot reexecute.
                self.assertEqual(agent.run(once=True), 0)
        self.assertEqual(tested.call_count, 2)
        for source, control, baseline, rid, _ in projects:
            remote_receipt = self.git(
                control, "show", "origin/do-again-isolated:"
                + "automation/do_again/receipts/" + rid + ".json")
            self.assertEqual(json.loads(remote_receipt)["state"], "succeeded")

    def test_executor_rejects_commands_and_caller_managed_paths(self):
        source, _, _, _, request = self.project("Sonary")
        executor = IsolatedPatchExecutor(
            repo=source, image=self.image, state_dir=self.root / "evidence")
        bad = dict(request, operation="scratch_script")
        with patch("do_again.service.isolated_worker.run_isolated_cycle") as execute:
            with self.assertRaisesRegex(OperatorError, "only an inline"):
                executor.execute(bad)
            bad = dict(request, args={"manifest": request["args"]["manifest"],
                                      "cwd": "/etc"})
            with self.assertRaises(OperatorError):
                executor.execute(bad)
        execute.assert_not_called()

    def test_worker_refuses_production_control_branch(self):
        source, control, _, _, _ = self.project("jobpipe")
        with self.assertRaisesRegex(OperatorError, "dedicated"):
            create_isolated_worker(repo=source, control_worktree=control,
                state_dir=self.root / "state", branch="operator-control",
                image=self.image)


if __name__ == "__main__":
    unittest.main()
