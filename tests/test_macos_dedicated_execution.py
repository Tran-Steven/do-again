from __future__ import annotations
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.supervisor import macos_execution as boundary
from do_again.supervisor.authority import AuthorityRegistry, project_identity


class DedicatedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.repo=self.root/'repo';self.repo.mkdir()
        self.base=self.root/'execution'
        self.work=self.base/project_identity(self.repo)/'worktree';self.work.mkdir(parents=True)
        self.scratch=self.root/'scratch';self.scratch.mkdir()
        self.cache=self.root/'cache';self.cache.mkdir()
        self.project=boundary.ProjectExecution(self.repo,400,400,'_doagain_da',self.work,(Path(sys.executable),))
        self.packet={'operation':'execute','request_id':'request-123','argv':[sys.executable,'-c','print(1)'],'cwd':'.','timeout':5}

    def test_distinct_non_root_identity_and_allocated_root_required(self):
        with patch.object(boundary,'EXECUTION_ROOT',self.base):self.project.validate(501)
        for uid in (0,501):
            invalid=boundary.ProjectExecution(self.repo,uid,400,'_doagain_da',self.work,(Path(sys.executable),))
            with self.assertRaises(boundary.ExecutionBlocked):invalid.validate(501)

    def test_launch_clears_credentials_groups_descriptors_and_git_authority(self):
        spec=boundary.launch_spec(self.project,self.packet,self.scratch,self.cache)
        self.assertEqual(spec['user'],400);self.assertEqual(spec['group'],400)
        self.assertEqual(spec['extra_groups'],[]);self.assertTrue(spec['close_fds'])
        self.assertEqual(spec['umask'],0o077);self.assertTrue(spec['start_new_session'])
        self.assertEqual(spec['args'][0],'/usr/bin/sandbox-exec')
        self.assertIn('(deny job-creation)',spec['args'][2])
        self.assertIn(str(self.work/'.git'),spec['args'][2])
        self.assertNotIn('SSH_AUTH_SOCK',spec['env']);self.assertNotIn('PYTHONPATH',spec['env'])
        self.assertEqual(spec['env']['HOME'],str(self.scratch))

    def test_request_cannot_select_project_identity_root_environment_or_admin_operation(self):
        mutations=[{'project':'other'},{'uid':0},{'env':{'SSH_AUTH_SOCK':'host'}},
                   {'operation':'pause'},{'argv':['/usr/bin/sudo','true']},
                   {'cwd':'../outside'},{'timeout':True},{'timeout':float('inf')},
                   {'request_id':'../escape'}]
        for mutation in mutations:
            with self.subTest(mutation=mutation),self.assertRaises(boundary.ExecutionBlocked):
                boundary.launch_spec(self.project,{**self.packet,**mutation},self.scratch,self.cache)

    @unittest.skipUnless(os.name=='posix','POSIX aliases required')
    def test_aliases_are_rejected_before_spawn(self):
        sentinel=self.root/'protected';sentinel.write_text('UNCHANGED')
        link=self.work/'alias';link.symlink_to(sentinel)
        with self.assertRaises(boundary.ExecutionBlocked):boundary.profile(self.project,self.scratch,self.cache)
        link.unlink();os.link(sentinel,link)
        with self.assertRaises(boundary.ExecutionBlocked):boundary.profile(self.project,self.scratch,self.cache)
        self.assertEqual(sentinel.read_text(),'UNCHANGED')

    def test_started_ledger_survives_restart_and_never_replays(self):
        ledger=boundary.ExecutionLedger(self.root/'execution.sqlite')
        self.assertIsNone(ledger.reserve('project','request-123','fingerprint'))
        reopened=boundary.ExecutionLedger(self.root/'execution.sqlite')
        for identity,fp in (('request-123','fingerprint'),('request-other','other')):
            with self.assertRaises(boundary.ExecutionBlocked):reopened.reserve('project',identity,fp)
        ledger.finish('project','request-123',{'returncode':0})
        self.assertEqual(reopened.reserve('project','request-123','fingerprint'),{'returncode':0})
        with self.assertRaises(boundary.ExecutionBlocked):reopened.reserve('project','request-123','changed')

    def test_output_is_bounded_and_descendants_drained_before_result(self):
        process=Mock();process.stdout=io.BytesIO(b'A'*(boundary.MAX_OUTPUT+10));process.stderr=io.BytesIO(b'B')
        process.returncode=0
        processes=Mock();processes.owned.return_value={}
        result=boundary.capture({'user':400},1,processes=processes,process=process)
        self.assertEqual(len(result['stdout']),boundary.MAX_OUTPUT)
        self.assertTrue(result['stdout_truncated']);processes.drain.assert_called_once_with(400)

    def test_unclassifiable_processes_preserve_started_execution(self):
        processes=Mock();processes.owned.return_value={123:(400,1,2,2)}
        with patch.object(boundary.subprocess,'Popen') as spawn,self.assertRaises(boundary.ExecutionBlocked):
            boundary.capture({'user':400},1,processes=processes)
        spawn.assert_not_called()

    def test_pid_reuse_is_never_signalled(self):
        process=object.__new__(boundary.MacOSProcesses)
        process.owned=Mock(side_effect=[{123:(400,10,1,2)},{}])
        process.identity=Mock(return_value=(501,20,1,2))
        with patch.object(boundary.os,'kill') as kill:process.drain(400)
        kill.assert_not_called()

    @unittest.skipUnless(sys.platform=='darwin','macOS kernel credentials required')
    def test_peer_uid_is_kernel_authenticated(self):
        a,b=socket.socketpair();self.addCleanup(a.close);self.addCleanup(b.close)
        self.assertEqual(boundary.peer_uid(a),os.getuid())

    def test_missing_platform_has_no_unsandboxed_fallback(self):
        with patch.object(boundary.sys,'platform','win32'),self.assertRaises(boundary.ExecutionBlocked):
            boundary.load_configuration(self.root/'config.json')


class BrokerAdmissionTests(unittest.TestCase):
    def test_paused_operator_intent_prevents_process_launch(self):
        from do_again.supervisor.macos_server import ProjectBroker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();repo=root/'repo';repo.mkdir()
            registry=AuthorityRegistry(root/'authority.sqlite');registry.initialize()
            registry.set_intent(repo,'paused',goal_revision='accepted-plan')
            broker=object.__new__(ProjectBroker)
            broker.config={'operator_uid':501};broker.registry=registry
            broker.project=boundary.ProjectExecution(repo,400,400,'_doagain_da',root/'work',(Path(sys.executable),))
            import threading
            broker.lock=threading.Lock();broker.state=root;broker.ledger=Mock();broker._verified=Mock(return_value=True)
            packet={'operation':'execute','request_id':'request-123','argv':[sys.executable],'cwd':'.','timeout':1}
            with patch('do_again.supervisor.macos_server.verify_installation'),patch.object(boundary,'EXECUTION_ROOT',root),patch('do_again.supervisor.macos_server.launch_spec',return_value={}),patch('subprocess.Popen') as spawn:
                with self.assertRaises(boundary.AuthorityDenied):broker.dispatch(packet,501)
            spawn.assert_not_called();broker.ledger.reserve.assert_not_called()

    def test_untrusted_peer_never_reads_packet_or_authority(self):
        from do_again.supervisor.macos_server import ProjectBroker
        broker=object.__new__(ProjectBroker);broker.config={'operator_uid':501};broker.registry=Mock()
        with self.assertRaises(boundary.ExecutionBlocked):broker.dispatch({'operation':'status','uid':501},400)
        broker.registry.status.assert_not_called()

    @unittest.skipUnless(os.name=='posix','native file admission fence required')
    def test_pause_closes_admission_while_existing_execution_drains(self):
        import threading
        from do_again.supervisor.macos_server import ProjectBroker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();repo=root/'repo';repo.mkdir()
            registry=AuthorityRegistry(root/'authority.sqlite');registry.initialize()
            registry.set_intent(repo,'active',goal_revision='accepted-plan')
            broker=object.__new__(ProjectBroker);broker.registry=registry;broker.state=root
            broker.config={'operator_uid':501,'source_sha':'synthetic','production_ready':True}
            broker.project=boundary.ProjectExecution(repo,400,400,'_doagain_da',root/'work',(Path(sys.executable),))
            broker.lock=threading.Lock();broker.ledger=boundary.ExecutionLedger(root/'authority.sqlite')
            broker._verified=Mock(return_value=True)
            started,release=threading.Event(),threading.Event();errors=[]
            def capture(*args,**kwargs):
                started.set();release.wait(3)
                return {'returncode':0}
            def execute():
                try:broker.dispatch({'operation':'execute','request_id':'request-123','argv':[sys.executable],'cwd':'.','timeout':1},501)
                except Exception as exc:errors.append(exc)
            with patch('do_again.supervisor.macos_server.EXECUTION_ROOT',root),patch('do_again.supervisor.macos_server.os.chown'),patch('do_again.supervisor.macos_server.verify_installation'),patch('do_again.supervisor.macos_server.launch_spec',return_value={}),patch.object(boundary,'MacOSProcesses') as processes,patch('subprocess.Popen'),patch('do_again.supervisor.macos_server.capture',side_effect=capture):
                processes.return_value.owned.return_value={}
                thread=threading.Thread(target=execute);thread.start()
                self.assertTrue(started.wait(2))
                self.assertEqual(broker.operator_intent('paused')['intent'],'paused')
                self.assertEqual(registry.status(repo)['intent'],'paused')
                release.set();thread.join(3)
            self.assertFalse(errors)

    def test_resume_cannot_bypass_incomplete_production_migration(self):
        from do_again.supervisor.macos_server import ProjectBroker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();repo=root/'repo';repo.mkdir()
            registry=AuthorityRegistry(root/'authority.sqlite');registry.initialize()
            registry.set_intent(repo,'maintenance',goal_revision='accepted-plan')
            broker=object.__new__(ProjectBroker);broker.registry=registry;broker.state=root
            broker.project=Mock(repo=repo);broker.config={'production_ready':False}
            if os.name!='posix':self.skipTest('native admission fence unavailable')
            with self.assertRaises(boundary.ExecutionBlocked):broker.operator_intent('active')
            self.assertEqual(registry.status(repo)['intent'],'maintenance')
