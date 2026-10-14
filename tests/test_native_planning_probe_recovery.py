import contextlib,hashlib,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from do_again.supervisor import native_planning_probe_recovery as r
from do_again.supervisor.macos_execution import ExecutionLedger,ExecutionBlocked
from do_again.service.offline_continuation import digest,controller_artifact

@unittest.skipUnless(os.name=='posix','native recovery admission fences require POSIX fcntl')
class ProbeRecoveryEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.ctx=contextlib.ExitStack();self.addCleanup(self.ctx.close)
        self.root=Path(self.ctx.enter_context(tempfile.TemporaryDirectory())).resolve();self.stage=self.root/'stage';self.stage.mkdir();(self.root/'current').mkdir()
        self.key='a'*64;self.repo=self.root/'worktree';self.repo.mkdir();(self.repo/'source.py').write_bytes(b'# public\n'*4000)
        scope={'application_submissions':False,'branch':'operator-control','browser_messages':False,'epoch':37,'expires_at_utc':None,
            'goal_sha256':'e283738090e18d3634230e7d49a78804793c133a0d9a17c642e457e30255a07e','integration':'proposal-only',
            'issued_at_utc':'2026-10-14T05:02:45.628846+00:00','kind':'jobpipe-native-controller-v1','max_cycles':60,
            'max_outstanding':1,'max_requests':60,'max_retries':0,'network_access':False,'planner':'chatgpt-browser-cdp',
            'repository':'Tran-Steven/jobpipe','source_sha':r.SOURCE}
        self.assertEqual(digest(scope),r.SCOPE);self.assertEqual(digest({'scope':scope,'cycle':4,'kind':'planning-probe'}),r.FP)
        self.state=self.root/'state'/self.key;self.state.mkdir(parents=True);self.lock=self.state/'admission.lock';self.lock.write_bytes(b'');self.lock.chmod(0o600)
        project={'key':self.key,'account':'_doagain_jp','repo':str(self.repo),'worktree':str(self.repo),'uid':402}
        browser={'kind':'fixture-browser','source_sha':r.SOURCE,'epoch':37,'issued_at_utc':scope['issued_at_utc'],'max_calls':60}
        self.config={'source_sha':r.SOURCE,'production_ready':False,'authority_path':str(self.root/'authority.sqlite'),
            'projects':[project],'controller_scope':scope,'browser_planning_scope':browser,
            'controller_profile':{'kind':'native-development-autonomous-v1','allowed_paths':['source.py'],'test_files':{}}}
        (self.root/'current/config.json').write_text(json.dumps(self.config));target=json.loads(json.dumps(self.config));target['source_sha']='d'*40
        target['controller_scope'].update(source_sha='d'*40,epoch=39,max_cycles=57,max_requests=57)
        target['browser_planning_scope'].update(source_sha='d'*40,epoch=39,max_calls=57)
        (self.stage/'config.json').write_text(json.dumps(target))
        (self.stage/'planning-probe-recovery.json').write_text(json.dumps(r.proposed_action()))
        (self.stage/'install.py').write_bytes((Path(__file__).resolve().parents[1]/'tools/macos_supervisor_install.py').read_bytes())
        frozen=self.root/'current/package/do_again/supervisor/controller_native_effects.py';frozen.parent.mkdir(parents=True);frozen.write_bytes(b'fixture immutable read-only capture contract')
        expected=['offline-90fb5360625f39209d26-'+str(i) for i in (1,2,3)]
        self.controller=self.state/controller_artifact(scope,'controller.json')
        self.controller.write_text(json.dumps({'state':'stopped','scope_sha256':r.SCOPE,'outstanding':None,'acknowledged':expected,
            'requests':[{'request':{'request_id':rid},'phase':'acknowledged'} for rid in expected]}))
        self.calls=self.state/controller_artifact(scope,'browser-planning-calls.json');self.calls.write_text(json.dumps({'scope_sha256':digest(browser),'calls':{str(i):{'cycle':i,'state':'reserved','call_id':'planner-'+digest(browser)[:20]+'-'+str(i)} for i in (1,2,3)}}))
        self.ledger=ExecutionLedger(Path(self.config['authority_path']));self.intent={'operation':'execute','source_sha':r.SOURCE,'request_fingerprint':r.FP}
        self.ledger.reserve(self.key,r.REQUEST,r.FP,intent=self.intent)
        original_spec=importlib.util.spec_from_file_location
        def staged(name,path):
            spec=original_spec(name,path);execute=spec.loader.exec_module
            def load(module):execute(module);module.ROOT=self.root;module.verify_worker_quiescence=lambda *a,**kw:None
            spec.loader.exec_module=load;return spec
        actual=os.fstat
        def root_stat(fd):
            s=actual(fd);values={n:getattr(s,n) for n in dir(s) if n.startswith('st_')};values['st_uid']=0;return SimpleNamespace(**values)
        flags={n:getattr(sys.flags,n) for n in dir(sys.flags) if not n.startswith('_') and not callable(getattr(sys.flags,n))};flags.update(isolated=1,no_site=1,dont_write_bytecode=1)
        self.native_verify_calls=0
        def verify(config):
            import fcntl
            self.native_verify_calls+=1
            with self.lock.open('rb') as other:
                with self.assertRaises(BlockingIOError):fcntl.flock(other.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        for item in (patch.object(r,'INSTALL_ROOT',self.root),patch.object(r,'private_root_file'),
            patch('do_again.supervisor.macos_execution.private_root_file'),patch.object(r.os,'geteuid',return_value=0),
            patch.object(r.os,'fstat',side_effect=root_stat),patch.object(r.sys,'platform','darwin'),patch.object(r.sys,'flags',SimpleNamespace(**flags)),
            patch.object(r,'EFFECTS_SHA',hashlib.sha256(frozen.read_bytes()).hexdigest()),patch('importlib.util.spec_from_file_location',side_effect=staged),
            patch('do_again.supervisor.macos_server.verify_installation',side_effect=verify),
            patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':r.HEAD}),
            patch('do_again.supervisor.authority.AuthorityRegistry',return_value=SimpleNamespace(status=lambda repo:{'intent':'maintenance','epoch':39})),
            patch.object(r,'MacOSProcesses',return_value=SimpleNamespace(owned=lambda uid:[])),
            patch.object(r,'verify_checkpoints',return_value={self.key:{'commit':r.HEAD,'source_sha':r.SOURCE,'request_id':'offline-90fb5360625f39209d26-3'}})):
            self.ctx.enter_context(item)
    def test_actual_staged_dry_run_append_and_reentry_keep_originals(self):
        before=self.ledger.evidence(self.key);old_controller=self.controller.read_bytes();old_calls=self.calls.read_bytes()
        preview=r.recover(self.stage);self.assertFalse(preview['tests_claimed_passed']);self.assertEqual(self.ledger.evidence(self.key),before)
        self.assertEqual(len(self.ledger.pending(self.key)),1);self.assertEqual(self.ledger.quarantined_planning_probes(self.key),[])
        result=r.recover(self.stage,execute_approved_action=True);self.assertTrue(result['original_journals_unchanged']);self.assertFalse(result['terminal_success'])
        self.assertEqual(self.ledger.evidence(self.key),before);self.assertEqual(self.ledger.pending(self.key),[])
        self.assertEqual(r.recover(self.stage,execute_approved_action=True)['state'],'existing_probe_disposition_verified_read_only')
        self.assertEqual(self.controller.read_bytes(),old_controller);self.assertEqual(self.calls.read_bytes(),old_calls)
        self.assertEqual(self.ledger.observe_request(self.key,r.REQUEST,r.FP)['state'],'post_dispatch_uncertain')
        with self.assertRaises(ExecutionBlocked):self.ledger.finish(self.key,r.REQUEST,{'returncode':0,'source_sha':r.SOURCE})
        self.assertEqual(self.native_verify_calls,3)
    def test_fourth_browser_reservation_blocks_without_audit(self):
        value=json.loads(self.calls.read_text());value['calls']['4']={'cycle':4};self.calls.write_text(json.dumps(value))
        before=self.ledger.evidence(self.key)
        with self.assertRaisesRegex(ExecutionBlocked,'fourth model'):r.recover(self.stage,execute_approved_action=True)
        self.assertEqual(self.ledger.evidence(self.key),before);self.assertEqual(self.ledger.quarantined_planning_probes(self.key),[])

    def test_target_cannot_reset_budget_change_goal_or_expand_projects(self):
        original=json.loads((self.stage/'config.json').read_text())
        variants=[]
        budget=json.loads(json.dumps(original));budget['browser_planning_scope']['max_calls']=60;variants.append(budget)
        goal=json.loads(json.dumps(original));goal['controller_goal']={'goal':'different'};variants.append(goal)
        identity=json.loads(json.dumps(original));identity['projects'][0]['uid']=403;variants.append(identity)
        profile=json.loads(json.dumps(original));profile['controller_profile']['allowed_paths'].append('other.py');variants.append(profile)
        for target in variants:
            before=self.ledger.evidence(self.key);(self.stage/'config.json').write_text(json.dumps(target))
            with self.subTest(target=target),self.assertRaises(ExecutionBlocked):r.recover(self.stage,execute_approved_action=True)
            self.assertEqual(self.ledger.evidence(self.key),before);self.assertEqual(self.ledger.quarantined_planning_probes(self.key),[])
