"""Production factory/loop/pipeline/verifier with local kernel/GitHub adapters.

Real source commits and real child tests; no synthetic attestation or verifier.
Native privilege/confinement installation remains a separate admin qualification.
"""
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

    def capture(self,sid,argv,timeout):
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
