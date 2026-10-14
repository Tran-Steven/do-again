"""One explicitly approved disposition of an original read-only planning probe.

No replay, test result, task acknowledgment or retrospective completion.
"""
import hashlib,importlib.util,json,os,sqlite3,sys
from pathlib import Path
from contextlib import closing
from .macos_execution import INSTALL_ROOT,ExecutionBlocked,ExecutionLedger,MacOSProcesses,private_root_file
from .native_checkpoint import verify_checkpoints
from .planning_probe_quarantine import append,exists
from ..service.offline_continuation import digest,controller_artifact

SOURCE='00b4cf9beafa4c19ac6232a293bf5e41fb64a723'
SCOPE='90fb5360625f39209d26242b1be460e85653d82057cf35dbc597c1b79be35bab'
REQUEST='offline-90fb5360625f39209d26-4-probe'
FP='f113c0c6912618ea04b847e8f26a003816640e8e35824d6f5fb0c2065243594a'
HEAD='ef38b5257158bb1efc5bc428884e8d92898a6d2d'
EFFECTS_SHA='86590b5307394796488c3fdd3d43371ddfa2b59bcd024089359afd538ab1a2ad'
REFERENCE='native-read-only-probe-recovery-90fb-4'

def proposed_action():
    return {'kind':'native-read-only-planning-probe-recovery-v1','source_sha':SOURCE,
            'request_id':REQUEST,'request_fingerprint':FP,'scope_sha256':SCOPE,
            'expected_head':HEAD,'maintenance_epoch':39,'completed_native_tasks_retained':3,'remaining_calls':57}

def validate_target(previous,target):
    if target.get('controller_goal')!=previous.get('controller_goal') or target.get('controller_profile')!=previous.get('controller_profile'):
        raise ExecutionBlocked('probe recovery cannot change the goal or execution profile')
    for name,budgets in [('controller_scope',{'max_cycles','max_requests'}),('browser_planning_scope',{'max_calls'})]:
        old=previous[name];new=target[name];changed={'source_sha','issued_at_utc','epoch'}|budgets
        if ({k:v for k,v in old.items() if k not in changed}!={k:v for k,v in new.items() if k not in changed}
                or new.get('source_sha')!=target.get('source_sha') or new.get('epoch')!=39
                or any(new.get(k)!=57 for k in budgets)):
            raise ExecutionBlocked('probe recovery cannot broaden persistent authority or reset the remaining budget')
    if target['controller_scope']['issued_at_utc']!=target['browser_planning_scope']['issued_at_utc']:
        raise ExecutionBlocked('persistent issue identities differ')
    fields=('account','key','repo','worktree','uid','gid','source_sha')
    if [{k:p.get(k) for k in fields} for p in previous['projects']]!=[{k:p.get(k) for k in fields} for p in target['projects']]:
        raise ExecutionBlocked('probe recovery cannot change protected project identities or snapshots')

def recover(stage,*,execute_approved_action=False):
    if sys.platform!='darwin' or os.geteuid()!=0 or not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise ExecutionBlocked('planning probe disposition requires isolated protected administrator staging')
    stage=Path(stage)
    for p in (stage/'config.json',stage/'planning-probe-recovery.json',stage/'install.py'):private_root_file(p)
    if json.loads((stage/'planning-probe-recovery.json').read_text())!=proposed_action():raise ExecutionBlocked('probe disposition proposal differs')
    current=INSTALL_ROOT/'current/config.json';private_root_file(current)
    previous=json.loads(current.read_text());target=json.loads((stage/'config.json').read_text())
    if previous.get('source_sha')!=SOURCE or previous.get('production_ready') is not False:raise ExecutionBlocked('original probe source differs')
    validate_target(previous,target)
    spec=importlib.util.spec_from_file_location('probe_recovery_installer',stage/'install.py');installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    from .macos_server import verify_installation,worktree_authority
    from .authority import AuthorityRegistry
    with installer.installation_fences(previous):
        verify_installation(previous);installer.verify_worker_quiescence(previous,reconcile=False)
        registry=AuthorityRegistry(Path(previous['authority_path']),owner_uid=0)
        if any(registry.status(Path(p['repo']))['intent']!='maintenance' or MacOSProcesses().owned(p['uid']) for p in previous['projects']):raise ExecutionBlocked('all identities must remain quiescent in maintenance')
        project=next(p for p in previous['projects'] if p['account']=='_doagain_jp');state=INSTALL_ROOT/'state'/project['key']
        if registry.status(Path(project['repo']))['epoch']!=39 or worktree_authority(Path(project['worktree']))['repo_head']!=HEAD:raise ExecutionBlocked('retained head or stopped epoch differs')
        if previous.get('controller_profile',{}).get('kind')!='native-development-autonomous-v1' or digest(previous['controller_scope'])!=SCOPE:
            raise ExecutionBlocked('original confined read-only probe profile differs')
        source=INSTALL_ROOT/'current/package/do_again/supervisor/controller_native_effects.py';private_root_file(source)
        if hashlib.sha256(source.read_bytes()).hexdigest()!=EFFECTS_SHA:raise ExecutionBlocked('original read-only capture and source exposure code differs')
        claim=verify_checkpoints(target,previous)[project['key']]
        if claim['commit']!=HEAD or claim['source_sha']!=SOURCE or claim['request_id']!='offline-90fb5360625f39209d26-3':raise ExecutionBlocked('last genuine tested head checkpoint differs')
        scope=previous['controller_scope'];controller=state/controller_artifact(scope,'controller.json');private_root_file(controller)
        value=json.loads(controller.read_text());expected=['offline-90fb5360625f39209d26-'+str(i) for i in (1,2,3)]
        records=value.get('requests',[])
        if (value.get('scope_sha256')!=SCOPE or value.get('state')!='stopped'
                or value.get('outstanding') is not None or value.get('acknowledged')!=expected
                or [v.get('request',{}).get('request_id') for v in records]!=expected
                or any(v.get('phase')!='acknowledged' for v in records)):
            raise ExecutionBlocked('original controller stopped history differs')
        calls=state/controller_artifact(scope,'browser-planning-calls.json');private_root_file(calls);planned=json.loads(calls.read_text())
        if planned.get('scope_sha256')!=digest(previous['browser_planning_scope']) or set(planned.get('calls',{}))!={'1','2','3'}:
            raise ExecutionBlocked('unexpected fourth model reservation or browser history')
        for key,record in planned['calls'].items():
            if record.get('cycle')!=int(key) or record.get('state')!='reserved' or record.get('call_id')!='planner-'+planned['scope_sha256'][:20]+'-'+key:
                raise ExecutionBlocked('original three browser reservation identities differ')
        if sum((Path(project['worktree'])/p).stat().st_size for p in dict.fromkeys([*previous['controller_profile']['allowed_paths'],*previous['controller_profile']['test_files']]) if (Path(project['worktree'])/p).exists())<=24576:
            raise ExecutionBlocked('original combined-source budget failure no longer applies')
        if digest({'scope':scope,'cycle':4,'kind':'planning-probe'})!=FP:raise ExecutionBlocked('original probe fingerprint differs')
        ledger=ExecutionLedger.__new__(ExecutionLedger);ledger.path=Path(previous['authority_path']);private_root_file(ledger.path)
        intent=ledger.intent(project['key'],REQUEST)
        if intent!={'operation':'execute','source_sha':SOURCE,'request_fingerprint':FP}:raise ExecutionBlocked('original read-only probe intent differs')
        before=ledger.evidence(project['key'])
        with closing(sqlite3.connect(ledger.path)) as db:
            if exists(db,project['key'],REQUEST):
                ledger.pending(project['key'])
                if db.execute('SELECT approval_reference FROM planning_probe_quarantine WHERE project=? AND request=?',(project['key'],REQUEST)).fetchone()!=('explicit-operator-approval:'+REFERENCE,):raise ExecutionBlocked('original probe approval reference differs')
                result={'state':'existing_probe_disposition_verified_read_only','terminal_success':False,'replay_permitted':False,'tests_claimed_passed':False}
            else:
                if [p['request_id'] for p in ledger.pending(project['key'])]!=[REQUEST]:raise ExecutionBlocked('another unresolved execution prevents this exact disposition')
                result=append(db,project['key'],REQUEST,expected_intent=intent,approval_reference='explicit-operator-approval:'+REFERENCE,dry_run=not execute_approved_action)
        if ledger.evidence(project['key'])!=before:raise ExecutionBlocked('original execution or intent journals changed')
        return dict(result,original_journals_unchanged=True,task_completion_claim=False,model_calls=0,github_mutations=0,retained_head=HEAD)
