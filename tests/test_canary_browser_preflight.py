"""Read-only background ChatGPT canary preflight: no target changes or Send."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from do_again.supervisor.canary_bootstrap import preflight_background_delivery


class BackgroundCanaryPreflightTests(unittest.TestCase):
    def setUp(self):
        self.target=SimpleNamespace(url="https://chatgpt.com/c/fake-fixture")
        self.browser=SimpleNamespace(
            load_config=Mock(return_value={"preferred_mode":"background"}),
            browser_status=Mock(return_value={
                "running":True,"authenticated":True,"auth_required":False,
                "mode":"background","port":9224,
            }),
        )
        self.cdp=SimpleNamespace(
            targets=Mock(return_value=[self.target]),
            evaluate=Mock(return_value={
                "focused":False,"visible":True,"composer":True,"challenge":False,
            }),
            create_target=Mock(),target_call=Mock(),insert_text=Mock(),
            click_send=Mock(),
        )

    def probe(self):
        return preflight_background_delivery(browser=self.browser,
                                             cdp_module=self.cdp)

    def assert_read_only(self, result):
        self.assertIs(result["read_only"],True)
        self.assertIs(result["nonce_consumed"],False)
        self.assertIs(result["message_submitted"],False)
        self.assertIs(result["delivery_verified"],False)
        self.cdp.create_target.assert_not_called()
        self.cdp.target_call.assert_not_called()
        self.cdp.insert_text.assert_not_called()
        self.cdp.click_send.assert_not_called()
        self.browser.browser_status.assert_called_with(verify_session=False)

    def test_actual_unfocused_background_chrome_is_blocked_before_nonce(self):
        result=self.probe()
        self.assertEqual(result["state"],"blocked")
        self.assertIn("unfocused",result["reason"])
        self.assert_read_only(result)
        self.cdp.evaluate.assert_called_once()

    def test_existing_focused_tab_is_only_provisionally_eligible(self):
        self.cdp.evaluate.return_value={
            "focused":True,"visible":True,"composer":True,"challenge":False}
        result=self.probe()
        self.assertEqual(result["state"],"preflight_eligible")
        self.assertIn("NOT yet proven",result["reason"])
        self.assert_read_only(result)

    def test_hidden_challenged_or_missing_composer_fails_closed(self):
        good={"focused":True,"visible":True,"composer":True,"challenge":False}
        for override in ({"focused":False},{"visible":False},
                         {"composer":False},{"challenge":True},{}):
            with self.subTest(override=override):
                self.cdp.evaluate.return_value={**good,**override}
                if override=={}:
                    self.cdp.evaluate.return_value={}
                self.assertEqual(self.probe()["state"],"blocked")

    def test_missing_target_and_bad_browser_mode_are_blocked(self):
        self.cdp.targets.return_value=[]
        self.assertEqual(self.probe()["state"],"blocked")
        self.cdp.evaluate.assert_not_called()
        self.cdp.targets.return_value=[self.target]
        for status in ({"running":False},{"authenticated":False},
                       {"auth_required":True},{"mode":"headless"},{"port":None}):
            with self.subTest(status=status):
                saved=self.browser.browser_status.return_value
                self.browser.browser_status.return_value={**saved,**status}
                try:
                    self.assertEqual(self.probe()["state"],"blocked")
                finally:
                    self.browser.browser_status.return_value=saved

    def test_failed_cdp_read_does_not_claim_delivery(self):
        self.cdp.targets.side_effect=RuntimeError("CDP unavailable")
        result=self.probe()
        self.assertEqual(result["state"],"blocked")
        self.assertIn("observation failed",result["reason"])
        self.assert_read_only(result)


if __name__=="__main__":
    unittest.main()
