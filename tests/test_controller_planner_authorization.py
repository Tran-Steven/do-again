import unittest
from datetime import datetime, timezone, timedelta
from do_again.service.controller_planner import CodexPlanner
from do_again.supervisor.macos_execution import ExecutionBlocked

class HostedAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,12,15,tzinfo=timezone.utc)
        self.grant={'provider':'openai','transport':'codex-cli','model':'gpt-6.1-sol',
            'backend_sha256':'a'*64,'execution_uid':501,'max_calls_per_cycle':1,'max_total_calls':3,
            'max_call_seconds':120,'max_total_seconds':360,'max_prompt_bytes':32768,'max_answer_bytes':65536,
            'worker_network_access':False,'browser_messages':False,'application_submissions':False,
            'issued_at_utc':'2026-10-12T15:00:00Z','expires_at_utc':'2026-10-12T17:00:00Z'}
        self.effects=[]
        self.planner=CodexPlanner(lambda now:self.grant,
            lambda *args:self.effects.append('reservation') or {'call_id':'fixture'},
            lambda *args,**kwargs:self.effects.append('transport') or {'gap':'gap','acceptance':'test','script':'pass'},
            lambda answer,evidence,now:answer)
    def select(self):return self.planner.select(goal={},evidence={},state={'acknowledged':[],'requests':[]},now=self.now)
    def test_qualified_fixture_adapter_only_after_authorization(self):
        self.assertEqual(self.select()['script'],'pass')
        self.assertEqual(self.effects,['reservation','transport'])
    def test_missing_inconsistent_future_expired_grants_have_no_effect(self):
        original=dict(self.grant)
        for key,value in [('model','other'),('backend_sha256',None),('execution_uid',0),
                          ('worker_network_access',True),('max_total_calls',4),
                          ('issued_at_utc','2026-10-12T16:00:00Z'),
                          ('expires_at_utc','2026-10-12T15:00:00Z'),('expires_at_utc','invalid')]:
            self.grant=dict(original,**{key:value})
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):self.select()
            self.assertEqual(self.effects,[])
    def test_excessive_data_exposure_rejected_before_reservation(self):
        with self.assertRaises(ExecutionBlocked):
            self.planner.select(goal={'text':'x'*32768},evidence={},state={'acknowledged':[],'requests':[]},now=self.now)
        self.assertEqual(self.effects,[])
