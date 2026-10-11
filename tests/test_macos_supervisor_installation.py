"""Installation regressions use temporary fixtures, never administrator mutations."""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


@unittest.skipUnless(os.name=='posix','macOS installer uses POSIX identity modules')
class InstallationTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('installer',Path(__file__).resolve().parents[1]/'tools/macos_supervisor_install.py')
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    @unittest.skipUnless(sys.platform=='darwin','root installer is supported only on macOS')
    def test_installer_early_chat_gate_works_in_isolated_system_python(self):
        import subprocess
        current=self.root/'current';current.mkdir()
        stage=self.root/'stage';stage.mkdir()
        (current/'config.json').write_text(json.dumps({
            'live_canary':{'chat_url':'https://chatgpt.com/c/fixture-old'}}))
        (stage/'config.json').write_text(json.dumps({
            'source_sha':'b'*40,
            'live_canary':{'chat_url':'https://chatgpt.com/c/fixture-old'}}))
        installer=Path(__file__).resolve().parents[1]/'tools/macos_supervisor_install.py'
        script="""
import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('sealed_installer',sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROOT=Path(sys.argv[2])
module.verify_stage=lambda path:{'source_sha':'b'*40}
module.os.geteuid=lambda:0
try:
    module.install(Path(sys.argv[3]))
except RuntimeError as exc:
    if 'distinct ChatGPT conversation' not in str(exc):
        raise
else:
    raise AssertionError('duplicate conversation was accepted')
"""
        child=subprocess.run([sys.executable,'-I','-S','-c',script,str(installer),
                              str(self.root),str(stage)],capture_output=True,text=True)
        self.assertEqual(child.returncode,0,child.stderr[-800:])

    def test_installer_refuses_mixed_browser_and_codex_grants_before_any_service_effect(self):
        cfg={'live_canary':{'nonce':'a'*24},
             'codex_canary':{'nonce':'b'*24},
             'projects':[]}
        with patch.object(self.module,'ROOT',self.root),patch.object(
                self.module.subprocess,'run') as effects:
            with self.assertRaisesRegex(RuntimeError,'may not coexist'):
                self.module.verify_worker_quiescence(cfg)
        effects.assert_not_called()

    def worker_quiescence_fixture(self):
        from do_again.supervisor.macos_execution import ExecutionBlocked
        key='c'*64
        repo=self.root/'synthetic-canary';repo.mkdir()
        state=self.root/'state'/key;state.mkdir(parents=True)
        agents=self.root/'agents';agents.mkdir()
        label='io.github.tran-steven.do-again.worker.'+key[:12]
        definition=agents/(label+'.plist')
        definition.write_bytes(b'known-root-owned-plist-fixture')
        lease=state/'worker-instance.json'
        lease.write_text(json.dumps({'epoch':2,'pid':12345,'identity':[501,12,34,1]}))
        journal=state/'worker-deployment.json'
        record={'phase':'running','epoch':2,'source_sha':'a'*40,
                'plist_sha256':hashlib.sha256(definition.read_bytes()).hexdigest()}
        journal.write_text(json.dumps(record))
        config={'source_sha':'a'*40,'operator_uid':501,'operator_gid':20,
                'operator_home':str(self.root),'authority_path':str(self.root/'authority'),
                'projects':[{'repo':str(repo),'key':key,'uid':401,'gid':401}]}
        actual_stat=type(journal).stat
        def root_owned(path,*args,**kwargs):
            st=actual_stat(path,*args,**kwargs)
            if path in (journal,lease,definition):
                from types import SimpleNamespace
                return SimpleNamespace(st_uid=0,st_nlink=1,st_mode=st.st_mode)
            return st
        return config,journal,record,agents,root_owned

    def test_install_reconciles_proven_withdrawal_without_repeating_launchd_effect(self):
        config,journal,record,agents,root_owned=self.worker_quiescence_fixture()
        result=Mock(returncode=113,stderr='Could not find service')
        with patch.object(self.module,'ROOT',self.root), \
             patch.object(self.module,'WORKER_AGENTS',agents), \
             patch.object(type(journal),'stat',new=root_owned), \
             patch.object(self.module.subprocess,'run',return_value=result) as launchd, \
             patch('do_again.supervisor.authority.AuthorityRegistry') as registry, \
             patch('do_again.supervisor.service_probe.verify_process_withdrawn') as birth:
            registry.return_value.status.return_value={'intent':'maintenance'}
            self.module.verify_worker_quiescence(config)
            launchd.assert_called_once()
            self.assertEqual(launchd.call_args.args[0][:2],['/bin/launchctl','print'])
            birth.assert_called_once_with(12345,[501,12,34,1],timeout=3)
        saved=json.loads(journal.read_text())
        self.assertEqual(saved['phase'],'withdrawn')
        self.assertEqual(saved['recovery_evidence']['previous_phase'],'running')
        self.assertTrue(saved['recovery_evidence']['original_worker_withdrawn'])

    def test_install_refuses_live_or_unverified_withdrawal_without_journal_mutation(self):
        config,journal,record,agents,root_owned=self.worker_quiescence_fixture()
        original=journal.read_bytes()
        cases=[('active',None),('maintenance',RuntimeError('original worker remains alive'))]
        for intent,error in cases:
            with self.subTest(intent=intent,error=bool(error)), \
                 patch.object(self.module,'ROOT',self.root), \
                 patch.object(self.module,'WORKER_AGENTS',agents), \
                 patch.object(type(journal),'stat',new=root_owned), \
                 patch.object(self.module.subprocess,'run',
                              return_value=Mock(returncode=113,stderr='Could not find service')), \
                 patch('do_again.supervisor.authority.AuthorityRegistry') as registry, \
                 patch('do_again.supervisor.service_probe.verify_process_withdrawn',
                       side_effect=error) as birth:
                registry.return_value.status.return_value={'intent':intent}
                with self.assertRaises(RuntimeError):
                    self.module.verify_worker_quiescence(config)
                if intent!='maintenance':birth.assert_not_called()
            self.assertEqual(journal.read_bytes(),original)
        with patch.object(self.module,'ROOT',self.root), \
             patch.object(self.module,'WORKER_AGENTS',agents), \
             patch.object(type(journal),'stat',new=root_owned), \
             patch.object(self.module.subprocess,'run',
                          return_value=Mock(returncode=0,stderr='')), \
             patch('do_again.supervisor.authority.AuthorityRegistry') as registry, \
             patch('do_again.supervisor.service_probe.verify_process_withdrawn') as birth:
            registry.return_value.status.return_value={'intent':'maintenance'}
            with self.assertRaisesRegex(RuntimeError,'loaded engineering worker'):
                self.module.verify_worker_quiescence(config)
            birth.assert_not_called()
        self.assertEqual(journal.read_bytes(),original)

    @unittest.skipUnless(sys.platform=='darwin','root installer is supported only on macOS')
    def test_install_rejects_reused_canary_chat_before_privileged_effects(self):
        current=self.root/'current';current.mkdir()
        stage=self.root/'stage';stage.mkdir()
        old={'live_canary':{'nonce':'a'*24,'chat_url':'https://chatgpt.com/c/old'}}
        candidate={'live_canary':{'nonce':'b'*24,'chat_url':'https://chatgpt.com/c/old'},
                   'source_sha':'b'*40}
        (current/'config.json').write_text(json.dumps(old))
        (stage/'config.json').write_text(json.dumps(candidate))
        with patch.object(self.module,'ROOT',self.root), \
             patch.object(self.module.os,'geteuid',return_value=0), \
             patch.object(self.module,'verify_stage',return_value={'source_sha':'b'*40}), \
             patch.object(self.module,'secure_directory') as privileged:
            with self.assertRaisesRegex(RuntimeError,'distinct ChatGPT conversation'):
                self.module.install(stage)
            privileged.assert_not_called()
        self.assertEqual(json.loads((current/'config.json').read_text()),old)

    def test_runtime_executable_paths_are_validated_before_cutover(self):
        current=self.root/'current'
        valid={'python':str(current/'runtimes/python/bin/python3'),
               'git':str(current/'runtimes/git/bin/git')}
        self.assertEqual(len(self.module.runtime_executables(valid,current)),2)
        for invalid in ({'python':valid['python']}, {**valid,'git':'/usr/bin/git'},
                        {**valid,'python':'/tmp/untrusted'}):
            with self.assertRaises(RuntimeError):
                self.module.runtime_executables(invalid,current)

    def recovery_fixture(self):
        from do_again.supervisor.authority import AuthorityRegistry
        from do_again.supervisor.macos_execution import ExecutionLedger
        self.root=self.root.resolve()
        current=self.root/'current';candidate=self.root/'previous-fixture'
        current.mkdir();candidate.mkdir()
        database=self.root/'effects.sqlite'
        registry=AuthorityRegistry(database);registry.initialize()
        ledger=ExecutionLedger(database)
        ledger.reserve('fixture','completed','fingerprint')
        ledger.finish('fixture','completed',{'status':'succeeded'})
        config={'source_sha':'a'*40,'production_ready':False,
                'recovery_contract':self.module.RECOVERY_CONTRACT,
                'authority_path':str(database),'operator_uid':501,'operator_gid':20,
                'operator_home':'/fixture','dependency_artifacts':{},'projects':[]}
        (current/'config.json').write_text(json.dumps(config))
        (candidate/'config.json').write_text(json.dumps({**config,'source_sha':'b'*40}))
        (current/'manifest.json').write_text(json.dumps({'source_sha':'a'*40}))
        (candidate/'manifest.json').write_text(json.dumps({'source_sha':'b'*40}))
        return current,candidate,database,ledger

    def test_recovery_preserves_newer_effects_and_requires_exact_compatible_source(self):
        current,candidate,database,ledger=self.recovery_fixture()
        original=database.read_bytes()
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module,'verify_stage',
                side_effect=lambda path: {'source_sha':json.loads((path/'config.json').read_text())['source_sha']}):
            self.assertEqual(self.module.recovery_candidate('b'*40),candidate)
            self.assertEqual(database.read_bytes(),original)
            self.assertEqual(ledger.lookup('fixture','completed','fingerprint')['status'],'succeeded')
            for source in ('main','b'*7,'c'*40):
                with self.assertRaises(RuntimeError):self.module.recovery_candidate(source)
            duplicate=self.root/'previous-duplicate';duplicate.mkdir()
            (duplicate/'config.json').write_bytes((candidate/'config.json').read_bytes())
            (duplicate/'manifest.json').write_bytes((candidate/'manifest.json').read_bytes())
            self.assertIn(self.module.recovery_candidate('b'*40),(candidate,duplicate))
            (duplicate/'manifest.json').write_text(json.dumps({'source_sha':'b'*40,'different_build':True}))
            with self.assertRaisesRegex(RuntimeError,'ambiguous'):self.module.recovery_candidate('b'*40)
            (duplicate/'config.json').unlink();(duplicate/'manifest.json').unlink();duplicate.rmdir()
            with self.assertRaisesRegex(RuntimeError,'already installed'):self.module.recovery_candidate('a'*40)
            config=json.loads((candidate/'config.json').read_text());config.pop('recovery_contract')
            (candidate/'config.json').write_text(json.dumps(config))
            with self.assertRaisesRegex(RuntimeError,'contract'):self.module.recovery_candidate('b'*40)

    def test_recovery_rejects_pending_effect_changed_scope_and_new_schema(self):
        import sqlite3
        current,candidate,database,ledger=self.recovery_fixture()
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module,'verify_stage',
                side_effect=lambda path: {'source_sha':json.loads((path/'config.json').read_text())['source_sha']}):
            config=json.loads((candidate/'config.json').read_text())
            (candidate/'config.json').write_text(json.dumps({**config,'operator_uid':502}))
            with self.assertRaisesRegex(RuntimeError,'scope'):self.module.recovery_candidate('b'*40)
            (candidate/'config.json').write_text(json.dumps(config))
            with sqlite3.connect(database) as db:db.execute('PRAGMA user_version=2')
            with self.assertRaisesRegex(RuntimeError,'schema'):self.module.recovery_candidate('b'*40)
            with sqlite3.connect(database) as db:db.execute('PRAGMA user_version=1')
            ledger.reserve('fixture','uncertain','fingerprint')
            with self.assertRaisesRegex(RuntimeError,'unresolved'):self.module.recovery_candidate('b'*40)

    def test_cutover_crash_preserves_ticket_runtime_and_newer_effects(self):
        current=self.root/'current';stage=self.root/'stage'
        current.mkdir();stage.mkdir()
        (current/'version').write_text('old');(stage/'version').write_text('new')
        effects=self.root/'effects';effects.write_text('newer terminal effect')
        journal_path=self.root/'installation.json';journal={'source_sha':'a'*40}
        rename=os.rename;calls=[]
        def interrupt(source,target):
            calls.append((source,target))
            ticket=json.loads(journal_path.read_text())['runtime_transition']
            self.assertEqual(ticket['phase'],'cutover_started')
            if len(calls)==2:raise OSError('injected interruption after retaining old runtime')
            rename(source,target)
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module.os,'rename',side_effect=interrupt):
            with self.assertRaisesRegex(OSError,'injected'):
                self.module.select_runtime(stage,current,journal_path,journal,'b'*40,recovery=False)
        ticket=json.loads(journal_path.read_text())['runtime_transition']
        self.assertEqual((Path(ticket['retained_path'])/'version').read_text(),'old')
        self.assertEqual((stage/'version').read_text(),'new');self.assertFalse(current.exists())
        self.assertEqual(effects.read_text(),'newer terminal effect')
        self.assertEqual(ticket['phase'],'cutover_started')

    def test_successful_runtime_selection_flushes_each_rename_before_advancing(self):
        current=self.root/'current';stage=self.root/'stage'
        current.mkdir();stage.mkdir();(current/'version').write_text('old');(stage/'version').write_text('new')
        journal_path=self.root/'installation.json';journal={'source_sha':'a'*40}
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module,'sync_directory',wraps=self.module.sync_directory) as sync:
            self.module.select_runtime(stage,current,journal_path,journal,'b'*40,recovery=True)
        self.assertEqual(sync.call_count,4)  # Two durable tickets and two package renames.
        ticket=json.loads(journal_path.read_text())['runtime_transition']
        self.assertEqual(ticket['phase'],'runtime_selected');self.assertEqual(ticket['operation'],'recovery')
        self.assertEqual((current/'version').read_text(),'new')
        self.assertEqual((Path(ticket['retained_path'])/'version').read_text(),'old')



    def test_sonary_snapshot_freshness_rejects_stale_or_ambiguous_remote(self):
        spec=importlib.util.spec_from_file_location(
            'preparer',Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        repo=self.root/'Sonary'
        local='a'*40;other='b'*40
        def run(*args):
            if args[1]=='rev-parse':return local
            if args[1]=='ls-remote':return other+'\trefs/heads/main'
            raise AssertionError('unexpected Git operation')
        with patch.object(module,'git',side_effect=run) as command:
            with self.assertRaisesRegex(ValueError,'stale'):
                module.require_fresh_sonary_snapshot(repo)
            self.assertEqual([c.args[1] for c in command.call_args_list],
                             ['rev-parse','ls-remote'])
        with patch.object(module,'git',side_effect=[
                local,local+'\trefs/heads/main']):
            self.assertEqual(module.require_fresh_sonary_snapshot(repo),local)
        for bad in ('',other,other+'\trefs/heads/other',local+'\trefs/heads/main\n'+local):
            with patch.object(module,'git',side_effect=[local,bad]):
                with self.subTest(remote=bad),self.assertRaises(ValueError):
                    module.require_fresh_sonary_snapshot(repo)

    def test_sonary_enrollment_is_explicit_add_only_and_preserves_parent_snapshots(self):
        from do_again.supervisor.authority import project_identity
        from do_again.supervisor.macos_execution import EXECUTION_ROOT
        home=Path('/Users/fixture')
        def scoped(name,account,uid):
            repo=home/name;key=project_identity(repo)
            return {'repo':str(repo),'key':key,'uid':uid,'gid':uid,'account':account,
                    'worktree':str(EXECUTION_ROOT/key/'worktree'),
                    'github_repository':'Tran-Steven/'+name,
                    'source_sha':str(uid)*40,'goal_revision':'maintenance-fixture'}
        parents=[scoped('do-again','_doagain_da',401),scoped('jobpipe','_doagain_jp',402)]
        old={'source_sha':'a'*40,'operator_home':str(home),'production_ready':False,
             'projects':parents}
        next_cfg={**old,'source_sha':'b'*40,'projects':[dict(p) for p in parents]}
        self.assertFalse(self.module.validate_project_scope(old))
        self.assertFalse(self.module.validate_project_scope(next_cfg,old))
        sonary=scoped('Sonary','_doagain_so',403)
        new={**next_cfg,'projects':[ *next_cfg['projects'],sonary ],
             'scope_enrollment':{'kind':'sonary_add_only','previous_source_sha':'a'*40}}
        self.assertTrue(self.module.validate_project_scope(new,old))
        self.assertFalse(self.module.validate_project_scope(
            {**new,'source_sha':'c'*40,'scope_enrollment':None},new))
        for candidate in (
            {**new,'scope_enrollment':None},
            {**new,'scope_enrollment':{'kind':'sonary_add_only','previous_source_sha':'c'*40}},
            {**new,'projects':[parents[0],sonary]},
            {**new,'projects':[parents[0],{**parents[1],'source_sha':'c'*40},sonary]},
            {**new,'projects':[ *parents,{**sonary,'uid':402,'gid':402}]},
            {**new,'projects':[ *parents,{**sonary,'repo':str(home/'sonary')}]},
            {**new,'projects':[ *parents,{**sonary,'github_repository':'Tran-Steven/jobpipe'}]},
            {**new,'projects':[ *parents,{**sonary,'worktree':parents[1]['worktree']}]},
            {**new,'projects':[ *parents,{**sonary,'account':'_doagain_other'}]},
            {**new,'projects':[ *parents,sonary,{**sonary,'account':'_doagain_x','uid':404,'gid':404}]},
        ):
            with self.subTest(candidate=candidate),self.assertRaises(RuntimeError):
                self.module.validate_project_scope(candidate,old)
        with self.assertRaisesRegex(RuntimeError,'scope'):
            self.module.validate_project_scope(next_cfg,new)
        with self.assertRaisesRegex(RuntimeError,'implicitly enroll'):
            self.module.validate_project_scope(new)

    def test_account_collision_requires_trusted_provenance_without_mutation(self):
        project={'account':'_doagain_da','uid':400,'gid':400}
        with patch.object(self.module.pwd,'getpwnam',return_value=Mock()),patch.object(self.module,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'provenance'):
                self.module.create_account(project,self.root/'journal',{'accounts':{}})
            run.assert_not_called()

    def test_partial_account_creation_records_ambiguity_before_effect(self):
        project={'account':'_doagain_da','uid':400,'gid':400};path=self.root/'journal'
        journal={'accounts':{}}
        with patch.object(self.module.pwd,'getpwnam',side_effect=KeyError),patch.object(self.module.pwd,'getpwuid',side_effect=KeyError),patch.object(self.module.grp,'getgrgid',side_effect=KeyError),patch.object(self.module,'run',side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):self.module.create_account(project,path,journal)
        self.assertEqual(json.loads(path.read_text())['pending_account']['uid'],400)
        self.assertEqual(json.loads(path.read_text())['accounts'],{})

    def test_root_stage_rejects_alias_before_installer_execution(self):
        stage=self.root/'stage';stage.mkdir();outside=self.root/'outside';outside.write_text('fixture')
        (stage/'alias').symlink_to(outside)
        (stage/'manifest.json').write_text(json.dumps({'files':{}}))
        with patch.object(self.module.os,'geteuid',return_value=0):
            with self.assertRaises(RuntimeError):self.module.verify_stage(stage)

    def test_secure_directory_does_not_follow_symlink(self):
        target=self.root/'target';target.mkdir();alias=self.root/'alias';alias.symlink_to(target)
        with patch.object(self.module.os,'chown') as chown:
            with self.assertRaises(RuntimeError):self.module.secure_directory(alias)
            chown.assert_not_called()

    def test_preparation_refuses_dirty_source_before_runtime_or_identity_mutation(self):
        spec=importlib.util.spec_from_file_location('preparer',Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module,'git',return_value=' M preserved-file'),patch.object(module,'seal_runtime') as runtime:
            with self.assertRaisesRegex(ValueError,'commit and validate'):
                module.prepare(self.root,self.root,self.root,self.root/'output')
            runtime.assert_not_called();self.assertFalse((self.root/'output').exists())

    @unittest.skipUnless(sys.platform=='darwin','Apple-signed Git packaging is macOS-only')
    def test_failed_git_signature_removes_unverified_copy(self):
        spec=importlib.util.spec_from_file_location('failed_git_preparer',Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(module.subprocess,'run',side_effect=ValueError('signature rejected')):
            with self.assertRaisesRegex(ValueError,'signature rejected'):
                module.seal_git(self.root)
        self.assertFalse((self.root/'runtimes/git/bin/git').exists())

    @unittest.skipUnless(sys.platform=='darwin','Apple-signed Git packaging is macOS-only')
    def test_sealed_git_runs_without_developer_tool_launcher(self):
        import subprocess
        spec=importlib.util.spec_from_file_location('git_preparer',Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        sealed=module.seal_git(self.root)
        binary=self.root/'runtimes/git/bin/git'
        self.assertTrue(sealed.endswith('/current/runtimes/git/bin/git'))
        self.assertTrue(binary.is_file());self.assertFalse(binary.is_symlink())
        version=subprocess.check_output([str(binary),'--version'],env={'PATH':'/nonexistent','HOME':str(self.root)},text=True)
        self.assertTrue(version.startswith('git version'))

    def test_remote_tracking_bundle_materializes_exact_commit(self):
        import subprocess
        git='/usr/bin/git' if Path('/usr/bin/git').exists() else 'git'
        repo=self.root/'source';repo.mkdir()
        def command(*args):
            return subprocess.check_output([git,*args],text=True,stderr=subprocess.DEVNULL).strip()
        command('init',str(repo));(repo/'tracked.txt').write_text('approved snapshot')
        command('-C',str(repo),'add','tracked.txt')
        command('-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','snapshot')
        sha=command('-C',str(repo),'rev-parse','HEAD')
        command('-C',str(repo),'update-ref','refs/remotes/origin/main',sha)
        bundle=self.root/'snapshot.bundle';command('-C',str(repo),'bundle','create',str(bundle),'refs/remotes/origin/main')
        key='fixture';target=self.root/'execution'/key/'worktree'
        project={'worktree':str(target),'key':key,'uid':os.getuid(),'gid':os.getgid(),'source_sha':sha,'bundle':'snapshot.bundle'}
        config={'operator_uid':os.getuid(),'operator_gid':os.getgid(),'operator_home':str(self.root)}
        def allowed_directory(path,mode=0o755):path.mkdir(parents=True,exist_ok=True);path.chmod(mode)
        def run(args,**kwargs):
            # Drop only root-required credential setters for an unprivileged fixture.
            kwargs={k:v for k,v in kwargs.items() if k not in {'user','group','extra_groups'}}
            result=subprocess.run(args,capture_output=True,text=True,check=True,**kwargs)
            return result
        with patch.object(self.module,'EXEC',self.root/'execution'),patch.object(self.module,'secure_directory',side_effect=allowed_directory),patch.object(self.module.os,'chown'),patch.object(self.module,'run',side_effect=run):
            self.module.provision_worktree(config,project,self.root)
        self.assertEqual((target/'tracked.txt').read_text(),'approved snapshot')
        self.assertEqual(command('-C',str(target),'rev-parse','HEAD'),sha)

    def test_preview_cutover_rejects_ambiguous_execution_before_service_effect(self):
        import sqlite3
        from do_again.supervisor.macos_execution import ExecutionLedger
        config={'production_ready':False,'projects':[{'repo':'/synthetic','key':'test','uid':401,'gid':401,'account':'_doagain_da','worktree':'/synthetic/work','source_sha':'approved'}]}
        (self.root/'current').mkdir();(self.root/'current/config.json').write_text(json.dumps(config))
        (self.root/'state/test').mkdir(parents=True)
        path=self.root/'authority.sqlite';ExecutionLedger(path).reserve('test','request-ambiguous','fingerprint')
        registry=Mock(path=path);registry.status.return_value={'intent':'maintenance'}
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module,'verify_stage'), \
             patch.object(self.module,'validate_project_scope',return_value=False), \
             patch.object(self.module,'run') as run:
            for live in (True,False):
                with self.subTest(supervisor_running=live),self.assertRaisesRegex(RuntimeError,'ambiguous'):
                    with self.module.preview_cutover(config,registry,live):self.fail('unsafe cutover admitted')
            run.assert_not_called()

    def test_worker_cutover_requires_absence_and_preserves_ambiguous_deployment(self):
        config={'operator_uid':501,'operator_gid':20,'operator_home':'/synthetic',
                'projects':[{'key':'a'*64}]}
        state=self.root/'state'/('a'*64);state.mkdir(parents=True)
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module.subprocess,'run') as effect:
            effect.return_value=Mock(returncode=113,stderr='Could not find service')
            self.module.verify_worker_quiescence(config)
            effect.return_value=Mock(returncode=1,stderr='Permission denied')
            with self.assertRaisesRegex(RuntimeError,'unproven'):self.module.verify_worker_quiescence(config)
            effect.return_value=Mock(returncode=0,stderr='')
            with self.assertRaisesRegex(RuntimeError,'loaded'):self.module.verify_worker_quiescence(config)
            (state/'worker-deployment.json').write_text(json.dumps({'phase':'start_started'}))
            # Unknown ownership is preserved rather than adopted by an upgrade.
            with self.assertRaisesRegex(RuntimeError,'ownership|withdrawal'):
                self.module.verify_worker_quiescence(config)

    def test_interrupted_staging_recovery_requires_definition_pending_and_service_absence(self):
        config={'operator_uid':501,'operator_gid':20,'operator_home':'/synthetic','projects':[{'key':'a'*64}]}
        state=self.root/'state'/('a'*64);state.mkdir(parents=True)
        journal=state/'worker-deployment.json';journal.write_text(json.dumps({'phase':'stage_started','source_sha':'preserved'}))
        agents=self.root/'agents';agents.mkdir()
        original_stat=type(journal).stat
        def owned(path,*args,**kwargs):
            if path==journal:return Mock(st_uid=0,st_nlink=1,st_mode=0o100600)
            return original_stat(path,*args,**kwargs)
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module,'WORKER_AGENTS',agents),              patch.object(type(journal),'stat',new=owned),patch.object(self.module.subprocess,'run',return_value=Mock(returncode=113,stderr='Could not find service')):
            pending=agents/('io.github.tran-steven.do-again.worker.'+'a'*12+'.plist.pending')
            pending.write_bytes(b'preserve interrupted artifact')
            with self.assertRaisesRegex(RuntimeError,'service artifacts'):self.module.verify_worker_quiescence(config)
            self.assertEqual(json.loads(journal.read_text())['phase'],'stage_started')
            pending.unlink()  # Synthetic test artifact only.
            self.module.verify_worker_quiescence(config)
            recovered=json.loads(journal.read_text())
            self.assertEqual(recovered['phase'],'stage_failed_pre_effect')
            self.assertEqual(recovered['source_sha'],'preserved')
            self.assertTrue(recovered['recovery_evidence']['service_absent'])

    def test_preview_cutover_preserves_production_helper(self):
        config={'production_ready':True,'projects':[]}
        (self.root/'current').mkdir();(self.root/'current/config.json').write_text(json.dumps(config))
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module,'verify_stage'),patch.object(self.module,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'production'):
                with self.module.preview_cutover(config,Mock(),True):self.fail('production helper admitted')
            run.assert_not_called()

    @unittest.skipUnless(sys.platform=='darwin','native relocation requires macOS tools')
    def test_sealed_runtime_does_not_import_host_packages_or_framework(self):
        spec=importlib.util.spec_from_file_location('preparer',Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        # CI setup-python commonly uses a non-framework distribution. The
        # production builder rejects it; relocation runs on supported frameworks.
        if not (Path(sys.base_prefix)/'Resources/Python.app/Contents/MacOS/Python').is_file():
            self.skipTest('framework Python is required for installation preparation')
        module.seal_runtime(self.root)
        runtime=self.root/'runtimes/python'
        self.assertFalse(list(runtime.rglob('site-packages')))
        self.assertFalse(list(runtime.rglob('*.pyc')))
        import subprocess
        observed=subprocess.check_output([str(runtime/'bin/python3'),'-I','-S','-B','-c','import sys;print(sys.base_prefix)'],text=True).strip()
        self.assertEqual(Path(observed).resolve(),runtime.resolve())
