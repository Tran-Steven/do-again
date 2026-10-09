from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from do_again.supervisor import git_broker
from do_again.supervisor.macos_execution import ExecutionBlocked, ExecutionLedger, ProjectExecution
from do_again.supervisor.macos_server import worktree_authority


@unittest.skipUnless(shutil.which('git'), 'real Git fixture required')
class GitBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.git = shutil.which('git')
        self.env = {'PATH':os.environ.get('PATH',''),'HOME':str(self.root),
                    'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
                    'SYSTEMROOT':os.environ.get('SYSTEMROOT','')}
        self.command('init', '-q', str(self.repo))
        self.command('-C',str(self.repo),'config','user.name','Fixture')
        self.command('-C',str(self.repo),'config','user.email','fixture@example.invalid')
        (self.repo/'selected.txt').write_text('before\n')
        (self.repo/'excluded.txt').write_text('before\n')
        self.command('-C',str(self.repo),'add','.')
        self.command('-C',str(self.repo),'commit','-qm','baseline')
        self.before = self.command('-C',str(self.repo),'rev-parse','HEAD').strip()
        (self.repo/'selected.txt').write_text('after\n')
        (self.repo/'excluded.txt').write_text('excluded change\n')
        self.status = {'intent':'active','epoch':1,'goal_revision':'approved-fixture-task'}
        sealed_git = self.root/'install/current/runtimes/git/bin/git'
        project = ProjectExecution(self.repo,401,401,'_doagain_da',self.repo,(sealed_git,))
        self.broker = SimpleNamespace(project=project,registry=SimpleNamespace(status=lambda repo:dict(self.status)),
                                     config={'production_ready':True,'git':str(sealed_git),'source_sha':'fixture-source'},
                                     ledger=ExecutionLedger(self.root/'ledger.sqlite'),lock=threading.Lock(),_verified=lambda:True)
        @contextmanager
        def admission():
            yield
        self.broker.admission = admission
        self.packet = {'operation':'git_commit','request_id':'git-request-0001','expected_head':self.before,
                       'expected_epoch':1,'paths':['selected.txt'],'message':'selected change'}
        self.calls = []
        self.after_command = lambda:None
        def capture(spec, timeout, *, start_guard):
            with start_guard():
                argv = spec['argv']; self.calls.append(argv)
                result = subprocess.run([self.git,*argv[1:]],env=self.env,text=True,capture_output=True)
            self.after_command()
            return {'returncode':result.returncode,'timed_out':False,'stdout':result.stdout,'stderr':result.stderr}
        # Protocol fixtures only: native isolation is separately measured through
        # installed probes. These substitutions do not qualify production execution.
        replacements = [
            patch.object(git_broker.sys,'platform','darwin'),
            patch.object(git_broker.os,'geteuid',return_value=0,create=True),
            patch.object(git_broker.os,'chown',create=True),
            patch.object(git_broker,'INSTALL_ROOT',self.root/'install'),
            patch.object(git_broker,'EXECUTION_ROOT',self.root/'execution'),
            patch.object(git_broker,'launch_spec',side_effect=lambda project,packet,scratch,cache:{'argv':packet['argv']}),
            patch.object(git_broker,'capture',side_effect=capture),
            patch.object(git_broker,'MacOSProcesses',return_value=SimpleNamespace(owned=lambda uid:[])),
            patch.object(git_broker,'seal_metadata'),
            patch.object(git_broker,'sync_directory'),
            patch('do_again.supervisor.macos_server.verify_installation'),
            patch.object(git_broker,'atomic_swap',side_effect=self.exchange_fixture)]
        for replacement in replacements:
            replacement.start();self.addCleanup(replacement.stop)

    def command(self,*args):
        return subprocess.check_output([self.git,*args],env=self.env,text=True,stderr=subprocess.DEVNULL)

    def exchange_fixture(self, first, second):
        # Test-only exchange, not an implementation fallback.
        temporary = self.root/'old-metadata'
        first.rename(temporary);second.rename(first);temporary.rename(second)

    def test_exact_commit_promotes_and_replay_returns_same_receipt(self):
        result = git_broker.commit_via_broker(self.broker,self.packet)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(result['authority'],worktree_authority(self.repo))
        self.assertTrue(result['authority']['repo_branch'].startswith('do-again/task-'))
        self.assertEqual(result['paths'],['selected.txt'])
        self.assertEqual(self.command('-C',str(self.repo),'show','HEAD:excluded.txt'),'before\n')
        self.calls.clear()
        self.assertEqual(git_broker.commit_via_broker(self.broker,self.packet),result)
        self.assertEqual(self.calls,[])

    def test_no_change_is_a_failed_receipt_not_a_useful_commit(self):
        (self.repo/'selected.txt').write_text('before\n')
        result = git_broker.commit_via_broker(self.broker,self.packet)
        self.assertEqual(result['state'],'failed_pre_promotion')
        self.assertEqual(result['returncode'],1)
        self.assertEqual(worktree_authority(self.repo)['repo_head'],self.before)

    def test_pause_between_commands_defers_without_promoting(self):
        def pause():
            if len(self.calls)==2:self.status.update(intent='paused',epoch=2)
        self.after_command = pause
        result = git_broker.commit_via_broker(self.broker,self.packet)
        self.assertEqual(result['state'],'failed_pre_promotion')
        self.assertEqual(result['returncode'],1)
        self.assertEqual(worktree_authority(self.repo)['repo_head'],self.before)
        self.assertEqual(len(self.calls),2)

    def test_pause_immediately_before_exchange_has_no_effect(self):
        with patch.object(git_broker,'seal_metadata',side_effect=lambda path:self.status.update(intent='paused',epoch=2)):
            result = git_broker.commit_via_broker(self.broker,self.packet)
        self.assertEqual(result['state'],'failed_pre_promotion')
        self.assertEqual(result['returncode'],1)
        self.assertEqual(worktree_authority(self.repo)['repo_head'],self.before)

    def test_crash_after_exchange_preserves_started_and_never_replays(self):
        def exchange_then_crash(first,second):
            self.exchange_fixture(first,second)
            raise OSError('synthetic crash after effect')
        with patch.object(git_broker,'atomic_swap',side_effect=exchange_then_crash):
            with self.assertRaises(OSError):git_broker.commit_via_broker(self.broker,self.packet)
        self.assertNotEqual(worktree_authority(self.repo)['repo_head'],self.before)
        self.assertEqual(self.broker.ledger.pending(self.broker.project.key)[0]['request_id'],self.packet['request_id'])
        self.calls.clear()
        with self.assertRaisesRegex(ExecutionBlocked,'ambiguous started'):
            git_broker.commit_via_broker(self.broker,self.packet)
        self.assertEqual(self.calls,[])

    def test_receipt_failure_after_effect_cannot_create_another_commit(self):
        with patch.object(self.broker.ledger,'finish',side_effect=OSError('receipt unavailable')):
            with self.assertRaises(OSError):git_broker.commit_via_broker(self.broker,self.packet)
        self.calls.clear()
        with self.assertRaisesRegex(ExecutionBlocked,'ambiguous started'):
            git_broker.commit_via_broker(self.broker,self.packet)
        self.assertEqual(self.calls,[])

    def test_wrong_epoch_head_extra_authority_and_fingerprint_conflict_fail_closed(self):
        for change in ({'expected_epoch':True},{'expected_epoch':2},{'expected_head':'0'*40},
                       {'branch':'main'},{'project':'other'},{'argv':['git','push']},{'paths':['.git/config']}):
            with self.subTest(change=change),self.assertRaises(ExecutionBlocked):
                git_broker.commit_via_broker(self.broker,self.packet|change)
        self.assertEqual(self.calls,[])
        git_broker.commit_via_broker(self.broker,self.packet)
        with self.assertRaisesRegex(ExecutionBlocked,'fingerprint conflict'):
            git_broker.commit_via_broker(self.broker,self.packet|{'message':'changed intent'})

    def test_missing_goal_and_unqualified_installation_deny_before_reservation(self):
        for change in ({'intent':'maintenance'},{'goal_revision':None}):
            previous = dict(self.status);self.status.update(change)
            with self.assertRaises(ExecutionBlocked):git_broker.commit_via_broker(self.broker,self.packet)
            self.status = previous
        self.broker.config['production_ready'] = False
        with self.assertRaises(ExecutionBlocked):git_broker.commit_via_broker(self.broker,self.packet)
        self.assertEqual(self.calls,[])


class AtomicSwapTests(unittest.TestCase):
    def test_unsupported_platform_has_no_rename_fallback(self):
        with patch.object(git_broker.sys,'platform','win32'),patch.object(git_broker.os,'rename') as rename:
            with self.assertRaises(ExecutionBlocked):git_broker.atomic_swap(Path('/a'),Path('/b'))
            rename.assert_not_called()

    @unittest.skipUnless(sys.platform=='darwin','Darwin native exchange qualification')
    def test_real_native_directory_exchange_preserves_both_generations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();first=root/'first';second=root/'second'
            first.mkdir();second.mkdir();(first/'HEAD').write_text('old');(second/'HEAD').write_text('new')
            git_broker.atomic_swap(first,second)
            self.assertEqual((first/'HEAD').read_text(),'new')
            self.assertEqual((second/'HEAD').read_text(),'old')
