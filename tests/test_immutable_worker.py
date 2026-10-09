from __future__ import annotations
import copy
import json
import os
import tempfile
import threading
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from do_again.core.schema import OperatorError
from do_again.supervisor import immutable_worker as worker
from do_again.supervisor.authority import project_identity
from do_again.supervisor.macos_execution import EXECUTION_ROOT


class WorkerAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.flags=SimpleNamespace(**{k:getattr(sys.flags,k) for k in dir(sys.flags) if not k.startswith('_') and isinstance(getattr(sys.flags,k),int)})
        self.flags.isolated=self.flags.no_site=self.flags.dont_write_bytecode=1
        self.root=worker.INSTALL_ROOT
        self.home=Path.home()
        self.policy=json.loads((Path(__file__).resolve().parents[1]/'src/do_again/worker_policy.json').read_text())
        self.projects=[]
        for index,name in enumerate(('do-again','jobpipe')):
            repo=self.home/name;key=project_identity(repo)
            self.projects.append({'repo':str(repo),'key':key,'uid':401+index,'gid':401+index,
                'account':worker.PROJECT_ACCOUNTS[name], 'worktree':str(EXECUTION_ROOT/key/'worktree')})
        self.config={'schema_version':1,'operator_uid':501,'operator_gid':20,'operator_home':str(self.home),
            'source_sha':'a'*40,'production_ready':True,'projects':self.projects,
            'python':str(self.root/'current/runtimes/python/bin/python3')}
        self.status={'source_sha':'a'*40,'operator_intent':'active','production_ready':True,
            'enforcement_verified':True,'enforcement_blocker':None,'unresolved_executions':[],
            'goal_revision':'accepted-goal','epoch':2,'authority':{'repo_head':'b'*40,'repo_branch':None},
            'uid':401,'worktree':self.projects[0]['worktree']}

    def context(self, **overrides):
        args=dict(project_name='do-again',config=self.config,status=self.status,policy=self.policy,
            installed_root=self.root,module_path=self.root/'current/package/do_again/supervisor/immutable_worker.py',
            interpreter=Path(self.config['python']),uid=501,euid=501)
        args.update(overrides)
        return worker.validate_worker_context(**args)

    def test_exact_project_context_and_isolated_control_state(self):
        first=self.context()
        self.assertEqual(first[0],self.home/'do-again')
        jp=copy.deepcopy(self.status);jp.update(uid=402,worktree=self.projects[1]['worktree'])
        second=self.context(project_name='jobpipe',status=jp)
        self.assertEqual(second[0],self.home/'jobpipe')
        self.assertNotEqual(first[1],second[1]);self.assertNotEqual(first[2],second[2])

    def test_root_other_identity_mutable_module_and_executable_are_rejected(self):
        for values in ({'uid':0,'euid':0},{'uid':502,'euid':502},{'uid':501,'euid':0},
                       {'uid':True,'euid':True},{'project_name':'sonary'},
                       {'module_path':self.home/'do-again/src/do_again/supervisor/immutable_worker.py'},
                       {'interpreter':Path('/usr/bin/python3')}):
            with self.subTest(values=values),self.assertRaises(OperatorError):self.context(**values)

    def test_operator_pause_stop_maintenance_ambiguous_and_unverified_never_admit(self):
        cases=[{'operator_intent':v} for v in ('maintenance','paused','stopped',None)]
        cases += [{'source_sha':'c'*40},{'enforcement_verified':False},{'production_ready':False},
                  {'enforcement_blocker':{}},{'unresolved_executions':[{'state':'started'}]},
                  {'goal_revision':''},{'epoch':0},{'epoch':True},{'epoch':None},
                  {'authority':{'repo_head':'not-a-sha'}},{'uid':402},{'worktree':self.projects[1]['worktree']}]
        for changes in cases:
            status={**self.status,**changes}
            with self.subTest(changes=changes),self.assertRaises(OperatorError):self.context(status=status)

    def test_sealed_false_flag_missing_source_and_foreign_binding_do_not_admit(self):
        for changes in ({'production_ready':False},{'source_sha':None},{'source_sha':'not-a-sha'},
                        {'schema_version':2},{'projects':self.projects[:1]}):
            with self.subTest(changes=changes),self.assertRaises(OperatorError):
                self.context(config={**self.config,**changes})
        for field,value in (('repo',str(self.home/'sonary')),('key','forged'),('gid',402),
                            ('uid',501),('worktree',self.projects[1]['worktree']),('account','_doagain_other')):
            config=copy.deepcopy(self.config);config['projects'][0][field]=value
            with self.subTest(field=field),self.assertRaises(OperatorError):self.context(config=config)

    def test_policy_cannot_expand_operations(self):
        for operations in (list(worker.WORKER_OPERATIONS)+['service_restart'],[],['status']*9):
            with self.assertRaises(OperatorError):self.context(policy={**self.policy,'allowed_operations':operations})
        for changes in ({'control_branch':'main'},{'worker_policy_schema':2}):
            with self.assertRaises(OperatorError):self.context(policy={**self.policy,**changes})

    @unittest.skipUnless(os.name=='posix','native path ownership uses POSIX')
    def test_control_ancestor_alias_and_group_writable_state_reject(self):
        # Put the fixture in the private home, avoiding world-writable temp roots.
        with tempfile.TemporaryDirectory(dir=Path.home()) as root:
            base=Path(root);control=base/'control';state=base/'state'
            control.mkdir();state.mkdir()
            worker.validate_control_paths(control,state,os.getuid())
            state.chmod(0o770)
            with self.assertRaises(OperatorError):worker.validate_control_paths(control,state,os.getuid())
            state.chmod(0o700);alias=base/'alias';alias.symlink_to(base,target_is_directory=True)
            with self.assertRaises(OperatorError):worker.validate_control_paths(alias/'control',state,os.getuid())

    def test_configuration_reader_does_not_require_root_or_mutate_authority(self):
        with patch.object(worker,'private_root_file') as check, \
             patch.object(Path,'read_text',return_value=json.dumps(self.config)):
            self.assertEqual(worker.read_worker_configuration(),self.config)
            check.assert_called_once_with(self.root/'current/config.json')

    def test_main_unsupported_platform_has_no_inspection_or_daemon_effect(self):
        with patch.object(worker.sys,'platform','win32'),patch.object(worker,'read_worker_configuration') as config:
            with self.assertRaises(OperatorError):worker.main(['--project','do-again'])
            config.assert_not_called()

    @unittest.skipUnless(hasattr(os,'getuid'),'POSIX identity mock')
    def test_host_python_import_environment_rejects_before_configuration(self):
        with patch.object(worker.sys,'platform','darwin'), \
             patch.object(worker.sys,'flags',Mock(isolated=0,no_site=0,dont_write_bytecode=0)), \
             patch.object(worker,'read_worker_configuration') as config:
            with self.assertRaises(OperatorError):worker.main(['--project','do-again'])
            config.assert_not_called()

    def test_revoked_admission_prevents_control_git_process(self):
        from do_again.core.agent import Agent
        from do_again.supervisor.authority import AuthorityDenied
        agent=object.__new__(Agent)
        agent.admission_check=Mock(side_effect=AuthorityDenied('paused'))
        with patch('do_again.core.agent.subprocess.run') as process:
            with self.assertRaises(AuthorityDenied):agent.git('push','origin','HEAD:operator-control')
            process.assert_not_called()

    def test_agent_exits_on_pause_without_sync_retry_or_remote_status(self):
        from do_again.core.agent import Agent
        from do_again.supervisor.authority import AuthorityDenied
        with tempfile.TemporaryDirectory() as root:
            agent=object.__new__(Agent);agent.executor=object();agent.ledger_dir=Path(root)/'ledger'
            agent.stop_requested=False;agent.publish_status=Mock();agent.sync=Mock()
            agent.admission_check=Mock(side_effect=AuthorityDenied('paused'))
            with patch('do_again.core.agent.time.sleep') as retry:
                self.assertEqual(agent.run(),0)
                agent.sync.assert_not_called();retry.assert_not_called()
            # Even initial status publication is excluded once pause is known.
            agent.publish_status.assert_not_called()

    def test_browser_monitor_pause_never_delivers_or_restarts(self):
        from do_again.service.daemon import _browser_monitor
        from do_again.supervisor.authority import AuthorityDenied
        with tempfile.TemporaryDirectory() as root:
            stop=threading.Event()
            with patch('do_again.service.daemon._drain_browser_outbox') as delivery:
                _browser_monitor(repo=Path(root),state_dir=Path(root),stop_event=stop,
                    work_event=threading.Event(),admission_check=Mock(side_effect=AuthorityDenied('paused')))
                delivery.assert_not_called();self.assertTrue(stop.is_set())
            self.assertEqual(json.loads((Path(root)/'browser_status.json').read_text())['state'],'paused')

    @unittest.skipUnless(hasattr(os,'getuid'),'POSIX identity mock')
    def test_pause_during_startup_prevents_daemon_creation(self):
        paused={**self.status,'operator_intent':'paused','epoch':3}
        with patch.object(worker.sys,'platform','darwin'),patch.object(worker.os,'getuid',return_value=501), \
             patch.object(worker.os,'geteuid',return_value=501),patch.object(worker,'read_worker_configuration',return_value=self.config), \
             patch.object(worker.os,'getgid',return_value=20),patch.object(worker.os,'getegid',return_value=20), \
             patch.object(worker.sys,'flags',self.flags), \
             patch('do_again.supervisor.macos_server.verify_installation'), \
             patch.object(worker,'broker_request',side_effect=[self.status,paused]), \
             patch.object(Path,'read_text',return_value=json.dumps(self.policy)), \
             patch.object(worker,'__file__',str(self.root/'current/package/do_again/supervisor/immutable_worker.py')), \
             patch.object(worker.sys,'executable',self.config['python']),patch.object(worker,'validate_control_paths'), \
             patch('do_again.service.daemon.main') as daemon:
            with self.assertRaises(OperatorError):worker.main(['--project','do-again','--once'])
            daemon.assert_not_called()


if __name__=='__main__':unittest.main()
