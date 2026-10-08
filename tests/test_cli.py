from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from do_again.cli import (
    list_projects,
    main,
    request_history,
    setup_project,
    show_logs,
    trace_request,
    verify_project,
)
from do_again.service.runtime import runtime_layout


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
            self.assertIn("SETUP_CONFIGURED", text)
            self.assertIn("verification=not_run", text)
            self.assertNotIn("SETUP_OK", text)
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

    def test_setup_falls_back_when_headless_auth_works_but_messages_fail(self) -> None:
        from do_again.browser import BrowserError

        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            with patch("do_again.cli.setup_browser", return_value={"running": True, "mode": "headless"}), patch("do_again.cli.ensure_project_chat", side_effect=[BrowserError("headless send failed"), {"chat_url": "https://chatgpt.com/c/project"}]), patch("do_again.cli.use_background_fallback", return_value={"running": True, "mode": "background"}) as fallback, patch("do_again.cli.service_status", return_value={"installed": False}), redirect_stdout(io.StringIO()):
                self.assertEqual(setup_project(str(repo), browser=True, install_background=False), 0)
            fallback.assert_called_once()


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

    def test_verify_reports_setup_ok_only_after_matching_success_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self.make_repo(root)
            (repo / "do-again.toml").write_text(
                '[do_again]\n'
                'control_branch = "operator-control"\n'
                'remote = "origin"\n'
                'browser = true\n',
                encoding="utf-8",
            )
            control = root / "control"
            request_dir = control / "automation/do_again/requests"
            receipt_dir = control / "automation/do_again/receipts"
            request_dir.mkdir(parents=True)
            receipt_dir.mkdir(parents=True)

            class Layout:
                browser_enabled = True
                branch = "operator-control"
                remote = "origin"
                control_worktree = control

            captured = {}

            def fake_send(target, prompt, **kwargs):
                match = __import__("re").search(r'"request_id": "(verify-[a-f0-9]+)"', prompt)
                self.assertIsNotNone(match)
                request_id = match.group(1)
                captured["request_id"] = request_id
                (request_dir / f"{request_id}.json").write_text("{}")
                (receipt_dir / f"{request_id}.json").write_text(
                    __import__("json").dumps({"request_id": request_id, "state": "succeeded"})
                )
                return {"response": "submitted", "chat_url": "https://chatgpt.com/c/project"}

            fake_proc = subprocess.CompletedProcess([], 0, "", "")
            output = io.StringIO()
            with (
                patch("do_again.cli.runtime_layout", return_value=Layout()),
                patch("do_again.cli.service_status", return_value={"running": True}),
                patch("do_again.cli.project_record", return_value={"chat_url": "https://chatgpt.com/c/project"}),
                patch("do_again.cli.ensure_browser_running", return_value={"port": 9223}),
                patch("do_again.cli.cdp.create_target", return_value=object()),
                patch("do_again.cli.send_message", side_effect=fake_send),
                patch("do_again.cli.subprocess.run", return_value=fake_proc),
                redirect_stdout(output),
            ):
                rc = verify_project(str(repo), timeout_seconds=1.0)
            self.assertEqual(rc, 0)
            self.assertIn("SETUP_OK", output.getvalue())
            self.assertIn("verification=end_to_end", output.getvalue())
            self.assertIn(captured["request_id"], output.getvalue())

    def test_verify_fails_actionably_when_browser_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))

            class Layout:
                browser_enabled = False

            error = io.StringIO()
            with patch("do_again.cli.runtime_layout", return_value=Layout()), redirect_stderr(error):
                rc = verify_project(str(repo), timeout_seconds=0.01)
            self.assertEqual(rc, 1)
            self.assertIn("requires browser automation", error.getvalue())
            self.assertNotIn("SETUP_OK", error.getvalue())



    def test_history_reads_request_and_receipt_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            layout = runtime_layout(repo)
            requests = layout.control_worktree / "automation/do_again/requests"
            receipts = layout.control_worktree / "automation/do_again/receipts"
            requests.mkdir(parents=True, exist_ok=True)
            receipts.mkdir(parents=True, exist_ok=True)
            (requests / "req-a.json").write_text(
                json.dumps({
                    "request_id": "req-a",
                    "operation": "status",
                    "issued_at_utc": "2026-10-08T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            (receipts / "req-a.json").write_text(
                json.dumps({
                    "request_id": "req-a",
                    "operation": "status",
                    "state": "succeeded",
                    "finished_at_utc": "2026-10-08T00:01:00+00:00",
                }),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                rc = request_history(str(repo), limit=20)
            self.assertEqual(rc, 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["history"][0]["request_id"], "req-a")
            self.assertEqual(value["history"][0]["state"], "succeeded")

    def test_trace_combines_durable_request_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            layout = runtime_layout(repo)
            base = layout.control_worktree / "automation/do_again"
            for name in ("requests", "claims", "receipts"):
                (base / name).mkdir(parents=True, exist_ok=True)
            layout.state_dir.joinpath("ledger").mkdir(parents=True, exist_ok=True)
            (base / "requests/req-x.json").write_text(
                json.dumps({"request_id": "req-x", "operation": "status"}),
                encoding="utf-8",
            )
            (base / "claims/req-x.json").write_text(
                json.dumps({"request_id": "req-x", "agent_instance_id": "agent"}),
                encoding="utf-8",
            )
            (layout.state_dir / "ledger/req-x.json").write_text(
                json.dumps({"state": "started"}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                rc = trace_request("req-x", str(repo))
            self.assertEqual(rc, 0)
            value = json.loads(output.getvalue())
            self.assertEqual(value["request"]["operation"], "status")
            self.assertEqual(value["ledger"]["state"], "started")

    def test_logs_tail_reads_runtime_logs_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            layout = runtime_layout(repo)
            layout.stdout_log.parent.mkdir(parents=True, exist_ok=True)
            layout.stdout_log.write_text("one\ntwo\nthree\n", encoding="utf-8")
            layout.stderr_log.write_text("problem\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                rc = show_logs(str(repo), lines=2, stream="stdout")
            self.assertEqual(rc, 0)
            rendered = output.getvalue()
            self.assertNotIn("\none\n", rendered)
            self.assertIn("two", rendered)
            self.assertIn("three", rendered)

    def test_chats_cleanup_is_dry_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.make_repo(Path(tmp))
            payload = {
                "dry_run": True,
                "eligible_chat_ids": ["old"],
                "queued_chat_ids": [],
                "inventory": {"candidates": []},
                "queue": {"items": []},
            }
            output = io.StringIO()
            with patch(
                "do_again.cli.queue_verified_archives", return_value=payload
            ) as cleanup, patch(
                "sys.argv", ["do-again", "chats", "cleanup", str(repo)]
            ), redirect_stdout(output):
                rc = main()
            self.assertEqual(rc, 0)
            cleanup.assert_called_once()
            called_repo = cleanup.call_args.args[0]
            self.assertEqual(called_repo.resolve(), repo.resolve())
            self.assertEqual(cleanup.call_args.kwargs, {"candidate_ids": [], "apply": False})
            self.assertIn('"dry_run": true', output.getvalue())



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
