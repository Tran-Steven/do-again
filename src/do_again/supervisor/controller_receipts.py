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
        if proof.get('kind') == 'native-development-local-v2':
            return self._local(record, result, proof)
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

    def _local(self, record, result, proof):
        request=record['request'];fp=record['fingerprint'];head=proof.get('implementation_commit')
        run=proof.get('native_test_run',{})
        if (proof.get('source_sha')!=self.source_sha or proof.get('request_fingerprint')!=fp
                or result.get('local_development')!=proof
                or not re.fullmatch('[0-9a-f]{40}',str(head))
                or proof.get('base_commit')!=request['expected']['repo_head'] or head==proof['base_commit']
                or proof.get('publication_performed') is not False
                or proof.get('test_before_commit') is not True
                or proof.get('worker_network_denied') is not True
                or proof.get('application_submissions')!=0
                or proof.get('repository')!='Tran-Steven/jobpipe'
                or not str(proof.get('branch','')).startswith('do-again/task-')
                or run.get('commit_before')!=proof['base_commit'] or run.get('commit_after')!=proof['base_commit']
                or run.get('tree_before')!=run.get('tree_after')
                or proof.get('committed_tree_sha256')!=run.get('tree_after')
                or not run.get('cases') or any(c.get('exit_code')!=0 for c in run['cases'])
                or run.get('network_denied') is not True
                or not isinstance(proof.get('original_task'),dict)):
            raise ExecutionBlocked('local receipt lacks original task and independently tested tree')
        return {'remote_durable':True,'native_terminal':True,'tests_passed':True,
            'implementation_changed':True,'authority_matches':True,'no_submission':True,
            'request_fingerprint':fp,'implementation_commit':head,'test_commit':head,
            'branch':proof['branch'],'base_commit':proof['base_commit']}

    def failed(self, record, receipt):
        """A failed request is retained and never acknowledged as completed work."""
        rid=record['request']['request_id'];fp=record['fingerprint']
        native=self.observe_execution(rid,fp);result=native.get('result');envelope=receipt.get('result')
        if (receipt.get('request_id')!=rid or receipt.get('request_fingerprint')!=fp
                or receipt.get('state')!='failed' or native.get('state')!='terminal'
                or native.get('source_sha')!=self.source_sha or native.get('request_fingerprint')!=fp
                or not isinstance(result,dict) or result.get('source_sha')!=self.source_sha
                or result.get('returncode')!=1 or result.get('timed_out') is not False
                or not isinstance(envelope,dict) or envelope.get('request_fingerprint')!=fp
                or envelope.get('result')!=result):
            raise ExecutionBlocked('failed receipt lacks original protected terminal evidence')
        local=result.get('local_failure')
        if isinstance(local,dict):
            rollback=local.get('rollback',{});measurement=rollback.get('measurement',{})
            if (rollback.get('byte_perfect') is not True or measurement.get('clean') is not True
                    or measurement.get('commit')!=record['request']['expected']['repo_head']
                    or not re.fullmatch('[0-9a-f]{64}',str(measurement.get('tree_sha256','')))):
                raise ExecutionBlocked('local failure lacks byte-perfect native rollback')
            return {'clean_source_verified':True,'current_commit':measurement['commit'],
                'tree_sha256':measurement['tree_sha256'],'failed_stage':local['stage'],
                'original_task':local['original_task'],'exit_code':1}
        failure=result.get('verified_failure')
        if (not isinstance(failure,dict) or failure.get('clean_source_verified') is not True
                or failure.get('failed_stage') not in {'implementation','test'}
                or type(failure.get('exit_code')) is not int or failure['exit_code']==0
                or not re.fullmatch('[0-9a-f]{40}',str(failure.get('current_commit','')))
                or not re.fullmatch('[0-9a-f]{64}',str(failure.get('tree_sha256','')))):
            raise ExecutionBlocked('failed receipt has no verified clean source')
        return failure
