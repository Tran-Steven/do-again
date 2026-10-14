import copy,unittest
from do_again.supervisor import native_publication_recovery as r
from do_again.supervisor.macos_execution import ExecutionBlocked

class KnownCollisionTests(unittest.TestCase):
    def setUp(self):
        self.previous={'source_sha':r.SOURCE,'production_ready':False}
        self.status={'intent':'maintenance','epoch':r.EPOCH}
        self.job={'fingerprint':r.TASK_FP,'state':'blocked','blocker':'development failed/uncertain: FileExistsError',
            'stages':{'snapshot':{},'implementation':{},'test':{},'commit':{'authority':{'repo_branch':'do-again/task-original'}}},
            'stage_ids':{'publication':r.REQUEST+'-publication'}}
        self.intent={'operation':'git_publish','repository':'Tran-Steven/jobpipe','head':r.HEAD,
            'branch':'do-again/task-original','source_sha':r.SOURCE,'title':'Do Again tested offline proposal '+r.REQUEST,
            'body':'Offline development only; exact head '+r.HEAD+'. Native test evidence recorded in the control receipt.',
            'request_fingerprint':r.TASK_FP,'classification':'development_publication'}
        self.original={'state':'started','result':None}
        self.checkpoint={'kind':r.STAGED_KIND,'request_id':r.REQUEST,'request_fingerprint':r.TASK_FP,
            'source_sha':r.SOURCE,'commit':r.HEAD,'tree_sha256':r.TREE,'verified':True}
    def validate(self,**kwargs):
        args=dict(checkpoint=self.checkpoint,workspace_proven=True,no_workers=True,remote_head=r.BASE)
        args.update(kwargs)
        return r.validate_collision(r.proposed_action(),self.previous,self.status,self.job,self.intent,self.original,**args)
    def test_exact_known_pre_network_collision_has_no_completion_claim(self):
        before=copy.deepcopy((self.previous,self.status,self.job,self.intent,self.original,self.checkpoint))
        self.assertEqual(self.validate()['commit'],r.HEAD)
        self.assertEqual((self.previous,self.status,self.job,self.intent,self.original,self.checkpoint),before)
    def test_changed_source_epoch_progress_or_network_evidence_refuses(self):
        for change in ('source','epoch','active','blocker','publication','terminal','intent'):
            fixture=KnownCollisionTests();fixture.setUp()
            if change=='source':fixture.previous['source_sha']='b'*40
            if change=='epoch':fixture.status['epoch']+=1
            if change=='active':fixture.status['intent']='active'
            if change=='blocker':fixture.job['blocker']='network outcome unknown'
            if change=='publication':fixture.job['stages']['publication']={'returncode':0}
            if change=='terminal':fixture.original['result']={'returncode':0}
            if change=='intent':fixture.intent['head']='b'*40
            with self.subTest(change=change),self.assertRaises(ExecutionBlocked):fixture.validate()
        for kwargs in ({'remote_head':r.HEAD},{'workspace_proven':False},{'no_workers':False},{'checkpoint':{}}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ExecutionBlocked):self.validate(**kwargs)

import os
@unittest.skipUnless(os.name=='posix','real existing admission file locks require POSIX')
class RecoveryEntrypointTests(unittest.TestCase):
    def test_recovery_loads_actual_installer_and_acquires_existing_fence(self):
        import tempfile,json,fcntl,importlib.util,sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();stage=root/'stage';stage.mkdir();(root/'current').mkdir()
            config={'projects':[{'key':'a'*64}]}
            (root/'current/config.json').write_text(json.dumps(config))
            (stage/'config.json').write_text('{}');(stage/'publication-quarantine.json').write_text(json.dumps(r.proposed_action()))
            installer=Path(__file__).resolve().parents[1]/'tools/macos_supervisor_install.py'
            (stage/'install.py').write_bytes(installer.read_bytes())
            lock=root/'state'/('a'*64)/'admission.lock';lock.parent.mkdir(parents=True);lock.write_bytes(b'');lock.chmod(0o600)
            original_spec=importlib.util.spec_from_file_location
            def loaded_spec(name,path):
                spec=original_spec(name,path);execute=spec.loader.exec_module
                def execute_local(module):execute(module);module.ROOT=root
                spec.loader.exec_module=execute_local;return spec
            fstat=os.fstat
            def root_file(fd):
                info=fstat(fd);values={name:getattr(info,name) for name in dir(info) if name.startswith('st_')}
                values['st_uid']=0;return SimpleNamespace(**values)
            class ReachedVerifier(Exception):pass
            def verify(config):
                with lock.open('rb') as other:
                    with self.assertRaises(BlockingIOError):fcntl.flock(other.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                raise ReachedVerifier()
            flags={name:getattr(sys.flags,name) for name in dir(sys.flags) if not name.startswith('_') and not callable(getattr(sys.flags,name))}
            flags.update(isolated=1,no_site=1,dont_write_bytecode=1)
            # Synthetic root metadata only; actual installer loading and fcntl
            # use a disposable file, never any installed package or live state.
            with patch.object(r,'INSTALL_ROOT',root),patch.object(r,'private_root_file'), \
                 patch.object(r.sys,'platform','darwin'),patch.object(r.os,'geteuid',return_value=0), \
                 patch.object(r.sys,'flags',SimpleNamespace(**flags)),patch.object(r.os,'fstat',side_effect=root_file), \
                 patch('do_again.supervisor.macos_execution.private_root_file'), \
                 patch('do_again.supervisor.macos_server.verify_installation',side_effect=verify), \
                 patch('importlib.util.spec_from_file_location',side_effect=loaded_spec):
                with self.assertRaises(ReachedVerifier):r.recover(stage)
            with lock.open('rb') as released:fcntl.flock(released.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
