import unittest
from unittest.mock import patch, Mock
from pathlib import Path

from do_again.browser.planner_driver import ChatGPTPlannerDriver
from do_again.supervisor.macos_execution import ExecutionBlocked


class BrowserDriverTests(unittest.TestCase):
    def setUp(self):
        self.cid = '01234567-89ab-4cde-8012-0123456789ab'
        self.url = 'https://chatgpt.com/c/' + self.cid
        self.calls = []
        self.driver = ChatGPTPlannerDriver(
            repo=Path('/tmp/jobpipe-test-fixture'),
            profile_attestor=lambda: 'd'*64,
            record_dispatch=lambda event: self.calls.append(event))

    def test_mismatched_project_conversation_never_opens_browser(self):
        with patch('do_again.browser.planner_driver.os.geteuid', return_value=501), \
             patch('do_again.browser.runtime.project_record',
                   return_value={'chat_url': 'https://chatgpt.com/c/different'}), \
             patch('do_again.browser.runtime.ensure_browser_running') as open_browser:
            with self.assertRaisesRegex(ExecutionBlocked, 'conversation'):
                self.driver.send_once('hello', conversation_id=self.cid,
                                      call_id='planner-'+'a'*20+'-1',
                                      profile_sha256='d'*64)
            open_browser.assert_not_called()

    def test_mismatched_profile_never_reads_chat(self):
        with patch('do_again.browser.planner_driver.os.geteuid', return_value=501), \
             patch('do_again.browser.runtime.project_record') as project:
            with self.assertRaisesRegex(ExecutionBlocked, 'profile identity'):
                self.driver.send_once('hello', conversation_id=self.cid,
                                      call_id='planner-'+'a'*20+'-1',
                                      profile_sha256='0'*64)
            project.assert_not_called()

    def test_existing_chat_single_send_and_dispatch_receipt(self):
        target = Mock(url=self.url)
        def send(target_arg, message, *, before_dispatch, **kwargs):
            before_dispatch()
            return {'chat_url': self.url, 'response': '{"call_id":"ok"}'}
        with patch('do_again.browser.planner_driver.os.geteuid', return_value=501), \
             patch('do_again.browser.runtime.project_record',
                   return_value={'chat_url': self.url}), \
             patch('do_again.browser.runtime.ensure_browser_running',
                   return_value={'port': 9223}), \
             patch('do_again.browser.runtime._find_chatgpt_target',
                   return_value=target), \
             patch('do_again.browser.runtime.wait_for_authenticated',
                   return_value=(target, {})), \
             patch('do_again.browser.runtime.send_message', side_effect=send) as sender:
            output = self.driver.send_once('hello', conversation_id=self.cid,
                                            call_id='planner-'+'a'*20+'-1',
                                            profile_sha256='d'*64)
        self.assertEqual(output['conversation_id'], self.cid)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['state'], 'dispatch_started')
        sender.assert_called_once()


if __name__ == '__main__':
    unittest.main()
