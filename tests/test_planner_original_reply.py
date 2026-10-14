"""Replay safety under original-answer recovery and UI message virtualization."""
import os
import unittest
if os.name != 'posix':
    raise unittest.SkipTest('native POSIX controller and recovery fixtures')

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from do_again.browser.planner_observation import original_reply
from do_again.browser.planner_bridge_server import (OperatorBrowserBridge, DurableDeliveryReservation,
    DurableReplyJournal)
from do_again.service.browser_planner import BrowserResponseTransport, browser_grant_digest
from do_again.service.controller_planner import PlanningCallJournal
from do_again.supervisor.controller_backend import planning_goal
from do_again.supervisor.macos_execution import ExecutionBlocked
from test_operator_browser_bridge import FakeConnection,operator_record_fixture


class OriginalReplyTests(unittest.TestCase):
    cid='01234567-89ab-4cde-8012-0123456789ab'
    def test_reply_association_uses_exact_prompt_and_immediate_next_turn(self):
        target=Mock(); message='{"call_id":"original","payload":"exact spaces"}'
        snap={'url':'https://chatgpt.com/c/'+self.cid,'busy':False,'messages':[
            {'role':'assistant','text':'old answer'},
            {'role':'user','text':'You said:\n'+message+'\n…\nShow more','expandable':True},
            {'role':'assistant','text':'ChatGPT said:\n{"result":"original"}'},
            {'role':'user','text':'another question'},
            {'role':'assistant','text':'a later answer'}]}
        with patch('do_again.browser.runtime.cdp.evaluate', return_value=snap):
            self.assertEqual(original_reply(target,message,conversation_id=self.cid)['response'],
                             '{"result":"original"}')
            self.assertIsNone(original_reply(target,message+' ',conversation_id=self.cid))
            snap['messages'].insert(2,{'role':'user','text':'intervening question'})
            self.assertIsNone(original_reply(target,message,conversation_id=self.cid))

    def test_busy_or_duplicate_prompt_never_recovers_an_answer(self):
        msg='exact'; snap={'url':'https://chatgpt.com/c/'+self.cid,'busy':True,
            'messages':[{'role':'user','text':msg},{'role':'assistant','text':'answer'}]}
        with patch('do_again.browser.runtime.cdp.evaluate',return_value=snap):
            self.assertIsNone(original_reply(Mock(),msg,conversation_id=self.cid))
            snap['busy']=False;snap['messages']*=2
            self.assertIsNone(original_reply(Mock(),msg,conversation_id=self.cid))

    def test_expanded_prompt_removes_only_display_control(self):
        msg='a'*300
        snap={'url':'https://chatgpt.com/c/'+self.cid,'busy':False,
              'messages':[{'role':'user','text':'You said:\n'+msg+'\nShow less','expanded':True},
                          {'role':'assistant','text':'answer'}]}
        with patch('do_again.browser.runtime.cdp.evaluate',return_value=snap):
            self.assertEqual(original_reply(Mock(),msg,conversation_id=self.cid)['response'],'answer')
            self.assertIsNone(original_reply(Mock(),msg+' ',conversation_id=self.cid))

    def test_collapsed_prefix_expands_but_requires_full_digest_afterward(self):
        msg='a'*300+' EXACT ORIGINAL'
        before={'url':'https://chatgpt.com/c/'+self.cid,'busy':False,
                'messages':[{'role':'user','text':'You said:\n'+'a'*300+'\n…\nShow more','expandable':True},
                            {'role':'assistant','text':'answer'}]}
        after={'url':before['url'],'busy':False,
               'messages':[{'role':'user','text':msg+'\nShow less','expanded':True},
                           {'role':'assistant','text':'answer'}]}
        with patch('do_again.browser.runtime.cdp.evaluate',side_effect=[before,True,after]) as evaluate:
            self.assertEqual(original_reply(Mock(),msg,conversation_id=self.cid)['response'],'answer')
            script=evaluate.call_args_list[1].args[1]
            self.assertIn('location.origin',script)
            self.assertIn("==='Show more'",script)
            self.assertNotIn('Send',script)
        after['messages'][0]['text']=msg+' altered\nShow less'
        with patch('do_again.browser.runtime.cdp.evaluate',side_effect=[before,True,after]):
            self.assertIsNone(original_reply(Mock(),msg,conversation_id=self.cid))

    def test_control_words_without_actual_controls_are_prompt_content(self):
        msg='a'*300
        snap={'url':'https://chatgpt.com/c/'+self.cid,'busy':False,
              'messages':[{'role':'user','text':msg+'\nShow less'},
                          {'role':'assistant','text':'answer'}]}
        with patch('do_again.browser.runtime.cdp.evaluate',return_value=snap):
            self.assertIsNone(original_reply(Mock(),msg,conversation_id=self.cid))
            self.assertEqual(original_reply(Mock(),msg+'\nShow less',conversation_id=self.cid)['response'],'answer')

    def test_duplicate_or_unrelated_collapsed_prompt_is_never_expanded(self):
        msg='a'*300+' exact'
        node={'role':'user','text':'a'*300+'\n…\nShow more','expandable':True}
        snap={'url':'https://chatgpt.com/c/'+self.cid,'busy':False,'messages':[node,node]}
        with patch('do_again.browser.runtime.cdp.evaluate',return_value=snap) as evaluate:
            self.assertIsNone(original_reply(Mock(),msg,conversation_id=self.cid));evaluate.assert_called_once()
        snap['messages']=[dict(node,text='unrelated'*40+'\n…\nShow more')]
        with patch('do_again.browser.runtime.cdp.evaluate',return_value=snap) as evaluate:
            self.assertIsNone(original_reply(Mock(),msg,conversation_id=self.cid));evaluate.assert_called_once()
        snap['url']='https://other.invalid/c/'+self.cid
        with patch('do_again.browser.runtime.cdp.evaluate',return_value=snap) as evaluate:
            with self.assertRaises(ExecutionBlocked):original_reply(Mock(),msg,conversation_id=self.cid)
            evaluate.assert_called_once()

    def test_prompt_schema_is_full_call_bound_envelope(self):
        grant={'conversation_id':self.cid,'profile_sha256':'a'*64,
            'max_prompt_bytes':32768,'max_answer_bytes':65536}
        call={'call_id':'planner-'+'b'*20+'-1'}; captured=[]
        def sender(message,**kwargs):
            captured.append(json.loads(message))
            return {'conversation_id':self.cid,'response':json.dumps({'call_id':call['call_id'],'result':{}})}
        BrowserResponseTransport(sender)('{}',{'type':'object'},call=call,grant=grant)
        schema=captured[0]['response_schema']
        self.assertEqual(schema['required'],['call_id','result'])
        self.assertEqual(schema['properties']['call_id'],{'const':call['call_id']})
        self.assertEqual(schema['properties']['result'],{'type':'object'})

    def test_original_reservation_resumes_observation_only_and_invalid_answer_consumes_budget(self):
        grant={'kind':'browser-planning-persistent-v1','max_calls':2}
        with tempfile.TemporaryDirectory() as tmp:
            journal=PlanningCallJournal(Path(tmp)/'calls.json')
            first=journal.reserve(grant,1,'prompt',{})
            restored=PlanningCallJournal(journal.path).reserve(grant,1,'prompt',{})
            self.assertTrue(restored['observe_only']);self.assertEqual(restored['call_id'],first['call_id'])
            with self.assertRaises(ExecutionBlocked): journal.reserve(grant,1,'different prompt',{})
            journal.answered_invalid(restored,'original reply schema differs')
            second=journal.reserve(grant,1,'prompt',{})
            self.assertNotEqual(first['call_id'],second['call_id'])
            journal.answered_invalid(second,'bad reply again')
            with self.assertRaises(ExecutionBlocked): journal.reserve(grant,1,'prompt',{})

    def test_observed_call_can_never_invoke_sender(self):
        grant={'conversation_id':self.cid,'profile_sha256':'a'*64,'max_prompt_bytes':32768,'max_answer_bytes':65536}
        call={'call_id':'planner-'+'b'*20+'-1','observe_only':True}
        send=Mock(side_effect=AssertionError('replayed Send'))
        observe=Mock(return_value={'conversation_id':self.cid,'response':json.dumps({'call_id':call['call_id'],'result':{}})})
        BrowserResponseTransport(send,observe)('{}',{},call=call,grant=grant)
        send.assert_not_called();observe.assert_called_once()

    @unittest.skipUnless(os.name=='posix','descriptor-based native journal fixture')
    def test_reply_is_durable_before_socket_disconnect_and_cache_never_sends(self):
        with tempfile.TemporaryDirectory() as tmp,operator_record_fixture(), \
             patch('do_again.browser.planner_bridge_server.private_operator_directory',return_value=True), \
             patch('do_again.browser.planner_bridge_server.os.geteuid',return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid',return_value=0):
            root=Path(tmp);delivery=root/'delivery';replies=root/'replies'
            delivery.mkdir();replies.mkdir()
            grant={'conversation_id':self.cid,'profile_sha256':'a'*64,'max_prompt_bytes':32768,'max_answer_bytes':65536}
            call='planner-'+'b'*20+'-1'; req={'call_id':call,'conversation_id':self.cid,
                'profile_sha256':'a'*64,'grant_sha256':browser_grant_digest(grant),'message':'{}'}
            driver=Mock();driver.send_once.return_value={'conversation_id':self.cid,'response':'original'}
            handler=OperatorBrowserBridge(grant=grant,driver=driver,
                reserve_delivery=DurableDeliveryReservation(delivery), replies=DurableReplyJournal(replies))
            conn=FakeConnection(req);conn.sendall=Mock(side_effect=BrokenPipeError)
            with self.assertRaises(BrokenPipeError):handler.handle_once(conn)
            driver.send_once.assert_called_once()
            recovered=FakeConnection(dict(req,operation='observe-original'))
            handler.handle_once(recovered)
            driver.send_once.assert_called_once();driver.observe_once.assert_not_called()
            self.assertEqual(json.loads(recovered.outgoing[0])['response'],'original')

    def test_goal_exposes_edit_boundaries_without_mutating_sealed_goal(self):
        goal={'title':'useful goal'}; profile={'allowed_paths':['code.py'],'test_files':['immutable.py'],'test_cases':['immutable.py']}
        result=planning_goal(goal,profile)
        self.assertEqual(result['execution_contract']['editable_paths'],['code.py'])
        self.assertEqual(result['execution_contract']['immutable_test_files'],['immutable.py'])
        self.assertEqual(result['execution_contract']['immutable_test_file_hash_algorithm'],'git-blob-sha1')
        self.assertIn('not SHA-1 or SHA-256 of raw file bytes',result['execution_contract']['instructions'])
        self.assertNotIn('execution_contract',goal)

class NativeLifecycleRecoveryTests(unittest.TestCase):
    def test_stop_unloads_runner_even_when_worker_withdrawal_is_uncertain(self):
        from do_again.service.native_operating import stop
        with patch('do_again.service.native_operating._target',return_value=Path('/tmp/fixture')), \
             patch('do_again.service.native_operating.broker_request',return_value={'state':'stopped'}), \
             patch('do_again.service.native_operating.worker_service_request',side_effect=ExecutionBlocked('withdrawal uncertain')), \
             patch('do_again.service.native_operating.subprocess.run',return_value=Mock(returncode=0)) as unload:
            result=stop(Path('/tmp/fixture'))
        unload.assert_called_once()
        self.assertEqual(result['state'],'worker_withdrawal_uncertain')
        self.assertTrue(result['journals_retained']);self.assertTrue(result['service_unloaded'])

class ScriptBytesTests(unittest.TestCase):
    def test_syntax_error_rejected_before_native_request_builder(self):
        from do_again.service.planner_contract import validate_planning_answer
        with self.assertRaisesRegex(ExecutionBlocked,'valid Python before dispatch'):
            validate_planning_answer({'gap':'gap','acceptance':'test','script':'if True:\nprint(1)'})
        valid='if True:\n    print(1)'
        self.assertEqual(validate_planning_answer({'gap':'gap','acceptance':'test','script':valid})['script'],valid)
