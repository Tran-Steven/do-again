from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser import runtime as browser
from do_again.browser.errors import BrowserSubmissionUncertain
from do_again.service import daemon


class ReceiptAcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.repo = self.state / 'repo'
        self.repo.mkdir()
        self.path = daemon._queue_receipt(self.state, {'request_id': 'receipt-1', 'state': 'succeeded'})
        self.record = {'chat_url': 'https://chatgpt.com/c/exact', 'binding_generation': 'generation'}
        self.intent = {
            'request_ids': ['receipt-1'], 'chat_url': self.record['chat_url'],
            'binding_identity': browser.binding_identity(self.record),
            'batch_marker': 'DO_AGAIN_RECEIPTS_READY request_ids=receipt-1',
            'acknowledgment_token': 'DO_AGAIN_RECEIPT_ACK token=' + 'a' * 32,
            'payload_sha256': 'b' * 64, 'state': 'dispatch_started', 'first_seen_epoch': 1000,
        }
        daemon.atomic_json(daemon._uncertain_delivery_path(self.state), self.intent)

    def reconcile(self, observation):
        with patch.object(browser, 'project_record', return_value=self.record), \
             patch.object(daemon, 'ensure_browser_running', return_value={'port': 9224}), \
             patch.object(browser, '_find_chatgpt_target', return_value=object()), \
             patch.object(browser, '_page_contains', return_value=True), \
             patch.object(browser, 'receipt_acknowledgment', return_value=observation), \
             patch.object(browser, 'send_message') as send:
            try:
                return daemon._reconcile_uncertain_delivery(self.repo, self.state,
                    daemon._read_uncertain_delivery(self.state))
            finally:
                send.assert_not_called()

    def test_visible_without_ack_survives_restart_and_escalates(self):
        for _ in range(2):
            with self.assertRaises(BrowserSubmissionUncertain):
                self.reconcile({'visible': True, 'acknowledged': False})
        saved = daemon._read_uncertain_delivery(self.state)
        self.assertEqual(saved['state'], 'visible')
        self.assertTrue(saved['message_visible'])
        self.assertFalse(saved.get('assistant_acknowledged', False))
        self.assertTrue(self.path.exists())
        self.assertTrue((self.state / 'incidents/browser-submission-unacknowledged.json').exists())

    def test_ack_requires_original_user_evidence(self):
        with self.assertRaises(BrowserSubmissionUncertain):
            self.reconcile({'visible': False, 'acknowledged': True})
        self.assertTrue(self.path.exists())

    def test_terminal_evidence_precedes_cleanup_and_restart_finishes_cleanup(self):
        original_unlink = Path.unlink
        def fail_receipt(path, *args, **kwargs):
            if path == self.path:
                raise OSError('injected crash during cleanup')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_receipt), self.assertRaises(OSError):
            self.reconcile({'visible': True, 'acknowledged': True})
        evidence = json.loads(daemon._delivery_evidence_path(self.state, self.intent).read_text())
        self.assertEqual(evidence['state'], 'reconciled')
        self.assertTrue(evidence['assistant_acknowledged'])
        # Cleanup does not require another browser observation or gesture.
        with patch.object(daemon, 'ensure_browser_running') as browser_start:
            self.assertEqual(daemon._reconcile_uncertain_delivery(self.repo, self.state,
                daemon._read_uncertain_delivery(self.state)), 1)
            browser_start.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assertFalse(daemon._uncertain_delivery_path(self.state).exists())

    def test_legacy_token_is_not_upgraded_into_acknowledgment(self):
        with patch.object(browser.cdp, 'evaluate') as evaluate:
            self.assertEqual(browser.receipt_acknowledgment(object(), 'marker', ''),
                {'visible': False, 'acknowledged': False})
            evaluate.assert_not_called()

    def test_dom_observation_rejects_non_boolean_or_visibility_missing_results(self):
        for result in [None, {'visible': 'true', 'acknowledged': True},
                       {'visible': False, 'acknowledged': True}]:
            with patch.object(browser.cdp, 'evaluate', return_value=result):
                self.assertFalse(browser.receipt_acknowledgment(object(),
                    self.intent['batch_marker'], self.intent['acknowledgment_token'])['acknowledged'])

    @unittest.skipUnless(shutil.which('node'), 'Node required for deterministic DOM fixture')
    def test_causal_dom_order_split_markers_duplicates_and_busy_generation(self):
        marker = self.intent['batch_marker']
        token = self.intent['acknowledgment_token']
        cases = [
            ([('user', marker + '\n' + token), ('assistant', token)], False, True),
            ([('assistant', token), ('user', marker + '\n' + token)], False, False),
            ([('user', marker), ('user', token), ('assistant', token)], False, False),
            ([('user', marker + '\n' + token), ('assistant', 'Quoted ' + token)], False, False),
            ([('user', marker + '\n' + token), ('assistant', token)], True, False),
            ([('user', marker + '\n' + token), ('user', marker + '\n' + token), ('assistant', token)], False, False),
        ]
        for turns, busy, expected in cases:
            def evaluate(target, expression, **kwargs):
                setup = "const turns=" + json.dumps(turns) + ";const busy=" + json.dumps(busy) + ";"
                setup += """
const nodes = turns.map(([role,text],index) => ({role,innerText:text,
 hasAttribute:()=>false, compareDocumentPosition:other=>other.index>index?4:2,index}));
const Node={DOCUMENT_POSITION_FOLLOWING:4};
const document={querySelectorAll:selector=>selector==='h1,h2,h3,h4,h5,h6'?[]:
 nodes.filter(node=>selector.includes('\"user\"')?node.role==='user':
 selector.includes('\"assistant\"')?node.role==='assistant':true),
 querySelector:()=>busy?{disabled:false}:null};
"""
                result = subprocess.run([shutil.which('node'), '-e', setup +
                    'process.stdout.write(JSON.stringify(' + expression + '));'],
                    text=True, capture_output=True, check=True, timeout=30)
                return json.loads(result.stdout)
            with self.subTest(turns=turns, busy=busy), patch.object(browser.cdp, 'evaluate', side_effect=evaluate):
                self.assertEqual(browser.receipt_acknowledgment(object(), marker, token)['acknowledged'], expected)

    def test_dispatch_started_cannot_be_erased_by_a_busy_error(self):
        self.path.unlink()
        self.path = daemon._queue_receipt(self.state, {'request_id':'receipt-1','state':'succeeded'})
        daemon._uncertain_delivery_path(self.state).unlink()
        def send(repo, receipts, *, before_dispatch):
            before_dispatch({key:self.intent[key] for key in
                ('chat_url','binding_identity','payload_sha256','acknowledgment_token','state')})
            from do_again.browser.errors import BrowserError
            raise BrowserError('ChatGPT is still generating; retry delivery later')
        with patch.object(daemon,'activate_project'), patch.object(daemon,'ensure_browser_running'), \
             patch.object(browser,'project_record',return_value=self.record), \
             patch.object(daemon,'notify_receipts',side_effect=send):
            from do_again.browser.errors import BrowserError
            with self.assertRaises(BrowserError):
                daemon._drain_browser_outbox_locked(self.repo,self.state)
        self.assertEqual(daemon._read_uncertain_delivery(self.state)['state'],'dispatch_started')
        self.assertTrue(self.path.exists())

    def test_invalid_request_identity_fails_before_browser_or_path_cleanup(self):
        invalid = dict(self.intent, request_ids=['../outside'])
        daemon.atomic_json(daemon._uncertain_delivery_path(self.state),invalid)
        from do_again.browser.errors import BrowserError
        with self.assertRaises(BrowserError):
            daemon._read_uncertain_delivery(self.state)
