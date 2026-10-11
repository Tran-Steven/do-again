import copy
import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from do_again.supervisor.ci_observation import observe_ci
from do_again.supervisor.github import GitHubRepository
from do_again.supervisor.macos_execution import ExecutionBlocked, ExecutionLedger
from do_again.core.broker_executor import BrokerExecutor


class CIObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.ledger = ExecutionLedger(Path(self.temp.name)/'journal.sqlite')
        self.repository = 'Tran-Steven/do-again'; self.head = 'b'*40; self.source = 'a'*40
        self.branch = 'do-again/task-'+'c'*20; self.rid = 'publication-original-task'
        self.intent = {'operation':'git_publish','request_fingerprint':'d'*64,
                       'source_sha':self.source,'repository':self.repository,
                       'head':self.head,'branch':self.branch}
        self.result = dict(self.intent,state='succeeded',returncode=0,pull_request=99)
        self.ledger.reserve('test',self.rid,'e'*64,intent=self.intent)
        self.ledger.finish('test',self.rid,self.result)
        self.broker = SimpleNamespace(project=SimpleNamespace(key='test',worktree=Path('/synthetic')),
            config={'source_sha':self.source,'production_ready':False,
                    'projects':[{'key':'test','github_repository':self.repository}]},
            ledger=self.ledger,lock=threading.Lock(),admission=nullcontext)
        repo = {'full_name':self.repository}
        self.pr = {'number':99,'state':'open','draft':True,
                   'head':{'sha':self.head,'ref':self.branch,'repo':repo},
                   'base':{'ref':'main','repo':repo}}
        self.run = {'id':123,'head_sha':self.head,'head_branch':self.branch,
                    'event':'pull_request','path':'.github/workflows/ci.yml',
                    'repository':repo,'head_repository':repo,'pull_requests':[{'number':99}],
                    'status':'completed','conclusion':'success','run_attempt':1}
        self.inventory = {'total_count':1,'workflow_runs':[copy.deepcopy(self.run)]}
        self.calls = []
        self.query = urlencode({'head_sha':self.head,'branch':self.branch,
                                'event':'pull_request','per_page':100})
        def request(method,endpoint,payload=None):
            self.calls.append((method,endpoint,payload))
            self.assertEqual(method,'GET'); self.assertIsNone(payload)
            if endpoint == 'pulls/99': return copy.deepcopy(self.pr)
            if endpoint == 'actions/workflows/ci.yml/runs?'+self.query: return copy.deepcopy(self.inventory)
            if endpoint == 'actions/runs/123': return copy.deepcopy(self.run)
            raise AssertionError(endpoint)
        for target,kwargs in [
            ('do_again.supervisor.macos_server.verify_installation',{}),
            ('do_again.supervisor.macos_server.worktree_authority',{'return_value':{'repo_head':self.head}}),
            ('do_again.supervisor.control_history.api_for',{'return_value':SimpleNamespace(request=request)})]:
            context=patch(target,**kwargs); context.start(); self.addCleanup(context.stop)
        self.packet={'operation':'ci_observe','request_id':self.rid}

    def test_exact_publication_discovers_existing_ci_read_only_even_in_maintenance(self):
        before=self.ledger.evidence('test')
        result=observe_ci(self.broker,self.packet)
        self.assertEqual(result['run_id'],123); self.assertEqual(result['returncode'],0)
        self.assertEqual(result['state'],'terminal'); self.assertFalse(result['replay'])
        self.assertEqual(before,self.ledger.evidence('test'))
        self.assertEqual(result,observe_ci(self.broker,self.packet))

    def test_no_run_is_wait_not_successful_ci(self):
        self.inventory={'total_count':0,'workflow_runs':[]}
        result=observe_ci(self.broker,self.packet)
        self.assertEqual(result['state'],'waiting'); self.assertIsNone(result['run_id'])

    def test_failed_ci_is_terminal_failure(self):
        self.run['conclusion']='failure'
        self.assertEqual(observe_ci(self.broker,self.packet)['returncode'],1)

    def test_uncertain_publication_never_searches_or_replays(self):
        self.ledger.reserve('test','publication-uncertain','f'*64,intent=self.intent)
        result=observe_ci(self.broker,dict(self.packet,request_id='publication-uncertain'))
        self.assertEqual(result['state'],'publication_uncertain'); self.assertEqual(self.calls,[])

    def test_repository_head_workflow_event_and_pr_mismatches_fail_closed(self):
        for field,value in [('head_sha','f'*40),('head_branch','other'),('event','push'),
                            ('path','.github/workflows/unrelated.yml'),
                            ('repository',{'full_name':'Tran-Steven/jobpipe'}),
                            ('head_repository',{'full_name':'unrelated/fork'}),
                            ('pull_requests',[{'number':100}]),('id',True)]:
            with self.subTest(field=field):
                inventory=copy.deepcopy(self.inventory)
                self.inventory['workflow_runs'][0][field]=value
                with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet)
                self.inventory=inventory

    def test_changed_head_source_and_payload_cannot_expand_authority(self):
        with patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'f'*40}):
            with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet)
        self.broker.config['source_sha']='f'*40
        with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet)
        for extra in ({'repository':'other/repo'},{'head_sha':self.head},{'run_id':123}):
            with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet|extra)
        self.assertEqual(self.calls,[])

    def test_partial_inventory_and_rerun_race_block_success(self):
        self.inventory['total_count']=101
        with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet)
        self.inventory['total_count']=1
        self.run['run_attempt']=2
        with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet)

    def test_closed_or_non_draft_pr_is_not_canary_evidence(self):
        for field,value in [('state','closed'),('draft',False)]:
            original=self.pr[field]; self.pr[field]=value
            with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet)
            self.pr[field]=original

    def test_network_lost_response_does_not_change_effect_ledger(self):
        before=self.ledger.evidence('test')
        with patch('do_again.supervisor.control_history.api_for',side_effect=ExecutionBlocked('lost response')):
            with self.assertRaises(ExecutionBlocked):observe_ci(self.broker,self.packet)
        self.assertEqual(before,self.ledger.evidence('test'))

    def test_http_capability_accepts_only_fixed_workflow_query(self):
        api=GitHubRepository(self.repository,'x'*30,control=True)
        with patch('do_again.supervisor.github.https_bytes',return_value=(200,json.dumps(self.inventory).encode())) as network:
            api.request('GET','actions/workflows/ci.yml/runs?'+self.query)
            self.assertEqual(network.call_count,1)
            for endpoint in ['actions/workflows/other.yml/runs?'+self.query,
                             'actions/workflows/ci.yml/runs?'+self.query+'&head_sha='+self.head,
                             'actions/workflows/ci.yml/runs?'+self.query.replace('pull_request','push'),
                             'actions/workflows/ci.yml/runs?'+self.query+'&page=2']:
                with self.assertRaises(ExecutionBlocked):api.request('GET',endpoint)
            with self.assertRaises(ExecutionBlocked):api.request('POST','actions/workflows/ci.yml/dispatches',{})
            self.assertEqual(network.call_count,1)

    def test_worker_restart_reobserves_ci_without_execution_reservation(self):
        executor=object.__new__(BrokerExecutor)
        executor.repo=Path('/synthetic'); executor.policy={'allowed_operations':['ci_observe']}
        executor.rpc=lambda repo,packet:observe_ci(self.broker,packet)
        request={'request_id':'ci-original-observation','operation':'ci_observe',
                 'args':{'original_request_id':self.rid}}
        before=self.ledger.evidence('test')
        with patch('do_again.core.broker_executor.sys.platform','darwin'):
            first=executor.recover(request); second=executor.recover(request)
        self.assertEqual(first,second); self.assertTrue(first['recovered_read_only'])
        self.assertEqual(before,self.ledger.evidence('test'))
