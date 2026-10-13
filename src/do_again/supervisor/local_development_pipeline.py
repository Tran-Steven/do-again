"""Sealed local-only variant of the existing native development pipeline.

Uses the same native execute/test/Git capabilities and root effect ledger.
Exact output hashes and write confinement replace arbitrary editable tests.
No publication effect is constructed. Original failed tasks remain immutable.
"""
import hashlib
import json
from datetime import datetime, timezone
from ..core.schema import atomic_json, canonical_json, parse_utc, request_fingerprint
from .macos_execution import ExecutionBlocked
from .source_transaction import LOCAL_KIND


class LocalDevelopmentPipeline:
    def __init__(self, pipeline, profile):
        self.parent, self.profile = pipeline, profile

    def execute(self, request, now):
        p = self.parent; rid = request['request_id']; fp = request_fingerprint(request)
        state = p.controller.status(now)
        if (state['state'] != 'waiting' or state['outstanding'] != rid
                or state['requests'][-1]['phase'] != 'published'
                or state['requests'][-1]['request'] != request
                or request['expected']['repo_head'] != self.profile['original_task']['base_commit']):
            raise ExecutionBlocked('local development lacks original sealed reservation and pinned source')
        if not parse_utc(request['issued_at_utc']) <= now < parse_utc(request['expires_at_utc']):
            raise ExecutionBlocked('local development request is expired or premature')
        # Compilation is pure; a corrupt browser reply never gets dispatched.
        compile(request['args']['content'], '<sealed-local-development>', 'exec')
        path = p.directory / (rid + '.local.json')
        job = json.loads(path.read_text()) if path.exists() else {
            'kind':LOCAL_KIND, 'fingerprint':fp, 'profile_sha256':hashlib.sha256(canonical_json(self.profile)).hexdigest(),
            'state':'prepared', 'stages':{}, 'stage_ids':{name:rid+'-'+name
                for name in ('regression_baseline', 'implementation', 'test', 'commit')}}
        if job['fingerprint'] != fp or job['profile_sha256'] != hashlib.sha256(canonical_json(self.profile)).hexdigest():
            raise ExecutionBlocked('local development journal identity differs')
        if job['state'] in {'terminal', 'failed'}:
            observed = p.ledger.observe_request(p.project, rid, fp)
            if observed.get('state') != 'terminal' or observed['result'] != job['result']:
                raise ExecutionBlocked('local terminal evidence differs')
            return job['result']
        if job['state'] == 'blocked': raise ExecutionBlocked(job['blocker'])
        atomic_json(path, job)
        stage = 'snapshot'
        try:
            with p.admission(): pass
            p.effects('source_snapshot', rid, request, job['stages'])
            for stage in ('regression_baseline', 'implementation', 'test', 'commit'):
                current = datetime.now(timezone.utc)
                if current >= parse_utc(request['expires_at_utc']):
                    raise ExecutionBlocked('local request expired before next stage')
                if p.controller.status(current)['state'] != 'waiting':
                    raise ExecutionBlocked('controller stopped before next local stage')
                with p.admission(): pass
                sid = job['stage_ids'][stage]
                observed = p.ledger.observe_request(p.project, sid, fp)
                if observed['state'] == 'post_dispatch_uncertain':
                    raise ExecutionBlocked('original '+stage+' effect uncertain; no replay')
                if observed['state'] == 'terminal':
                    result = observed['result']
                else:
                    job.update(state='running', next_stage=stage); atomic_json(path, job)
                    result = p.effects(stage, sid, request, job['stages'])
                    captured = p.ledger.observe_request(p.project, sid, fp)
                    if captured.get('state') != 'terminal' or captured['result'] != result:
                        raise ExecutionBlocked('local stage lacks protected terminal evidence')
                job['stages'][stage] = result; atomic_json(path, job)
                if (type(result.get('returncode')) is not int or result['returncode'] != 0
                        or result.get('timed_out', False)):
                    raise ExecutionBlocked('native local '+stage+' stage failed')
            # Both the committed Git tree and current bytes must equal the tree
            # independently tested BEFORE the commit capability was invoked.
            verified = p.effects('source_committed', rid, request, job['stages'])
            proof = self._proof(request, job, verified)
            p.attestations.record(p.project, rid, fp, proof)
            result = {'returncode':0, 'timed_out':False, 'source_sha':p.source_sha,
                      'local_development':proof}
            self._finish(rid, fp, result)
            job.update(state='terminal', result=result); atomic_json(path, job)
            return result
        except Exception as exc:
            try:
                # Never restore source over unknown Git promotion. Original
                # commit intent must first be positively absent or terminal at
                # the pinned pre-promotion authority.
                commit = p.ledger.observe_request(p.project, job['stage_ids']['commit'], fp)
                if commit['state'] == 'post_dispatch_uncertain':
                    raise ExecutionBlocked('commit promotion uncertain; no blind rollback')
                rollback = p.effects('source_rollback', rid, request, job['stages'])
                if rollback.get('byte_perfect') is not True:
                    raise ExecutionBlocked('source rollback is unverified')
                result = {'returncode':1, 'timed_out':False, 'source_sha':p.source_sha,
                    'local_failure':{'stage':stage, 'reason':str(exc), 'original_task':self.profile['original_task'],
                        'rollback':rollback, 'stages':job['stages']}}
                # A read-only/strictly confined interrupted child can be closed
                # as a FAILURE only after native drain and byte-perfect rollback.
                # Never apply this disposition to an uncertain Git effect.
                for name in ('regression_baseline', 'implementation', 'test'):
                    sid = job['stage_ids'][name]
                    old = p.ledger.observe_request(p.project, sid, fp)
                    if old['state'] == 'post_dispatch_uncertain':
                        intent = p.ledger.intent(p.project, sid)
                        if (not intent or intent.get('operation') != 'execute'
                                or intent.get('source_sha') != p.source_sha):
                            raise ExecutionBlocked('interrupted local child scope differs')
                        failed = {'returncode':1, 'timed_out':True, 'source_sha':p.source_sha,
                            'interrupted_original_effect':True, 'rollback_verified':True}
                        p.ledger.finish(p.project, sid, failed)
                self._finish(rid, fp, result)
                job.update(state='failed', result=result); atomic_json(path, job)
                return result
            except Exception as rollback_exc:
                job.update(state='blocked', blocker='local task/rollback requires reconciliation: '+str(rollback_exc))
                atomic_json(path, job); p.controller.block(job['blocker'], now)
                raise ExecutionBlocked(job['blocker']) from exc

    def _finish(self, rid, fp, result):
        p = self.parent
        old = p.ledger.observe_request(p.project, rid, fp)
        if old['state'] == 'not_started':
            p.ledger.reserve(p.project, rid, fp, intent={'operation':'execute',
                'source_sha':p.source_sha, 'request_fingerprint':fp, 'aggregate':True})
            p.ledger.finish(p.project, rid, result)
        elif old['state'] != 'terminal' or old['result'] != result:
            raise ExecutionBlocked('local aggregate original evidence conflicts')

    def _proof(self, request, job, verified):
        p = self.parent; fp = request_fingerprint(request)
        results = {}
        for stage, sid in job['stage_ids'].items():
            original = p.ledger.observe_request(p.project, sid, fp)
            intent = p.ledger.intent(p.project, sid)
            result = original.get('result')
            if (original.get('state') != 'terminal' or original.get('source_sha') != p.source_sha
                    or not isinstance(result, dict) or result.get('returncode') != 0
                    or result.get('timed_out', False) is not False
                    or result != job['stages'][stage] or not intent
                    or intent.get('operation') != ('git_commit' if stage == 'commit' else 'execute')):
                raise ExecutionBlocked('local attestation lacks original native stage evidence')
            results[stage] = result
        baseline = results['regression_baseline'].get('native_regression_baseline',{})
        if not baseline.get('cases') or not any(c['exit_code']!=0 for c in baseline['cases']):
            raise ExecutionBlocked('regression baseline lacks actual native failure evidence')
        tests = results['test']['native_test_run']; commit = results['commit']
        authority = commit.get('authority', {}); head = authority.get('repo_head')
        if (results['implementation'].get('network_denied') is not True
                or results['implementation'].get('source_write_paths') != self.profile['allowed_paths']
                or results['implementation'].get('execution_uid') != p.execution_uid
                or type(p.execution_uid) is not int or type(verified.get('epoch')) is not int
                or tests.get('commit_before') != request['expected']['repo_head']
                or tests.get('commit_after') != request['expected']['repo_head']
                or tests.get('tree_before') != tests.get('tree_after')
                or verified.get('tree_sha256') != tests['tree_after']
                or verified.get('commit') != head or not verified.get('clean')
                or commit.get('base_sha') != request['expected']['repo_head']
                or set(commit.get('paths', [])) != set(self.profile['allowed_paths'])
                or head == request['expected']['repo_head']
                or tests.get('network_denied') is not True
                or not tests.get('cases') or any(c['exit_code'] != 0 for c in tests['cases'])):
            raise ExecutionBlocked('local commit does not equal independently tested approved tree')
        return {'kind':LOCAL_KIND, 'request_fingerprint':fp, 'source_sha':p.source_sha,
            'original_task':self.profile['original_task'], 'contract_sha256':job['profile_sha256'],
            'base_commit':request['expected']['repo_head'], 'implementation_commit':head,
            'branch':authority['repo_branch'], 'repository':'Tran-Steven/jobpipe',
            'source_contract':self.profile['source_contract'], 'native_test_run':tests,
            'native_regression_baseline':baseline,
            'test_before_commit':True, 'committed_tree_sha256':verified['tree_sha256'],
            'worker_network_denied':True, 'publication_performed':False,
            'execution_uid':results['implementation'].get('execution_uid'),
            'epoch':verified.get('epoch'),
            'application_submissions':0, 'stage_ids':job['stage_ids'],
            'diff':verified['diff'], 'diff_sha256':hashlib.sha256(verified['diff'].encode()).hexdigest()}
