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
