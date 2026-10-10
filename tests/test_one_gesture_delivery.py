import unittest
import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

from do_again.browser import cdp, runtime as browser
from do_again.browser.errors import BrowserError, BrowserSubmissionUncertain


class OneGestureDeliveryTests(unittest.TestCase):
    def test_sealed_binding_blocks_both_rollover_paths(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'DO_AGAIN_HOME':tmp+'/home'}):
            repo=Path(tmp)/'repo';repo.mkdir()
            record=browser.register_project(repo,chat_url=self.target.url)
            guard={'chat_url':record['chat_url'],'binding_identity':browser.binding_identity(record)}
            with patch.object(browser,'ensure_browser_running',return_value={'port':9224}), \
                 patch.object(browser,'_find_chatgpt_target',return_value=self.target), \
                 patch.object(browser,'wait_for_authenticated',return_value=(self.target,{})), \
                 patch.object(browser,'_assistant_snapshot',return_value={'busy':False}), \
                 patch.object(browser,'_page_contains',return_value=False), \
                 patch.object(browser,'_context_limit_warning',return_value='context limit'), \
                 patch.object(browser,'_rollover_project_chat') as rollover, \
                 patch.object(browser,'send_message',side_effect=BrowserError('pre-dispatch')) as send:
                with patch.object(browser,'_rollover_needed',return_value=True):
                    with self.assertRaises(BrowserSubmissionUncertain):
                        browser.notify_receipts(repo,[{'request_id':'synthetic','state':'succeeded'}],binding_guard=guard)
                    send.assert_not_called()
                with patch.object(browser,'_rollover_needed',return_value=False):
                    with self.assertRaises(BrowserError):
                        browser.notify_receipts(repo,[{'request_id':'synthetic','state':'succeeded'}],binding_guard=guard)
                rollover.assert_not_called()

    def test_explicit_send_uses_one_trusted_mouse_click_and_never_enter(self):
        point={'x':85.5,'y':32.0}
        with patch.object(cdp,'evaluate',return_value=point) as evaluate, \
             patch.object(cdp,'target_call') as effect, \
             patch.object(cdp,'press_enter') as enter:
            cdp.click_send(self.target)
            evaluate.assert_called_once()
            self.assertIn('elementFromPoint',evaluate.call_args.args[1])
            self.assertNotIn('button.click()',evaluate.call_args.args[1])
            self.assertEqual([c.args[1] for c in effect.call_args_list],
                             ['Input.dispatchMouseEvent','Input.dispatchMouseEvent'])
            pressed,released=[c.args[2] for c in effect.call_args_list]
            self.assertEqual(pressed,{'type':'mousePressed','x':85.5,'y':32.0,
                                      'button':'left','clickCount':1,'buttons':1})
            self.assertEqual(released,{'type':'mouseReleased','x':85.5,'y':32.0,
                                       'button':'left','clickCount':1,'buttons':0})
            enter.assert_not_called()
        with patch.object(cdp,'evaluate',return_value=None), \
             patch.object(cdp,'target_call') as effect, \
             patch.object(cdp,'press_enter') as enter:
            with self.assertRaises(BrowserError):cdp.click_send(self.target)
            effect.assert_not_called()
            enter.assert_not_called()

    def test_single_mouse_press_timeout_never_releases_or_retries(self):
        with patch.object(cdp,'evaluate',return_value={'x':50.0,'y':50.0}), \
             patch.object(cdp,'target_call',side_effect=cdp.CdpTimeoutError('press uncertain')) as effect:
            with self.assertRaises(cdp.CdpTimeoutError):cdp.click_send(self.target)
            effect.assert_called_once()
            self.assertEqual(effect.call_args.args[2]['type'],'mousePressed')

    def test_target_lookup_cannot_match_a_different_conversation_prefix(self):
        wrong=cdp.Target('wrong','https://chatgpt.com/c/abc-other','','ws://127.0.0.1/wrong')
        right=cdp.Target('right','https://chatgpt.com/c/abc?model=fixture','','ws://127.0.0.1/right')
        with patch.object(cdp,'targets',return_value=[wrong,right]):
            self.assertEqual(browser._find_chatgpt_target(9224,'https://chatgpt.com/c/abc'),right)
        with patch.object(cdp,'targets',return_value=[wrong]):
            self.assertIsNone(browser._find_chatgpt_target(9224,'https://chatgpt.com/c/abc'))

    def setUp(self):
        self.target = cdp.Target("synthetic", "https://chatgpt.com/c/synthetic", "", "ws://127.0.0.1/synthetic")

    def mocks(self):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(browser, "_assistant_snapshot", return_value={"busy": False}))
        stack.enter_context(patch.object(browser, "_context_limit_warning", return_value=""))
        stack.enter_context(patch.object(browser.time, "sleep"))
        evaluate = stack.enter_context(patch.object(cdp, "evaluate", side_effect=["ready"] + [False]*20))
        insert = stack.enter_context(patch.object(cdp, "insert_text"))
        enter = stack.enter_context(patch.object(cdp, "click_send"))
        return evaluate, insert, enter

    def test_missing_acceptance_never_clicks_or_sends_again(self):
        evaluate, _, enter = self.mocks()
        with self.assertRaises(BrowserSubmissionUncertain):
            browser.send_message(self.target, "synthetic", wait_for_response=False)
        enter.assert_called_once_with(self.target)
        self.assertFalse(any("button.click()" in call.args[1] for call in evaluate.call_args_list))

    def test_generic_error_after_gesture_is_uncertain(self):
        evaluate, _, enter = self.mocks()
        evaluate.side_effect = ["ready", BrowserError("renderer lost")]
        with self.assertRaises(BrowserSubmissionUncertain):
            browser.send_message(self.target, "synthetic", wait_for_response=False)
        enter.assert_called_once()

    def test_pre_dispatch_timeout_has_no_gesture(self):
        _, insert, enter = self.mocks()
        insert.side_effect = cdp.CdpTimeoutError("insert timeout")
        with self.assertRaises(BrowserError) as result:
            browser.send_message(self.target, "synthetic", wait_for_response=False)
        self.assertNotIsInstance(result.exception, BrowserSubmissionUncertain)
        enter.assert_not_called()

    def test_timeout_during_single_gesture_is_uncertain(self):
        _, _, enter = self.mocks()
        enter.side_effect = cdp.CdpTimeoutError("Send timeout")
        with self.assertRaises(BrowserSubmissionUncertain):
            browser.send_message(self.target, "synthetic", wait_for_response=False)
        enter.assert_called_once()

    def test_durable_commit_precedes_the_single_gesture(self):
        _,_,enter=self.mocks();events=[]
        enter.side_effect=lambda target:events.append('gesture')
        with self.assertRaises(BrowserSubmissionUncertain):
            browser.send_message(self.target,'synthetic',wait_for_response=False,
                                 before_dispatch=lambda:events.append('durable_dispatch'))
        self.assertEqual(events,['durable_dispatch','gesture'])

    def test_lost_durable_commit_response_has_no_gesture_and_is_uncertain(self):
        _,_,enter=self.mocks()
        def interrupted():raise OSError('injected crash after durable commit')
        with self.assertRaises(BrowserSubmissionUncertain):
            browser.send_message(self.target,'synthetic',wait_for_response=False,before_dispatch=interrupted)
        enter.assert_not_called()

    @unittest.skipUnless(os.name=='posix','native macOS durability uses directory fsync')
    def test_directory_flush_failure_blocks_gesture_and_retains_uncertain_intent(self):
        from do_again.core.schema import atomic_json
        _,_,enter=self.mocks();flush=os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('injected directory flush failure')
            flush(fd)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'intent.json'
            with patch('do_again.core.schema.os.fsync',side_effect=fail_directory):
                with self.assertRaises(BrowserSubmissionUncertain):
                    browser.send_message(self.target,'synthetic',wait_for_response=False,
                        before_dispatch=lambda:atomic_json(path,{'state':'dispatch_started'}))
            self.assertEqual(json.loads(path.read_text())['state'],'dispatch_started')
        enter.assert_not_called()
