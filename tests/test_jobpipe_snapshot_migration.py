"""Offline jobpipe snapshot migration never rewrites the normal checkout.

These fixture tests exercise the root installer helpers without running as
root, touching real macOS worktrees, or authorizing applications.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(os.name == 'posix', 'native migration fixture requires POSIX Git')
class JobpipeSnapshotMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        installer=Path(__file__).resolve().parents[1]/'tools/macos_supervisor_install.py'
        spec=importlib.util.spec_from_file_location('migration_installer',installer)
        self.module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.home=self.root/'operator'
        self.home.mkdir()
        self.source=self.home/'jobpipe'
        self.source.mkdir()
        self.git_exe='/usr/bin/git'
        if not Path(self.git_exe).exists():
            self.skipTest('native POSIX Git required')
        self.git(self.source,'init','-q')
        tracked=self.source/'tracked.txt'
        tracked.write_text('old approved snapshot\n')
        self.git(self.source,'add','tracked.txt')
        self.git(self.source,'-c','user.email=fixture@example.invalid',
                 '-c','user.name=Fixture','commit','-qm','old snapshot')
        self.old=self.git(self.source,'rev-parse','HEAD')
        tracked.write_text('current upstream snapshot\n')
        self.git(self.source,'add','tracked.txt')
        self.git(self.source,'-c','user.email=fixture@example.invalid',
                 '-c','user.name=Fixture','commit','-qm','new snapshot')
        self.latest=self.git(self.source,'rev-parse','HEAD')
        self.key=hashlib.sha256(os.fsencode(self.source)).hexdigest()
        self.exec_root=self.root/'execution'
        self.work=self.exec_root/self.key/'worktree'
        self.work.parent.mkdir(parents=True)
        self.git(self.root,'clone','-q',str(self.source),str(self.work))
        self.git(self.work,'checkout','-q','--detach',self.old)
        self.snapshot_dir=self.root/'current'
        (self.snapshot_dir/'snapshots').mkdir(parents=True)
        self.bundle=self.snapshot_dir/'snapshots/jobpipe.bundle'
        self.git(self.source,'bundle','create',str(self.bundle),'HEAD')
        (self.source/'private-uncommitted.txt').write_text('do not change me\n')
        parent=self.home/'do-again'
        parent_key=hashlib.sha256(os.fsencode(parent)).hexdigest()
        self.old_jobpipe={
            'repo':str(self.source),'account':'_doagain_jp','key':self.key,
            'uid':402,'gid':402,'worktree':str(self.work),
            'github_repository':'Tran-Steven/jobpipe','source_sha':self.old,
            'bundle':'snapshots/jobpipe.bundle','goal_revision':'jobpipe-maintenance',
        }
        parent_project={
            'repo':str(parent),'account':'_doagain_da','key':parent_key,
            'uid':401,'gid':401,'worktree':str(self.exec_root/parent_key/'worktree'),
            'github_repository':'Tran-Steven/do-again','source_sha':'a'*40,
            'bundle':'snapshots/do-again.bundle','goal_revision':'do-again-maintenance',
        }
        self.previous={'source_sha':'b'*40,'production_ready':False,
            'operator_home':str(self.home),'operator_uid':os.getuid(),
            'operator_gid':os.getgid(),'projects':[parent_project,self.old_jobpipe]}
        self.updated=copy.deepcopy(self.previous)
        self.updated['source_sha']='c'*40
        self.updated['projects'][1]['source_sha']=self.latest
        self.updated['snapshot_migration']={
            'kind':'jobpipe_fast_forward_v1','installed_source_sha':'b'*40,
            'project_key':self.key,'from_sha':self.old,'to_sha':self.latest}
        utc_now=datetime.now(timezone.utc).replace(microsecond=0)
        self.updated['development_scope']={
            'kind':'jobpipe-offline-v1','project_key':self.key,
            'source_sha':self.updated['source_sha'],
            'goal_revision':'jobpipe-maintenance',
            'issued_at_utc':(utc_now-timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'expires_at_utc':(utc_now+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'network_access':False,'application_submissions':False}
        self.journal_path=self.root/'install.json'
        self.journal={}

    def git(self,repo,*args):
        process=subprocess.run([self.git_exe,'-C',str(repo),*args],
            check=True,capture_output=True,text=True)
        return process.stdout.strip()

    def fixture_run(self,args,**kw):
        kw={k:v for k,v in kw.items() if k not in ('user','group','extra_groups')}
        return subprocess.run(args,check=True,capture_output=True,text=True,**kw)

    def test_only_the_pinned_jobpipe_fast_forward_changes(self):
        with patch.object(self.module,'EXEC',self.exec_root):
            self.assertFalse(self.module.validate_project_scope(self.updated,self.previous))
            for field,val in [('project_key','e'*64),('from_sha','d'*40),
                              ('installed_source_sha','0'*40),('to_sha','e'*40)]:
                bad=copy.deepcopy(self.updated)
                bad['snapshot_migration'][field]=val
                with self.subTest(field=field),self.assertRaisesRegex(RuntimeError,'migration'):
                    self.module.validate_project_scope(bad,self.previous)
            for mutate in (
                lambda cfg:cfg['projects'][0].update(source_sha='f'*40),
                lambda cfg:cfg['projects'][1].update(uid=404,gid=404),
                lambda cfg:cfg['projects'][1].update(goal_revision='other'),
                lambda cfg:cfg.update(scope_enrollment={'kind':'sonary_add_only'}),
                lambda cfg:cfg['development_scope'].update(application_submissions=True),
            ):
                bad=copy.deepcopy(self.updated)
                mutate(bad)
                with self.assertRaises(RuntimeError):
                    self.module.validate_project_scope(bad,self.previous)

    def test_dirty_protected_worktree_refused_before_staging(self):
        (self.work/'tracked.txt').write_text('unsaved isolated modification\n')
        with patch.object(self.module,'EXEC',self.exec_root):
            with self.assertRaisesRegex(RuntimeError,'modified'):
                self.module.verify_jobpipe_migration_preflight(self.updated,self.previous)
        self.assertFalse(self.journal_path.exists())
        self.assertTrue((self.work/'tracked.txt').exists())

    def test_fast_forward_promotes_new_and_retains_old_without_primary_changes(self):
        with patch.object(self.module,'EXEC',self.exec_root),\
             patch.object(self.module.os,'chown'),\
             patch.object(self.module,'run',side_effect=self.fixture_run):
            proof=self.module.verify_jobpipe_migration_preflight(self.updated,self.previous)
            self.module.migrate_jobpipe_snapshot(
                self.updated,self.snapshot_dir,self.previous,self.journal_path,self.journal)
        self.assertEqual(self.git(self.work,'rev-parse','HEAD'),self.latest)
        self.assertEqual(self.git(proof['retained'],'rev-parse','HEAD'),self.old)
        self.assertEqual((self.source/'private-uncommitted.txt').read_text(),
                         'do not change me\n')
        self.assertEqual(self.journal['snapshot_migration']['phase'],'complete')
        self.assertEqual(self.journal['snapshot_migration']['from_sha'],self.old)

    def test_crash_after_retaining_old_preserves_both_snapshots_no_retry(self):
        real_rename=os.rename
        moves=[]
        def interrupt(src,dst):
            moves.append((str(src),str(dst)))
            if len(moves)==2:
                raise OSError('injected crash before promotion')
            return real_rename(src,dst)
        with patch.object(self.module,'EXEC',self.exec_root),\
             patch.object(self.module.os,'chown'),\
             patch.object(self.module,'run',side_effect=self.fixture_run),\
             patch.object(self.module.os,'rename',side_effect=interrupt):
            with self.assertRaisesRegex(OSError,'injected crash'):
                self.module.migrate_jobpipe_snapshot(
                    self.updated,self.snapshot_dir,self.previous,
                    self.journal_path,self.journal)
        journal=json.loads(self.journal_path.read_text())['snapshot_migration']
        self.assertEqual(journal['phase'],'original_retained')
        self.assertFalse(self.work.exists())
        self.assertEqual(self.git(Path(journal['retained']),'rev-parse','HEAD'),self.old)
        self.assertEqual(self.git(Path(journal['staged']),'rev-parse','HEAD'),self.latest)
        with patch.object(self.module,'EXEC',self.exec_root):
            with self.assertRaises(RuntimeError):
                self.module.verify_jobpipe_migration_preflight(self.updated,self.previous)


if __name__ == '__main__':
    unittest.main()
