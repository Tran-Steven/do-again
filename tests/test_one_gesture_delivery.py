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
        enter = stack.enter_context(patch.object(cdp, "press_enter"))
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
        enter.side_effect = cdp.CdpTimeoutError("Enter timeout")
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
