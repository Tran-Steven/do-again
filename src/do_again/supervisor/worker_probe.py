"""Installed maintenance qualification of the native Agent/executor/history loop.

This uses synthetic requests and an in-memory control repository. It does not
start a production service, send browser messages, or qualify live acceptance.
"""
from __future__ import annotations
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from ..core.schema import atomic_json, canonical_json, utc_now
from .macos_execution import EXECUTION_ROOT, ExecutionBlocked, MacOSProcesses


def qualify_worker(broker):
    from .capability_probe import qualification_gate
    from .macos_server import ProjectBroker, machine_identity, worktree_authority
    from .qualification_control import FixtureControlRepository
    from .control_history import blob_sha, sync_control, publish_control, reconcile_control, api_for, snapshot
    from ..core.agent import Agent
    from ..core.broker_executor import BrokerExecutor
    from ..core.control_transport import BrokerControlHistory
    with broker.lock:
        before=qualification_gate(broker)
        identity=machine_identity(broker.config)
        nonce=hashlib.sha256(canonical_json(identity)).hexdigest()[:24]
        journal=broker.state/('worker-qualification-'+nonce+'.json')
        if journal.exists():
            prior=json.loads(journal.read_text())
            if prior['identity']!=identity or prior['before']!=before or prior['phase']!='complete':
                raise ExecutionBlocked('interrupted worker qualification requires evidence review; no replay')
            return prior['result']
        if broker.ledger.pending(broker.project.key):
            raise ExecutionBlocked('unresolved execution blocks worker qualification')
        live_head,_,live_entries=snapshot(api_for(broker))
        live_control={'head':live_head,'records':len(live_entries),'repository':next(
            p['github_repository'] for p in broker.config['projects'] if p['key']==broker.project.key),
            'read_only':True}
        session={'identity':identity,'before':before,'phase':'preparation_started','live_control':live_control}
        atomic_json(journal,session)
        root=EXECUTION_ROOT/broker.project.key/'worker-probes'/nonce
        root.mkdir(parents=True,mode=0o711);root.chmod(0o711);root.parent.chmod(0o711)
        work=root/'worktree';work.mkdir(mode=0o700)
        os.chown(work,broker.project.uid,broker.project.gid)
        metadata=work/'.git';metadata.mkdir(mode=0o755)
        atomic_head=before['authority']['repo_head']
        (metadata/'HEAD').write_text(atomic_head+'\n');(metadata/'HEAD').chmod(0o644)
        private_root=root/'trusted-state';private_root.mkdir(mode=0o700)
        control=private_root/'control';control.mkdir()
        state=private_root/'agent';state.mkdir()
        policy=private_root/'policy.json'
        atomic_json(policy,{'allowed_operations':['scratch_script'],'default_timeout_seconds':20})
        status={'intent':'active','epoch':1,'goal_revision':'synthetic-worker-'+nonce}
        @contextmanager
        def guard():
            with broker.probe_admission():
                if (broker.registry.status(broker.project.repo)!=before['operator']
                        or broker.config.get('production_ready') is not False or not broker._verified()):
                    raise ExecutionBlocked('live maintenance authority changed during qualification')
                yield
        # Root-only fixture authority; all native effects still acquire the real
        # maintenance fence. No socket operation can select these overrides.
        private=object.__new__(ProjectBroker)
        private.project=replace(broker.project,worktree=work)
        private.config=dict(broker.config,production_ready=True)
        private.registry=SimpleNamespace(status=lambda repo:dict(status))
        private.state=private_root;private.ledger=broker.ledger
        private.lock=threading.Lock();private.admission=guard;private._verified=broker._verified
        private._probe_blocker=lambda:None
        remote=FixtureControlRepository();executions=[]
        def rpc(repo,packet):
            if Path(repo)!=broker.project.repo:raise ExecutionBlocked('synthetic project identity differs')
            if packet['operation']=='control_sync':return sync_control(private,packet,api=remote)
            if packet['operation']=='control_publish':return publish_control(private,packet,api=remote)
            if packet['operation']=='execute':executions.append(packet['request_id'])
            return private.dispatch(packet,broker.config['operator_uid'])
        def check():
            with guard():
                if status['intent']!='active':raise ExecutionBlocked('synthetic authority is closed')
        tasks=[('calculation.py','def total(x,y): return x+y\n'),
               ('test_calculation.py','import unittest,calculation\nclass Test(unittest.TestCase):\n def test_total(self): self.assertEqual(calculation.total(2,3),5)\n'),
               ('README.md','Synthetic qualification: addition and a passing regression.\n')]
        for index,(filename,content) in enumerate(tasks):
            now=utc_now();rid='worker-proof-'+nonce+'-'+str(index)
            script='from pathlib import Path;Path('+repr(filename)+').write_text('+repr(content)+')'
            if index==2:
                script+=';import unittest;result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromName("test_calculation"));assert result.wasSuccessful()'
            request={'schema_version':1,'request_id':rid,'issued_at_utc':now.isoformat(),
                'expires_at_utc':(now+timedelta(minutes=10)).isoformat(),'operation':'scratch_script',
                'args':{'language':'python','content':script},'expected':{'repo_head':atomic_head},
                'limits':{'timeout_seconds':20}}
            data=canonical_json(request)+b'\n';sha=blob_sha(data)
            remote.blobs[sha]=data;remote.trees[remote.tree]['automation/do_again/requests/'+rid+'.json']=sha
        session['phase']='execution_started';atomic_json(journal,session)
        for _ in range(2):
            agent=Agent(repo=broker.project.repo,control_worktree=control,branch='operator-control',
                policy_path=policy,state_dir=state,admission_check=check,
                executor=BrokerExecutor(repo=broker.project.repo,policy_path=policy,state_dir=state,rpc=rpc),
                control_transport=BrokerControlHistory(broker.project.repo,control,1,rpc=rpc))
            if agent.run(once=True)!=0:raise ExecutionBlocked('native worker loop did not complete')
        receipts=[json.loads(p.read_text()) for p in (control/'automation/do_again/receipts').glob('*.json')]
        if (len(executions)!=3 or len(receipts)!=3 or any(r['state']!='succeeded' for r in receipts)
                or not (work/'README.md').is_file()):
            raise ExecutionBlocked('native worker task or restart evidence differs')
        # Simulate a lost control-ref response. Only read-only reconciliation
        # may resolve the original effect; a restart cannot send it again.
        packet={'operation':'control_publish','request_id':'worker-uncertain-'+nonce,'epoch':1,
            'path':'automation/do_again/agent_status.json','value':{'state':'synthetic_uncertainty'}}
        remote.lose_patch=True
        try:publish_control(private,packet,api=remote)
        except ExecutionBlocked:pass
        else:raise ExecutionBlocked('synthetic lost-response boundary was not exercised')
        count=len(remote.writes)
        try:publish_control(private,packet,api=remote)
        except ExecutionBlocked:pass
        else:raise ExecutionBlocked('uncertain control publication was replayed')
        if len(remote.writes)!=count:raise ExecutionBlocked('uncertain publication triggered another mutation')
        if reconcile_control(private,{'operation':'control_reconcile','request_id':packet['request_id']},api=remote).get('state')!='succeeded':
            raise ExecutionBlocked('original synthetic control publication did not reconcile')
        status['intent']='paused'
        try:publish_control(private,dict(packet,request_id='worker-paused-'+nonce),api=remote)
        except ExecutionBlocked:pass
        else:raise ExecutionBlocked('synthetic pause admitted a control effect')
        if (len(remote.writes)!=count or broker.ledger.pending(broker.project.key)
                or MacOSProcesses().owned(broker.project.uid)
                or qualification_gate(broker)!=before or worktree_authority(work)['repo_head']!=atomic_head):
            raise ExecutionBlocked('qualification changed live authority or left unresolved execution')
        result={'verified':True,'identity':identity,'execution_uid':broker.project.uid,
            'tasks':3,'native_execution':True,'restart_without_reexecution':True,
            'control_transport':'in_memory_fixture','live_control_read_only':live_control,
            'uncertain_control_reconciled_read_only':True,
            'pause_denied_control_effect':True,'live_authority_unchanged':True,
            'production_service':'not_measured','browser_delivery':'not_measured',
            'unattended_live_acceptance':'not_started'}
        session.update(phase='complete',result=result);atomic_json(journal,session)
        return result
