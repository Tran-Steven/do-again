from __future__ import annotations

import unittest
from unittest.mock import patch

from do_again.platforms.detect import detect_platform


class PlatformDetectionTests(unittest.TestCase):
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
        self.assertEqual(value.service_manager, "windows-service")
        self.assertTrue(value.supported)
