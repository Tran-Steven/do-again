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
            with self.assertRaisesRegex(RuntimeError,'ambiguous'):self.module.recovery_candidate('b'*40)
            (duplicate/'config.json').unlink();duplicate.rmdir()
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
        with patch.object(self.module,'ROOT',self.root),patch.object(self.module,'verify_stage'),patch.object(self.module,'run') as run:
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
