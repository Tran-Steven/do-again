"""Protocol fixtures, not native qualification or unattended live acceptance."""
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
from test_control_history import Remote
from do_again.core.agent import Agent
from do_again.core.control_transport import BrokerControlHistory
from do_again.core.schema import atomic_json,utc_now
from do_again.supervisor.control_history import publish_control,sync_control
from do_again.supervisor.macos_execution import ExecutionLedger


class SealedWorkerProtocolTests(unittest.TestCase):
    def test_both_project_loops_execute_publish_recover_and_never_use_host_git(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root).resolve();projects={}
            for name,uid in (('do-again',401),('jobpipe',402)):
                repo=root/name;repo.mkdir();control=repo/'control';control.mkdir();work=repo/'work';work.mkdir()
                state=repo/'state';state.mkdir();policy=repo/'policy.json'
                atomic_json(policy,{'allowed_operations':['scratch_script'],'default_timeout_seconds':10})
                status={'operator_intent':'active','production_ready':True,'enforcement_verified':True,
                    'epoch':1,'authority':{'repo_head':'a'*40,'repo_branch':None},'worktree':str(work),
                    'executables':['/sealed/bin/python3']}
                authority={'intent':'active','epoch':1,'goal_revision':'synthetic'}
                broker=SimpleNamespace(project=SimpleNamespace(key=name,repo=repo),config={'production_ready':True},
                    registry=SimpleNamespace(status=lambda repo,a=authority:dict(a)),ledger=ExecutionLedger(repo/'ledger.sqlite'),
                    lock=threading.Lock(),admission=nullcontext,_verified=lambda:True)
                remote=Remote()
                for index,(filename,content) in enumerate((('calculation.py','def total(x,y): return x+y\n'),
                                                         ('test_calculation.py','import unittest,calculation\nclass Test(unittest.TestCase):\n def test_total(self): self.assertEqual(calculation.total(2,3),5)\n'),
                                                         ('README.md','Synthetic project: verified addition with regression coverage.\n'))):
                    now=utc_now();rid=f'synthetic-{name}-{index}'
                    script='from pathlib import Path;Path('+repr(filename)+').write_text('+repr(content)+')'
                    if index==2:script+=';import subprocess,sys;subprocess.run([sys.executable,"-m","unittest","test_calculation"],check=True)'
                    request={'schema_version':1,'request_id':rid,'issued_at_utc':now.isoformat(),
                        'expires_at_utc':(now+__import__('datetime').timedelta(minutes=10)).isoformat(),
                        'operation':'scratch_script','args':{'language':'python','content':script},'expected':{'repo_head':'a'*40},'limits':{'timeout_seconds':10}}
                    data=(json.dumps(request)+'\n').encode();sha=__import__('do_again.supervisor.control_history',fromlist=['blob_sha']).blob_sha(data)
                    remote.blobs[sha]=data;remote.trees[remote.tree]['automation/do_again/requests/'+rid+'.json']=sha
                projects[repo]={'broker':broker,'remote':remote,'status':status,'work':work,'control':control,'state':state,'policy':policy,'executions':0}
            def api_for(broker):return projects[broker.project.repo]['remote']
            def receive(repo,packet):
                selected=projects[repo];broker=selected['broker']
                if packet['operation']=='status':return dict(selected['status'])
                if packet['operation']=='control_sync':return sync_control(broker,packet)
                if packet['operation']=='control_publish':return publish_control(broker,packet)
                if packet['operation']=='execute':
                    selected['executions']+=1
                    # Fixed synthetic source only. Production uses the native broker.
                    proc=subprocess.run([sys.executable,*packet['argv'][1:]],cwd=selected['work'],capture_output=True,text=True,timeout=10)
                    return {'returncode':proc.returncode,'stdout':proc.stdout,'stderr':proc.stderr,'timed_out':False}
                raise AssertionError(packet)
            with patch('do_again.supervisor.control_history.sys.platform','darwin'), \
                 patch('do_again.supervisor.control_history.os.geteuid',return_value=0,create=True), \
                 patch('do_again.supervisor.macos_server.verify_installation'), \
                 patch('do_again.supervisor.control_history.api_for',side_effect=api_for), \
                 patch('do_again.core.control_transport.broker_request',side_effect=receive), \
                 patch('do_again.core.broker_executor.broker_request',side_effect=receive), \
                 patch('do_again.supervisor.admission.broker_request',side_effect=receive), \
                 patch.object(Agent,'git',side_effect=AssertionError('host Git fallback')):
                for repo,selected in projects.items():
                    for _ in range(2):
                        transport=BrokerControlHistory(repo,selected['control'],1)
                        agent=Agent(repo=repo,control_worktree=selected['control'],branch='operator-control',
                            policy_path=selected['policy'],state_dir=selected['state'],control_transport=transport)
                        self.assertEqual(agent.run(once=True),0)
                    receipts=list((selected['control']/'automation/do_again/receipts').glob('*.json'))
                    self.assertEqual(len(receipts),3)
                    self.assertTrue(all(json.loads(p.read_text())['state']=='succeeded' for p in receipts))
                    self.assertEqual(selected['executions'],3)
                    self.assertTrue((selected['work']/'README.md').is_file())
                    self.assertEqual(selected['broker'].ledger.pending(repo.name),[])
            self.assertNotEqual(projects[next(iter(projects))]['remote'].head,projects[list(projects)[1]]['remote'].head)


if __name__=='__main__':unittest.main()
