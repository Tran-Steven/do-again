"""Scoped JSON control history over trusted GitHub Git-data APIs; never host Git."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from ..core.schema import canonical_json
from .github import GitHubRepository, validate_control_branch
from .macos_execution import ExecutionBlocked, REQUEST_ID
from .publication import read_credential

PATH = re.compile(r'automation/do_again/(?:agent_status\.json|(?:requests|receipts|claims|cancellations|invalid)/[A-Za-z0-9._-]{1,170}\.json|conflicts/[A-Za-z0-9._-]{1,160}/[0-9a-f]{16}\.json)\Z')
SHA = re.compile('[0-9a-f]{40}\\Z')
MAX_JSON = 1024*1024


def valid_path(path, *, write=False):
    if not isinstance(path,str) or not PATH.fullmatch(path) or '..' in path:
        raise ExecutionBlocked('control history path is outside the JSON protocol')
    if write and ('/requests/' in path or '/cancellations/' in path):
        raise ExecutionBlocked('worker cannot author requests or cancellations')
    return path


def blob_sha(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def api_for(broker):
    project=next(p for p in broker.config['projects'] if p['key']==broker.project.key)
    expected={'_doagain_da':'Tran-Steven/do-again','_doagain_jp':'Tran-Steven/jobpipe'}
    if project.get('github_repository') != expected.get(broker.project.account):
        raise ExecutionBlocked('control repository binding is excluded')
    return GitHubRepository(project['github_repository'],read_credential(broker),control=True,
        control_branch=project.get('control_branch','operator-control'))


def gate(broker,epoch):
    from .live_canary import effect_authorized
    status=broker.registry.status(broker.project.repo)
    if (type(epoch) is not int or epoch<1 or status['epoch']!=epoch or status['intent']!='active'
            or not effect_authorized(broker) or not broker._verified()
            or not status.get('goal_revision')):
        raise ExecutionBlocked('control history admission is closed')


def control_branch(api):
    return validate_control_branch(getattr(api,'control_branch','operator-control'))


def snapshot(api):
    ref=api.request('GET','git/ref/heads/'+control_branch(api))
    if not ref or not SHA.fullmatch(ref['object']['sha']):
        raise ExecutionBlocked('approved control branch is unavailable')
    head=ref['object']['sha'];commit=api.request('GET','git/commits/'+head)
    if not commit or commit.get('sha')!=head or not SHA.fullmatch(commit.get('tree',{}).get('sha','')):
        raise ExecutionBlocked('control commit identity differs')
    tree=api.request('GET','git/trees/'+commit['tree']['sha']+'?recursive=1')
    if not tree or tree.get('sha')!=commit['tree']['sha']:
        raise ExecutionBlocked('control tree identity differs')
    if tree.get('truncated') is not False:
        raise ExecutionBlocked('control tree evidence is incomplete')
    entries={}
    for entry in tree['tree']:
        path=entry.get('path')
        if isinstance(path,str) and PATH.fullmatch(path):
            valid_path(path)
            if entry['type']!='blob' or entry['mode']!='100644' or not SHA.fullmatch(entry['sha']):
                raise ExecutionBlocked('control record is not an ordinary JSON blob')
            entries[path]=entry['sha']
    return head,commit,entries


def read_json_blob(api,sha):
    blob=api.request('GET','git/blobs/'+sha)
    if blob.get('encoding')!='base64' or type(blob.get('size')) is not int or not 0<=blob['size']<=MAX_JSON:
        raise ExecutionBlocked('control JSON blob exceeds scope or budget')
    try:
        data=base64.b64decode(blob['content'],validate=False)
        if len(data)!=blob['size'] or blob_sha(data)!=sha:raise ValueError()
        value=json.loads(data)
    except (ValueError,TypeError):raise ExecutionBlocked('control blob identity or JSON is invalid') from None
    if not isinstance(value,dict):raise ExecutionBlocked('control record must be an object')
    return value


def sync_control(broker,packet,*,api=None):
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('control synchronization requires the immutable macOS broker')
    if set(packet)!={'operation','epoch','known'} or packet['operation']!='control_sync':
        raise ExecutionBlocked('control synchronization cannot select a repository or branch')
    known=packet['known']
    if not isinstance(known,dict) or len(known)>5000:raise ExecutionBlocked('control cache exceeds budget')
    for path,sha in known.items():
        valid_path(path)
        if not isinstance(sha,str) or not SHA.fullmatch(sha):raise ExecutionBlocked('invalid control cache identity')
    with broker.lock,broker.admission():
        gate(broker,packet['epoch'])
        from .macos_server import verify_installation
        verify_installation(broker.config)
        api=api or api_for(broker)
        cached=getattr(broker,'control_cache',None)
        binding=(getattr(api,'repository',None),control_branch(api))
        if cached is None or len(cached)!=4 or cached[3]!=binding or time.monotonic()-cached[0]>10:
            head,commit,entries=snapshot(api)
            broker.control_cache=(time.monotonic(),head,entries,binding)
        else:_,head,entries,_=cached
        if getattr(broker,'canary',None) is not None:
            entries=canary_entries(broker,entries)
        if getattr(broker,'codex',None) is not None:
            entries=codex_entries(broker,entries)
        changed=[path for path,sha in entries.items() if known.get(path)!=sha]
        files={};used=0
        for path in sorted(changed)[:16]:
            value=read_json_blob(api,entries[path])
            if getattr(broker,'canary',None) is not None and '/requests/' in path:
                broker.canary.request(value,path)
            if getattr(broker,'codex',None) is not None and '/requests/' in path:
                broker.codex.request(value,path)
            size=len(canonical_json(value))
            if files and used+size>MAX_JSON:break
            used+=size;files[path]={'sha':entries[path],'value':value}
        return {'head':head,'files':files,'removed':sorted(set(known)-set(entries)),
                'complete':len(files)==len(changed)}


def reconcile_control(broker,packet,*,api=None):
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('control reconciliation requires the immutable macOS broker')
    from .macos_server import verify_installation
    verify_installation(broker.config)
    if set(packet)!={'operation','request_id'} or packet['operation']!='control_reconcile':
        raise ExecutionBlocked('control reconciliation accepts original identity only')
    if not isinstance(packet['request_id'],str) or not REQUEST_ID.fullmatch(packet['request_id']):
        raise ExecutionBlocked('invalid control reconciliation identity')
    with broker.lock:
        intent=broker.ledger.intent(broker.project.key,packet['request_id'])
        if not intent or intent.get('operation')!='control_publish':
            raise ExecutionBlocked('original control publication is unavailable')
        api=api or api_for(broker)
        if control_branch(api)!=intent.get('control_branch','operator-control'):
            raise ExecutionBlocked('original control branch changed; reconciliation is blocked')
        head,commit,entries=snapshot(api)
        if (commit.get('message')!=intent['message'] or
                [p['sha'] for p in commit['parents']]!=[intent['base']]
                or entries.get(intent['path'])!=intent['blob']):
            return {'state':'post_dispatch_uncertain','replay':False}
        result={'state':'succeeded','returncode':0,'head':head,'path':intent['path'],
                'blob':intent['blob'],'reconciled_read_only':True}
        broker.ledger.finish(broker.project.key,packet['request_id'],result)
        broker.control_cache=None
        return result


def publish_control(broker,packet,*,api=None):
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('control publication requires the immutable macOS broker')
    if set(packet)!={'operation','request_id','epoch','path','value'} or packet['operation']!='control_publish':
        raise ExecutionBlocked('control publication cannot select refs, commands or credentials')
    valid_path(packet['path'],write=True)
    if not isinstance(packet['request_id'],str) or not REQUEST_ID.fullmatch(packet['request_id']):
        raise ExecutionBlocked('invalid control publication identity')
    if not isinstance(packet['value'],dict):raise ExecutionBlocked('control payload must be an object')
    data=canonical_json(packet['value'])+b'\n'
    if len(data)>MAX_JSON:raise ExecutionBlocked('control payload exceeds budget')
    fingerprint=hashlib.sha256(canonical_json(packet)).hexdigest()
    with broker.lock:
        from .macos_server import verify_installation
        verify_installation(broker.config)
        with broker.admission():
            gate(broker,packet['epoch'])
            api=api or api_for(broker)
            original=broker.ledger.intent(broker.project.key,packet['request_id'])
            if original and control_branch(api)!=original.get('control_branch','operator-control'):
                raise ExecutionBlocked('original control branch changed; terminal evidence is not transferable')
            recovered=broker.ledger.lookup(broker.project.key,packet['request_id'],fingerprint)
            if recovered is not None:return recovered
            base,commit,entries=snapshot(api)
            sha=blob_sha(data)
            if entries.get(packet['path'])==sha:
                return {'state':'succeeded','returncode':0,'head':base,'path':packet['path'],'blob':sha,'already_visible':True}
            if '/claims/' in packet['path'] and packet['path'] in entries:
                raise ExecutionBlocked('existing remote claim cannot be replaced')
            stamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            message='Do Again control '+packet['request_id']
            broker.ledger.reserve(broker.project.key,packet['request_id'],fingerprint,
                intent={'operation':'control_publish','base':base,'path':packet['path'],'blob':sha,'message':message,'control_branch':control_branch(api)})
        @contextmanager
        def effect():
            with broker.admission():
                gate(broker,packet['epoch'])
                yield
        # Reservation precedes every remote mutation. All uncertain outcomes
        # remain started, including a crash before the reference update.
        with effect():
            blob=api.request('POST','git/blobs',{'encoding':'base64','content':base64.b64encode(data).decode()})
            if blob.get('sha')!=sha:raise ExecutionBlocked('remote control blob identity differs')
        with effect():
            tree=api.request('POST','git/trees',{'base_tree':commit['tree']['sha'],'tree':[
                {'path':packet['path'],'mode':'100644','type':'blob','sha':sha}]})
            if not SHA.fullmatch(tree.get('sha','')):raise ExecutionBlocked('invalid control tree identity')
        with effect():
            author={'name':'Do Again','email':'do-again@users.noreply.github.com','date':stamp}
            created=api.request('POST','git/commits',{'message':message,'tree':tree['sha'],'parents':[base],
                                                       'author':author,'committer':author})
            head=created.get('sha','')
            if not SHA.fullmatch(head):raise ExecutionBlocked('invalid control commit identity')
        with effect():
            current=api.request('GET','git/ref/heads/'+control_branch(api))
            if current['object']['sha']!=base:raise ExecutionBlocked('control branch changed before publication')
            api.request('PATCH','git/refs/heads/'+control_branch(api),{'sha':head,'force':False})
            observed=api.request('GET','git/ref/heads/'+control_branch(api))
            if observed['object']['sha']!=head:raise ExecutionBlocked('control reference outcome is uncertain')
        observed_head,observed_commit,observed_entries=snapshot(api)
        if (observed_head!=head or observed_commit.get('message')!=message
                or [p['sha'] for p in observed_commit['parents']]!=[base]
                or observed_commit['tree']['sha']!=tree['sha'] or observed_entries.get(packet['path'])!=sha):
            raise ExecutionBlocked('control publication read-back evidence differs')
        result={'state':'succeeded','returncode':0,'head':head,'path':packet['path'],'blob':sha}
        broker.ledger.finish(broker.project.key,packet['request_id'],result)
        broker.control_cache=None
        return result


def canary_entries(broker,entries):
    result={}
    for path,sha in entries.items():
        if '/requests/' in path:
            try:
                task,stage=broker.canary.task(path.rsplit('/',1)[1][:-5])
                if task==2:broker.canary.second_ready()
                broker.canary.repair_gate(task,stage)
            except (ExecutionBlocked,OSError,ValueError,KeyError):continue
        result[path]=sha
    return result


def codex_entries(broker,entries):
    """Exclude any request not in the explicitly sealed Codex synthetic plan."""
    result={}
    prefix='canary-'+broker.codex.scope.nonce+'-'
    stages={'edit','test','commit','publish','ci'}
    for path,sha in entries.items():
        if path=='automation/do_again/agent_status.json':
            result[path]=sha
            continue
        parts=path.split('/')
        if (len(parts)!=4 or parts[:2]!=['automation','do_again']
                or parts[2] not in {'requests','receipts','claims','cancellations'}
                or not parts[3].endswith('.json')):
            continue
        if parts[2]=='cancellations':
            continue
        rid=parts[3][:-5]
        if not rid.startswith(prefix):
            continue
        suffix=rid[len(prefix):]
        if len(suffix)<3 or suffix[0] not in '12' or suffix[1]!='-' or suffix[2:] not in stages:
            continue
        if suffix[0]=='2':
            try:
                broker.codex.second_ready()
            except (ExecutionBlocked,OSError,ValueError,KeyError):
                continue
        result[path]=sha
    return result
