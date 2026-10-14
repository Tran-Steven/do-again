"""Explicit staged completion of one authorized, partially published checkpoint.

The only external mutation is PR28 title/body. No Git publication is retried.
This is an administrator entrypoint, never a controller recovery shortcut.
"""
from __future__ import annotations
import importlib.util,json,os,sys,threading
from pathlib import Path
from types import SimpleNamespace
from .macos_execution import INSTALL_ROOT,ExecutionBlocked,ExecutionLedger,MacOSProcesses,private_root_file
from .native_checkpoint import verify_checkpoints
from .publication import read_credential,reconcile_publication
from .github import GitHubRepository,branch_pull_requests,wait_for_pull_payload
from .authority import AuthorityRegistry

SOURCE='6c73f51581ac841f220c49fce0411a3aa96b7bd1'
REQUEST='checkpoint-cfe34074-20261014-01'
HEAD='cfe34074b6bde7cdacb1367e5fc40e288b4813d0'
BASE='71c15f7a0469aad4fa1b462e0d8efe397c460a6e'
BRANCH='do-again/task-8aebfc9bae205bc7f367'
FP='35d49d58325cf10220c972f34662f8ccc0b0150fa7ca880cb1529008a723fc10'
OLD_TITLE='Review native-tested ATS question triage baseline'
OLD_BODY=('Previously reviewed local-only baseline, separately authorized for draft publication. '
          'Unsupported input controls require human review; verified contact behavior has regression coverage. '
          'This is zero autonomous coding cycles. No application submission, deployment or automatic merge. '
          'Exact native-tested commit: '+BASE)
TITLE='Review native-tested development checkpoint'
BODY=('AI-authored edits retained after a publication failure. Fresh, separately authorized draft publication; '
      'the original request remains uncompleted without replay or acknowledgment. '
      'No application submissions, deployment or automatic merge. Exact native-tested commit: '+HEAD)

def proposed_action():
    return {'kind':'native-checkpoint-pr-metadata-completion-v1','source_sha':SOURCE,
            'request_id':REQUEST,'request_fingerprint':FP,'head':HEAD,'epoch':37,
            'pull_request':28,'title':TITLE,'body':BODY}

def validate_intent(intent):
    if intent!={'operation':'git_publish','repository':'Tran-Steven/jobpipe','head':HEAD,
                'branch':BRANCH,'source_sha':SOURCE,'title':TITLE,'body':BODY,
                'request_fingerprint':FP,'classification':'operator_approved_tested_checkpoint'}:
        raise ExecutionBlocked('original fresh publication intent differs')

def validate_terminal(result):
    if result!={'operation':'git_publish','state':'succeeded','returncode':0,'source_sha':SOURCE,
            'repository':'Tran-Steven/jobpipe','head':HEAD,'branch':BRANCH,'pull_request':28,
            'url':'https://github.com/Tran-Steven/jobpipe/pull/28','reconciled_read_only':True}:
        raise ExecutionBlocked('terminal assisted publication evidence differs')

def validate_pr(pr, *, allow_original):
    if (not isinstance(pr,dict) or pr.get('number')!=28 or pr.get('state')!='open'
            or pr.get('draft') is not True or pr.get('html_url')!='https://github.com/Tran-Steven/jobpipe/pull/28'
            or pr.get('head',{}).get('sha')!=HEAD or pr.get('head',{}).get('ref')!=BRANCH
            or pr.get('head',{}).get('repo',{}).get('full_name')!='Tran-Steven/jobpipe'
            or pr.get('base',{}).get('ref')!='main'
            or pr.get('base',{}).get('repo',{}).get('full_name')!='Tran-Steven/jobpipe'):
        raise ExecutionBlocked('checkpoint draft identity differs')
    payload=(pr.get('title'),pr.get('body'))
    if payload==(TITLE,BODY):return 'complete'
    if allow_original and payload==(OLD_TITLE,OLD_BODY):return 'original'
    raise ExecutionBlocked('checkpoint metadata changed independently')

def complete_metadata(api,audit,proposal,guard,*,execute_approved_action):
    """One write-once metadata action; an existing reservation only observes."""
    def readback():
        ref=api.request('GET','git/ref/heads/'+BRANCH)
        pulls=branch_pull_requests(api,BRANCH)
        if (not ref or ref['object']['sha']!=HEAD or len(pulls)!=1 or pulls[0]['number']!=28):
            raise ExecutionBlocked('exact published branch or unique draft differs')
        return api.request('GET','pulls/28')
    pr=readback();state=validate_pr(pr,allow_original=True)
    if not execute_approved_action:
        if audit.exists() and json.loads(audit.read_text()).get('proposal')!=proposal:
            raise ExecutionBlocked('metadata reservation conflicts')
        return {'state':'prepared','metadata_state':state,'metadata_mutations':0,'proposal':proposal}
    if audit.exists():
        recorded=json.loads(audit.read_text())
        if recorded.get('proposal')!=proposal:raise ExecutionBlocked('metadata reservation conflicts')
        if state!='complete':raise ExecutionBlocked('reserved metadata action is uncertain; no replay')
    elif state=='original':
        reservation={'proposal':proposal,'state':'started','original_title':pr['title'],'original_body':pr['body']}
        fd=os.open(audit,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(reservation,f,sort_keys=True);f.flush();os.fsync(f.fileno())
        directory=os.open(audit.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:os.fsync(directory)
        finally:os.close(directory)
        if validate_pr(readback(),allow_original=True)!='original':
            raise ExecutionBlocked('metadata changed before the reserved PATCH')
        guard()
        api.request('PATCH','pulls/28',{'title':TITLE,'body':BODY})
    validate_pr(wait_for_pull_payload(api,28,BRANCH,HEAD,TITLE,BODY,(OLD_TITLE,OLD_BODY)),allow_original=False)
    return {'state':'metadata_verified','git_mutations':0}

def recover(stage, *, execute_approved_action=False):
    if sys.platform!='darwin' or os.geteuid()!=0 or not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise ExecutionBlocked('metadata completion requires isolated administrator staging')
    stage=Path(stage)
    for path in (stage/'config.json',stage/'publication-metadata-recovery.json',stage/'install.py'):
        private_root_file(path)
    proposal=json.loads((stage/'publication-metadata-recovery.json').read_text())
    if proposal!=proposed_action():raise ExecutionBlocked('metadata proposal differs')
    current=INSTALL_ROOT/'current/config.json';private_root_file(current)
    config=json.loads(current.read_text());target=json.loads((stage/'config.json').read_text())
    if config['source_sha']!=SOURCE or config.get('production_ready') is not False:
        raise ExecutionBlocked('installed source differs from original partial publication')
    spec=importlib.util.spec_from_file_location('metadata_recovery_installer',stage/'install.py')
    installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
    from .macos_server import verify_installation,worktree_authority
    with installer.installation_fences(config):
        verify_installation(config);installer.verify_worker_quiescence(config,reconcile=False)
        registry=AuthorityRegistry(Path(config['authority_path']),owner_uid=0)
        if any(registry.status(Path(p['repo']))['intent']!='maintenance' or MacOSProcesses().owned(p['uid']) for p in config['projects']):
            raise ExecutionBlocked('all protected projects must be quiescent in maintenance')
        project=next(p for p in config['projects'] if p['account']=='_doagain_jp')
        status=registry.status(Path(project['repo']))
        if status['epoch']!=37 or worktree_authority(Path(project['worktree']))!={'repo_head':HEAD,'repo_branch':BRANCH}:
            raise ExecutionBlocked('checkpoint authority or epoch differs')
        if verify_checkpoints(target,config)[project['key']]['commit']!=HEAD:
            raise ExecutionBlocked('retained native tested checkpoint differs')
        ledger=ExecutionLedger.__new__(ExecutionLedger);ledger.path=Path(config['authority_path']);private_root_file(ledger.path)
        intent=ledger.intent(project['key'],REQUEST);validate_intent(intent)
        pending=ledger.pending(project['key'])
        terminal=None
        if not pending:
            observed=ledger.observe_request(project['key'],REQUEST,FP)
            if observed.get('state')!='terminal':raise ExecutionBlocked('original publication is missing')
            terminal=observed['result'];validate_terminal(terminal)
        elif len(pending)!=1 or pending[0]['request_id']!=REQUEST:
            raise ExecutionBlocked('only the original fresh reservation may be pending')
        broker=SimpleNamespace(config=config,project=SimpleNamespace(key=project['key']),
                               state=INSTALL_ROOT/'state'/project['key'],ledger=ledger,lock=threading.RLock())
        api=GitHubRepository('Tran-Steven/jobpipe',read_credential(broker))
        before=ledger.observe_request(project['key'],'offline-3efb655c06613370021a-1-publication',
            '4b34694fa5f16e808412ff157105bea4ad6dc4902035483c56803e49d572e182')
        quarantine=ledger.quarantined_publications(project['key'])
        if before.get('state')!='post_dispatch_uncertain' or [q['request_id'] for q in quarantine]!=['offline-3efb655c06613370021a-1-publication']:
            raise ExecutionBlocked('original43 unresolved quarantine differs')
        audit=broker.state/(REQUEST+'-metadata-completion.json')
        if audit.exists():
            private_root_file(audit)
        def guard():
            if registry.status(Path(project['repo']))!=status or worktree_authority(Path(project['worktree']))!={'repo_head':HEAD,'repo_branch':BRANCH}:
                raise ExecutionBlocked('metadata authority changed before PATCH')
        metadata=complete_metadata(api,audit,proposal,guard,
            execute_approved_action=execute_approved_action and terminal is None)
        if terminal is not None and metadata.get('metadata_state')!='complete':
            raise ExecutionBlocked('terminal metadata changed; no additional PATCH')
        if not execute_approved_action:return metadata
        result=terminal or reconcile_publication(broker,{'operation':'git_publication_reconcile','request_id':REQUEST})
        validate_terminal(result)
        if result.get('state')!='succeeded' or result.get('reconciled_read_only') is not True:
            raise ExecutionBlocked('original fresh publication still lacks exact readback evidence')
        after=ledger.observe_request(project['key'],'offline-3efb655c06613370021a-1-publication',
            '4b34694fa5f16e808412ff157105bea4ad6dc4902035483c56803e49d572e182')
        if after!=before:raise ExecutionBlocked('original43 publication changed')
        completion=broker.state/(REQUEST+'-metadata-completion-result.json')
        completion_record={'proposal':proposal,'state':'terminal','result':result,'original43_unchanged':True}
        if completion.exists():
            private_root_file(completion)
            if json.loads(completion.read_text())!=completion_record:raise ExecutionBlocked('completion record conflicts')
        else:
            fd=os.open(completion,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(completion_record,f,sort_keys=True);f.flush();os.fsync(f.fileno())
            directory=os.open(completion.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
            try:os.fsync(directory)
            finally:os.close(directory)
        return dict(result,original43_unchanged=True,task_completion_claim=False,git_mutations=0)
