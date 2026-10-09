import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from do_again.supervisor.browser_broker import browser_tick, ci_evidence
from do_again.supervisor.macos_execution import ExecutionBlocked,ExecutionLedger


class BrowserBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.active=False;self.status={'intent':'active','epoch':1,'goal_revision':'goal'}
        @contextmanager
        def admission():
            self.assertFalse(self.active);self.active=True
            try:yield
            finally:self.active=False
        project={'key':'original','repo':str(Path.home()/'do-again')}
        self.broker=SimpleNamespace(project=SimpleNamespace(**{'key':'original','repo':Path(project['repo'])}),
            config={'projects':[project],'operator_home':str(Path.home()),'operator_uid':501,'operator_gid':20,
                    'python':'/sealed/python','source_sha':'a'*40,'production_ready':True},
            registry=SimpleNamespace(status=lambda repo:dict(self.status)),_verified=lambda:True,
            ledger=ExecutionLedger(Path(self.temp.name)/'ledger.sqlite'),lock=threading.Lock(),admission=admission)
        self.packet={'operation':'browser_tick','request_id':'browser-synthetic-proof','epoch':1}
        for target,kwargs in [('do_again.supervisor.browser_broker.sys.platform',{'new':'darwin'}),
                ('do_again.supervisor.browser_broker.os.geteuid',{'return_value':0,'create':True}),
                ('do_again.supervisor.worker_service.verified_worker_pid',{'return_value':123}),
                ('do_again.supervisor.macos_server.verify_installation',{})]:
            p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)

    def test_fence_covers_spawn_and_browser_effect_until_terminal_receipt(self):
        proc=Mock(returncode=0)
        def communicate(timeout):
            self.assertTrue(self.active)
            self.assertEqual(len(self.broker.ledger.pending('original')),1)
            return b'{"state":"completed","delivered":1}',None
        proc.communicate.side_effect=communicate
        with patch('do_again.supervisor.browser_broker.subprocess.Popen',return_value=proc) as spawn:
            result=browser_tick(self.broker,self.packet)
            self.assertEqual(result['delivered'],1)
            self.assertFalse(self.active)
            browser_tick(self.broker,self.packet);self.assertEqual(spawn.call_count,1)
            self.assertEqual(spawn.call_args.kwargs['user'],501)
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])

    def test_paused_or_forged_project_never_spawns(self):
        with patch('do_again.supervisor.browser_broker.subprocess.Popen') as spawn:
            self.status['intent']='paused'
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,self.packet)
            self.status['intent']='active'
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,{**self.packet,'project':'other'})
            spawn.assert_not_called()

    def test_uncertain_child_never_repeats_after_restart(self):
        proc=Mock(returncode=1);proc.communicate.return_value=(b'',None)
        with patch('do_again.supervisor.browser_broker.subprocess.Popen',return_value=proc) as spawn:
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,self.packet)
            self.broker.ledger=ExecutionLedger(self.broker.ledger.path)
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,self.packet)
            self.assertEqual(spawn.call_count,1)
            self.assertEqual(len(self.broker.ledger.pending('original')),1)

    def test_ci_continuation_requires_exact_repository_run_and_current_head(self):
        self.broker.project.worktree=Path(self.temp.name)/'worktree'
        self.broker.config['projects'][0]['github_repository']='Tran-Steven/do-again'
        goal={'goal_state':'waiting_for_ci','ci':{'repository':'Tran-Steven/do-again','run_id':123,'head_sha':'b'*40}}
        with patch('do_again.service.liveness._latest_goal',return_value=goal),              patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'b'*40}),              patch('do_again.supervisor.control_history.api_for') as selected:
            api=selected.return_value
            api.request.return_value={'id':123,'head_sha':'b'*40,'status':'completed','conclusion':'success'}
            self.assertEqual(ci_evidence(self.broker,Path(self.temp.name))['state'],'terminal')
            api.request.assert_called_once_with('GET','actions/runs/123')
            for response in ({'id':124,'head_sha':'b'*40},{'id':123,'head_sha':'c'*40}):
                api.request.return_value=response
                self.assertEqual(ci_evidence(self.broker,Path(self.temp.name))['state'],'mismatch')
            api.reset_mock();goal['ci']['repository']='Tran-Steven/sonary'
            self.assertEqual(ci_evidence(self.broker,Path(self.temp.name))['state'],'mismatch')
            api.request.assert_not_called()

    def test_ci_head_change_invalidates_previously_successful_run(self):
        self.broker.project.worktree=Path(self.temp.name)/'worktree'
        self.broker.config['projects'][0]['github_repository']='Tran-Steven/do-again'
        goal={'goal_state':'waiting_for_ci','ci':{'repository':'Tran-Steven/do-again','run_id':123,'head_sha':'b'*40}}
        with patch('do_again.service.liveness._latest_goal',return_value=goal),              patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'c'*40}),              patch('do_again.supervisor.control_history.api_for') as api:
            self.assertEqual(ci_evidence(self.broker,Path(self.temp.name))['state'],'mismatch')
            api.assert_not_called()


if __name__=='__main__':unittest.main()
