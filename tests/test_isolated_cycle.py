"""Offline acceptance tests for the opt-in, non-autonomous coding-cycle primitive."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.schema import OperatorError
from do_again.service.isolated_cycle import FIXED_TEST_SUITES, run_isolated_cycle, _docker_test


class IsolatedCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.output = self.root / "receipts"
        def git(*args):
            return subprocess.run(["git", "-C", str(self.repo), *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.git = git
        git("init", "-b", "main")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Do Again Test")
        git("config", "core.autocrlf", "false")
        (self.repo / "app.py").write_text("def answer():\n    return 1\n")
        git("add", "app.py")
        git("commit", "-m", "baseline")
        self.head = git("rev-parse", "HEAD")
        self.manifest = {
            "expected_head": self.head,
            "changes": [{
                "path": "app.py",
                "before_sha256": hashlib.sha256(
                    (self.repo / "app.py").read_bytes()).hexdigest(),
                "content": "def answer():\n    return 2\n",
            }],
            "test_command": ["python", "-m", "unittest", "discover"],
        }
        self.image = "python@sha256:" + "a" * 64

    def test_patch_commit_receipt_is_isolated_from_live_repo(self):
        with patch("do_again.service.isolated_cycle._docker_test", return_value=True) as test:
            result = run_isolated_cycle(
                self.repo, self.manifest, image=self.image, output_dir=self.output)
        test.assert_called_once()
        self.assertEqual(result["state"], "verified_isolated_patch")
        self.assertFalse(result["production_deployed"])
        self.assertFalse(result["automatic_merge"])
        self.assertFalse(result["chatgpt_planning_verified"])
        self.assertEqual(result["files"], ["app.py"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual((self.repo / "app.py").read_text(),
                         "def answer():\n    return 1\n")
        patch_bytes = (self.output / result["patch_file"]).read_bytes()
        self.assertIn(b"return 2", patch_bytes)
        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(),
                         result["patch_sha256"])
        receipt = json.loads(next(self.output.glob("*.receipt.json")).read_text())
        self.assertEqual(receipt, result)

    def test_wrong_base_hash_fails_before_tests(self):
        self.manifest["expected_head"] = "b" * 40
        with patch("do_again.service.isolated_cycle._docker_test") as test:
            with self.assertRaisesRegex(OperatorError, "HEAD changed"):
                run_isolated_cycle(
                    self.repo, self.manifest, image=self.image, output_dir=self.output)
        test.assert_not_called()

    def test_traversal_and_untracked_paths_are_denied(self):
        for path in ("../secrets.py", "/tmp/other.py", ".git/config.py", "new.py"):
            with self.subTest(path=path):
                self.manifest["changes"][0]["path"] = path
                with patch("do_again.service.isolated_cycle._docker_test") as test:
                    with self.assertRaises(OperatorError):
                        run_isolated_cycle(
                            self.repo, self.manifest, image=self.image, output_dir=self.output)
                test.assert_not_called()

    def test_failed_offline_test_cannot_produce_a_verified_receipt(self):
        with patch("do_again.service.isolated_cycle._docker_test", return_value=False):
            with self.assertRaisesRegex(OperatorError, "test failed"):
                run_isolated_cycle(
                    self.repo, self.manifest, image=self.image, output_dir=self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_user_output_cannot_modify_source_repo(self):
        with patch("do_again.service.isolated_cycle._docker_test") as test:
            with self.assertRaisesRegex(OperatorError, "outside source"):
                run_isolated_cycle(self.repo, self.manifest, image=self.image,
                                   output_dir=self.repo / "output")
        test.assert_not_called()

    def test_container_is_offline_nonroot_and_image_pinned(self):
        image = self.image
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"", b"Ran 1 test in 0.001s\\nOK")
        with patch("do_again.service.isolated_cycle.subprocess.run",
                   side_effect=fake_run):
            self.assertTrue(_docker_test(self.repo, image,
                ["python", "-m", "unittest", "discover"]))
        command = calls[-1]
        for required in ("--network=none", "--read-only", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges", "--user=65534:65534"):
            self.assertIn(required, command)
        self.assertTrue(any("readonly" in part for part in command))
        self.assertIn("--pull=never", command)
        self.assertIn(image, command)

    def test_bun_and_pytest_require_positive_execution_receipts(self):
        cases = (
            ("pytest", b"2 passed in 0.03s", True),
            ("pytest", b"no tests ran in 0.03s", False),
            ("pytest", b"0 passed in 0.03s", False),
            ("bun", b" 3 pass\\n 0 fail\\n", True),
            ("bun", b" 0 pass\\n 0 fail\\n", False),
            ("bun", b"No tests found!", False),
        )
        for suite, report, expected in cases:
            with self.subTest(suite=suite, report=report):
                calls = []
                def fake_run(argv, **kwargs):
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, report, b"")
                with patch(
                    "do_again.service.isolated_cycle.subprocess.run",
                    side_effect=fake_run
                ):
                    self.assertEqual(
                        _docker_test(self.repo, self.image, list(FIXED_TEST_SUITES[suite])),
                        expected)
                runner = calls[-1]
                for flag in ("--network=none", "--read-only", "--pull=never",
                             "--cap-drop=ALL", "--user=65534:65534"):
                    self.assertIn(flag, runner)
                self.assertEqual(
                    runner[-len(FIXED_TEST_SUITES[suite]):],
                    list(FIXED_TEST_SUITES[suite]))

    def test_unapproved_test_runner_variants_never_start_containers(self):
        for command in (
            ["pytest", "-q"],
            ["python", "-m", "pytest", "-q"],
            ["bun", "test", "some-other-file.test.ts"],
            ["bun", "run", "deploy"],
            ["npm", "run", "deploy"],
        ):
            with self.subTest(command=command):
                with patch("do_again.service.isolated_cycle.subprocess.run") as execute:
                    with self.assertRaisesRegex(OperatorError, "not an admitted"):
                        _docker_test(self.repo, self.image, command)
                    execute.assert_not_called()

    def test_unpinned_image_and_unapproved_command_fail_before_docker(self):
        with patch("do_again.service.isolated_cycle.subprocess.run") as execute:
            with self.assertRaisesRegex(OperatorError, "pinned"):
                _docker_test(self.repo, "python:latest", ["python", "-m", "unittest", "discover"])
            with self.assertRaisesRegex(OperatorError, "not an admitted"):
                _docker_test(self.repo, self.image, ["bash", "-c", "curl example.com"])
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
