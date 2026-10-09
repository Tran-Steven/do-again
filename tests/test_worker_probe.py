"""Qualification harness regressions; native enforcement is measured separately."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from do_again.supervisor.macos_execution import ExecutionLedger,ExecutionBlocked,ProjectExecution
from do_again.supervisor.worker_probe import qualify_worker


@unittest.skipUnless(os.name=='posix','installed harness uses macOS identity modules')
class WorkerProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();repo=self.root/'do-again';repo.mkdir()
        self.before={'operator':{'intent':'maintenance','epoch':1,'goal_revision':'accepted'},
                     'authority':{'repo_head':'a'*40,'repo_branch':None}}
        self.state=self.root/'state';self.state.mkdir();self.calls=[]
        project=ProjectExecution(repo,401,401,'_doagain_da',self.root/'work',(Path('/sealed/bin/python3'),))
        self.broker=SimpleNamespace(project=project,config={'production_ready':False,'source_sha':'b'*40,'operator_uid':501},
            registry=SimpleNamespace(status=lambda repo:dict(self.before['operator'])),
            state=self.state,ledger=ExecutionLedger(self.root/'ledger.sqlite'),lock=threading.Lock(),
            _verified=lambda:True,probe_admission=nullcontext)
        def dispatch(private,packet,uid):
            if packet['operation']=='status':
                return {'operator_intent':'active','production_ready':True,'enforcement_verified':True,
                    'epoch':1,'authority':self.before['authority'],'worktree':str(private.project.worktree),
                    'executables':['/sealed/bin/python3']}
            self.calls.append(packet['request_id'])
            result=subprocess.run([sys.executable,*packet['argv'][1:]],cwd=private.project.worktree,capture_output=True,text=True,timeout=20)
            return {'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr,'timed_out':False}
        targets=[('do_again.supervisor.worker_probe.EXECUTION_ROOT',{'new':self.root/'execution'}),
            ('do_again.supervisor.worker_probe.os.chown',{}),
            ('do_again.supervisor.worker_probe.MacOSProcesses',{}),
            ('do_again.supervisor.capability_probe.qualification_gate',{'return_value':self.before}),
            ('do_again.supervisor.macos_server.machine_identity',{'return_value':{'source_sha':'b'*40}}),
            ('do_again.supervisor.macos_server.ProjectBroker.dispatch',{'new':dispatch}),
            ('do_again.supervisor.macos_server.verify_installation',{}),
            ('do_again.supervisor.control_history.sys.platform',{'new':'darwin'}),
            ('do_again.supervisor.control_history.os.geteuid',{'return_value':0,'create':True})]
        for target,kwargs in targets:
            p=patch(target,**kwargs);mock=p.start();self.addCleanup(p.stop)
            if target.endswith('MacOSProcesses'):mock.return_value.owned.return_value=[]

    def test_tasks_restart_uncertainty_pause_and_qualification_replay(self):
        result=qualify_worker(self.broker)
        self.assertTrue(result['verified']);self.assertEqual(result['tasks'],3)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(result['browser_delivery'],'not_measured')
        self.assertEqual(qualify_worker(self.broker),result)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(self.broker.ledger.pending(self.broker.project.key),[])

    def test_interrupted_qualification_never_reexecutes(self):
        with patch('do_again.core.agent.Agent.run',side_effect=ExecutionBlocked('synthetic crash')):
            with self.assertRaises(ExecutionBlocked):qualify_worker(self.broker)
        with self.assertRaisesRegex(ExecutionBlocked,'no replay'):qualify_worker(self.broker)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
