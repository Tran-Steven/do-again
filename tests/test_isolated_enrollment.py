import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.schema import OperatorError
from do_again.service.isolated_enrollment import (
    enroll_isolated_project, enrolled_config, run_enrolled_isolated,
)


class IsolatedEnrollmentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "Sonary"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = patch.dict("os.environ", {"DO_AGAIN_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True, capture_output=True, text=True, timeout=30
            ).stdout.strip()
        self.git = git
        git("init", "-b", "main")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "core.autocrlf", "false")
        (self.repo / "app.py").write_text("def value():\n    return 1\n")
        git("add", "app.py")
        git("commit", "-m", "baseline")
        self.head = git("rev-parse", "HEAD")
        remote = self.root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            check=True, capture_output=True, timeout=30)
        git("remote", "add", "origin", str(remote))
        git("push", "origin", "main")
        self.chat = "https://chatgpt.com/c/01234567-89ab-4cde-8012-0123456789ab"
        self.args = {
            "source_repo": self.repo,
            "image": "python@sha256:" + "a" * 64,
            "goal": "Fix the synthetic test implementation",
            "files": ["app.py"],
            "test_suite": "python",
            "cycles": 3,
        }

    def enroll(self, **overrides):
        with patch(
            "do_again.service.isolated_enrollment._check_image"
        ) as check, patch(
            "do_again.service.isolated_enrollment.runtime.project_record",
            return_value={"chat_url": self.chat}
        ):
            result = enroll_isolated_project(**dict(self.args, **overrides))
        check.assert_called_once()
        return result

    def test_disposable_git_branch_enrollment_is_idempotent_and_preserves_main(self):
        result = self.enroll()
        self.assertEqual(result["state"], "configured")
        self.assertTrue(result["ready"])
        self.assertTrue(result["branch"].startswith("do-again-isolated-"))
        layout, config = enrolled_config(self.repo)
        self.assertEqual(config["original_head"], self.head)
        self.assertEqual(config["branch"], result["branch"])
        self.assertEqual(config["image"], self.args["image"])
        self.assertEqual(config["cycles"], 3)
        self.assertEqual(config["files"], ["app.py"])
        self.assertEqual(config["automatic_merge"], False)
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(
            (self.repo / "app.py").read_text(), "def value():\n    return 1\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(
            self.git("ls-remote", "--heads", "origin", result["branch"]).split()[0],
            self.head)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(layout["control_worktree"]), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True
            ).stdout.strip(), self.head)
        self.assertTrue(layout["state_dir"].is_dir())
        again = self.enroll()
        self.assertEqual(again["state"], "already_configured")
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_dirty_source_fails_before_creating_any_remote_branch(self):
        (self.repo / "app.py").write_text("uncommitted\n")
        with self.assertRaisesRegex(OperatorError, "clean"):
            self.enroll()
        self.assertFalse((self.home / "isolated-projects").exists())

    def test_absent_project_chat_blocks_before_mutating_repository(self):
        with patch(
            "do_again.service.isolated_enrollment._check_image"
        ), patch(
            "do_again.service.isolated_enrollment.runtime.project_record",
            return_value={}
        ):
            with self.assertRaisesRegex(OperatorError, "bound ChatGPT"):
                enroll_isolated_project(**self.args)
        self.assertFalse((self.home / "isolated-projects").exists())
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_reenrollment_with_changed_scope_fails_closed(self):
        self.enroll()
        with self.assertRaisesRegex(OperatorError, "differs"):
            self.enroll(goal="Different source-wide goal")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_modified_enrollment_configuration_cannot_expand_scope(self):
        self.enroll()
        layout, config = enrolled_config(self.repo)
        config_path = layout["config_path"]
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        stored["goal"] = "Unapproved unrelated change"
        config_path.write_text(json.dumps(stored))
        with self.assertRaisesRegex(OperatorError, "incomplete"):
            enrolled_config(self.repo)

    def test_enrolled_runtime_requires_exact_original_conversation(self):
        self.enroll()
        with patch(
            "do_again.service.isolated_enrollment.runtime.project_record",
            return_value={"chat_url": "https://chatgpt.com/c/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}
        ), patch(
            "do_again.service.isolated_enrollment.run_isolated_auto"
        ) as execute:
            with self.assertRaisesRegex(OperatorError, "conversation changed"):
                run_enrolled_isolated(self.repo)
        execute.assert_not_called()

    def test_enrolled_runtime_dispatches_bounded_private_cycle_without_legacy(self):
        self.enroll()
        with patch(
            "do_again.service.isolated_enrollment.runtime.project_record",
            return_value={"chat_url": self.chat}
        ), patch(
            "do_again.service.isolated_enrollment.run_isolated_auto",
            return_value={"state": "completed_bounded_cycles", "completed_cycles": 3}
        ) as execute:
            result = run_enrolled_isolated(self.repo)
        self.assertEqual(result["completed_cycles"], 3)
        parameters = execute.call_args.kwargs
        self.assertEqual(parameters["source_repo"], self.repo.resolve())
        self.assertEqual(parameters["cycles"], 3)
        self.assertEqual(parameters["goal"], self.args["goal"])
        self.assertEqual(parameters["files"], ["app.py"])
        self.assertEqual(
            parameters["test_command"],
            ["python", "-m", "unittest", "discover", "-s", "tests"])
        self.assertTrue(str(parameters["state_dir"]).startswith(str(self.home)))

    def test_enrollment_rejects_invalid_budget_before_mutation(self):
        with self.assertRaisesRegex(OperatorError, "cycle budget"):
            self.enroll(cycles=0)
        self.assertFalse((self.home / "isolated-projects").exists())


if __name__ == "__main__":
    unittest.main()
