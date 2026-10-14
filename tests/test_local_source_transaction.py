import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import nullcontext
from do_again.core.schema import canonical_json, request_fingerprint
from do_again.supervisor.git_manifest import tree_manifest
from do_again.supervisor.macos_execution import ExecutionBlocked, ExecutionLedger, profile
from do_again.supervisor.native_test_evidence import observe_tests
from do_again.supervisor.source_transaction import SourceTransaction, validate_source_contract, LOCAL_KIND
from do_again.supervisor.local_development_pipeline import LocalDevelopmentPipeline

def sha(data): return hashlib.sha256(data).hexdigest()
def blob(data): return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()

@unittest.skipUnless(os.name=="posix", "native ownership, descriptors or process control require POSIX")
class LocalSourceTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.repo=self.root/'repo';self.repo.mkdir()
        self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.original={'product.py':b'def answer():\r\n    return 0\r\n',
            'test_product.py':b'import unittest\nfrom product import answer\nclass Tests(unittest.TestCase):\n    def test_answer(self): self.assertEqual(answer(),1)\n'}
        self.updated=dict(self.original)
        self.updated['product.py']=self.original['product.py'].replace(b'return 0',b'return 1')
        self.updated['test_product.py']=self.original['test_product.py']+b'\n# Exact appended public regression coverage\n'
        for name,data in self.original.items():(self.repo/name).write_bytes(data)
        subprocess.run(['git','init','-q',str(self.repo)],check=True,capture_output=True)
        self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        self.git('add','.');self.git('commit','-qm','Pinned fixture')
        self.base=self.head();self.manifest=tree_manifest(Path('/usr/bin/git'),self.repo/'.git',self.base)
        self.contract={'kind':LOCAL_KIND,'allowed_paths':list(self.original),'test_runner':'unittest',
            'test_cases':['test_product'],'test_files':{'test_product.py':blob(self.original['test_product.py'])},
            'public_evidence':True,'offline_tasks':[],
            'source_contract':{k:{'before_sha256':sha(v),'after_sha256':sha(self.updated[k]),
                'before_blob':blob(v),'after_blob':blob(self.updated[k]),'mode':'100644',
                'preserve_prefix':k.startswith('test_'),'after_content':self.updated[k].decode()} for k,v in self.original.items()},
            'original_task':{'request_id':'original-task-01','request_fingerprint':'f'*64,
                'planner_call_id':'planner-original-1','base_commit':self.base}}
        self.txn=SourceTransaction(self.repo,self.state,self.contract,head=self.base,
            manifest=self.manifest,read_authority=self.head,owner_uid=os.geteuid())
    def git(self,*args):
        return subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True).stdout.decode().strip()
    def head(self):return self.git('rev-parse','HEAD')
    def apply(self):
        for name,data in self.updated.items():(self.repo/name).write_bytes(data)
    def test_partial_failure_and_reopened_snapshot_restore_crlf(self):
        self.txn.prepare();(self.repo/'product.py').write_bytes(b'partial')
        reopened=SourceTransaction(self.repo,self.state,self.contract,head=self.base,
            manifest=self.manifest,read_authority=self.head,owner_uid=os.geteuid())
        self.assertTrue(reopened.rollback()['byte_perfect'])
        for name,data in self.original.items():self.assertEqual((self.repo/name).read_bytes(),data)
        self.assertTrue(self.txn.path.exists());self.assertEqual(self.head(),self.base)
    def test_committed_manifest_equals_approved_tested_tree(self):
        self.txn.prepare();self.apply();measured=self.txn.candidate()
        self.git('add','.');self.git('commit','-qm','Local fixture')
        result=self.txn.committed(self.head(),tree_manifest(Path('/usr/bin/git'),self.repo/'.git',self.head()))
        self.assertEqual(measured['tree_sha256'],result['tree_sha256']);self.assertIn('return 1',result['diff'])
    def test_authority_drift_prevents_blind_rollback(self):
        self.txn.prepare();self.apply();self.git('add','.');self.git('commit','-qm','New authority')
        with self.assertRaisesRegex(ExecutionBlocked,'authority changed'):self.txn.rollback()
    def test_extra_source_is_rejected(self):
        self.txn.prepare();self.apply();(self.repo/'extra.py').write_bytes(b'new')
        with self.assertRaises(ExecutionBlocked):self.txn.candidate()
    def test_snapshot_tampering_is_rejected(self):
        self.txn.prepare();d=json.loads(self.txn.path.read_text());d['files']['product.py']['bytes']='bm8='
        self.txn.path.write_text(json.dumps(d))
        with self.assertRaisesRegex(ExecutionBlocked,'snapshot hash'):self.txn.rollback()
    def test_alias_never_overwrites_external_file(self):
        self.txn.prepare();outside=self.root/'outside';outside.write_bytes(b'protected')
        (self.repo/'product.py').unlink();(self.repo/'product.py').symlink_to(outside)
        with self.assertRaises(OSError):self.txn.rollback()
        self.assertEqual(outside.read_bytes(),b'protected')
    def test_editable_tests_require_original_prefix(self):
        bad=copy.deepcopy(self.contract);bad['source_contract']['test_product.py']['preserve_prefix']=False
        with self.assertRaises(ExecutionBlocked):validate_source_contract(bad)
    def test_literal_write_scope_does_not_grant_worktree_subpath(self):
        scratch=self.root/'scratch';cache=self.root/'cache';scratch.mkdir();cache.mkdir()
        project=SimpleNamespace(worktree=self.repo)
        policy=profile(project,scratch,cache,source_write_paths=('product.py',))
        write=policy.split('(allow file-write*',1)[1].split('(deny file-write*',1)[0]
        self.assertIn('(literal '+json.dumps(str(self.repo/'product.py'))+')',write)
        self.assertNotIn('(subpath '+json.dumps(str(self.repo))+')',write)
        with self.assertRaises(ExecutionBlocked):profile(project,scratch,cache,source_write_paths=('../outside',))

@unittest.skipUnless(os.name=="posix", "native ownership, descriptors or process control require POSIX")
class LocalPipelineTests(unittest.TestCase):
    setUp = LocalSourceTransactionTests.setUp
    git = LocalSourceTransactionTests.git
    head = LocalSourceTransactionTests.head
    apply = LocalSourceTransactionTests.apply
    def make_pipeline(self, *, fail_test=False, uncertain_commit=False, fail_commit=False, interrupted_child=False):
        now=datetime.now(timezone.utc)
        content='from pathlib import Path\n'+''.join('Path('+repr(k)+').write_bytes('+repr(v)+')\n' for k,v in self.updated.items())
        self.request={'schema_version':1,'request_id':'amended-task-01','operation':'scratch_script',
            'args':{'language':'python','content':content},'expected':{'repo_head':self.base},
            'issued_at_utc':(now-timedelta(seconds=1)).isoformat(),
            'expires_at_utc':(now+timedelta(minutes=15)).isoformat(),'limits':{'timeout_seconds':600}}
        fp=request_fingerprint(self.request);self.events=[];self.proofs={}
        self.ledger=ExecutionLedger(self.root/'ledger.sqlite')
        record={'phase':'published','request':self.request}
        controller=SimpleNamespace(status=lambda now:{'state':'waiting','outstanding':self.request['request_id'],
            'requests':[record]},block=lambda reason,now:self.events.append(('blocked',reason)))
        def effects(stage,sid,request,stages):
            self.events.append((stage,sid))
            if stage=='source_snapshot':return self.txn.prepare()
            if stage=='source_rollback':return self.txn.rollback()
            if stage=='source_committed':return dict(self.txn.committed(self.head(),tree_manifest(Path('/usr/bin/git'),self.repo/'.git',self.head())),epoch=1)
            self.ledger.reserve('fixture',sid,sha(canonical_json({'stage':stage,'sid':sid})),
                intent={'operation':'git_commit' if stage=='commit' else 'execute','source_sha':'a'*40,'request_fingerprint':fp})
            if stage=='regression_baseline':
                (self.repo/'test_product.py').write_bytes(self.updated['test_product.py'])
                def execute(case):
                    r=subprocess.run([os.sys.executable,'-B','-m','unittest',case],cwd=self.repo,capture_output=True,text=True)
                    return {'returncode':r.returncode,'timed_out':False,'network_denied':True,'stdout':r.stdout,'stderr':r.stderr}
                run=observe_tests(expected_head=self.base,cases=('test_product',),measure=self.txn.regression_baseline,
                    execute=execute,allow_failures=True)
                result={'returncode':0 if any(c['exit_code']!=0 for c in run['cases']) else 1,
                    'timed_out':False,'native_regression_baseline':run}
            elif stage=='implementation':
                if interrupted_child:
                    (self.repo/'product.py').write_bytes(b'partial')
                    raise ExecutionBlocked('interrupted confined child')
                subprocess.run([os.sys.executable,'-B','-c',request['args']['content']],cwd=self.repo,capture_output=True,check=True)
                result={'returncode':0,'timed_out':False,'execution_uid':os.geteuid(),'network_denied':True,'source_write_paths':list(self.original)}
            elif stage=='test':
                def execute(case):
                    r=subprocess.run([os.sys.executable,'-B','-m','unittest',case],cwd=self.repo,capture_output=True,text=True)
                    return {'returncode':1 if fail_test else r.returncode,'timed_out':False,'network_denied':True,'stdout':r.stdout,'stderr':r.stderr}
                run=observe_tests(expected_head=self.base,cases=('test_product',),measure=self.txn.candidate,execute=execute,allow_failures=True)
                result={'returncode':int(fail_test),'timed_out':False,'native_test_run':run}
            elif stage=='commit':
                if uncertain_commit:raise ExecutionBlocked('loss after commit reservation')
                if fail_commit:result={'returncode':1,'timed_out':False,'state':'failed_pre_promotion'}
                else:
                    self.git('checkout','-q','-B','do-again/task-fixture');self.git('add','.');self.git('commit','-qm','Fixture')
                    result={'returncode':0,'timed_out':False,'base_sha':self.base,'paths':list(self.original),
                        'authority':{'repo_head':self.head(),'repo_branch':'do-again/task-fixture'}}
            else:raise AssertionError('publication forbidden')
            result['source_sha']='a'*40;self.ledger.finish('fixture',sid,result);return result
        parent=SimpleNamespace(controller=controller,ledger=self.ledger,project='fixture',source_sha='a'*40,
            directory=self.state,admission=lambda:nullcontext(),effects=effects,execution_uid=os.geteuid(),
            attestations=SimpleNamespace(record=lambda project,rid,fp,proof:self.proofs.update({rid:proof})))
        return LocalDevelopmentPipeline(parent,self.contract),now
    def test_tests_before_commit_and_no_publication(self):
        p,now=self.make_pipeline();result=p.execute(self.request,now);self.assertEqual(result['returncode'],0)
        order=[x[0] for x in self.events];self.assertLess(order.index('test'),order.index('commit'));self.assertNotIn('publication',order)
        proof=result['local_development'];self.assertTrue(proof['test_before_commit']);self.assertFalse(proof['publication_performed'])
        self.assertEqual(proof['original_task'],self.contract['original_task'])
        before=list(self.events);self.assertEqual(p.execute(self.request,now),result);self.assertEqual(before,self.events)
    def test_failed_tests_restore_bytes_without_commit(self):
        p,now=self.make_pipeline(fail_test=True);result=p.execute(self.request,now)
        self.assertEqual(result['returncode'],1);self.assertTrue(result['local_failure']['rollback']['byte_perfect'])
        self.assertNotIn('commit',[e[0] for e in self.events]);self.assertEqual(self.head(),self.base)
        for k,v in self.original.items():self.assertEqual((self.repo/k).read_bytes(),v)
    def test_failed_pre_promotion_commit_restores_source(self):
        p,now=self.make_pipeline(fail_commit=True);result=p.execute(self.request,now)
        self.assertEqual(result['returncode'],1);self.assertEqual(self.head(),self.base)
        self.assertTrue(result['local_failure']['rollback']['byte_perfect'])
    def test_uncertain_commit_cannot_replay_or_blindly_restore(self):
        p,now=self.make_pipeline(uncertain_commit=True)
        with self.assertRaisesRegex(ExecutionBlocked,'uncertain'):p.execute(self.request,now)
        self.assertNotIn('source_rollback',[e[0] for e in self.events]);self.assertTrue(self.txn.path.exists())
        before=list(self.events)
        with self.assertRaises(ExecutionBlocked):p.execute(self.request,now)
        self.assertEqual(before,self.events)
    def test_interrupted_confined_child_becomes_failure_only_after_exact_rollback(self):
        p,now=self.make_pipeline(interrupted_child=True);result=p.execute(self.request,now)
        self.assertEqual(result['returncode'],1)
        self.assertTrue(result['local_failure']['rollback']['byte_perfect'])
        fp=request_fingerprint(self.request)
        observed=self.ledger.observe_request('fixture',self.request['request_id']+'-implementation',fp)
        self.assertEqual(observed['state'],'terminal');self.assertEqual(observed['result']['returncode'],1)
        self.assertTrue(observed['result']['interrupted_original_effect'])
        self.assertEqual(self.ledger.pending('fixture'),[])
        for name,data in self.original.items():self.assertEqual((self.repo/name).read_bytes(),data)

    def test_corrupt_python_never_dispatches(self):
        p,now=self.make_pipeline();self.request['args']['content']='if True:\npass'
        with self.assertRaises(IndentationError):p.execute(self.request,now)
        self.assertEqual(self.events,[]);self.assertFalse(self.txn.path.exists())

@unittest.skipUnless(os.name=="posix", "native ownership, descriptors or process control require POSIX")
class NativePytestCaptureTests(unittest.TestCase):
    setUp = LocalSourceTransactionTests.setUp
    git = LocalSourceTransactionTests.git
    head = LocalSourceTransactionTests.head
    apply = LocalSourceTransactionTests.apply

    def test_assertion_failure_is_distinguished_from_missing_test_collection(self):
        from do_again.supervisor.controller_native_effects import NativeControllerEffects
        import importlib.util,shutil
        from unittest.mock import patch
        # The real isolated subprocess deliberately disables host site-packages.
        # Provide its dependency closure in this fixture, never borrow an
        # administrator's installed native runtime or its private state.
        install=self.root/'fixture-install'
        packages=install/'current/runtimes/test-packages';packages.mkdir(parents=True)
        for name in ('pytest','_pytest','py','pluggy','iniconfig','packaging','pygments'):
            spec=importlib.util.find_spec(name)
            if spec is None:
                self.fail('Install the declared .[test] dependency to run real pytest capture fixtures')
            if spec.submodule_search_locations:
                shutil.copytree(next(iter(spec.submodule_search_locations)),packages/name,
                                ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            else:shutil.copyfile(spec.origin,packages/(name+'.py'))
        context=patch('do_again.supervisor.controller_native_effects.INSTALL_ROOT',install)
        context.start();self.addCleanup(context.stop)
        effects=NativeControllerEffects.__new__(NativeControllerEffects)
        effects.profile={'test_runner':'pytest','kind':LOCAL_KIND}
        effects.broker=SimpleNamespace(config={'python':os.sys.executable})
        def capture(sid,argv,timeout):
            result=subprocess.run(argv,cwd=self.repo,capture_output=True,text=True,timeout=timeout)
            return {'returncode':result.returncode,'timed_out':False,'network_denied':True,
                'stdout':result.stdout,'stderr':result.stderr}
        effects.capture=capture
        failing=effects.run_case('fixture-case','test_product.py')
        self.assertEqual(failing['native_test_counts']['pytest_exit_code'],1)
        self.assertEqual(failing['native_test_counts']['failed'],1)
        missing=effects.run_case('fixture-case','test_product.py::missing')
        self.assertEqual(missing['native_test_counts']['pytest_exit_code'],4)
        self.assertEqual(missing['native_test_counts']['collected'],0)
        self.apply()
        passing=effects.run_case('fixture-case','test_product.py')
        self.assertEqual(passing['returncode'],0)
        self.assertEqual(passing['native_test_counts']['passed'],1)
        self.assertEqual(passing['native_test_counts']['skipped'],0)

@unittest.skipUnless(os.name=="posix", "native ownership, descriptors or process control require POSIX")
class LocalControllerProfileTests(unittest.TestCase):
    setUp = LocalSourceTransactionTests.setUp
    git = LocalSourceTransactionTests.git
    head = LocalSourceTransactionTests.head
    apply = LocalSourceTransactionTests.apply

    def test_new_profile_requires_exact_outputs_legacy_overlap_still_denied(self):
        from test_controller_provider_scope import ProviderScopeTests
        from do_again.supervisor.controller_configuration import validate_controller_configuration
        fixture=ProviderScopeTests();fixture.setUp();cfg=fixture.config
        cfg['controller_scope'].update(kind='jobpipe-native-controller-v1',expires_at_utc=None)
        cfg['browser_planning_scope'].update(kind='browser-planning-persistent-v1',expires_at_utc=None)
        cfg['development_scope']['kind']='jobpipe-native-v1'
        cfg['controller_profile']=copy.deepcopy(self.contract)
        validate_controller_configuration(cfg,now=fixture.now)
        cfg['controller_profile']['source_contract']['product.py']['after_content']='tampered'
        with self.assertRaises(ExecutionBlocked):validate_controller_configuration(cfg,now=fixture.now)
        cfg['controller_profile']=copy.deepcopy(self.contract)
        cfg['controller_profile']['kind']='native-development-v1'
        cfg['controller_profile'].pop('original_task');cfg['controller_profile'].pop('source_contract')
        with self.assertRaises(ExecutionBlocked):validate_controller_configuration(cfg,now=fixture.now)

class LocalSingleTaskHistoryTests(unittest.TestCase):
    def test_verified_original_success_closes_controller_with_unused_retry_budget(self):
        from unittest.mock import patch
        from do_again.supervisor.controller_transport import ControllerHistory
        from do_again.supervisor.control_history import blob_sha
        receipt={'request_id':'amended-task-01','request_fingerprint':'f'*64,'state':'succeeded','result':{}}
        state={'outstanding':receipt['request_id'],'requests':[{'request':{'request_id':receipt['request_id']}}],
            'acknowledged':[receipt['request_id']],'state':'planning'}
        saved=[]
        controller=SimpleNamespace(scope={'kind':'jobpipe-native-controller-v1'},status=lambda now:state,accept=lambda *args,**kw:dict(state),
            _save=lambda value:saved.append(value) or value)
        history=ControllerHistory(controller,SimpleNamespace(),lambda:nullcontext(),profile={'kind':LOCAL_KIND})
        history.effect=lambda:nullcontext()
        sha=blob_sha(canonical_json(receipt)+b'\n')
        history.snapshot=lambda:('head',{}, {'automation/do_again/receipts/amended-task-01.json':sha})
        with patch('do_again.supervisor.controller_transport.read_json_blob',return_value=receipt):
            result=history.completion(datetime.now(timezone.utc),lambda record,receipt:{'verified':True})
        self.assertEqual(result['state'],'completed')
        self.assertEqual(result['completion_reason'],'approved original task completed')
        self.assertEqual(len(saved),1)
