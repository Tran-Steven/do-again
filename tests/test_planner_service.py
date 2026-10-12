"""Operator browser planner service performs no work without explicit setup."""
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.browser import planner_service
from do_again.supervisor.macos_execution import ExecutionBlocked


class OperatorPlannerServiceTests(unittest.TestCase):
    def test_service_rejects_unsupported_platform_before_browser_access(self):
        with patch('do_again.browser.planner_service.sys.platform', 'linux'), \
             patch('do_again.browser.planner_service.runtime.load_config') as config:
            with self.assertRaisesRegex(ExecutionBlocked, 'macOS operator'):
                planner_service.serve_forever(repo=Path('/tmp/empty'),
                                              grant_path=Path('/tmp/absent.json'))
            config.assert_not_called()

    def test_profile_fingerprint_is_bound_to_directory_identity(self):
        info = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_uid=501,
            st_dev=200, st_ino=300)
        with patch('do_again.browser.planner_service.runtime.load_config',
                   return_value={'profile_dir':'/tmp/isolated-profile'}), \
             patch('pathlib.Path.stat', return_value=info):
            first = planner_service.profile_fingerprint()
            second = planner_service.profile_fingerprint()
        self.assertEqual(first, second)
        self.assertEqual(len(first),64)

    def test_non_private_operator_directory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o755)
            with self.assertRaisesRegex(ExecutionBlocked, 'not private'):
                planner_service.private_operator_directory(directory)

    def test_cli_does_not_start_without_explicit_execution(self):
        with patch('do_again.browser.planner_service.serve_forever') as service:
            planner_service.main(['--repo','/tmp/isolated-repo',
                                  '--grant','/tmp/isolated-grant.json'])
        service.assert_called_once()


if __name__ == '__main__':
    unittest.main()
