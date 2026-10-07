from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.platforms.base import PlatformInfo
from do_again.service.runtime import (
    ServiceError,
    _default_policy,
    _launchd_plist,
    runtime_layout,
)


class ServiceRuntimeTests(unittest.TestCase):
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

    @patch("do_again.service.runtime.detect_platform")
    def test_background_lifecycle_rejects_non_macos(self, detect) -> None:
        detect.return_value = PlatformInfo(name="linux", service_manager="systemd", supported=True)
        from do_again.service.runtime import install_service

        with self.assertRaises(ServiceError):
            install_service(".")


if __name__ == "__main__":
    unittest.main()
