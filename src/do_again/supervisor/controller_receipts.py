"""Match remote completion to protected native evidence; never trust stdout.

The installed backend must supply read-only root-ledger and independently
generated commit/test attestations. No synthetic fallback is permitted.
"""
from __future__ import annotations
import re

from .macos_execution import ExecutionBlocked


class NativeDevelopmentVerifier:
    def __init__(self, *, source_sha, observe_execution, observe_development):
        self.source_sha = source_sha
        self.observe_execution = observe_execution
        self.observe_development = observe_development

    def __call__(self, record, receipt):
        request = record['request']; rid = request['request_id']; fp = record['fingerprint']
        native = self.observe_execution(rid, fp)
        result = native.get('result')
        envelope = receipt.get('result')
        if (receipt.get('request_id') != rid or receipt.get('request_fingerprint') != fp
                or receipt.get('state') != 'succeeded'
                or native.get('state') != 'terminal'
                or native.get('source_sha') != self.source_sha
                or native.get('request_fingerprint') != fp
                or not isinstance(result, dict) or result.get('returncode') != 0
                or result.get('timed_out', False) is not False
                or not isinstance(envelope, dict)
                or envelope.get('request_fingerprint') != fp
                or envelope.get('result') != result):
            raise ExecutionBlocked('remote receipt and protected terminal execution differ')
        proof = self.observe_development(rid, fp)
        audit_keys = ('implementation_commit', 'test_commit', 'base_commit', 'branch',
                      'publication_commit', 'repository', 'tests_executed', 'test_exit_code')
        expected_audit = {key: proof.get(key) for key in audit_keys}
        # This proof must be computed by the protected pipeline, not received as
        # a worker/model supplied marker or copied from untrusted stdout.
        sha = proof.get('implementation_commit')
        if (proof.get('request_fingerprint') != fp
                or proof.get('source_sha') != self.source_sha
                or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha)
                or proof.get('test_commit') != sha
                or proof.get('base_commit') != request['expected']['repo_head']
                or sha == proof.get('base_commit')
                or proof.get('tests_passed') is not True
                or type(proof.get('tests_executed')) is not int
                or proof['tests_executed'] < 1
                or proof.get('test_exit_code') != 0
                or not proof.get('changed_paths')
                or proof.get('publication_commit') != sha
                or proof.get('repository') != 'Tran-Steven/jobpipe'
                or not str(proof.get('branch', '')).startswith('do-again/task-')
                or proof.get('worker_network_denied') is not True
                or proof.get('application_submissions') != 0):
            raise ExecutionBlocked('development lacks exact-head native test/publication evidence')
        if result.get('development') != expected_audit:
            raise ExecutionBlocked('durable receipt lacks exact native commit/test audit')
        return {'remote_durable': True, 'native_terminal': True, 'tests_passed': True,
                'implementation_changed': True, 'authority_matches': True,
                'no_submission': True, 'request_fingerprint': fp,
                'implementation_commit': sha, 'test_commit': sha,
                'branch': proof['branch'], 'base_commit': proof['base_commit']}
