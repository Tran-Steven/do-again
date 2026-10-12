import unittest
from datetime import datetime, timedelta, timezone
from do_again.service.browser_planner import validate_browser_planner_grant, BrowserResponseTransport, browser_grant_digest
from do_again.supervisor.macos_execution import ExecutionBlocked

class BrowserPlannerTests(unittest.TestCase):
    def test_grant_requires_exact_identity_and_no_privileged_effects(self):
        now = datetime.now(timezone.utc)
        grant = dict(
            kind='browser-planning-v1', transport='chatgpt-browser-cdp',
            repository='Tran-Steven/jobpipe', branch='operator-control',
            source_sha='a'*40, goal_sha256='b'*64, epoch=13,
            issued_at_utc=now.isoformat(),
            expires_at_utc=(now+timedelta(minutes=30)).isoformat(),
            conversation_id='01234567-89ab-4cde-8012-0123456789ab',
            profile_sha256='c'*64, max_calls=3,
            max_prompt_bytes=32768, max_answer_bytes=65536,
            application_submissions=False, worker_network_access=False,
            production_deployment=False, automatic_merge=False)
        def check(value):
            return validate_browser_planner_grant(
                value, source_sha='a'*40, goal_sha256='b'*64, epoch=13, now=now)
        self.assertEqual(check(grant),grant)
        for wrong in (
            dict(grant, source_sha='d'*40),
            dict(grant, repository='Tran-Steven/Sonary'),
            dict(grant, transport='codex-cli'),
            dict(grant, epoch=14),
            dict(grant, application_submissions=True),
            dict(grant, expires_at_utc=(now-timedelta(seconds=1)).isoformat()),
            dict(grant, max_calls=4),
            dict(grant, unknown='unexpected')):
            with self.subTest(wrong=wrong):
                with self.assertRaises(ExecutionBlocked):
                    check(wrong)

    def test_reply_bound_to_reserved_call_and_conversation(self):
        import json
        call = {'call_id': 'planner-' + 'a'*20 + '-1'}
        grant = {'conversation_id': '01234567-89ab-4cde-8012-0123456789ab',
                 'max_prompt_bytes': 32768, 'max_answer_bytes': 65536,
                 'profile_sha256': 'd'*64}
        result = {'gap': 'g', 'acceptance': 'a', 'script': 'print(1)'}
        def sender(message, *, conversation_id, call_id, profile_sha256, grant_sha256):
            self.assertEqual(grant_sha256, browser_grant_digest(grant))
            self.assertEqual(profile_sha256, grant['profile_sha256'])
            self.assertEqual(conversation_id, grant['conversation_id'])
            self.assertEqual(call_id, call['call_id'])
            return {'conversation_id': conversation_id,
                    'response': json.dumps({'call_id': call_id, 'result': result})}
        transport = BrowserResponseTransport(sender)
        self.assertEqual(transport('{}', {}, call=call, grant=grant), result)
        with self.assertRaises(ExecutionBlocked):
            transport('{}', {}, call={'call_id': 'unreserved'}, grant=grant)
        bad = BrowserResponseTransport(
            lambda *a, **kw: {'conversation_id': 'other', 'response': '{}'})
        with self.assertRaises(ExecutionBlocked):
            bad('{}', {}, call=call, grant=grant)
        wrong_call = BrowserResponseTransport(
            lambda *a, **kw: {'conversation_id': grant['conversation_id'],
                              'response': json.dumps({'call_id': 'other', 'result': result})})
        with self.assertRaises(ExecutionBlocked):
            wrong_call('{}', {}, call=call, grant=grant)

if __name__=='__main__':
    unittest.main()
