import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from do_again.service.offline_continuation import OfflineContinuation, digest
from do_again.supervisor.macos_execution import ExecutionBlocked


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime.now(timezone.utc)
        self.scope = dict(kind='jobpipe-offline-controller-v1', repository='Tran-Steven/jobpipe',
            branch='operator-control', goal_sha256=digest('test goal'), source_sha='a'*40,
            epoch=12, issued_at_utc=self.now.isoformat(),
            expires_at_utc=(self.now+timedelta(hours=1)).isoformat(), max_requests=3,
            max_cycles=3, max_outstanding=1, max_retries=0, planner='sealed-offline',
            integration='proposal-only', network_access=False,
            application_submissions=False, browser_messages=False)
        self.binding = {k: self.scope[k] for k in
                        ('repository', 'branch', 'goal_sha256', 'source_sha', 'epoch')}
        self.path = Path(self.temp.name)/'journal.json'
        self.c = OfflineContinuation(self.path, self.scope, self.binding)

    def proposal(self):
        return dict(request={'schema_version': 1, 'operation': 'scratch_script',
                    'args': {'language': 'python', 'content': 'pass'}, 'expected': {'repo_head': 'c'*40},
                    'limits': {'timeout_seconds': 120}, 'issued_at_utc': self.now.isoformat(),
                    'expires_at_utc': (self.now+timedelta(minutes=10)).isoformat()},
                    gap='fixture checkpoint recovery gap', acceptance='restart retains exact checkpoint',
                    evidence_sha256='b'*64)

    def finish(self, record):
        receipt = dict(request_id=record['request']['request_id'], state='succeeded',
                       request_fingerprint=record['fingerprint'])
        evidence = {k: True for k in ('remote_durable', 'native_terminal', 'tests_passed',
                    'implementation_changed', 'authority_matches', 'no_submission')}
        evidence['request_fingerprint'] = record['fingerprint']
        return receipt, evidence

    def test_three_mock_cycles_and_budget(self):
        self.c.start(self.now)
        for ordinal in range(3):
            record = self.c.reserve(self.proposal(), pending_ids=[], now=self.now)
            self.assertEqual(len(record['request']['continuation']['acknowledged_receipts']), ordinal)
            self.c.publication(record['request'], self.now)
            receipt, evidence = self.finish(record)
            self.c.accept(receipt, evidence=evidence, now=self.now)
        self.assertEqual(self.c.status(self.now)['state'], 'completed')
        with self.assertRaises(ExecutionBlocked):
            self.c.reserve(self.proposal(), pending_ids=[], now=self.now)

    def test_restart_reservation_and_duplicate_receipt(self):
        self.c.start(self.now)
        record = self.c.reserve(self.proposal(), pending_ids=[], now=self.now)
        restarted = OfflineContinuation(self.path, self.scope, self.binding)
        self.assertEqual(restarted.status(self.now)['requests'][-1], record)
        with self.assertRaises(ExecutionBlocked):
            restarted.reserve(self.proposal(), pending_ids=[], now=self.now)
        restarted.publication(record['request'], self.now)
        receipt, evidence = self.finish(record)
        first = restarted.accept(receipt, evidence=evidence, now=self.now)
        second = restarted.accept(receipt, evidence=evidence, now=self.now)
        self.assertEqual(first, second)
        self.assertEqual(len(second['requests']), 1)

    def test_existing_pending_blocks_reservation(self):
        self.c.start(self.now)
        with self.assertRaises(ExecutionBlocked):
            self.c.reserve(self.proposal(), pending_ids=['other'], now=self.now)
        self.assertEqual(self.c.status(self.now)['requests'], [])

    def test_mismatched_publication_blocks(self):
        self.c.start(self.now)
        record = self.c.reserve(self.proposal(), pending_ids=[], now=self.now)
        changed = copy.deepcopy(record['request']); changed['args'] = {}
        with self.assertRaises(ExecutionBlocked): self.c.publication(changed, self.now)
        self.assertEqual(self.c.status(self.now)['state'], 'blocked')

    def test_each_missing_attestation_blocks(self):
        for key in ('remote_durable', 'native_terminal', 'tests_passed',
                    'implementation_changed', 'authority_matches', 'no_submission',
                    'request_fingerprint'):
            with self.subTest(key=key):
                self.path.unlink(missing_ok=True)
                self.c.start(self.now)
                record = self.c.reserve(self.proposal(), pending_ids=[], now=self.now)
                self.c.publication(record['request'], self.now)
                receipt, evidence = self.finish(record); evidence.pop(key)
                with self.assertRaises(ExecutionBlocked):
                    self.c.accept(receipt, evidence=evidence, now=self.now)
                self.assertEqual(self.c.status(self.now)['state'], 'blocked')

    def test_expiration_rejects_restart(self):
        self.c.start(self.now)
        with self.assertRaises(ExecutionBlocked): self.c.start(self.now+timedelta(hours=1))

    def test_scope_change_and_unknown_authority_rejected(self):
        self.c.start(self.now)
        changed = dict(self.scope, epoch=13)
        with self.assertRaises(ExecutionBlocked):
            OfflineContinuation(self.path, changed, self.binding).status(self.now)
        changed = dict(self.scope, browser_messages=True)
        with self.assertRaises(ExecutionBlocked):
            OfflineContinuation(self.path, changed, self.binding).status(self.now)
        changed = dict(self.scope, arbitrary_command=True)
        with self.assertRaises(ExecutionBlocked):
            OfflineContinuation(self.path, changed, self.binding).status(self.now)

    def test_stop_preserves_outstanding_and_resume(self):
        self.c.start(self.now)
        record = self.c.reserve(self.proposal(), pending_ids=[], now=self.now)
        stopped = self.c.stop(self.now)
        self.assertEqual(stopped['outstanding'], record['request']['request_id'])
        self.assertEqual(self.c.start(self.now)['state'], 'waiting')

    def test_failed_receipt_blocks_without_retry(self):
        self.c.start(self.now)
        record = self.c.reserve(self.proposal(), pending_ids=[], now=self.now)
        self.c.publication(record['request'], self.now)
        receipt, evidence = self.finish(record); receipt['state'] = 'failed'
        with self.assertRaises(ExecutionBlocked):
            self.c.accept(receipt, evidence=evidence, now=self.now)
        with self.assertRaises(ExecutionBlocked): self.c.start(self.now)
        self.assertEqual(len(self.c.status(self.now)['requests']), 1)

    def test_request_outliving_grant_rejected(self):
        self.c.start(self.now)
        proposal = self.proposal()
        proposal['request']['expires_at_utc'] = (self.now+timedelta(hours=2)).isoformat()
        with self.assertRaises(ExecutionBlocked):
            self.c.reserve(proposal, pending_ids=[], now=self.now)
        self.assertEqual(self.c.status(self.now)['requests'], [])

    def test_status_receipt_is_not_development_progress(self):
        self.c.start(self.now)
        proposal = self.proposal(); proposal['request']['operation'] = 'status'
        with self.assertRaises(ExecutionBlocked):
            self.c.reserve(proposal, pending_ids=[], now=self.now)


if __name__ == '__main__': unittest.main()
