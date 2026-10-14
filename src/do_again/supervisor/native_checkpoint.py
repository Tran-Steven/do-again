"""Read-only upgrade admission for an already tested protected native head."""
import re
import stat
from pathlib import Path
from .macos_execution import ExecutionBlocked, ExecutionLedger


def verify_claim(claim, original, authority, measurement, *, execution_uid=402):
    fields={'request_id','request_fingerprint','source_sha','commit','tree_sha256'}
    if (not isinstance(claim,dict) or set(claim)!=fields
            or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{7,159}',str(claim['request_id']))
            or any(not re.fullmatch('[0-9a-f]{64}',str(claim[k])) for k in ('request_fingerprint','tree_sha256'))
            or any(not re.fullmatch('[0-9a-f]{40}',str(claim[k])) for k in ('source_sha','commit'))):
        raise ExecutionBlocked('native head checkpoint identity invalid')
    result=original.get('result')
    if (original.get('state')!='terminal' or original.get('source_sha')!=claim['source_sha']
            or original.get('request_fingerprint')!=claim['request_fingerprint']
            or not isinstance(result,dict) or result.get('source_sha')!=claim['source_sha']
            or result.get('returncode')!=0 or result.get('timed_out') is not False):
        raise ExecutionBlocked('native checkpoint has no original successful terminal evidence')
    commit=result.get('commit',{})
    if (commit.get('authority')!=authority or commit.get('returncode')!=0
            or commit.get('tested_tree_sha256')!=claim['tree_sha256']
            or result.get('tested_tree_sha256')!=claim['tree_sha256']
            or result.get('execution_uid')!=execution_uid or not result.get('tests')
            or any(t.get('returncode')!=0 or t.get('timed_out') is not False for t in result['tests'])
            or authority.get('repo_head')!=claim['commit']
            or not str(authority.get('repo_branch','')).startswith('do-again/task-')
            or measurement!={'commit':claim['commit'],'clean':True,'tree_sha256':claim['tree_sha256']}):
        raise ExecutionBlocked('native checkpoint head, tested tree or source cleanliness differs')
    return True


STAGED_KIND='native-staged-tested-head-v1'
CLAIM_FIELDS={'request_id','request_fingerprint','source_sha','commit','tree_sha256'}


def verify_staged_claim(claim, stages, intents, authority, measurement, *, profile, execution_uid):
    """Retain a tested commit without claiming publication or task completion."""
    if (not isinstance(claim,dict) or set(claim)!=CLAIM_FIELDS|{'kind'} or claim['kind']!=STAGED_KIND
            or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{7,159}',str(claim['request_id']))
            or any(not re.fullmatch('[0-9a-f]{64}',str(claim[k])) for k in ('request_fingerprint','tree_sha256'))
            or any(not re.fullmatch('[0-9a-f]{40}',str(claim[k])) for k in ('source_sha','commit'))):
        raise ExecutionBlocked('staged checkpoint identity differs')
    if profile.get('kind')!='native-development-autonomous-v1':
        raise ExecutionBlocked('staged checkpoint requires autonomous native profile')
    values={}
    for name,operation in [('snapshot','execute'),('implementation','execute'),('test','execute'),('commit','git_commit')]:
        observed=stages.get(name,{})
        value=observed.get('result')
        if (observed.get('state')!='terminal' or observed.get('source_sha')!=claim['source_sha']
                or observed.get('request_fingerprint')!=claim['request_fingerprint']
                or not isinstance(value,dict) or value.get('source_sha')!=claim['source_sha']
                or type(value.get('returncode')) is not int or value['returncode']!=0
                or value.get('timed_out',False) is not False
                or (intents.get(name) or {}).get('operation')!=operation):
            raise ExecutionBlocked('staged checkpoint lacks original terminal stages')
        values[name]=value
    snapshot=values['snapshot'].get('baseline',{});implementation=values['implementation']
    commit=values['commit'];run=values['test'].get('native_test_run',{});cases=run.get('cases',[])
    counts_valid=bool(cases) and all(isinstance(c,dict) and isinstance(c.get('native_test_counts'),dict)
        and type(c['native_test_counts'].get('collected')) is int
        and c['native_test_counts'].get('collected')==c['native_test_counts'].get('passed')
        and type(c['native_test_counts'].get('passed')) is int and c['native_test_counts']['passed']>0
        and all(type(c['native_test_counts'].get(k)) is int and c['native_test_counts'][k]==0
                for k in ('failed','skipped','pytest_exit_code')) for c in cases)
    if (not cases or any(not isinstance(c,dict) for c in cases)
            or snapshot.get('clean') is not True or snapshot.get('commit')!=commit.get('base_sha')
            or implementation.get('execution_uid')!=execution_uid or implementation.get('network_denied') is not True
            or set(implementation.get('source_write_paths',[]))!=set(profile['allowed_paths'])
            or commit.get('execution_uid')!=execution_uid or commit.get('authority')!=authority
            or not commit.get('paths') or not set(commit['paths'])<=set(profile['allowed_paths'])
            or not set(commit['paths'])&set(profile['implementation_paths'])
            or authority.get('repo_head')!=claim['commit'] or commit.get('base_sha')==claim['commit']
            or not str(authority.get('repo_branch','')).startswith('do-again/task-')
            or run.get('commit_before')!=commit.get('base_sha') or run.get('commit_after')!=commit.get('base_sha')
            or run.get('tree_before')!=claim['tree_sha256'] or run.get('tree_after')!=claim['tree_sha256']
            or commit.get('tested_tree_sha256')!=claim['tree_sha256'] or run.get('network_denied') is not True
            or [c.get('case') for c in cases]!=profile['test_cases']
            or any(type(c.get('exit_code')) is not int or c['exit_code']!=0 or c.get('timed_out') is not False for c in cases)
            or (profile['test_runner']=='pytest' and not counts_valid)
            or measurement!={'commit':claim['commit'],'clean':True,'tree_sha256':claim['tree_sha256']}):
        raise ExecutionBlocked('staged checkpoint tested tree, cases, confinement or current authority differs')
    return True


def require_checkpoint_metadata(path, *, operator_uid):
    """The existing Git broker may retain operator-owned protected metadata."""
    root=Path(path)
    for item in (root,root/'HEAD'):
        info=item.lstat()
        if (item.is_symlink() or info.st_uid not in {0,operator_uid}
                or info.st_mode & 0o022 or (item==root and not stat.S_ISDIR(info.st_mode))
                or (item!=root and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1))):
            raise ExecutionBlocked('retained Git metadata has an unapproved owner, alias or write boundary')


def verify_checkpoints(config, previous):
    claims=config.get('native_head_checkpoints',{})
    if not claims:return {}
    if not isinstance(claims,dict) or len(claims)!=1 or previous is None:
        raise ExecutionBlocked('native checkpoint requires one existing protected project')
    from .macos_server import worktree_authority
    from .git_manifest import tree_manifest
    from .native_test_evidence import measure_committed_tree
    from .macos_execution import private_root_file
    ledger=ExecutionLedger.__new__(ExecutionLedger)
    ledger.path=Path(previous['authority_path'])
    private_root_file(ledger.path)
    verified={}
    for key,claim in claims.items():
        project=next((p for p in config['projects'] if p['key']==key),None)
        prior=next((p for p in previous['projects'] if p['key']==key),None)
        if (project is None or prior is None or project['account']!='_doagain_jp'
                or any(project[k]!=prior[k] for k in ('uid','gid','repo','worktree','source_sha'))):
            raise ExecutionBlocked('native checkpoint cannot change project identities or snapshots')
        # Validate the identity before using any claim field in a ledger lookup.
        if not isinstance(claim,dict) or set(claim) not in (CLAIM_FIELDS,CLAIM_FIELDS|{'kind'}):
            raise ExecutionBlocked('native checkpoint fields differ')
        for field in ('request_fingerprint','tree_sha256'):
            if not re.fullmatch('[0-9a-f]{64}',str(claim[field])):raise ExecutionBlocked('native checkpoint digest invalid')
        for field in ('source_sha','commit'):
            if not re.fullmatch('[0-9a-f]{40}',str(claim[field])):raise ExecutionBlocked('native checkpoint SHA invalid')
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{7,159}',str(claim['request_id'])):
            raise ExecutionBlocked('native checkpoint request invalid')
        staged=claim.get('kind')==STAGED_KIND
        if 'kind' in claim and not staged:raise ExecutionBlocked('unknown native checkpoint kind')
        original=ledger.observe_request(key,claim['request_id'],claim['request_fingerprint'])
        root=Path(project['worktree']);metadata=root/'.git'
        require_checkpoint_metadata(metadata,operator_uid=config['operator_uid'])
        authority=worktree_authority(root)
        manifest=tree_manifest(Path(config['git']),metadata,claim['commit'])
        for name,blob in config.get('controller_profile',{}).get('test_files',{}).items():
            if manifest.get(name,{}).get('blob_sha')!=blob:
                raise ExecutionBlocked('retained native baseline tests differ from the newly sealed profile')
        measured=measure_committed_tree(root,commit=claim['commit'],manifest=manifest,
            read_authority=lambda:worktree_authority(root)['repo_head'])
        if staged:
            names=('snapshot','implementation','test','commit')
            stages={name:ledger.observe_request(key,claim['request_id']+'-'+name,claim['request_fingerprint']) for name in names}
            intents={name:ledger.intent(key,claim['request_id']+'-'+name) for name in names}
            verify_staged_claim(claim,stages,intents,authority,measured,
                profile=previous['controller_profile'],execution_uid=project['uid'])
        else:
            verify_claim(claim,original,authority,measured,execution_uid=project["uid"])
        verified[key]=dict(claim,verified=True)
    return verified
