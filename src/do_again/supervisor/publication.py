"""Project-bound publication of a broker-owned commit, never arbitrary Git/network."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from ..core.schema import canonical_json
from .git_broker import validate_packet
from .github import GitHubRepository, publish_commit, matching_pull_requests, branch_pull_requests, validate_publication_paths
from .macos_execution import EXECUTION_ROOT, INSTALL_ROOT, REQUEST_ID, ExecutionBlocked, MacOSProcesses, capture, launch_spec, private_root_file


def read_credential(broker) -> str:
    credential = broker.state / 'github-token'
    private_root_file(credential)
    if credential.stat().st_mode & 0o077 or credential.stat().st_size > 256:
        raise ExecutionBlocked('publication credential is not private')
    return credential.read_text().strip()


def publish_via_broker(broker, packet: dict) -> dict:
    from .live_canary import effect_authorized
    from .macos_server import verify_installation, worktree_authority
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('publication requires the immutable macOS broker')
    if set(packet) != {'operation','request_id','expected_head','expected_epoch','title','body','request_fingerprint'} or packet['operation'] != 'git_publish':
        raise ExecutionBlocked('publication cannot select a repository, ref, credential or API operation')
    validate_packet({k:v for k,v in packet.items() if k not in {'operation','title','body'}} |
                    {'operation':'git_commit','paths':['placeholder'],'message':'publication'})
    if (not isinstance(packet['title'],str) or not 1 <= len(packet['title'].strip()) <= 200
            or not isinstance(packet['body'],str) or len(packet['body'].encode())>16000):
        raise ExecutionBlocked('invalid pull request content')
    project_config = next(p for p in broker.config['projects'] if p['key']==broker.project.key)
    repository = project_config.get('github_repository')
    expected = {'_doagain_da':'Tran-Steven/do-again','_doagain_jp':'Tran-Steven/jobpipe'}
    if repository != expected.get(broker.project.account):
        raise ExecutionBlocked('publication repository is not sealed for this project')
    api = GitHubRepository(repository,read_credential(broker))
    fingerprint = hashlib.sha256(canonical_json(packet)).hexdigest()
    with broker.lock:
        def gate():
            status = broker.registry.status(broker.project.repo)
            branch = 'do-again/task-' + hashlib.sha256((broker.project.key+':'+str(status.get('goal_revision'))).encode()).hexdigest()[:20]
            authority = worktree_authority(broker.project.worktree)
            if (status['intent']!='active' or status['epoch']!=packet['expected_epoch']
                    or not status.get('goal_revision') or not effect_authorized(broker)
                    or not broker._verified() or authority != {'repo_head':packet['expected_head'],'repo_branch':branch}):
                raise ExecutionBlocked('authority or broker-owned branch blocks publication')
            return branch
        verify_installation(broker.config)
        with broker.admission():
            branch = gate()
            recovered = broker.ledger.lookup(broker.project.key,packet['request_id'],fingerprint)
            if recovered is not None:return recovered
            if MacOSProcesses().owned(broker.project.uid):
                raise ExecutionBlocked('dedicated identity has unresolved processes')
            broker.ledger.reserve(broker.project.key,packet['request_id'],fingerprint,
                    intent={'operation':'git_publish','repository':repository,'head':packet['expected_head'],
                            'branch':branch,'source_sha':broker.config['source_sha'],
                            'title':packet['title'],'body':packet['body'],
                            'request_fingerprint':packet['request_fingerprint']})
        root = EXECUTION_ROOT / broker.project.key / 'requests' / packet['request_id']
        scratch, cache = root/'scratch',root/'cache'
        for path in (scratch,cache):
            path.mkdir(parents=True,mode=0o700);os.chown(path,broker.project.uid,broker.project.gid)
        for path in (root,root.parent):path.chmod(0o711)
        @contextmanager
        def admission():
            with broker.admission():
                gate()
                yield
        try:
            code = ('import sys;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
                    'from do_again.supervisor.git_export import export_commit;from pathlib import Path;import json;'
                    'print(json.dumps(export_commit(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3])))')
            packet_execute = {'operation':'execute','request_id':packet['request_id'],'cwd':'.','timeout':60,
                              'argv':[broker.config['python'],'-I','-S','-B','-c',code,broker.config['git'],
                                      str(broker.project.worktree/'.git'),packet['expected_head']]}
            outcome = capture(launch_spec(broker.project,packet_execute,scratch,cache),60,start_guard=admission)
            if outcome['returncode'] or outcome['timed_out'] or outcome.get('stdout_truncated'):
                raise ExecutionBlocked('bounded commit export failed')
            export = json.loads(outcome['stdout'])
            if export['head'] != packet['expected_head']:raise ExecutionBlocked('exported commit changed')
            validate_publication_paths(export)
            if getattr(broker,'canary',None) is not None:broker.canary.export(export)
        except Exception:
            result = {'operation':'git_publish','returncode':1,'state':'failed_pre_publication',
                      'error':'local publication preparation failed','source_sha':broker.config['source_sha']}
            broker.ledger.finish(broker.project.key,packet['request_id'],result)
            return result
        # Started is already durable. Any network exception, lost branch/PR
        # response or missing read-back evidence retains it and cannot replay.
        evidence = publish_commit(api,export,branch,packet['title'],packet['body'],effect_guard=admission)
        result = {'operation':'git_publish','returncode':0,'state':'succeeded',
                  'source_sha':broker.config['source_sha'],**evidence}
        broker.ledger.finish(broker.project.key,packet['request_id'],result)
        return result


def reconcile_publication(broker, packet: dict) -> dict:
    """Original bound evidence only; this operation never dispatches a mutation."""
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('publication reconciliation requires the immutable macOS broker')
    if (set(packet)!={'operation','request_id'} or packet['operation']!='git_publication_reconcile'
            or not isinstance(packet['request_id'],str) or not REQUEST_ID.fullmatch(packet['request_id'])):
        raise ExecutionBlocked('invalid publication reconciliation identity')
    from .macos_server import verify_installation
    with broker.lock:
        verify_installation(broker.config)
        intent=broker.ledger.intent(broker.project.key,packet['request_id'])
        pending={item['request_id']:item for item in broker.ledger.pending(broker.project.key)}
        if intent is None or intent.get('operation')!='git_publish':
            raise ExecutionBlocked('original publication intent is unavailable')
        if packet['request_id'] not in pending:
            return {'state':'terminal','replay':False}
        configured=next(p for p in broker.config['projects'] if p['key']==broker.project.key)
        if configured.get('github_repository')!=intent['repository']:
            raise ExecutionBlocked('publication binding changed; reconciliation is blocked')
        api=GitHubRepository(intent['repository'],read_credential(broker))
        reference=api.request('GET','git/ref/heads/'+intent['branch'])
        matches=matching_pull_requests(api,intent['branch'],intent['head'])
        if (reference is None or reference['object']['sha']!=intent['head'] or len(matches)!=1
                or matches[0]['state']!='open'
                or matches[0].get('title')!=intent['title'] or matches[0].get('body')!=intent['body']
                or matches[0]['html_url']!='https://github.com/'+intent['repository']+'/pull/'+str(matches[0]['number'])):
            return {'state':'post_dispatch_uncertain','replay':False,'request_id':packet['request_id']}
        result={'operation':'git_publish','state':'succeeded','returncode':0,
                'source_sha':intent['source_sha'],'repository':intent['repository'],'head':intent['head'],
                'branch':intent['branch'],'pull_request':matches[0]['number'],'url':matches[0]['html_url'],
                'reconciled_read_only':True}
        broker.ledger.finish(broker.project.key,packet['request_id'],result)
        return result


def classify_canary_partial_publication(broker):
    """Explicit administrator disposition, never automatic recovery or success."""
    from ..core.schema import atomic_json
    from .macos_server import worktree_authority
    if sys.platform!='darwin' or os.geteuid()!=0 or getattr(broker,'canary',None) is None:
        raise ExecutionBlocked('partial classification requires an administrator and sealed canary')
    with broker.lock,broker.admission():
        if (broker.config.get('production_ready') is not False
                or broker.registry.status(broker.project.repo)['intent']!='maintenance'
                or MacOSProcesses().owned(broker.project.uid)):
            raise ExecutionBlocked('partial classification requires withdrawn quiescent maintenance')
        rid=broker.canary.rid(2,'publish');prior=broker.canary.rid(1,'publish')
        original=broker.ledger.intent(broker.project.key,rid)
        previous=broker.ledger.intent(broker.project.key,prior)
        pending=broker.ledger.pending(broker.project.key)
        if ([row['request_id'] for row in pending]!=[rid] or not original or not previous
                or any(v.get('operation')!='git_publish' or v.get('source_sha')!=broker.config['source_sha']
                       for v in (original,previous))
                or original['branch']!=previous['branch'] or original['repository']!=previous['repository']
                or original['head']==previous['head']):
            raise ExecutionBlocked('only the original second canary publication may be classified')
        before=broker.ledger.observe_request(broker.project.key,prior,previous['request_fingerprint'])
        if (before.get('state')!='terminal' or before['result'].get('state')!='succeeded'
                or before['result'].get('returncode')!=0
                or before['result'].get('source_sha')!=broker.config['source_sha']
                or before['result'].get('head')!=previous['head']):
            raise ExecutionBlocked('original successful canary PR evidence is required')
        api=GitHubRepository(original['repository'],read_credential(broker))
        def evidence():
            ref=api.request('GET','git/ref/heads/'+original['branch'])
            commit=api.request('GET','git/commits/'+original['head'])
            matches=branch_pull_requests(api,original['branch'])
            if (not ref or ref['object']['sha']!=original['head'] or not commit
                    or [p['sha'] for p in commit['parents']]!=[previous['head']]
                    or len(matches)!=1 or matches[0]['head']['sha']!=original['head']):
                raise ExecutionBlocked('partial publication lacks exact branch/parent/PR evidence')
            pr=api.request('GET','pulls/'+str(matches[0]['number']))
            if (not pr or pr['number']!=before['result']['pull_request'] or pr.get('draft') is not True
                    or pr['state']!='open' or pr['head']['sha']!=original['head']
                    or pr['head']['ref']!=original['branch'] or pr['head']['repo']['full_name']!=original['repository']
                    or pr['base']['repo']['full_name']!=original['repository'] or pr['base']['ref']!='main'
                    or pr['title']!=previous['title'] or pr['body']!=previous['body']
                    or (pr['title'],pr['body'])==(original['title'],original['body'])):
                raise ExecutionBlocked('partial classification requires the unchanged original draft metadata')
            return {'head':original['head'],'pull_request':pr['number'],'title':pr['title'],'body':pr['body']}
        observed=evidence()
        if (worktree_authority(broker.project.worktree)['repo_head']!=original['head']
                or evidence()!=observed):
            raise ExecutionBlocked('partial evidence changed during administrator classification')
        result={'operation':'git_publish','state':'blocked_partial_publication','returncode':1,
                'source_sha':broker.config['source_sha'],'replay':False,'administrator_classified':True,
                'engineering_complete':False,'repository':original['repository'],**observed}
        atomic_json(broker.state/'canary-partial-publication-disposition.json',
                    {'original_intent':original,'previous_intent':previous,'result':result})
        broker.ledger.finish(broker.project.key,rid,result)
        return result
