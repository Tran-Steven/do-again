"""Sealed controller cannot borrow one planner's grants for another."""
import unittest
from datetime import datetime, timedelta, timezone

from do_again.service.offline_continuation import digest
from do_again.supervisor.controller_configuration import validate_controller_configuration
from do_again.supervisor.macos_execution import ExecutionBlocked


class ProviderScopeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        start, end = self.now.isoformat(), (self.now+timedelta(minutes=30)).isoformat()
        goal = {'goal': 'approved isolated development qualification'}
        goal_hash = digest(goal)
        scope = {
            'kind':'jobpipe-offline-controller-v1',
            'repository':'Tran-Steven/jobpipe', 'branch':'operator-control',
            'goal_sha256':goal_hash, 'source_sha':'a'*40, 'epoch':13,
            'issued_at_utc':start, 'expires_at_utc':end,
            'max_requests':3, 'max_cycles':3, 'max_outstanding':1,
            'max_retries':0, 'planner':'chatgpt-browser-cdp',
            'integration':'proposal-only', 'network_access':False,
            'application_submissions':False, 'browser_messages':False,
        }
        browser = {
            'kind':'browser-planning-v1', 'transport':'chatgpt-browser-cdp',
            'repository':scope['repository'], 'branch':scope['branch'],
            'source_sha':scope['source_sha'], 'goal_sha256':goal_hash,
            'epoch':13, 'issued_at_utc':start, 'expires_at_utc':end,
            'conversation_id':'01234567-89ab-4cde-8012-0123456789ab',
            'profile_sha256':'b'*64, 'max_calls':3, 'max_prompt_bytes':32768,
            'max_answer_bytes':65536,
            'application_submissions':False, 'worker_network_access':False,
            'production_deployment':False, 'automatic_merge':False,
        }
        self.config = {
            'source_sha':'a'*40,
            'development_scope':{'kind':'jobpipe-offline-v1'},
            'production_ready':False,
            'controller_scope':scope,
            'controller_goal':goal,
            'controller_goal_sha256':goal_hash,
            'controller_profile': {
                'kind':'native-development-v1',
                'allowed_paths':['core/leases.py'],
                'test_cases':['tests/test_first.py', 'tests/test_second.py',
                              'tests/test_third.py'],
                'test_files':{'tests/test_baseline.py':'c'*40},
                'test_runner':'unittest','public_evidence':True,'offline_tasks':[],
            },
            'browser_planning_scope':browser,
        }

    def test_valid_browser_scope_is_recognized_but_not_activated(self):
        self.assertEqual(validate_controller_configuration(self.config, now=self.now),
                         self.config['controller_scope'])

    def test_install_grant_applies_separate_browser_authority(self):
        from do_again.supervisor.controller_installation import apply_controller_grant
        candidate = {'source_sha':'a'*40, 'production_ready':False,
                     'development_scope':{'kind':'jobpipe-offline-v1'}}
        package = {'scope':self.config['controller_scope'],
                   'goal':self.config['controller_goal'],
                   'profile':self.config['controller_profile']}
        apply_controller_grant(candidate, package,
                               browser=self.config['browser_planning_scope'],
                               now=self.now)
        self.assertEqual(candidate['browser_planning_scope']['max_calls'], 3)
        self.assertNotIn('hosted_planning_scope', candidate)
        with self.assertRaisesRegex(ExecutionBlocked, 'cannot combine'):
            apply_controller_grant(dict(candidate), package, hosted={'provider':'openai'},
                                   browser=self.config['browser_planning_scope'], now=self.now)

    def test_missing_browser_grant_is_not_codex_fallback(self):
        self.config.pop('browser_planning_scope')
        with self.assertRaises(ExecutionBlocked):
            validate_controller_configuration(self.config, now=self.now)

    def test_provider_authority_cannot_be_combined(self):
        self.config['hosted_planning_scope'] = {'provider':'openai'}
        with self.assertRaisesRegex(ExecutionBlocked, 'cannot inherit'):
            validate_controller_configuration(self.config, now=self.now)

    def test_invalid_browser_epoch_fails_closed(self):
        self.config['browser_planning_scope']['epoch'] = 14
        with self.assertRaises(ExecutionBlocked):
            validate_controller_configuration(self.config, now=self.now)


if __name__ == '__main__':
    unittest.main()
