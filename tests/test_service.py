from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.platforms.base import PlatformInfo
from do_again.service.runtime import (
    ServiceError,
    _config_branch,
    _config_policy_source,
    _config_remote,
    _default_policy,
    _launchd_plist,
    _posix_launcher_text,
    _systemd_quote,
    _systemd_unit,
    _windows_launcher_text,
    runtime_layout,
    service_status,
)


class ServiceRuntimeTests(unittest.TestCase):
    def test_control_branch_rejects_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            (repo / "do-again.toml").write_text(
                '[do_again]\ncontrol_branch = "main"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ServiceError, "unsafe control branch"):
                _config_branch(repo)

    def test_custom_remote_is_read_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "do-again.toml").write_text(
                '[do_again]\n'
                'control_branch = "operator-control"\n'
                'remote = "automation"\n',
                encoding="utf-8",
            )
            self.assertEqual(_config_remote(repo), "automation")

    def test_invalid_remote_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "do-again.toml").write_text(
                '[do_again]\n'
                'control_branch = "operator-control"\n'
                'remote = "../bad"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ServiceError, "invalid Git remote"):
                _config_remote(repo)

    def test_custom_policy_source_is_project_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            policy = repo / "policy.json"
            policy.write_text('{"schema_version": 1}\n', encoding="utf-8")
            (repo / "do-again.toml").write_text(
                '[do_again]\n'
                'control_branch = "operator-control"\n'
                'policy = "policy.json"\n',
                encoding="utf-8",
            )
            self.assertEqual(_config_policy_source(repo), policy.resolve())

    def test_custom_policy_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            outside = root / "outside.json"
            outside.write_text('{"schema_version": 1}\n', encoding="utf-8")
            (repo / "do-again.toml").write_text(
                '[do_again]\n'
                'control_branch = "operator-control"\n'
                'policy = "../outside.json"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ServiceError, "inside the repository"):
                _config_policy_source(repo)

    def test_runtime_layout_is_stable_and_project_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(root / "home")}):
                first = runtime_layout(repo)
                second = runtime_layout(repo)
            self.assertEqual(first.key, second.key)
            self.assertEqual(first.label, second.label)
            self.assertTrue(first.label.startswith("io.github.tran-steven.do-again."))
            self.assertEqual(first.root.parent, (root / "home" / "projects").resolve())

    def test_default_policy_is_packaged(self) -> None:
        policy = _default_policy()
        self.assertEqual(policy["schema_version"], 1)
        self.assertIn("scratch_script", policy["allowed_operations"])
        self.assertTrue(policy["agent_launchd_label"].startswith("io.github.tran-steven.do-again"))

        self.assertNotIn("control_plane_launcher", policy["allowed_operations"])
        self.assertNotIn("deploy_cp1_bundle_at_stop", policy["allowed_operations"])
        self.assertNotIn("launchctl_action", policy["allowed_operations"])
        self.assertNotIn("capture_screenshot", policy["allowed_operations"])

    def test_launchd_plist_uses_copied_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(root / "home")}):
                layout = runtime_layout(repo)
            value = _launchd_plist(layout)
            self.assertEqual(value["Label"], layout.label)
            self.assertEqual(value["WorkingDirectory"], str(repo.resolve()))
            self.assertEqual(value["EnvironmentVariables"]["PYTHONPATH"], str(layout.runtime_source))
            self.assertIn("do_again.core.agent", value["ProgramArguments"])
            self.assertTrue(value["KeepAlive"])

    def test_systemd_unit_uses_user_scoped_copied_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo with spaces"
            repo.mkdir()
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(root / "home")}):
                layout = runtime_layout(repo)
            launcher = _posix_launcher_text(layout)
            unit = _systemd_unit(layout)
            self.assertIn("PYTHONPATH=", launcher)
            self.assertIn(str(layout.runtime_source), launcher)
            self.assertIn("do_again.core.agent", launcher)
            self.assertIn("Restart=always", unit)
            self.assertIn(_systemd_quote(str(layout.root / "run-agent.sh")), unit)
            self.assertIn(_systemd_quote(str(repo.resolve())), unit)

    def test_windows_launcher_uses_copied_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(root / "home")}):
                layout = runtime_layout(repo)
            launcher = _windows_launcher_text(layout)
            self.assertIn("PYTHONPATH=", launcher)
            self.assertIn(str(layout.runtime_source), launcher)
            self.assertIn("do_again.core.agent", launcher)
            self.assertIn("PYTHONDONTWRITEBYTECODE", launcher)

    @patch("do_again.service.runtime._linux_pid", return_value=4321)
    @patch("do_again.service.runtime._linux_running", return_value=True)
    @patch("do_again.service.runtime.linux_service_definition_path")
    @patch("do_again.service.runtime.detect_platform")
    @patch("do_again.service.runtime.find_repo")
    def test_linux_status_reports_installed_running(
        self, find_repo_mock, detect, unit_path, running, pid
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            find_repo_mock.return_value = repo.resolve()
            detect.return_value = PlatformInfo(name="linux", service_manager="systemd", supported=True)
            unit = root / "do-again.service"
            unit.write_text("[Service]\n")
            unit_path.return_value = unit
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(root / "home")}):
                value = service_status(repo)
            self.assertTrue(value["installed"])
            self.assertTrue(value["running"])
            self.assertEqual(value["pid"], 4321)

    @patch("do_again.service.runtime._windows_running", return_value=True)
    @patch("do_again.service.runtime._windows_task_exists", return_value=True)
    @patch("do_again.service.runtime.detect_platform")
    @patch("do_again.service.runtime.find_repo")
    def test_windows_status_reports_task_state(
        self, find_repo_mock, detect, exists, running
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            find_repo_mock.return_value = repo.resolve()
            detect.return_value = PlatformInfo(name="windows", service_manager="task-scheduler", supported=True)
            with patch.dict(os.environ, {"DO_AGAIN_HOME": str(root / "home")}):
                value = service_status(repo)
            self.assertTrue(value["installed"])
            self.assertTrue(value["running"])
            self.assertIsNone(value["pid"])

    @patch("do_again.service.runtime.detect_platform")
    def test_background_lifecycle_rejects_unknown_platform(self, detect) -> None:
        detect.return_value = PlatformInfo(name="plan9", service_manager="unknown", supported=False)
        from do_again.service.runtime import _require_supported_background_platform

        with self.assertRaises(ServiceError):
            _require_supported_background_platform()

    def test_run_error_wraps_missing_binary(self) -> None:
        from do_again.service.runtime import _run

        with patch("do_again.service.runtime.subprocess.run", side_effect=FileNotFoundError("missing")):
            proc = _run(["missing"], check=False)
            self.assertEqual(proc.returncode, 127)
            with self.assertRaises(ServiceError):
                _run(["missing"])


if __name__ == "__main__":
    unittest.main()
