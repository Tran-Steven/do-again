import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser.errors import BrowserAuthRequired, BrowserSubmissionUncertain
from do_again.supervisor import browser_runner


class BrowserRunnerTests(unittest.TestCase):
    def test_final_canary_ci_delivery_is_reserved_once_for_exact_bound_run(self):
        from do_again.browser.runtime import binding_identity
        from do_again.core.schema import atomic_json
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);repo=state/'repo';nonce='a'*24
            record={'chat_url':'https://chatgpt.com/c/'+'b'*36,'binding_generation':'fixture'}
            rid='canary-'+nonce+'-2-publish'
            atomic_json(state/'browser_delivery_evidence/publication.json',
                {'state':'reconciled','message_visible':True,'assistant_acknowledged':True,
                 'chat_url':record['chat_url'],'binding_identity':binding_identity(record),
                 'acknowledgment_token':'original-token','payload_sha256':'c'*64,'request_ids':[rid]})
            ci={'canary_nonce':nonce,'state':'terminal','task':2,'run_id':99,
                'conclusion':'success','publication_request_id':rid}
            with patch('sys.argv',['runner',str(repo),str(state),str(state),json.dumps(ci),'123']), \
                 patch('do_again.service.daemon._drain_browser_outbox',return_value=0), \
                 patch('do_again.supervisor.immutable_worker.read_worker_configuration',return_value={}), \
                 patch('do_again.supervisor.live_canary.scope',return_value=(
                     {'nonce':nonce,'chat_url':record['chat_url'],'binding_identity':binding_identity(record)},
                     {},{'repo':str(repo)})), \
                 patch('do_again.browser.runtime.project_record',return_value=record):
                browser_runner._tick();browser_runner._tick()
            queued=list((state/'browser_outbox').glob('*.json'))
            self.assertEqual(len(queued),1)
            value=json.loads(queued[0].read_text())
            self.assertEqual(value['purpose'],'ci_continuation')
            self.assertEqual(value['event_marker'],'DO_AGAIN_CANARY_CI_'+nonce+'_2_99')
            self.assertEqual(value['chat_url'],record['chat_url'])
            self.assertIn('Stop at the two-task boundary',value['prompt'])

    def test_known_blocked_observations_are_terminal_helper_results_not_delivery_success(self):
        for error, expected in [(BrowserSubmissionUncertain('visible only'), 'awaiting_ack'),
                                (BrowserAuthRequired('expired'), 'waiting_for_human')]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                output = io.StringIO()
                with patch('sys.argv', ['runner', tmp, tmp, tmp, '{}', '123', 'browser-fixture', 'a'*40, '1']), \
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
                receipt=json.loads((state/'browser_tick_receipts/browser-fixture.json').read_text())
                self.assertEqual(receipt['result'],result)
                self.assertEqual(receipt['source_sha'],'a'*40)
                drain.assert_called_once()
                liveness.assert_not_called()

    def test_unclassified_crash_does_not_fabricate_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sys.argv', ['runner', tmp, tmp, tmp, '{}', '123', 'browser-fixture', 'a'*40, '1']), \
             patch('do_again.service.daemon._drain_browser_outbox', side_effect=RuntimeError('crash')), \
             patch('sys.stdout', io.StringIO()) as output:
            with self.assertRaises(RuntimeError):
                browser_runner.main()
            self.assertEqual(output.getvalue(), '')
