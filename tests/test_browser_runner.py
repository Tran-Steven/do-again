import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser.errors import BrowserAuthRequired, BrowserSubmissionUncertain
from do_again.supervisor import browser_runner


class BrowserRunnerTests(unittest.TestCase):
    def test_known_blocked_observations_are_terminal_helper_results_not_delivery_success(self):
        for error, expected in [(BrowserSubmissionUncertain('visible only'), 'awaiting_ack'),
                                (BrowserAuthRequired('expired'), 'waiting_for_human')]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                output = io.StringIO()
                with patch('sys.argv', ['runner', tmp, tmp, tmp, '{}', '123']), \
                     patch('do_again.service.daemon._drain_browser_outbox', side_effect=error) as drain, \
                     patch('do_again.service.liveness.check_liveness') as liveness, \
                     patch('sys.stdout', output):
                    browser_runner.main()
                result = json.loads(output.getvalue())
                self.assertEqual(result['state'], 'completed')
                self.assertEqual(result['liveness'], expected)
                self.assertEqual(result['delivered'], 0)
                self.assertFalse(result['delivery_acknowledged'])
                self.assertTrue(result['outbox_preserved'])
                self.assertEqual(json.loads((state / 'attention.json').read_text())['state'], expected)
                drain.assert_called_once()
                liveness.assert_not_called()

    def test_unclassified_crash_does_not_fabricate_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sys.argv', ['runner', tmp, tmp, tmp, '{}', '123']), \
             patch('do_again.service.daemon._drain_browser_outbox', side_effect=RuntimeError('crash')), \
             patch('sys.stdout', io.StringIO()) as output:
            with self.assertRaises(RuntimeError):
                browser_runner.main()
            self.assertEqual(output.getvalue(), '')
