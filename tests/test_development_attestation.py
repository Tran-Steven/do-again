import copy
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from do_again.supervisor.macos_execution import ExecutionLedger, ExecutionBlocked
from do_again.supervisor.development_attestation import DevelopmentAttestations, produce_attestation
from do_again.supervisor.native_test_evidence import observe_tests


class AttestationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'native.sqlite'
        self.ledger = ExecutionLedger(self.path); self.path.chmod(0o600)
        self.store = DevelopmentAttestations(self.path, owner_uid=os.geteuid())
        self.fp = 'f'*64; self.source = 'a'*40; self.base = 'b'*40; self.head = 'c'*40
        self.ids = {'commit':'cycle-commit','test':'cycle-test','publication':'cycle-publication'}
        measured = {'commit':self.head,'clean':True,'tree_sha256':'d'*64}
        self.run = observe_tests(expected_head=self.head, cases=('tests.test_recovery',),
            measure=lambda:dict(measured), execute=lambda case: {
                'returncode':0,'timed_out':False,'network_denied':True,'stdout':'passed','stderr':''})
        self.results = {
            'commit':{'base_sha':self.base,'paths':['src/recovery.py'],
                'authority':{'repo_head':self.head,'repo_branch':'do-again/task-cycle'}},
            'test':{'native_test_run':self.run},
            'publication':{'head':self.head,'branch':'do-again/task-cycle','repository':'Tran-Steven/jobpipe'}}

    def stages(self, mutate=None):
        for stage, rid in self.ids.items():
            result = dict(self.results[stage], returncode=0,source_sha=self.source,timed_out=False)
            if mutate: mutate(stage,result)
            self.ledger.reserve('p',rid,self.fp,intent={'operation':{
                'commit':'git_commit','test':'execute','publication':'git_publish'}[stage],
                'source_sha':self.source,'request_fingerprint':self.fp})
            self.ledger.finish('p',rid,result)

    def produce(self):
        return produce_attestation(ledger=self.ledger,store=self.store,project='p',
            request_id='cycle',fingerprint=self.fp,source_sha=self.source,
            base_commit=self.base,stage_ids=self.ids)

    def test_protected_record_survives_restart_and_is_idempotent(self):
        self.stages(); proof=self.produce()
        restarted=DevelopmentAttestations(self.path,owner_uid=os.geteuid())
        self.assertEqual(restarted.observe('p','cycle',self.fp),proof)
        self.assertEqual(self.produce(),proof)
        with self.assertRaises(ExecutionBlocked):
            restarted.record('p','cycle',self.fp,dict(proof,tests_executed=7))

    def test_missing_stages_rejected(self):
        with self.assertRaises(ExecutionBlocked):self.produce()
        with self.assertRaises(ExecutionBlocked):self.store.observe('p','cycle',self.fp)

    def test_failed_native_test_rejected_despite_passed_text(self):
        self.stages(lambda stage,result:result.update(returncode=1) if stage=='test' else None)
        with self.assertRaises(ExecutionBlocked):self.produce()

    def test_mismatched_commit_rejected(self):
        self.results['test']['native_test_run']['commit_after']='e'*40
        self.stages()
        with self.assertRaises(ExecutionBlocked):self.produce()

    def test_payload_tampering_and_fingerprint_conflict_rejected(self):
        self.stages();self.produce()
        with self.assertRaises(ExecutionBlocked):self.store.observe('p','cycle','e'*64)
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE development_attestation SET payload='{}'")
        with self.assertRaises(ExecutionBlocked):self.store.observe('p','cycle',self.fp)

    def test_writable_database_rejected(self):
        self.path.chmod(0o666)
        with self.assertRaises(ExecutionBlocked):self.store.observe('p','cycle',self.fp)

    def test_alias_rejected(self):
        alias=self.path.with_name('alias.sqlite');os.link(self.path,alias)
        with self.assertRaises(ExecutionBlocked):self.store.observe('p','cycle',self.fp)

    def test_test_observer_rejects_dirty_changed_failed_and_unconfined_runs(self):
        clean={'commit':self.head,'clean':True,'tree_sha256':'d'*64}
        good={'returncode':0,'timed_out':False,'network_denied':True,'stdout':'passed','stderr':''}
        for update in ({'returncode':1},{'timed_out':True},{'network_denied':False},{'stdout_truncated':True}):
            with self.subTest(update=update),self.assertRaises(ExecutionBlocked):
                observe_tests(expected_head=self.head,cases=('tests.test_recovery',),
                    measure=lambda:clean,execute=lambda case:dict(good,**update))
        for changed in (dict(clean,clean=False),dict(clean,tree_sha256='e'*64),dict(clean,commit='e'*40)):
            measurements=iter((clean,changed))
            with self.assertRaises(ExecutionBlocked):
                observe_tests(expected_head=self.head,cases=('tests.test_recovery',),
                    measure=lambda:next(measurements),execute=lambda case:good)

    def test_profile_cannot_supply_command_or_repeat_case(self):
        for cases in (('tests.x;curl evil',),('tests.x','tests.x'),[],()):
            with self.assertRaises(ExecutionBlocked):
                observe_tests(expected_head=self.head,cases=cases,measure=lambda:self.fail(),execute=lambda c:self.fail())

class CommittedSourceTests(unittest.TestCase):
    def test_measures_real_committed_bytes_and_rejects_edits_links_and_extras(self):
        import hashlib
        from do_again.supervisor.native_test_evidence import measure_committed_tree
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'recovery.py';source.write_bytes(b'recovered = True\n')
            data=source.read_bytes();manifest={'recovery.py':{'mode':'100644',
                'blob_sha':hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()}}
            def measure():return measure_committed_tree(root,commit='c'*40,manifest=manifest,read_authority=lambda:'c'*40)
            self.assertTrue(measure()['clean'])
            source.write_bytes(b'recovered = False\n')
            with self.assertRaises(ExecutionBlocked):measure()
            source.write_bytes(data);extra=root/'extra';extra.write_text('untracked')
            with self.assertRaises(ExecutionBlocked):measure()
            extra.unlink();source.unlink();source.symlink_to('/etc/hosts')
            with self.assertRaises((ExecutionBlocked,OSError)):measure()

class RealTestProcessTests(unittest.TestCase):
    def test_real_commit_and_test_process_bind_evidence_and_detect_dirty_source(self):
        import subprocess
        from do_again.supervisor.native_test_evidence import measure_committed_tree
        from do_again.core.schema import canonical_json
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'source';root.mkdir()
            subprocess.run(['git','init','-q',str(root)],check=True,capture_output=True)
            def git(*args):
                return subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True).stdout.decode().strip()
            git('config','user.email','fixture@example.invalid');git('config','user.name','Offline fixture')
            (root/'recovery.py').write_text('def recover(checkpoint):\n    return None\n')
            (root/'test_recovery.py').write_text('import unittest\nfrom recovery import recover\nclass Recovery(unittest.TestCase):\n    def test_resume(self):\n        self.assertEqual(recover("saved"), "saved")\n')
            git('add','.');git('commit','-qm','Baseline failing recovery fixture');base=git('rev-parse','HEAD')
            failed=subprocess.run([os.sys.executable,'-B','-m','unittest','test_recovery'],cwd=root,capture_output=True,text=True)
            self.assertNotEqual(failed.returncode,0)
            (root/'recovery.py').write_text('def recover(checkpoint):\n    return checkpoint\n')
            git('add','.');git('commit','-qm','Preserve recovery checkpoint');head=git('rev-parse','HEAD')
            manifest={}
            for line in git('ls-tree','-r','HEAD').splitlines():
                metadata,name=line.split('\t');mode,kind,blob=metadata.split()
                self.assertEqual(kind,'blob');manifest[name]={'mode':mode,'blob_sha':blob}
            measure=lambda:measure_committed_tree(root,commit=head,manifest=manifest,read_authority=lambda:git('rev-parse','HEAD'))
            def execute(case):
                completed=subprocess.run([os.sys.executable,'-B','-m','unittest',case],cwd=root,capture_output=True,text=True,timeout=10)
                # Confinement is a test fixture; this is not macOS sandbox qualification.
                return {'returncode':completed.returncode,'timed_out':False,'network_denied':True,
                        'stdout':completed.stdout,'stderr':completed.stderr}
            run=observe_tests(expected_head=head,cases=('test_recovery',),measure=measure,execute=execute)
            path=Path(temporary)/'ledger.sqlite';ledger=ExecutionLedger(path);path.chmod(0o600)
            store=DevelopmentAttestations(path,owner_uid=os.geteuid());fp='f'*64;source='a'*40
            stages={'commit':'real-commit','test':'real-test','publication':'fixture-publication'}
            results={'commit':{'base_sha':base,'paths':['recovery.py'],'authority':{'repo_head':head,'repo_branch':'do-again/task-fixture'}},
                'test':{'native_test_run':run},'publication':{'head':head,'branch':'do-again/task-fixture','repository':'Tran-Steven/jobpipe'}}
            for stage,rid in stages.items():
                ledger.reserve('p',rid,fp,intent={'operation':{'commit':'git_commit','test':'execute','publication':'git_publish'}[stage],
                    'source_sha':source,'request_fingerprint':fp})
                ledger.finish('p',rid,dict(results[stage],returncode=0,source_sha=source))
            proof=produce_attestation(ledger=ledger,store=store,project='p',request_id='real',fingerprint=fp,
                source_sha=source,base_commit=base,stage_ids=stages)
            self.assertEqual(proof['test_commit'],head);self.assertNotEqual(head,base)
            self.assertEqual(proof['tests_executed'],1)
            (root/'recovery.py').write_text('def recover(checkpoint):\n    return None\n')
            with self.assertRaises(ExecutionBlocked):measure()
