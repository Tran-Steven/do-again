"""No-effect tests for bounded launchd stop observation during sealed cutover."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.name == "posix", "macOS installer uses POSIX system identity modules")
class NativeHelperStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "tools/macos_supervisor_install.py"
        spec = importlib.util.spec_from_file_location("shutdown_poll_installer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.installer = module

    def test_launchd_delayed_bootout_is_observed_not_reissued(self):
        returned = [
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=113),
        ]
        with patch.object(self.installer.subprocess, "run", side_effect=returned) as launchctl, \
             patch.object(self.installer.time, "monotonic", return_value=10.0), \
             patch.object(self.installer.time, "sleep") as sleep:
            self.installer.verify_helper_stopped(timeout=12.0)
        self.assertEqual(launchctl.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        for call in launchctl.call_args_list:
            self.assertEqual(
                call.args[0],
                ["/bin/launchctl", "print", "system/" + self.installer.LABEL],
            )
            self.assertTrue(call.kwargs["capture_output"])

    def test_unexpected_launchd_status_refuses_cutover_without_mutation(self):
        with patch.object(self.installer.subprocess, "run",
                          return_value=SimpleNamespace(returncode=1)) as launchctl, \
             patch.object(self.installer.time, "monotonic", return_value=10.0), \
             patch.object(self.installer.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "helper stop is uncertain"):
                self.installer.verify_helper_stopped()
        launchctl.assert_called_once()
        sleep.assert_not_called()

    def test_still_loaded_at_deadline_refuses_cutover(self):
        with patch.object(self.installer.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0)) as launchctl, \
             patch.object(self.installer.time, "monotonic", side_effect=[10.0, 24.0]), \
             patch.object(self.installer.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "helper stop is uncertain"):
                self.installer.verify_helper_stopped(timeout=12.0)
        launchctl.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
