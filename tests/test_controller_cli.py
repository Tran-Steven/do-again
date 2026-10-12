import contextlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from do_again.cli import main
from do_again.supervisor.macos_execution import ExecutionBlocked


class ControllerCliTests(unittest.TestCase):
    def test_commands_send_fixed_packet_only(self):
        for action in ('start', 'stop', 'status'):
            with (self.subTest(action=action), patch('do_again.cli.find_repo', return_value=Path('/fixture')),
                 patch('do_again.supervisor.macos_client.broker_request', return_value={'state': 'stopped'}) as rpc,
                 patch('sys.argv', ['do-again', 'controller', action, '/fixture']),
                 contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(main(), 0)
                rpc.assert_called_once_with(Path('/fixture'), {'operation': 'controller_'+action})

    def test_absent_authority_fails_closed(self):
        with (patch('do_again.cli.find_repo', return_value=Path('/fixture')),
             patch('do_again.supervisor.macos_client.broker_request', side_effect=ExecutionBlocked('controller absent')),
             patch('sys.argv', ['do-again', 'controller', 'start', '/fixture']),
             contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(main(), 1)
