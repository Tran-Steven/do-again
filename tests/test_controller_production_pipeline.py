"""Production factory/loop/pipeline/verifier with local kernel/GitHub adapters.

Real source commits and real child tests; no synthetic attestation or verifier.
Native privilege/confinement installation remains a separate admin qualification.
"""
import os
import unittest
if os.name != 'posix':
    raise unittest.SkipTest('native POSIX controller and recovery fixtures')

import base64
from contextlib import nullcontext
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from test_control_history import Remote
from do_again.core.schema import canonical_json,request_fingerprint
from do_again.service.offline_continuation import digest
from do_again.supervisor.controller_backend import ControllerBackend,create_controller_backend
from do_again.supervisor.macos_server import ProjectBroker
from do_again.core.broker_executor import BrokerExecutor
from do_again.supervisor.controller_native_effects import NativeControllerEffects
from do_again.supervisor.development_attestation import DevelopmentAttestations
from do_again.supervisor.git_manifest import tree_manifest
from do_again.supervisor.macos_execution import ExecutionBlocked,ExecutionLedger

class ProductionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'source';self.repo.mkdir()
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        self.git('config','user.email','fixture@example.invalid');self.git('config','user.name','Offline qualification')
        (self.repo/'product.py').write_text('def recover(x): return None\ndef dedupe(x): return x\ndef handoff(x,y): return x\n')
        cases={'recovery':'self.assertEqual(recover(2),2)',
               'duplicates':'self.assertEqual(dedupe([1,1]),[1])',
               'handoff':'self.assertFalse(handoff(True,False))'}
        for name,assertion in cases.items():
            (self.repo/('test_'+name+'.py')).write_text('import unittest\nfrom product import *\nclass Acceptance(unittest.TestCase):\n    def test_acceptance(self):\n        '+assertion+'\n')
        self.git('add','.');self.git('commit','-qm','Three failing acceptance fixtures')
        self.base=self.git('rev-parse','HEAD');manifest=tree_manifest(Path('/usr/bin/git'),self.repo/'.git',self.base)
        self.now=datetime.now(timezone.utc)
        goal={'goal':'offline recovery, duplicate prevention and attention qualification'}
        scope=dict(kind='jobpipe-offline-controller-v1',repository='Tran-Steven/jobpipe',branch='operator-control',
            source_sha='a'*40,goal_sha256=digest(goal),epoch=12,issued_at_utc=(self.now-timedelta(seconds=1)).isoformat(),
            expires_at_utc=(self.now+timedelta(hours=1)).isoformat(),max_requests=3,max_cycles=3,max_outstanding=1,
            max_retries=0,planner='sealed-offline',integration='proposal-only',network_access=False,
            application_submissions=False,browser_messages=False)
        replacements=[('def recover(x): return None','def recover(x): return x'),
            ('def dedupe(x): return x','def dedupe(x): return list(dict.fromkeys(x))'),
            ('def handoff(x,y): return x','def handoff(x,y): return x and y')]
        tasks=[{'case':'test_'+name,'gap':name,'acceptance':'native acceptance for '+name,
            'script':'from pathlib import Path\np=Path("product.py")\np.write_text(p.read_text().replace('+repr(before)+','+repr(after)+'))'}
            for name,(before,after) in zip(cases,replacements)]
        self.config={'source_sha':'a'*40,'production_ready':False,'development_scope':{'kind':'jobpipe-offline-v1'},
            'controller_scope':scope,'controller_goal':goal,'controller_goal_sha256':digest(goal),
            'controller_profile':{'kind':'native-development-v1','test_runner':'unittest','allowed_paths':['product.py'],
                'test_cases':['test_'+n for n in cases],
                'test_files':{p:v['blob_sha'] for p,v in manifest.items() if p.startswith('test_')},
                'public_evidence':True,'offline_tasks':tasks},'authority_path':str(self.root/'native.sqlite'),
            'python':os.sys.executable,'git':'/usr/bin/git','operator_uid':os.getuid()}
        self.ledger=ExecutionLedger(Path(self.config['authority_path']));self.ledger.path.chmod(0o600)
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.events=[];self.remote=Remote();self.remote.repository='Tran-Steven/jobpipe'
        self.broker=SimpleNamespace(config=self.config,state=self.state,ledger=self.ledger,
            project=SimpleNamespace(account='_doagain_jp',key='fixture',worktree=self.repo),_dispatch=self.dispatch)
        owner=os.geteuid()
        self.patchers=[patch.object(ControllerBackend,'admission',lambda this:nullcontext()),
            patch('do_again.supervisor.controller_backend.DevelopmentAttestations',
                lambda path:DevelopmentAttestations(path,owner_uid=owner)),
            patch.object(NativeControllerEffects,'head',lambda this:self.git('rev-parse','HEAD')),
            # Synthetic Git/kernel adapter: root-owned metadata snapshot is
            # exercised separately; simulated capture uses fixture .git only.
            patch.object(NativeControllerEffects,'prepare_manifest_metadata',
                         lambda this,sid,head:self.repo/'.git'),
            patch.object(NativeControllerEffects,'capture',self.capture),
            patch('do_again.supervisor.control_history.api_for',lambda broker:self.remote)]
        for p in self.patchers:p.start();self.addCleanup(p.stop)
        self.backend=self.factory();self.backend.controller.start(self.now)

    def factory(self):
        # Root/installation boundary is substituted only for an isolated fixture;
        # backend construction and its capability decisions are production code.
        with patch('do_again.supervisor.controller_backend.require_sealed_factory'):
            return create_controller_backend(self.broker)

    def worker_execute(self,request):
        executor=BrokerExecutor.__new__(BrokerExecutor)
        packet=executor._packet(request,{'controller_enabled':True})
        gateway=ProjectBroker.__new__(ProjectBroker)
        gateway.lock=threading.Lock();gateway.config=self.config;gateway.project=self.broker.project;gateway.controller_backend=self.backend
        return gateway._dispatch(packet,self.config['operator_uid'])

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True).stdout.decode().strip()

    def capture(self,sid,argv,timeout,**kwargs):
        if 'tree_manifest' in ' '.join(argv):
            head=argv[-1];return {'returncode':0,'timed_out':False,'network_denied':True,
                'stdout':json.dumps(tree_manifest(Path('/usr/bin/git'),self.repo/'.git',head)),'stderr':''}
        outcome=subprocess.run(argv,cwd=self.repo,capture_output=True,text=True,timeout=timeout,
            env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
        self.events.append(('test',sid,tuple(argv[-1:])))
        return {'returncode':outcome.returncode,'timed_out':False,'network_denied':True,
                'stdout':outcome.stdout,'stderr':outcome.stderr}

    def dispatch(self,packet,uid):
        self.backend.guard_packet(packet)
        rid=packet['request_id'];fp=packet['request_fingerprint'];op=packet['operation']
        self.ledger.reserve('fixture',rid,digest(packet),intent={'operation':op,'source_sha':'a'*40,'request_fingerprint':fp})
        self.events.append((op,rid))
        if op=='execute':
            outcome=subprocess.run(packet['argv'],cwd=self.repo,capture_output=True,text=True,timeout=10)
            result={'returncode':outcome.returncode,'timed_out':False}
        elif op=='git_commit':
            self.assertEqual(self.git('rev-parse','HEAD'),packet['expected_head'])
            self.git('checkout','-q','-B','do-again/task-fixture');self.git('add','--',*packet['paths'])
            self.git('commit','-qm',packet['message'])
            result={'returncode':0,'base_sha':packet['expected_head'],'paths':['product.py'],
                'authority':{'repo_head':self.git('rev-parse','HEAD'),'repo_branch':'do-again/task-fixture'}}
        else:
            self.assertEqual(op,'git_publish')
            result={'returncode':0,'head':packet['expected_head'],'branch':'do-again/task-fixture','repository':'Tran-Steven/jobpipe'}
        if 'tested_tree_sha256' in packet:result['tested_tree_sha256']=packet['tested_tree_sha256']
        result['source_sha']='a'*40;self.ledger.finish('fixture',rid,result);return result

    def receipt(self,record,result):
        receipt={'request_id':record['request']['request_id'],'request_fingerprint':record['fingerprint'],
            'state':'succeeded','result':{'request_fingerprint':record['fingerprint'],'result':result}}
        api=self.remote;data=canonical_json(receipt)+b'\n'
        blob=api.request('POST','git/blobs',{'encoding':'base64','content':base64.b64encode(data).decode()})
        tree=api.request('POST','git/trees',{'base_tree':api.commits[api.head]['tree']['sha'],
            'tree':[{'path':'automation/do_again/receipts/'+receipt['request_id']+'.json','mode':'100644','type':'blob','sha':blob['sha']}]})
        commit=api.request('POST','git/commits',{'tree':tree['sha'],'parents':[api.head],'message':'receipt'})
        api.request('PATCH','git/refs/heads/operator-control',{'sha':commit['sha'],'force':False})
        return receipt

    def test_three_cycles_production_factory_pipeline_and_receipt_verifier(self):
        heads=[];records=[]
        for cycle in range(3):
            self.backend.tick(datetime.now(timezone.utc))
            record=self.backend.controller.status(self.now)['requests'][-1];records.append(record)
            result=self.worker_execute(record['request'])
            before=len(self.events)
            self.assertEqual(self.backend.execute({'operation':'development_execute','request':record['request']}),result)
            self.assertEqual(len(self.events),before)
            receipt=self.receipt(record,result)
            # Reconstruct actual factory and components before automatic ack.
            self.backend=self.factory()
            self.backend.tick(datetime.now(timezone.utc))
            heads.append(result['development']['implementation_commit'])
            self.assertEqual(result['development']['tests_executed'],cycle+1)
            if cycle:self.assertEqual(result['development']['base_commit'],heads[cycle-1])
        state=self.backend.controller.status(self.now)
        self.assertEqual(state['state'],'completed');self.assertEqual(len(state['acknowledged']),3)
        self.assertEqual(len(set(heads)),3)
        self.assertEqual(state['ack_publication']['phase'],'committed')
        status_blob=self.remote.trees[self.remote.commits[self.remote.head]['tree']['sha']]['automation/do_again/agent_status.json']
        self.assertEqual(json.loads(self.remote.blobs[status_blob])['controller_ack']['acknowledged_receipts'],state['acknowledged'])
        self.backend.tick(datetime.now(timezone.utc));self.assertEqual(len(state['requests']),3)
        self.assertEqual(len([e for e in self.events if e[0]=='execute']),3)
        output=os.environ.get('DO_AGAIN_QUALIFICATION_REPORT')
        if output:
            report={'kind':'offline-production-path-simulation','real_jobpipe_cycles':0,
                'substituted_boundaries':['root installation identity','dedicated execution UID/kernel adapter','GitHub API'],
                'state':state['state'],'cycles':[{'request_id':record['request']['request_id'],
                    'request_fingerprint':record['fingerprint'],'implementation_commit':head,
                    'test_commit':head,'tests_executed':index+1,
                    'receipt':self.backend.attestations.observe('fixture',record['request']['request_id'],record['fingerprint'])}
                    for index,(record,head) in enumerate(zip(records,heads))],
                'acknowledgment_control_commit':state['ack_publication']['head'],
                'implementation_executions':3,'duplicate_executions':0,'restart_before_acknowledgment':True}
            Path(output).write_text(json.dumps(report,indent=2)+'\n')


    def test_restart_after_native_stage_terminal_before_pipeline_checkpoint(self):
        self.backend.tick(self.now);record=self.backend.controller.status(self.now)['requests'][-1]
        native=self.backend.pipeline.effects
        def crash(stage,sid,request,stages):
            result=native(stage,sid,request,stages)
            if stage=='implementation':raise KeyboardInterrupt('fixture process loss after native completion')
            return result
        self.backend.pipeline.effects=crash
        with self.assertRaises(KeyboardInterrupt):self.worker_execute(record['request'])
        self.backend=self.factory()
        result=self.worker_execute(record['request'])
        self.assertEqual(result['returncode'],0)
        self.assertEqual(len([e for e in self.events if e[0]=='execute']),1)

    def test_modified_baseline_tests_block_before_test_attestation(self):
        self.backend.tick(self.now);record=self.backend.controller.status(self.now)['requests'][-1]
        (self.repo/'test_recovery.py').write_text('print("tests passed")\n')
        with self.assertRaises(ExecutionBlocked):self.worker_execute(record['request'])
        self.assertEqual(self.backend.controller.status(self.now)['state'],'blocked')
        with self.assertRaises(ExecutionBlocked):
            self.backend.attestations.observe('fixture',record['request']['request_id'],record['fingerprint'])

    def test_wrong_remote_commit_audit_rejects_automatic_acknowledgment(self):
        self.backend.tick(self.now);record=self.backend.controller.status(self.now)['requests'][-1]
        result=self.worker_execute(record['request'])
        forged=json.loads(json.dumps(result));forged['development']['test_commit']='e'*40
        self.receipt(record,forged)
        with self.assertRaises(ExecutionBlocked):self.backend.tick(self.now)
        self.assertEqual(self.backend.controller.status(self.now)['acknowledged'],[])

    def test_empty_unittest_module_is_failure_despite_zero_exit_default(self):
        (self.repo/'empty_fixture.py').write_text('VALUE = 1\n')
        result=self.backend.effects.run_case('offline-empty-probe','empty_fixture')
        self.assertEqual(result['returncode'],1)
        self.assertIn('Ran 0 tests',result['stderr'])

    def test_growing_public_regression_file_does_not_leave_started_planning_probe(self):
        large=b'# public regression fixture\n'+b'# padding\n'*6000+b'def test_preserved_public_regression(): pass\n'
        (self.repo/'large_regressions.py').write_bytes(large)
        self.git('add','large_regressions.py');self.git('commit','-qm','large public fixture')
        profile=self.backend.effects.profile
        profile.update(kind='native-development-autonomous-v1',implementation_paths=['product.py'])
        profile['allowed_paths'].append('large_regressions.py')
        result=self.backend.effects.collect('growing-source-probe','e'*64)
        self.assertIn('product.py',result['public_sources'])
        self.assertNotIn('large_regressions.py',result['public_sources'])
        summary=result['public_source_inventory']['large_regressions.py']
        self.assertEqual(summary['sha256'],hashlib.sha256(large).hexdigest())
        self.assertEqual(summary['top_level_definitions'],['test_preserved_public_regression'])
        self.assertEqual(len(result['cases']),3)
        self.assertEqual(self.ledger.pending('fixture'),[])
        self.assertEqual(self.ledger.observe_request('fixture','growing-source-probe','e'*64)['state'],'terminal')

    def test_lost_acknowledgment_response_reconciles_without_repeat_publication(self):
        self.backend.tick(self.now);record=self.backend.controller.status(self.now)['requests'][-1]
        result=self.worker_execute(record['request']);self.receipt(record,result)
        self.remote.lose_patch=True
        with self.assertRaises(ExecutionBlocked):self.backend.tick(self.now)
        self.remote.lose_patch=False;self.backend=self.factory();self.backend.tick(self.now)
        writes=[payload for method,path,payload in self.remote.writes
            if path=='git/commits' and payload['message'].startswith('Do Again verified receipt acknowledgment')]
        self.assertEqual(len(writes),1)
        self.assertEqual(self.backend.controller.status(self.now)['ack_publication']['phase'],'committed')
        self.assertEqual(len([e for e in self.events if e[0]=='execute']),1)

    def test_native_daemon_monitor_drives_three_cycles_with_browser_disabled(self):
        import time
        from do_again.service.daemon import _run_admitted_worker
        owner=self
        class FixtureAgent:
            stop_requested=False
            def __init__(self,**kwargs):self.callback=kwargs['receipt_callback']
            def run(self,once=False):
                deadline=time.monotonic()+20;seen=set()
                while time.monotonic()<deadline:
                    state=owner.backend.controller.status(datetime.now(timezone.utc))
                    if state['state']=='completed' and state.get('ack_publication',{}).get('phase')=='committed':return 0
                    if state['state']=='waiting' and state['requests'][-1]['phase']=='published':
                        record=state['requests'][-1];rid=record['request']['request_id']
                        if rid not in seen:
                            result=owner.worker_execute(record['request']);seen.add(rid)
                            receipt=owner.receipt(record,result)
                            owner.backend=owner.factory() # Restart before ack; never prompt next task.
                            self.callback(receipt)
                    time.sleep(0.01)
                raise AssertionError('native monitor did not complete bounded three-cycle fixture')
        def continuation():
            state=self.backend.tick(datetime.now(timezone.utc))
            return {'delivered':int(state['state']=='planning'),'liveness':state['state']}
        args=SimpleNamespace(once=False,control_worktree=str(self.root/'control'),branch='operator-control',
            remote='origin',policy=str(self.root/'policy.json'))
        with patch('do_again.service.daemon.Agent',FixtureAgent),patch('signal.signal'), \
             patch('do_again.service.daemon._queue_receipt') as browser_queue, \
             patch('do_again.service.daemon.ensure_browser_running') as browser:
            result=_run_admitted_worker(args,self.repo,self.state,False,
                admission_check=lambda:None,control_transport=self.remote,
                browser_effect=continuation,continuation_effect=continuation)
        self.assertEqual(result,0);browser_queue.assert_not_called();browser.assert_not_called()
        state=self.backend.controller.status(datetime.now(timezone.utc))
        self.assertEqual(len(state['acknowledged']),3)
        self.assertEqual(len([e for e in self.events if e[0]=='execute']),3)
        output=os.environ.get('DO_AGAIN_QUALIFICATION_REPORT')
        if output:
            Path(output).with_name('v1a-native-monitor-simulation.json').write_text(json.dumps({
                'kind':'offline-native-monitor-simulation','production_monitor':True,'browser_enabled':False,
                'acknowledged':state['acknowledged'],'state':state['state'],
                'implementation_commits':[r['implementation_commit'] for r in state['requests']],
                'implementation_executions':3,'human_prompts_between_cycles':0,
                'substituted_boundaries':['root installation identity','dedicated UID/kernel adapter','GitHub API','worker protocol adapter'],
                'real_jobpipe_cycles':0},indent=2)+'\n')

    def persistent_request(self, script):
        from do_again.service.offline_continuation import OfflineContinuation
        scope=dict(self.config['controller_scope'],kind='jobpipe-native-controller-v1',
            planner='chatgpt-browser-cdp',expires_at_utc=None,max_requests=12,max_cycles=12)
        self.config['controller_scope']=scope
        self.broker.project.repo=self.repo
        self.broker.registry=SimpleNamespace(status=lambda repo:{'epoch':13})
        controller=OfflineContinuation(self.state/'persistent.json',scope,
            {k:scope[k] for k in ('source_sha','goal_sha256','epoch','repository','branch')})
        self.backend.controller=controller;self.backend.pipeline.controller=controller
        controller.start(self.now)
        proposal=self.backend.build_request({'gap':'real fixture defect','acceptance':'all tests pass',
            'script':script},{'repo_head':self.base},self.now)
        proposal['evidence_sha256']='b'*64
        record=controller.reserve(proposal,pending_ids=[],now=self.now)
        controller.publication(record['request'],self.now)
        return record

    def test_persistent_known_nonmutating_failure_retained_and_never_acknowledged(self):
        record=self.persistent_request('raise SystemExit(2)')
        result=self.worker_execute(record['request'])
        self.assertEqual(result['returncode'],1)
        self.assertEqual(result['verified_failure']['failed_stage'],'implementation')
        self.assertEqual(result['verified_failure']['current_commit'],self.base)
        receipt={'request_id':record['request']['request_id'],'request_fingerprint':record['fingerprint'],
            'state':'failed','result':{'request_fingerprint':record['fingerprint'],'result':result}}
        failure=self.backend.verifier.failed(record,receipt)
        state=self.backend.controller.failed(receipt,failure=failure,now=self.now)
        self.assertEqual(state['state'],'planning');self.assertEqual(state['acknowledged'],[])
        self.assertEqual(state['requests'][0]['phase'],'failed')
        self.assertIsNone(state['outstanding'])
        self.assertFalse(any(e[0]=='git_publish' for e in self.events))
        with self.assertRaises(ExecutionBlocked):self.worker_execute(record['request'])
        self.assertEqual(len([e for e in self.events if e[0]=='execute']),1)

    def test_persistent_failed_tests_retained_at_clean_unpublished_commit(self):
        record=self.persistent_request('from pathlib import Path\np=Path("product.py")\np.write_text(p.read_text().replace("def recover(x): return None", "def recover(x): return x"))')
        result=self.worker_execute(record['request'])
        self.assertEqual(result['returncode'],1)
        failure=result['verified_failure']
        self.assertEqual(failure['failed_stage'],'test')
        self.assertEqual(len(failure['test_cases']),3)
        self.assertTrue(any(c['exit_code'] for c in failure['test_cases']))
        self.assertEqual(failure['current_commit'],self.git('rev-parse','HEAD'))
        self.assertNotEqual(failure['current_commit'],self.base)
        self.assertEqual(self.git('status','--porcelain'),'')
        self.assertFalse(any(e[0]=='git_publish' for e in self.events))

    def test_persistent_failure_with_uncommitted_changes_stops_and_preserves_bytes(self):
        record=self.persistent_request('from pathlib import Path\nPath("product.py").write_text("partial edit")\nraise SystemExit(2)')
        with self.assertRaises(ExecutionBlocked):self.worker_execute(record['request'])
        self.assertEqual((self.repo/'product.py').read_text(),'partial edit')
        self.assertEqual(self.backend.controller.status(self.now)['state'],'blocked')
        self.assertEqual(self.ledger.observe_request('fixture',record['request']['request_id'],record['fingerprint'])['state'],'not_started')

    def autonomous_profile(self):
        from do_again.supervisor.candidate_transaction import KIND
        self.config['controller_profile'].update(kind=KIND,implementation_paths=['product.py'],offline_tasks=[])
        self.broker.admission=nullcontext;self.broker.project.uid=402

    def test_autonomous_runs_every_test_before_git_commit_and_attests_exact_tree(self):
        script='from pathlib import Path\nPath("product.py").write_text("def recover(x): return x\\ndef dedupe(x): return list(dict.fromkeys(x))\\ndef handoff(x,y): return x and y\\n")'
        record=self.persistent_request(script);self.autonomous_profile()
        result=self.worker_execute(record['request'])
        self.assertEqual(result['returncode'],0)
        commit_index=next(i for i,e in enumerate(self.events) if e[0]=='git_commit')
        test_indices=[i for i,e in enumerate(self.events) if e[0]=='test']
        self.assertEqual(len(test_indices),3);self.assertTrue(all(i<commit_index for i in test_indices))
        self.assertEqual(result['development']['implementation_commit'],self.git('rev-parse','HEAD'))
        self.assertEqual(result['development']['test_commit'],self.git('rev-parse','HEAD'))
        self.assertEqual(result['development']['tests_executed'],3)

    def test_autonomous_failed_tests_restore_source_before_fresh_task_without_commit(self):
        original=(self.repo/'product.py').read_bytes()
        record=self.persistent_request('from pathlib import Path\nPath("product.py").write_text("def recover(x): return x\\ndef dedupe(x): return x\\ndef handoff(x,y): return x\\n")')
        self.autonomous_profile()
        with patch('do_again.supervisor.macos_execution.MacOSProcesses') as processes:
            result=self.worker_execute(record['request'])
        self.assertEqual(result['returncode'],1)
        self.assertEqual(result['verified_failure']['failed_stage'],'test')
        self.assertEqual(result['verified_failure']['current_commit'],self.base)
        self.assertEqual((self.repo/'product.py').read_bytes(),original)
        self.assertEqual(self.git('status','--porcelain'),'')
        self.assertFalse(any(e[0] in {'git_commit','git_publish'} for e in self.events))
        processes.return_value.drain.assert_called_once_with(402)

    def test_unreserved_request_and_direct_execution_denied(self):
        with self.assertRaises(ExecutionBlocked):self.backend.guard_packet({'operation':'execute'})
        with self.assertRaises(ExecutionBlocked):self.backend.execute({'operation':'development_execute','request':{
            'request_id':'offline-fake-1','operation':'scratch_script'}})

    def test_started_stage_blocks_without_replaying(self):
        self.backend.tick(self.now);record=self.backend.controller.status(self.now)['requests'][-1]
        rid=record['request']['request_id']+'-implementation'
        self.ledger.reserve('fixture',rid,'e'*64,intent={'operation':'execute','source_sha':'a'*40,'request_fingerprint':record['fingerprint']})
        with self.assertRaises(ExecutionBlocked):self.backend.execute({'operation':'development_execute','request':record['request']})
        self.assertFalse(any(e[0]=='execute' for e in self.events))
        self.assertEqual(self.backend.controller.status(self.now)['state'],'blocked')
