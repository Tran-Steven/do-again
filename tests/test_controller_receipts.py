import copy
import unittest
from pathlib import Path
import test_offline_continuation as fixtures
from do_again.supervisor.controller_receipts import NativeDevelopmentVerifier
from do_again.supervisor.macos_execution import ExecutionBlocked, ExecutionLedger


class NativeReceiptTests(unittest.TestCase):
    def setUp(self):
        fixtures.ContinuationTests.setUp(self)
        self.c.start(self.now)
        proposal = fixtures.ContinuationTests.proposal(self)
        self.record = self.c.reserve(proposal, pending_ids=[], now=self.now)
        self.c.publication(self.record['request'], self.now)
        self.rid = self.record['request']['request_id']; self.fp = self.record['fingerprint']
        self.ledger = ExecutionLedger(Path(self.temp.name)/'native.sqlite')
        self.ledger.reserve('fixture', self.rid, self.fp, intent={
            'operation': 'execute', 'source_sha': 'a'*40, 'request_fingerprint': self.fp})
        self.result = {'returncode': 0, 'source_sha': 'a'*40, 'timed_out': False}
        self.receipt = {'request_id': self.rid, 'request_fingerprint': self.fp,
            'state': 'succeeded', 'result': {'request_fingerprint': self.fp, 'result': self.result}}
        self.proof = {'request_fingerprint': self.fp, 'source_sha': 'a'*40,
            'implementation_commit': 'd'*40, 'test_commit': 'd'*40, 'base_commit': 'c'*40,
            'tests_passed': True, 'tests_executed': 3, 'test_exit_code': 0,
            'changed_paths': ['adapters/workday.py', 'tests/test_workday.py'],
            'publication_commit': 'd'*40, 'repository': 'Tran-Steven/jobpipe',
            'branch': 'do-again/task-fixture', 'worker_network_denied': True,
            'application_submissions': 0}
        self.result['development'] = {key: self.proof[key] for key in
            ('implementation_commit', 'test_commit', 'base_commit', 'branch',
             'publication_commit', 'repository', 'tests_executed', 'test_exit_code')}
        self.verifier = NativeDevelopmentVerifier(source_sha='a'*40,
            observe_execution=lambda rid, fp: self.ledger.observe_request('fixture', rid, fp),
            observe_development=lambda rid, fp: copy.deepcopy(self.proof))

    def test_started_native_execution_cannot_be_acknowledged(self):
        with self.assertRaises(ExecutionBlocked): self.verifier(self.record, self.receipt)

    def test_native_terminal_exact_head_proof_advances_chain(self):
        self.ledger.finish('fixture', self.rid, self.result)
        evidence = self.verifier(self.record, self.receipt)
        state = self.c.accept(self.receipt, evidence=evidence, now=self.now)
        self.assertEqual(state['validated_head'], 'd'*40)
        self.assertEqual(state['requests'][0]['test_commit'], 'd'*40)
        old = fixtures.ContinuationTests.proposal(self); old['request']['expected']['repo_head'] = 'c'*40
        with self.assertRaises(ExecutionBlocked): self.c.reserve(old, pending_ids=[], now=self.now)

    def test_forged_remote_outcome_cannot_override_native_ledger(self):
        self.ledger.finish('fixture', self.rid, dict(self.result, returncode=1))
        with self.assertRaises(ExecutionBlocked): self.verifier(self.record, self.receipt)

    def test_wrong_test_head_or_merged_branch_is_rejected(self):
        self.ledger.finish('fixture', self.rid, self.result)
        for key, value in [('test_commit', 'e'*40), ('branch', 'main'),
                           ('tests_executed', 0), ('application_submissions', 1),
                           ('publication_commit', 'e'*40)]:
            original = self.proof[key]; self.proof[key] = value
            with self.subTest(key=key), self.assertRaises(ExecutionBlocked):
                self.verifier(self.record, self.receipt)
            self.proof[key] = original
