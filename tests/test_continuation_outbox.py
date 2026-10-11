from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser import runtime as browser
from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.service import daemon


class ContinuationOutboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / 'state'
        self.repo = Path(temporary.name) / 'repo'
        self.repo.mkdir()
        self.record = {'chat_url':'https://chatgpt.com/c/exact', 'binding_generation':'generation'}
        self.target = type('Target', (), {'url':self.record['chat_url']})()

    def enqueue(self, purpose='ci_continuation', prompt='Inspect exact CI head before proceeding.'):
        return daemon._queue_continuation(self.state, prompt, purpose=purpose, marker='CI_EVENT exact-head', binding=self.record)

    def test_ci_intent_uses_receipt_dispatch_ack_and_terminal_replay_protection(self):
        path = self.enqueue()
        self.assertEqual(self.enqueue(), path)
        def send(target, message, **options):
            self.assertIn('CI_EVENT exact-head', message)
            self.assertIn('Inspect exact CI head', message)
            options['before_dispatch']()
            return {'response':'submitted'}
        notify = browser.notify_receipts.__wrapped__
        with patch.object(daemon,'activate_project'), \
             patch.object(daemon,'ensure_browser_running',return_value={'port':9224}), \
             patch.object(browser,'ensure_browser_running',return_value={'port':9224}), \
             patch.object(browser,'project_record',return_value=self.record), \
             patch.object(browser,'_find_chatgpt_target',return_value=self.target), \
             patch.object(browser,'_rollover_needed',return_value=False), \
             patch.object(browser,'wait_for_authenticated',return_value=(self.target,{})), \
             patch.object(browser,'_assistant_snapshot',return_value={'busy':False}), \
             patch.object(browser,'_page_contains',return_value=False) as visible, \
             patch.object(browser,'send_message',side_effect=send) as gesture, \
             patch.object(daemon,'notify_receipts',side_effect=notify), \
             patch.object(browser,'receipt_acknowledgment',return_value={'visible':True,'acknowledged':False}) as acknowledgment:
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo,self.state)
            evidence = daemon._read_uncertain_delivery(self.state)
            self.assertEqual(evidence['purpose'],'ci_continuation')
            self.assertEqual(evidence['state'],'dispatch_started')
            self.assertEqual(evidence['batch_marker'],'CI_EVENT exact-head')
            self.assertTrue(evidence['acknowledgment_token'].startswith('DO_AGAIN_RECEIPT_ACK token='))
            visible.return_value = True
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo,self.state)
            self.assertTrue(path.exists())
            acknowledgment.return_value = {'visible':True,'acknowledged':True}
            self.assertEqual(daemon._drain_browser_outbox_locked(self.repo,self.state),1)
            gesture.assert_called_once()
        self.assertFalse(path.exists())
        # The same causal event cannot recreate a dispatched payload after cleanup.
        reservation = self.enqueue()
        self.assertEqual(json.loads(reservation.read_text())['state'],'reconciled')
        self.assertEqual(daemon._pending_outbox(self.state),[])

    def test_changed_payload_and_missing_queued_item_fail_closed(self):
        path = self.enqueue()
        with self.assertRaisesRegex(BrowserSubmissionUncertain,'payload conflicts'):
            self.enqueue(prompt='Different work')
        path.unlink()
        with self.assertRaisesRegex(BrowserSubmissionUncertain,'no automatic replay'):
            self.enqueue()
        self.assertEqual(daemon._pending_outbox(self.state),[])

    def test_reservation_crash_before_queue_does_not_replay(self):
        original = daemon._queue_receipt_locked
        with patch.object(daemon,'_queue_receipt_locked',side_effect=OSError('injected queue failure')):
            with self.assertRaises(OSError):
                self.enqueue()
        with patch.object(daemon,'_queue_receipt_locked',wraps=original) as create:
            with self.assertRaises(BrowserSubmissionUncertain):
                self.enqueue()
            create.assert_not_called()

    def test_receipts_and_continuations_are_not_merged_into_one_message(self):
        daemon._queue_receipt(self.state,{'request_id':'receipt-1'})
        continuation = self.enqueue(purpose='idle_continuation')
        daemon._queue_receipt(self.state,{'request_id':'receipt-2'})
        with patch.object(daemon,'activate_project'), patch.object(daemon,'ensure_browser_running'), \
             patch.object(browser,'project_record',return_value=self.record), \
             patch.object(daemon,'notify_receipts',side_effect=BrowserSubmissionUncertain('after gesture')) as submit:
            with self.assertRaises(BrowserSubmissionUncertain):
                daemon._drain_browser_outbox_locked(self.repo,self.state)
        self.assertEqual([row['request_id'] for row in submit.call_args.args[1]],['receipt-1'])
        self.assertTrue(continuation.exists())

    def test_same_url_rebinding_blocks_queued_event_before_submission(self):
        path=self.enqueue()
        with patch.object(daemon,'activate_project'), patch.object(daemon,'ensure_browser_running'), \
             patch.object(browser,'project_record',return_value={**self.record,'binding_generation':'changed'}), \
             patch.object(daemon,'notify_receipts') as submit:
            with self.assertRaisesRegex(BrowserSubmissionUncertain,'binding changed'):
                daemon._drain_browser_outbox_locked(self.repo,self.state)
            submit.assert_not_called()
        self.assertTrue(path.exists())

    def test_real_ci_scheduler_enqueues_once_without_nested_delivery_lock_or_send(self):
        from do_again.service import liveness
        control=self.repo/'control'
        requests=control/'automation/do_again/requests';requests.mkdir(parents=True)
        receipts=control/'automation/do_again/receipts';receipts.mkdir()
        (requests/'ci.json').write_text(json.dumps({'request_id':'ci','continuation':{
            'goal_state':'waiting_for_ci','goal_id':'synthetic-goal',
            'ci':{'repository':'Tran-Steven/do-again','run_id':123,'head_sha':'a'*40}}}))
        (receipts/'ci.json').write_text(json.dumps({'request_id':'ci','state':'succeeded'}))
        settings={'continuous':True,'report_stalls':False,'issue_repo':'',
                  'idle_seconds':60,'recovery_seconds':300}
        probe=lambda _: {'state':'terminal','status':'completed','conclusion':'success','head_sha':'a'*40}
        with patch.object(browser,'ensure_browser_running',return_value={'port':9224}), \
             patch.object(browser,'project_record',return_value=self.record), \
             patch.object(browser,'_find_chatgpt_target',return_value=self.target), \
             patch.object(browser,'_page_contains',return_value=False), \
             patch.object(browser,'send_message') as submit:
            self.assertEqual(liveness.check_liveness(self.repo,control,self.state,
                settings=settings,ci_probe=probe),'recovering')
            self.assertEqual(liveness.check_liveness(self.repo,control,self.state,
                settings=settings,ci_probe=probe),'recovering')
            submit.assert_not_called()
        queued=daemon._pending_outbox(self.state)
        self.assertEqual(len(queued),1)
        intent=json.loads(queued[0].read_text())
        self.assertEqual(intent['purpose'],'ci_continuation')
        self.assertEqual(intent['binding_identity'],browser.binding_identity(self.record))
