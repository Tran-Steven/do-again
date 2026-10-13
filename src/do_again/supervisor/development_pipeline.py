"""One reserved control request, protected stages, one attested terminal result.

Only the sealed factory constructs this pipeline. Effects are existing broker
capabilities; stage IDs are internal, not additional GitHub requests.
"""
from contextlib import contextmanager
import fcntl
import json
from datetime import datetime,timezone
from ..core.schema import atomic_json, request_fingerprint, canonical_json, parse_utc
from .macos_execution import ExecutionBlocked
from .development_attestation import produce_attestation

AUDIT_KEYS=('implementation_commit','test_commit','base_commit','branch',
            'publication_commit','repository','tests_executed','test_exit_code')

class DevelopmentPipeline:
    def __init__(self, *, controller, ledger, attestations, project, source_sha,
                 directory, admission, effects, profile=None, execution_uid=None):
        self.controller,self.ledger,self.attestations=controller,ledger,attestations
        self.project,self.source_sha,self.directory=project,source_sha,directory
        self.admission,self.effects=admission,effects
        self.profile=profile
        self.execution_uid=execution_uid

    @contextmanager
    def serialized(self):
        with (self.directory/'pipeline.lock').open('a') as fence:
            fcntl.flock(fence,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(fence,fcntl.LOCK_UN)

    def execute(self, request, now):
        with self.serialized():
            from .source_transaction import LOCAL_KIND
            if self.profile is not None and self.profile.get('kind') == LOCAL_KIND:
                from .local_development_pipeline import LocalDevelopmentPipeline
                return LocalDevelopmentPipeline(self, self.profile).execute(request, now)
            return self._execute(request,now)

    def _execute(self,request,now):
        state=self.controller.status(now)
        fp=request_fingerprint(request);rid=request['request_id']
        if (state['state']!='waiting' or state['outstanding']!=rid
                or not state['requests'] or state['requests'][-1]['phase']!='published'
                or state['requests'][-1]['request']!=request):
            raise ExecutionBlocked('development is not the exact published controller reservation')
        if not parse_utc(request['issued_at_utc']) <= now < parse_utc(request['expires_at_utc']):
            raise ExecutionBlocked('original development request expired or premature')
        with self.admission():pass
        path=self.directory/(rid+'.json')
        job=json.loads(path.read_text()) if path.exists() else {
            'fingerprint':fp,'state':'running','stages':{},'stage_ids':{
                name:rid+'-'+name for name in ('implementation','commit','test','publication')}}
        if job['fingerprint']!=fp:raise ExecutionBlocked('pipeline identity conflicts')
        if job['state']=='blocked':raise ExecutionBlocked(job['blocker'])
        if job['state'] in {'terminal','failed'}:
            observed=self.ledger.observe_request(self.project,rid,fp)
            if observed.get('state')!='terminal' or observed['result']!=job['result']:
                raise ExecutionBlocked('pipeline terminal ledger differs')
            return job['result']
        atomic_json(path,job)
        try:
            for stage in ('implementation','commit','test','publication'):
                current=datetime.now(timezone.utc)
                if current >= parse_utc(request['expires_at_utc']):
                    raise ExecutionBlocked('original request expired before next stage')
                if self.controller.status(current)['state'] != 'waiting':
                    raise ExecutionBlocked('controller stopped before next development stage')
                with self.admission():pass
                sid=job['stage_ids'][stage]
                observed=self.ledger.observe_request(self.project,sid,fp)
                if observed['state']=='post_dispatch_uncertain':
                    raise ExecutionBlocked('original '+stage+' effect uncertain; no replay')
                if observed['state']=='terminal': result=observed['result']
                else:
                    # Effect adapters reserve native ledger intent before effects.
                    result=self.effects(stage,sid,request,job['stages'])
                    captured=self.ledger.observe_request(self.project,sid,fp)
                    if captured.get('state')!='terminal' or captured.get('result')!=result:
                        raise ExecutionBlocked('stage lacks protected native terminal evidence')
                job['stages'][stage]=result;atomic_json(path,job)
                if type(result.get('returncode')) is not int or result['returncode']!=0 or result.get('timed_out',False):
                    raise ExecutionBlocked('native '+stage+' stage failed')
                job['stages'][stage]=result;atomic_json(path,job)
            with self.admission():
                proof=produce_attestation(ledger=self.ledger,store=self.attestations,
                    project=self.project,request_id=rid,fingerprint=fp,source_sha=self.source_sha,
                    base_commit=request['expected']['repo_head'],stage_ids={
                        k:job['stage_ids'][k] for k in ('commit','test','publication')})
                result={'returncode':0,'timed_out':False,'source_sha':self.source_sha,
                        'development':{k:proof[k] for k in AUDIT_KEYS}}
                # Parent has no separately dispatchable process: summarize only
                # after protected native stages and attestation exist. Crash here
                # repairs this summary, never repeats stages.
                observed=self.ledger.observe_request(self.project,rid,fp)
                if observed['state']=='not_started':
                    self.ledger.reserve(self.project,rid,fp,intent={'operation':'execute',
                        'source_sha':self.source_sha,'request_fingerprint':fp,'aggregate':True})
                elif observed['state']=='terminal' and observed['result']!=result:
                    raise ExecutionBlocked('aggregate result conflicts')
                self.ledger.finish(self.project,rid,result)
                job.update(state='terminal',result=result);atomic_json(path,job)
                return result
        except Exception as exc:
            from ..service.offline_continuation import persistent_scope
            failed = job['stages'].get(stage)
            if (persistent_scope(self.controller.scope) and stage in {'implementation','test'}
                    and isinstance(failed,dict) and type(failed.get('returncode')) is int
                    and failed['returncode'] != 0 and failed.get('timed_out') is False):
                try:
                    for name, captured in job['stages'].items():
                        original=self.ledger.observe_request(self.project,job['stage_ids'][name],fp)
                        if original.get('state')!='terminal' or original.get('result')!=captured:
                            raise ExecutionBlocked('failed stage terminal provenance differs')
                    clean=self.effects('failure_observation',rid,request,job['stages'])
                    if clean.get('clean_source_verified') is not True:
                        raise ExecutionBlocked('failed development left unverified source')
                    failure={'failed_stage':stage,'exit_code':failed['returncode'],
                        'current_commit':clean['current_commit'],'tree_sha256':clean['tree_sha256'],
                        'clean_source_verified':True,
                        'test_cases':failed.get('native_test_run',{}).get('cases',[])}
                    result={'returncode':1,'timed_out':False,'source_sha':self.source_sha,
                            'verified_failure':failure}
                    original=self.ledger.observe_request(self.project,rid,fp)
                    if original['state']=='not_started':
                        self.ledger.reserve(self.project,rid,fp,intent={'operation':'execute',
                            'source_sha':self.source_sha,'request_fingerprint':fp,'aggregate':True})
                    elif original['state']!='terminal' or original['result']!=result:
                        raise ExecutionBlocked('failed aggregate result differs')
                    self.ledger.finish(self.project,rid,result)
                    job.update(state='failed',result=result);atomic_json(path,job)
                    return result
                except Exception:
                    # Failure observation itself may be uncertain. Preserve all
                    # original effects and stop; no restore or replay fallback.
                    pass
            job.update(state='blocked',blocker='development failed/uncertain: '+type(exc).__name__)
            atomic_json(path,job)
            self.controller.block(job['blocker'],now)
            raise
