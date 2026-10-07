from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from do_again.cli import list_projects, main, setup_project


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
                rc = setup_project(str(repo), install_background=False, browser=False)
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
                    rc = setup_project(str(repo), install_background=True, browser=False)
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
                rc = setup_project(str(repo), install_background=False, browser=False)
            self.assertEqual(rc, 1)
            self.assertIn("remote", error.getvalue().lower())

    def test_setup_does_not_report_success_when_installed_service_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            error = io.StringIO()
            output = io.StringIO()
            with patch("do_again.cli.install_service", return_value={"installed": True, "running": False}), redirect_stderr(error), redirect_stdout(output):
                rc = setup_project(str(repo), browser=False)
            self.assertEqual(rc, 1)
            self.assertNotIn("SETUP_OK", output.getvalue())
            self.assertIn("did not start", error.getvalue())

    def test_setup_verifies_browser_again_after_service_install(self) -> None:
        from do_again.browser import BrowserError

        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            error = io.StringIO()
            output = io.StringIO()
            with patch("do_again.cli.setup_browser", return_value={"running": True}), patch("do_again.cli.ensure_project_chat", return_value={"chat_url": "https://chatgpt.com/c/project"}), patch("do_again.cli.install_service", return_value={"installed": True, "running": True}), patch("do_again.cli.ensure_browser_running", side_effect=BrowserError("browser failed after service restart")), redirect_stderr(error), redirect_stdout(output):
                rc = setup_project(str(repo), browser=True)
            self.assertEqual(rc, 1)
            self.assertNotIn("SETUP_OK", output.getvalue())
            self.assertIn("after service restart", error.getvalue())


    def test_setup_browser_path_is_integrated_without_duplicate_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            browser_state = {
                "running": True,
                "authenticated": True,
                "mode": "headless",
            }
            chat_state = {"chat_url": "https://chatgpt.com/c/test-project"}
            fake_status = {"installed": False, "running": False}
            with (
                patch("do_again.cli.setup_browser", return_value=browser_state) as setup_browser_mock,
                patch("do_again.cli.ensure_project_chat", return_value=chat_state) as chat_mock,
                patch("do_again.cli.service_status", return_value=fake_status),
            ):
                output = io.StringIO()
                with redirect_stdout(output):
                    rc = setup_project(str(repo), install_background=False, browser=True)
            self.assertEqual(rc, 0)
            setup_browser_mock.assert_called_once()
            chat_mock.assert_called_once()
            self.assertIn("browser_mode=headless", output.getvalue())
            self.assertIn("automation_chat=https://chatgpt.com/c/test-project", output.getvalue())
            self.assertIn("browser = true", (repo / "do-again.toml").read_text())

    def test_setup_no_browser_disables_existing_browser_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            fake_status = {"installed": False, "running": False}
            with patch("do_again.cli.service_status", return_value=fake_status):
                rc = setup_project(str(repo), install_background=False, browser=False)
            self.assertEqual(rc, 0)
            self.assertIn("browser = false", (repo / "do-again.toml").read_text())


    def test_list_projects_shows_service_and_shared_browser_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "example-project"
            repo.mkdir()
            runtime = home / "projects" / "abc123" / "runtime.json"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                __import__("json").dumps(
                    {
                        "repo": str(repo),
                        "browser_enabled": True,
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with (
                patch.dict(__import__("os").environ, {"DO_AGAIN_HOME": str(home)}),
                patch(
                    "do_again.cli.service_status",
                    return_value={"installed": True, "running": True},
                ),
                patch(
                    "do_again.cli.project_record",
                    return_value={"chat_url": "https://chatgpt.com/c/project"},
                ),
                patch(
                    "do_again.cli.browser_status",
                    return_value={"running": True, "mode": "headless"},
                ),
                redirect_stdout(output),
            ):
                rc = list_projects()
            self.assertEqual(rc, 0)
            text = output.getvalue()
            self.assertIn("example-project", text)
            self.assertIn("running", text)
            self.assertIn("headless", text)

    def test_primary_help_hides_low_level_legacy_commands(self) -> None:
        output = io.StringIO()
        with patch("sys.argv", ["do-again", "--help"]), redirect_stdout(output):
            with self.assertRaises(SystemExit):
                main()
        text = output.getvalue()
        self.assertIn("setup", text)
        self.assertIn("list", text)
        self.assertNotIn("Install or refresh", text)
        self.assertNotIn("Run the agent in the foreground", text)

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
        text = output.getvalue()
        self.assertIn("setup", text)
        self.assertIn("start", text)
        self.assertIn("browser", text)


if __name__ == "__main__":
    unittest.main()
