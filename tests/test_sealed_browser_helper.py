"""Sealed browser helper refuses absent or mismatched native qualification."""
import json
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from do_again.service.browser_planner import browser_grant_digest
from do_again.supervisor.controller_browser_helper import SealedBrowserPlannerHelper
from do_again.supervisor.macos_execution import ExecutionBlocked


class SealedBrowserHelperTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(timezone.utc)
        self.grant = {
            'kind':'browser-planning-v1',
            'transport':'chatgpt-browser-cdp',
            'repository':'Tran-Steven/jobpipe', 'branch':'operator-control',
            'source_sha':'a'*40, 'goal_sha256':'b'*64, 'epoch':13,
            'issued_at_utc':now.isoformat(),
            'expires_at_utc':(now+timedelta(minutes=30)).isoformat(),
            'conversation_id':'01234567-89ab-4cde-8012-0123456789ab',
            'profile_sha256':'c'*64, 'max_calls':3,
            'max_prompt_bytes':32768, 'max_answer_bytes':65536,
            'application_submissions':False, 'worker_network_access':False,
            'production_deployment':False, 'automatic_merge':False,
        }
        self.config = {
            'source_sha':'a'*40,
            'operator_home':'/Users/isolated-operator',
            'controller_scope':{'goal_sha256':'b'*64,'epoch':13},
            'browser_planning_scope':self.grant,
        }
        self.proof = {
            'state':'qualified_operator_bridge', 'source_sha':'a'*40,
            'goal_sha256':'b'*64, 'epoch':13,
            'grant_sha256':browser_grant_digest(self.grant),
            'operator_uid':501,'peer_uid':501,'browser_calls':0,
        }

    def make_helper(self, proof=None):
        proof = self.proof if proof is None else proof
        with patch('do_again.supervisor.controller_browser_helper.private_root_file'), \
             patch('pathlib.Path.read_text', return_value=json.dumps(proof)):
            return SealedBrowserPlannerHelper(
                self.config, Path('/fake-state'), lambda:nullcontext())

    def test_valid_offline_proof_creates_no_socket(self):
        with patch('do_again.service.browser_bridge.socket.socket') as network:
            helper = self.make_helper()
        network.assert_not_called()
        self.assertEqual(helper.socket_path,
                         Path('/Users/isolated-operator/.do_again/browser/planner.sock'))
        self.assertEqual(helper.authorize(datetime.now(timezone.utc)),self.grant)

    def test_native_proof_required_before_constructing_backend(self):
        with patch('do_again.supervisor.controller_browser_helper.private_root_file',
                   side_effect=ExecutionBlocked('missing qualification')):
            with self.assertRaisesRegex(ExecutionBlocked, 'missing qualification'):
                SealedBrowserPlannerHelper(self.config,Path('/fake-state'),
                                           lambda:nullcontext())

    def test_cross_source_or_prior_browser_calls_rejected(self):
        for bad in (dict(self.proof, source_sha='0'*40),
                    dict(self.proof, browser_calls=1),
                    dict(self.proof, grant_sha256='0'*64),
                    dict(self.proof, peer_uid=0)):
            with self.subTest(bad=bad):
                with self.assertRaises(ExecutionBlocked):
                    self.make_helper(bad)


if __name__ == '__main__':
    unittest.main()
