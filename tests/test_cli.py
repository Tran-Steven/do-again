from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from do_again.cli import main, setup_project


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        text=True,
        capture_output=True,
    )


class CliOnboardingTests(unittest.TestCase):
    def make_repo(self, root: Path) -> Path:
        bare = root / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            check=True,
            text=True,
            capture_output=True,
        )
        repo = root / "repo with spaces"
        repo.mkdir()
        git(repo, "init", "-b", "main")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
        (repo / "README.md").write_text("# test\n", encoding="utf-8")
        git(repo, "add", "README.md")
        git(repo, "commit", "-m", "initial")
        git(repo, "remote", "add", "origin", str(bare))
        git(repo, "push", "-u", "origin", "main")
        return repo

    def test_setup_no_service_is_one_command_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            fake_status = {"installed": False, "running": False}
            output = io.StringIO()
            with patch("do_again.cli.service_status", return_value=fake_status), redirect_stdout(output):
                rc = setup_project(str(repo), install_background=False)
            self.assertEqual(rc, 0)
            self.assertTrue((repo / "do-again.toml").is_file())
            self.assertTrue((repo / "do-again-policy.json").is_file())
            text = output.getvalue()
            self.assertIn("SETUP_OK", text)
            self.assertIn("next=do-again run", text)

    def test_setup_background_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            with patch(
                "do_again.cli.install_service",
                return_value={"installed": True, "running": True},
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    rc = setup_project(str(repo), install_background=True)
            self.assertEqual(rc, 0)
            text = output.getvalue()
            self.assertIn("background_installed=True", text)
            self.assertIn("background_running=True", text)
            self.assertIn("next=do-again status", text)

    def test_setup_fails_cleanly_without_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.invalid")
            (repo / "README.md").write_text("# test\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-m", "initial")
            error = io.StringIO()
            with redirect_stderr(error):
                rc = setup_project(str(repo), install_background=False)
            self.assertEqual(rc, 1)
            self.assertIn("remote", error.getvalue().lower())

    def test_bare_command_shows_start_here_hint(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["do-again"]), redirect_stdout(output):
            rc = main()
        self.assertEqual(rc, 0)
        self.assertIn("Start here: do-again setup", output.getvalue())

    def test_help_lists_setup(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["do-again", "--help"]), redirect_stdout(output):
            with self.assertRaises(SystemExit) as exit_info:
                main()
        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn("setup", output.getvalue())


if __name__ == "__main__":
    unittest.main()
