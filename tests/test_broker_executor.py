from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.agent import Agent
from do_again.core.broker_executor import BrokerExecutor
from do_again.core.schema import OperatorError, atomic_json
from do_again.supervisor.macos_server import worktree_authority
from do_again.service import runtime


class BrokerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve() / 'repo'
        self.repo.mkdir()
        self.policy = self.repo / 'policy.json'
        atomic_json(self.policy, {'allowed_operations': ['scratch_script','repo_script','run_tests','extended_exec','status','launchctl_action','self_update'],
                                 'repo_script_prefixes': ['tools/'], 'extended_exec_binaries': ['python3']})
        self.executor = BrokerExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.repo / 'state')
        self.status = {'operator_intent':'active', 'production_ready':True, 'enforcement_verified':True,
                       'worktree':'/assigned/worktree', 'executables':[str(Path('/sealed/python3')),str(Path('/bin/bash'))],
                       'authority':{'repo_head':'a'*40,'repo_branch':'main'}}
        self.request = {'request_id':'request-0001','operation':'scratch_script','args':{'language':'python','content':'print(1)'},'expected':{'repo_head':'a'*40}}

    def test_agent_defaults_to_broker_without_local_executor(self):
        agent = Agent(repo=self.repo, control_worktree=self.repo, branch='operator-control', policy_path=self.policy, state_dir=self.repo/'state')
        self.assertIsInstance(agent.executor, BrokerExecutor)

    def test_broker_preserves_identity_and_confines_interpreter(self):
        with patch('do_again.core.broker_executor.sys.platform','darwin'), patch('do_again.core.broker_executor.broker_request', side_effect=[self.status, {'returncode':0}, self.status]) as broker, patch('subprocess.Popen') as spawn:
            result = self.executor.execute(self.request)
        packet = broker.call_args_list[1].args[1]
        self.assertEqual(packet['argv'], [str(Path('/sealed/python3')),'-c','print(1)'])
        self.assertEqual(packet['cwd'], '.')
        self.assertEqual(packet['expected_head'], 'a'*40)
        self.assertEqual(packet['expected_authority'],self.request['expected'])
        self.assertEqual(packet['request_fingerprint'],result['request_fingerprint'])
        spawn.assert_not_called()

    def test_pause_corrupt_or_incomplete_admission_never_dispatches(self):
        for change in ({'operator_intent':'paused'},{'operator_intent':'stopped'},{'operator_intent':'maintenance'}, {'production_ready':False}, {'enforcement_verified':False}, {'operator_intent':None}):
            with self.subTest(change=change), patch('do_again.core.broker_executor.sys.platform','darwin'), patch('do_again.core.broker_executor.broker_request',return_value=self.status|change) as broker:
                with self.assertRaises(OperatorError):self.executor.execute(self.request)
                self.assertEqual(broker.call_count,1)

    def test_platform_and_broker_failure_have_no_fallback(self):
        with patch('do_again.core.broker_executor.sys.platform','win32'), patch('subprocess.Popen') as spawn:
            with self.assertRaises(OperatorError):self.executor.execute(self.request)
            spawn.assert_not_called()
        with patch('do_again.core.broker_executor.sys.platform','darwin'), patch('do_again.core.broker_executor.broker_request',side_effect=OperatorError('disconnected')), patch('subprocess.Popen') as spawn:
            with self.assertRaises(OperatorError):self.executor.execute(self.request)
            spawn.assert_not_called()

    def test_host_administration_and_unattested_fences_are_rejected(self):
        for change in ({'operation':'launchctl_action'}, {'operation':'self_update'}, {'expected':{'repo_dirty':False}}, {'expected':{'repo_head':'b'*40}}):
            with self.subTest(change=change), patch('do_again.core.broker_executor.sys.platform','darwin'), patch('do_again.core.broker_executor.broker_request',return_value=self.status) as broker:
                with self.assertRaises(OperatorError):self.executor.execute(self.request|change)
                self.assertEqual(broker.call_count,1)

    def test_path_escape_environment_and_unapproved_binary_are_rejected(self):
        for args in ({'language':'python','content':'print(1)','cwd':'../other'}, {'language':'python','content':'print(1)','cwd':'/other'}, {'language':'python','content':'print(1)','env':{'SSH_AUTH_SOCK':'secret'}}):
            with self.subTest(args=args):
                with self.assertRaises(OperatorError):self.executor._packet(self.request|{'args':args},self.status)
        with self.assertRaises(OperatorError):self.executor._packet(self.request|{'operation':'extended_exec','args':{'argv':['launchctl','kickstart']}},self.status)

    def test_tests_and_repository_scripts_use_assigned_worktree(self):
        packet=self.executor._packet(self.request|{'operation':'run_tests','args':{'discover':True}},self.status)
        self.assertEqual(packet['argv'][:4],[str(Path('/sealed/python3')),'-m','unittest','discover'])
        packet=self.executor._packet(self.request|{'operation':'repo_script','args':{'path':'tools/build.py','cwd':str(self.repo)}},self.status)
        self.assertEqual(packet['argv'],[str(Path('/sealed/python3')),str(Path('/assigned/worktree')/'tools/build.py')])
        self.assertEqual(packet['cwd'],'.')

    def test_foreground_rejects_before_preparation_or_service_probe(self):
        with patch('do_again.service.runtime.find_repo',return_value=self.repo), patch('do_again.service.runtime.reject_legacy_runtime',side_effect=OperatorError('paused')), patch('do_again.service.runtime.prepare_runtime') as prepare, patch('do_again.service.runtime.service_status') as status:
            with self.assertRaises(runtime.ServiceError):runtime.run_foreground(self.repo,once=True)
            prepare.assert_not_called();status.assert_not_called()

    def test_service_entrypoints_reject_before_any_launcher(self):
        for function in (runtime.install_service,runtime.restart_service, runtime._install_macos, runtime._restart_macos, runtime._install_linux, runtime._restart_linux, runtime._install_windows, runtime._restart_windows):
            with patch('do_again.service.runtime.find_repo',return_value=self.repo), patch('do_again.service.runtime.reject_legacy_runtime',side_effect=OperatorError('unregistered')), patch('do_again.service.runtime._require_supported_background_platform') as platform:
                with self.assertRaises(runtime.ServiceError):function(self.repo)
                platform.assert_not_called()
        with patch('do_again.service.runtime.reject_legacy_runtime',side_effect=OperatorError('paused')), patch('do_again.service.runtime.runtime_layout') as layout:
            with self.assertRaises(runtime.ServiceError):runtime.prepare_runtime(self.repo)
            layout.assert_not_called()

    def test_git_identity_does_not_execute_configuration(self):
        git=self.repo/'.git';git.mkdir();(git/'refs/heads').mkdir(parents=True)
        (git/'HEAD').write_text('ref: refs/heads/main\n');(git/'refs/heads/main').write_text('a'*40+'\n')
        with patch('subprocess.Popen') as spawn:
            self.assertEqual(worktree_authority(self.repo),{'repo_head':'a'*40,'repo_branch':'main'})
            spawn.assert_not_called()
        (git/'HEAD').write_text('ref: refs/heads/../../escape')
        with self.assertRaises(OperatorError):worktree_authority(self.repo)

    def test_daemon_rejects_before_agent_or_browser_creation(self):
        from do_again.service import daemon
        from argparse import Namespace
        args=Namespace(repo=str(self.repo))
        with patch.object(daemon,'parse_args',return_value=args), patch('do_again.supervisor.admission.require_active',side_effect=OperatorError('closed')), patch.object(daemon,'Agent') as agent, patch.object(daemon,'runtime_layout') as layout:
            with self.assertRaises(OperatorError):daemon.main([])
            agent.assert_not_called();layout.assert_not_called()

    def test_claim_rejects_before_control_branch_mutation(self):
        agent=Agent(repo=self.repo,control_worktree=self.repo,branch='operator-control',policy_path=self.policy,state_dir=self.repo/'state')
        with patch('do_again.supervisor.admission.require_active',side_effect=OperatorError('paused')), patch.object(agent,'sync') as sync, patch.object(agent,'publish_json') as publish:
            with self.assertRaises(OperatorError):agent.acquire_remote_claim(self.request)
            sync.assert_not_called();publish.assert_not_called()

    @unittest.skipUnless(__import__('os').name=='posix','native admission fence')
    def test_head_change_at_final_fence_prevents_spawn_and_started_marker(self):
        import threading
        from unittest.mock import Mock
        from do_again.supervisor.macos_server import ProjectBroker
        from do_again.supervisor.macos_execution import ProjectExecution
        from do_again.supervisor.authority import AuthorityRegistry
        broker=object.__new__(ProjectBroker)
        broker.registry=AuthorityRegistry(self.repo/'authority.sqlite');broker.registry.initialize()
        broker.registry.set_intent(self.repo,'active',goal_revision='accepted')
        broker.project=ProjectExecution(self.repo,401,401,'_doagain_da',self.repo/'work',(Path('/sealed/python3'),))
        broker.config={'operator_uid':501,'production_ready':True};broker.state=self.repo
        broker.lock=threading.Lock();broker.ledger=Mock();broker._verified=Mock(return_value=True)
        packet={'operation':'execute','request_id':'request-0001','argv':[str(Path('/sealed/python3')),'-c','print(1)'],'cwd':'.','timeout':1,'expected_head':'a'*40}
        with patch('do_again.supervisor.macos_server.EXECUTION_ROOT',self.repo/'execution'), patch('do_again.supervisor.macos_server.os.chown'), patch('do_again.supervisor.macos_server.verify_installation'), patch('do_again.supervisor.macos_server.launch_spec',return_value={}), patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'b'*40}), patch('subprocess.Popen') as spawn:
            with self.assertRaises(OperatorError):broker.dispatch(packet,501)
            spawn.assert_not_called();broker.ledger.reserve.assert_not_called()

    def test_packed_head_and_aliases(self):
        git=self.repo/'.git';git.mkdir();(git/'HEAD').write_text('ref: refs/heads/main\n')
        (git/'packed-refs').write_text('# packed\n'+'a'*40+' refs/heads/main\n')
        self.assertEqual(worktree_authority(self.repo)['repo_head'],'a'*40)
        (git/'HEAD').unlink()
        try:(git/'HEAD').symlink_to(git/'packed-refs')
        except OSError:self.skipTest('symlink unavailable')
        with self.assertRaises(OperatorError):worktree_authority(self.repo)

    def test_legacy_upgrade_defers_before_staging_when_admission_is_closed(self):
        from do_again.service import rollout
        with patch.object(rollout,'find_repo',return_value=self.repo), patch.object(rollout,'runtime_layout'), patch.object(rollout,'assess_upgrade',return_value={'blockers':[]}), patch('do_again.supervisor.admission.reject_legacy_runtime',side_effect=OperatorError('closed')), patch.object(rollout,'_stage_source') as stage:
            result=rollout.staged_upgrade(self.repo,apply=True)
            self.assertFalse(result['applied']);self.assertTrue(result['deferred'])
            self.assertEqual(result['effective_blockers'][0]['kind'],'production_admission')
            stage.assert_not_called()
