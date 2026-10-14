"""Explicit administrator recovery for one frozen, locally failed publication.

No socket endpoint or automatic caller. The original ledger row remains started,
result-free and permanently non-replayable. New publication requires a fresh ID.
"""
from __future__ import annotations
import hashlib,json,os,stat,sys,sqlite3
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from .macos_execution import ExecutionBlocked,INSTALL_ROOT,EXECUTION_ROOT,ExecutionLedger,private_root_file
from .native_checkpoint import STAGED_KIND,verify_checkpoints
from .publication_quarantine import append_quarantine,has_quarantine

SOURCE='43a45ca7a0259ddded1858225ea365f0240e4752'
REQUEST='offline-3efb655c06613370021a-1'
TASK_FP='4b34694fa5f16e808412ff157105bea4ad6dc4902035483c56803e49d572e182'
HEAD='cfe34074b6bde7cdacb1367e5fc40e288b4813d0'
BASE='71c15f7a0469aad4fa1b462e0d8efe397c460a6e'
TREE='d3c25c0f8ed92ca024cbf6bc3f6c44b99b2196626f8e5c040a2f7f01efb03fd3'
EPOCH=37
PUBLISHER_SHA='0d6181f957470535c19822e7f5b85c21a7c9092f8cd85bd496bb82c0682d9369'
KIND='native-publication-collision-quarantine-v1'
REFERENCE='native-publication-recovery-3efb655c06613370021a-1'


def proposed_action():
    return {'kind':KIND,'source_sha':SOURCE,'request_id':REQUEST+'-publication',
            'request_fingerprint':TASK_FP,'expected_head':HEAD,'tested_tree_sha256':TREE,
            'expected_epoch':EPOCH,'proposal_reference':REFERENCE}


def validate_collision(proposal,previous,status,job,intent,original,*,checkpoint,workspace_proven,no_workers,remote_head):
    if proposal!=proposed_action():raise ExecutionBlocked('recovery proposal differs from the one reviewed collision')
    if (previous.get('source_sha')!=SOURCE or previous.get('production_ready') is not False
            or status.get('intent')!='maintenance' or status.get('epoch')!=EPOCH
            or not no_workers or not workspace_proven or remote_head!=BASE):
        raise ExecutionBlocked('collision recovery requires original source, withdrawn maintenance, workspace proof and unchanged remote parent')
    expected_claim={'kind':STAGED_KIND,'request_id':REQUEST,'request_fingerprint':TASK_FP,
                    'source_sha':SOURCE,'commit':HEAD,'tree_sha256':TREE}
    if checkpoint!=dict(expected_claim,verified=True):raise ExecutionBlocked('recovery tested head checkpoint differs')
    if (job.get('fingerprint')!=TASK_FP or job.get('state')!='blocked'
            or job.get('blocker')!='development failed/uncertain: FileExistsError'
            or set(job.get('stages',{}))!={'snapshot','implementation','test','commit'}
            or job.get('stage_ids',{}).get('publication')!=REQUEST+'-publication'
            or original.get('state')!='started' or original.get('result') is not None):
        raise ExecutionBlocked('original private pipeline or reservation differs')
    if intent!={'operation':'git_publish','repository':'Tran-Steven/jobpipe','head':HEAD,
            'branch':job['stages']['commit']['authority']['repo_branch'],'source_sha':SOURCE,
            'title':'Do Again tested offline proposal '+REQUEST,
            'body':'Offline development only; exact head '+HEAD+'. Native test evidence recorded in the control receipt.',
            'request_fingerprint':TASK_FP,'classification':'development_publication'}:
        raise ExecutionBlocked('original publication intent differs')
    return expected_claim


def recover(stage,*,execute_approved_action=False):
    if sys.platform!='darwin' or os.geteuid()!=0 or not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise ExecutionBlocked('recovery requires isolated root administrator entrypoint')
    stage=Path(stage)
    for p in (stage,*stage.parents):
        if p.is_symlink():raise ExecutionBlocked('recovery stage is aliased')
    for p in (stage/'config.json',stage/'publication-quarantine.json',stage/'install.py'):
        private_root_file(p)
    current_path=INSTALL_ROOT/'current/config.json';private_root_file(current_path)
    previous=json.loads(current_path.read_text());target=json.loads((stage/'config.json').read_text())
    proposal=json.loads((stage/'publication-quarantine.json').read_text())
    # Load only the protected staged installer, never caller adapters.
    import importlib.util
    spec=importlib.util.spec_from_file_location('native_recovery_installer',stage/'install.py')
    installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    from .macos_server import verify_installation
    from .authority import AuthorityRegistry
    with installer.installation_fences(previous):
        verify_installation(previous)
        publisher=INSTALL_ROOT/'current/package/do_again/supervisor/publication.py';private_root_file(publisher)
        if hashlib.sha256(publisher.read_bytes()).hexdigest()!=PUBLISHER_SHA:
            raise ExecutionBlocked('original pre-network publisher source changed')
        project=next(p for p in previous['projects'] if p['account']=='_doagain_jp')
        registry=AuthorityRegistry(Path(previous['authority_path']))
        statuses={p['account']:registry.status(Path(p['repo'])) for p in previous['projects']}
        if any(s['intent']!='maintenance' for s in statuses.values()):raise ExecutionBlocked('all protected identities must remain in maintenance')
        installer.verify_worker_quiescence(previous,reconcile=False)
        from .macos_execution import MacOSProcesses
        if any(MacOSProcesses().owned(p['uid']) for p in previous['projects']):raise ExecutionBlocked('execution identity still owns processes')
        state=INSTALL_ROOT/'state'/project['key'];path=state/(REQUEST+'.json');private_root_file(path)
        job=json.loads(path.read_text())
        ledger=ExecutionLedger.__new__(ExecutionLedger);ledger.path=Path(previous['authority_path']);private_root_file(ledger.path)
        intent=ledger.intent(project['key'],REQUEST+'-publication')
        for name in ('snapshot','implementation','test','commit'):
            observed=ledger.observe_request(project['key'],REQUEST+'-'+name,TASK_FP)
            if observed.get('result')!=job['stages'].get(name):raise ExecutionBlocked('private job differs from original terminal stage')
        checked=verify_checkpoints(target,previous)[project['key']]
        workspace=EXECUTION_ROOT/project['key']/'requests'/(REQUEST+'-publication')
        for p in (workspace,workspace/'scratch',workspace/'cache'):
            info=p.lstat()
            if p.is_symlink() or not stat.S_ISDIR(info.st_mode):raise ExecutionBlocked('publication workspace is not an original directory')
            if p!=workspace and (info.st_uid!=project['uid'] or stat.S_IMODE(info.st_mode)!=0o700):raise ExecutionBlocked('publication workspace ownership or privacy differs')
        metadata=list((workspace/'cache').glob('git-manifest-*'))
        if not metadata or any(p.is_symlink() or p.stat().st_uid!=0 or stat.S_IMODE(p.stat().st_mode)!=0o555 for p in metadata):
            raise ExecutionBlocked('original inspection metadata footprint is missing')
        from .publication import read_credential
        from .github import GitHubRepository,matching_pull_requests
        api=GitHubRepository('Tran-Steven/jobpipe',read_credential(SimpleNamespace(state=state)))
        ref=api.request('GET','git/ref/heads/'+intent['branch']);remote_head=(ref or {}).get('object',{}).get('sha')
        matches=matching_pull_requests(api,intent['branch'],BASE)
        if (len(matches)!=1 or matches[0].get('state')!='open' or matches[0].get('draft') is not True
                or matches[0].get('number')!=28 or matches[0].get('html_url')!='https://github.com/Tran-Steven/jobpipe/pull/28'):
            raise ExecutionBlocked('original baseline draft parent is no longer the unique open proposal')
        with closing(sqlite3.connect(ledger.path)) as db:
            row=db.execute('SELECT state,result FROM execution WHERE project=? AND request=?',(project['key'],REQUEST+'-publication')).fetchone()
            raw_pending=db.execute("SELECT request FROM execution WHERE project=? AND state='started'",(project['key'],)).fetchall()
            if raw_pending!=[(REQUEST+'-publication',)]:raise ExecutionBlocked('another unresolved execution prevents this one-case recovery')
            validate_collision(proposal,previous,statuses['_doagain_jp'],job,intent,
                {'state':row[0],'result':row[1]},checkpoint=checked,workspace_proven=True,no_workers=True,remote_head=remote_head)
            before=ledger.evidence(project['key'])
            approval='explicit-operator-approval:'+REFERENCE
            if has_quarantine(db,project['key'],REQUEST+'-publication'):
                ledger.pending(project['key'])
                audit=db.execute('SELECT approval_reference FROM publication_quarantine WHERE project=? AND request=?',
                    (project['key'],REQUEST+'-publication')).fetchone()
                if audit!=(approval,):raise ExecutionBlocked('prior quarantine approval reference differs')
                result={'state':'existing_quarantine_verified_read_only','replay_permitted':False,'terminal_success':False}
            else:
                result=append_quarantine(db,project['key'],REQUEST+'-publication',expected_intent=intent,
                    approval_reference=approval,dry_run=not execute_approved_action)
            if ledger.evidence(project['key'])!=before:raise ExecutionBlocked('original execution or intent journal changed')
            return dict(result,original_journals_unchanged=True,tested_head_retained=HEAD,
                        task_completion_claim=False,github_mutations=0)
