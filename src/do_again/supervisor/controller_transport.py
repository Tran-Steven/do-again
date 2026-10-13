"""Dedicated controller publication; the worker publisher remains unchanged.

Caller owns the broker lock. Admission is repeated before each trusted GitHub
effect. The original request is reserved by OfflineContinuation first.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager

from ..core.schema import canonical_json
from .control_history import blob_sha, snapshot, read_json_blob, SHA
from .macos_execution import ExecutionBlocked


class ControllerHistory:
    def __init__(self, controller, api, admission, *, profile=None):
        self.controller, self.api, self.admission = controller, api, admission
        self.profile=profile

    @contextmanager
    def effect(self):
        with self.admission():
            if (self.api.repository != 'Tran-Steven/jobpipe'
                    or self.api.control_branch != 'operator-control'):
                raise ExecutionBlocked('controller transport repository/branch differs')
            yield

    def snapshot(self):
        with self.effect(): return snapshot(self.api)

    def pending(self):
        _, _, entries = self.snapshot()
        requests = {p.split('/')[-1][:-5] for p in entries if '/requests/' in p}
        receipts = {p.split('/')[-1][:-5] for p in entries if '/receipts/' in p}
        return sorted(requests-receipts)

    def publish_reserved(self, now):
        state = self.controller.status(now)
        if state['state'] != 'waiting' or not state['outstanding']:
            raise ExecutionBlocked('controller has no active publication reservation')
        record = state['requests'][-1]
        value = record['request']; rid = value['request_id']
        path = 'automation/do_again/requests/'+rid+'.json'
        data = canonical_json(value)+b'\n'; sha = blob_sha(data)
        base, commit, entries = self.snapshot()
        if path in entries:
            if entries[path] != sha:
                raise ExecutionBlocked('reserved request conflicts with durable remote content')
            with self.effect(): observed = read_json_blob(self.api, sha)
            self.controller.publication(observed, now)
            return {'request_id': rid, 'blob': sha, 'already_visible': True}
        pending = {p.split('/')[-1][:-5] for p in entries if '/requests/' in p}
        completed = {p.split('/')[-1][:-5] for p in entries if '/receipts/' in p}
        if pending-completed:
            raise ExecutionBlocked('another remote request is outstanding')
        if record.get('publication_started'):
            self.controller.block('publication uncertain and original request absent; no replay', now)
            raise ExecutionBlocked('publication uncertain and original request absent; no replay')
        record['publication_started'] = True
        self.controller._save(state)  # Durable intent precedes first API mutation.
        with self.effect(): blob = self.api.request('POST', 'git/blobs', {
            'encoding': 'base64', 'content': base64.b64encode(data).decode()})
        if blob.get('sha') != sha: raise ExecutionBlocked('controller blob identity differs')
        with self.effect(): tree = self.api.request('POST', 'git/trees', {
            'base_tree': commit['tree']['sha'], 'tree': [
                {'path': path, 'mode': '100644', 'type': 'blob', 'sha': sha}]})
        if not SHA.fullmatch(tree.get('sha', '')): raise ExecutionBlocked('invalid controller tree')
        message = 'Do Again authorized controller request '+rid
        with self.effect(): created = self.api.request('POST', 'git/commits', {
            'message': message, 'tree': tree['sha'], 'parents': [base]})
        head = created.get('sha', '')
        if not SHA.fullmatch(head): raise ExecutionBlocked('invalid controller commit')
        with self.effect():
            current = self.api.request('GET', 'git/ref/heads/operator-control')
            if current['object']['sha'] != base:
                raise ExecutionBlocked('control ref moved; publication remains uncertain')
            self.api.request('PATCH', 'git/refs/heads/operator-control', {'sha': head, 'force': False})
        observed_head, observed_commit, observed_entries = self.snapshot()
        if (observed_head != head or observed_entries.get(path) != sha
                or observed_commit.get('message') != message
                or [p['sha'] for p in observed_commit['parents']] != [base]):
            raise ExecutionBlocked('controller publication readback differs')
        self.controller.publication(value, now)
        return {'request_id': rid, 'blob': sha, 'head': head, 'already_visible': False}

    def completion(self, now, native_verify):
        state = self.controller.status(now)
        if not state['outstanding']: return None
        record = state['requests'][-1]
        path = 'automation/do_again/receipts/'+state['outstanding']+'.json'
        _, _, entries = self.snapshot()
        if path not in entries: return None
        with self.effect(): receipt = read_json_blob(self.api, entries[path])
        if blob_sha(canonical_json(receipt)+b'\n') != entries[path]:
            raise ExecutionBlocked('receipt canonical blob differs')
        # Trusted implementation must verify native ledger and exact-source
        # artifacts, never copy completion assertions from model/worker stdout.
        from ..service.offline_continuation import persistent_scope
        if persistent_scope(self.controller.scope) and receipt.get('state')=='failed':
            failure=native_verify.failed(record,receipt)
            return self.controller.failed(receipt,failure=failure,now=now)
        evidence = native_verify(record, receipt)
        accepted=self.controller.accept(receipt, evidence=evidence, now=now)
        if self.profile is not None and self.profile.get('kind')=='native-development-local-v2':
            # This profile authorizes precisely one original amended task. Extra
            # budget may cover proven invalid replies/failures, never unrelated
            # follow-on work. A successful receipt closes admission automatically.
            accepted.update(state='completed',completion_reason='approved original task completed')
            return self.controller._save(accepted)
        return accepted

    def acknowledgments(self,now):
        """Audit acknowledgment through existing agent_status, without a fourth request."""
        from ..service.offline_continuation import digest
        state=self.controller.status(now)
        if not state['acknowledged'] or state['state'] in {'stopped','blocked'}:return
        value={'scope_sha256':digest(self.controller.scope),'source_sha':self.controller.scope['source_sha'],
            'acknowledged_receipts':state['acknowledged'],'state':'completed' if state['state']=='completed' else 'in_progress'}
        original=state.get('ack_publication',{})
        if original.get('value')==value and original.get('phase')=='committed':return
        path='automation/do_again/agent_status.json'
        base,commit,entries=self.snapshot()
        current={}
        if path in entries:
            with self.effect():current=read_json_blob(self.api,entries[path])
        if current.get('controller_ack')==value:
            state['ack_publication']={'value':value,'phase':'committed','head':base}
            self.controller._save(state);return
        if original.get('value')==value and original.get('phase')=='started':
            self.controller.block('acknowledgment publication uncertain; no replay',now)
            raise ExecutionBlocked('acknowledgment publication uncertain; no replay')
        state['ack_publication']={'value':value,'phase':'started'}
        self.controller._save(state)
        data=canonical_json(dict(current,controller_ack=value))+b'\n';sha=blob_sha(data)
        with self.effect():blob=self.api.request('POST','git/blobs',{
            'encoding':'base64','content':base64.b64encode(data).decode()})
        if blob.get('sha')!=sha:raise ExecutionBlocked('acknowledgment blob differs')
        with self.effect():tree=self.api.request('POST','git/trees',{'base_tree':commit['tree']['sha'],
            'tree':[{'path':path,'mode':'100644','type':'blob','sha':sha}]})
        with self.effect():created=self.api.request('POST','git/commits',{'tree':tree['sha'],
            'parents':[base],'message':'Do Again verified receipt acknowledgment '+state['acknowledged'][-1]})
        head=created.get('sha','')
        if not SHA.fullmatch(head):raise ExecutionBlocked('acknowledgment commit invalid')
        with self.effect():
            reference=self.api.request('GET','git/ref/heads/operator-control')
            if reference['object']['sha']!=base:raise ExecutionBlocked('acknowledgment base moved; no replay')
            self.api.request('PATCH','git/refs/heads/operator-control',{'sha':head,'force':False})
        observed,_,visible=self.snapshot()
        if observed!=head or visible.get(path)!=sha:raise ExecutionBlocked('acknowledgment readback differs')
        state['ack_publication']={'value':value,'phase':'committed','head':head}
        self.controller._save(state)
