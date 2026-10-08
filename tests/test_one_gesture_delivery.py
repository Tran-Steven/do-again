import unittest
from unittest.mock import patch

from do_again.browser import cdp, runtime as browser
from do_again.browser.errors import BrowserError, BrowserSubmissionUncertain


class OneGestureDeliveryTests(unittest.TestCase):
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
