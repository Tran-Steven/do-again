from __future__ import annotations

import unittest
import os
from unittest.mock import patch

from do_again.platforms.detect import detect_platform
from do_again.platforms.process import pid_alive


class PlatformDetectionTests(unittest.TestCase):
    def test_live_process_probe_preserves_current_process(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_invalid_process_probe_is_false(self):
        for pid in [None, 0, -1, 999999999]:
            with self.subTest(pid=pid):
                self.assertFalse(pid_alive(pid))

    def test_macos(self):
        with patch("platform.system", return_value="Darwin"):
            value = detect_platform()
        self.assertEqual(value.name, "macos")
        self.assertEqual(value.service_manager, "launchd")
        self.assertTrue(value.supported)

    def test_linux(self):
        with patch("platform.system", return_value="Linux"):
            value = detect_platform()
        self.assertEqual(value.name, "linux")
        self.assertEqual(value.service_manager, "systemd")
        self.assertTrue(value.supported)

    def test_windows(self):
        with patch("platform.system", return_value="Windows"):
            value = detect_platform()
        self.assertEqual(value.name, "windows")
        self.assertEqual(value.service_manager, "task-scheduler")
        self.assertTrue(value.supported)
