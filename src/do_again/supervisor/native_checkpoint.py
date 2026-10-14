"""Read-only upgrade admission for an already tested protected native head."""
import re
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
        if not isinstance(claim,dict) or set(claim)!={'request_id','request_fingerprint','source_sha','commit','tree_sha256'}:
            raise ExecutionBlocked('native checkpoint fields differ')
        for field in ('request_fingerprint','tree_sha256'):
            if not re.fullmatch('[0-9a-f]{64}',str(claim[field])):raise ExecutionBlocked('native checkpoint digest invalid')
        for field in ('source_sha','commit'):
            if not re.fullmatch('[0-9a-f]{40}',str(claim[field])):raise ExecutionBlocked('native checkpoint SHA invalid')
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{7,159}',str(claim['request_id'])):
            raise ExecutionBlocked('native checkpoint request invalid')
        original=ledger.observe_request(key,claim['request_id'],claim['request_fingerprint'])
        root=Path(project['worktree']);metadata=root/'.git'
        private_root_file(metadata/'HEAD')
        authority=worktree_authority(root)
        manifest=tree_manifest(Path(config['git']),metadata,claim['commit'])
        measured=measure_committed_tree(root,commit=claim['commit'],manifest=manifest,
            read_authority=lambda:worktree_authority(root)['repo_head'])
        verify_claim(claim,original,authority,measured,execution_uid=project["uid"])
        verified[key]=dict(claim,verified=True)
    return verified
