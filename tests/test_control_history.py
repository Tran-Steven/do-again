import base64
import copy
import hashlib
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from do_again.core.schema import canonical_json
from do_again.supervisor.control_history import blob_sha,publish_control,reconcile_control,sync_control,valid_path
from do_again.supervisor.macos_execution import ExecutionBlocked,ExecutionLedger
from do_again.supervisor.github import GitHubRepository


class Remote:
    def __init__(self):
        self.control_branch='operator-control';self.head='a'*40;self.tree='b'*40;self.entries={};self.blobs={};self.writes=[];self.lose_patch=False
        self.commits={self.head:{'sha':self.head,'tree':{'sha':self.tree},'parents':[],'message':'baseline'}}
        self.trees={self.tree:{}}
    def request(self,method,endpoint,payload=None):
        if method!='GET':self.writes.append((method,endpoint,copy.deepcopy(payload)))
        if method=='GET':
            if endpoint=='git/ref/heads/'+self.control_branch:return {'object':{'sha':self.head}}
            kind,sha=endpoint.split('/')[1:3];sha=sha.split('?')[0]
            if kind=='commits':return self.commits[sha]
            if kind=='trees':return {'sha':sha,'truncated':False,'tree':[{'path':p,'sha':s,'mode':'100644','type':'blob'} for p,s in self.trees[sha].items()]}
            if kind=='blobs':
                data=self.blobs[sha];return {'encoding':'base64','size':len(data),'content':base64.b64encode(data).decode()}
        if endpoint=='git/blobs':
            data=base64.b64decode(payload['content']);sha=blob_sha(data);self.blobs[sha]=data;return {'sha':sha}
        if endpoint=='git/trees':
            entries=dict(self.trees[payload['base_tree']]);entries.update({e['path']:e['sha'] for e in payload['tree']})
            sha=hashlib.sha1(canonical_json(entries)).hexdigest();self.trees[sha]=entries;return {'sha':sha}
        if endpoint=='git/commits':
            sha=hashlib.sha1(canonical_json(payload)).hexdigest()
            self.commits[sha]=dict(payload,sha=sha,tree={'sha':payload['tree']},parents=[{'sha':p} for p in payload['parents']]);return {'sha':sha}
        if endpoint=='git/refs/heads/'+self.control_branch:
            assert payload['force'] is False;self.head=payload['sha']
            if self.lose_patch:raise ExecutionBlocked('lost response')
            return {'object':{'sha':self.head}}
        raise AssertionError(endpoint)


class ControlHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'ledger.sqlite';self.status={'intent':'active','epoch':2,'goal_revision':'goal'}
        self.remote=Remote()
        self.broker=SimpleNamespace(project=SimpleNamespace(key='original',repo=Path('/original')),
            config={'production_ready':True},registry=SimpleNamespace(status=lambda repo:dict(self.status)),
            lock=threading.Lock(),admission=nullcontext,_verified=lambda:True,ledger=ExecutionLedger(self.path))
        self.packet={'operation':'control_publish','request_id':'control-original-20261009','epoch':2,
            'path':'automation/do_again/receipts/synthetic-task.json','value':{'state':'succeeded'}}
        for target,kwargs in [('do_again.supervisor.control_history.sys.platform',{'new':'darwin'}),
            ('do_again.supervisor.control_history.os.geteuid',{'return_value':0,'create':True}),
            ('do_again.supervisor.control_history.api_for',{'return_value':self.remote}),
            ('do_again.supervisor.macos_server.verify_installation',{})]:
            context=patch(target,**kwargs);context.start();self.addCleanup(context.stop)

    def test_publish_readback_and_terminal_replay_without_another_effect(self):
        result=publish_control(self.broker,self.packet);count=len(self.remote.writes)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(publish_control(self.broker,self.packet),result)
        self.assertEqual(len(self.remote.writes),count)
        read=sync_control(self.broker,{'operation':'control_sync','epoch':2,'known':{}})
        self.assertEqual(read['files'][self.packet['path']]['value'],self.packet['value'])
        self.assertEqual(self.remote.writes[-1][2]['force'],False)

    def test_lost_ref_response_survives_restart_and_only_reconciles(self):
        self.remote.lose_patch=True
        with self.assertRaises(ExecutionBlocked):publish_control(self.broker,self.packet)
        count=len(self.remote.writes);self.broker.ledger=ExecutionLedger(self.path)
        with self.assertRaises(ExecutionBlocked):publish_control(self.broker,self.packet)
        self.assertEqual(len(self.remote.writes),count)
        self.status['intent']='maintenance'
        result=reconcile_control(self.broker,{'operation':'control_reconcile','request_id':self.packet['request_id']})
        self.assertTrue(result['reconciled_read_only']);self.assertEqual(len(self.remote.writes),count)
        self.assertEqual(self.broker.ledger.pending('original'),[])

    def test_pause_before_first_mutation_preserves_remote(self):
        self.status['intent']='paused'
        with self.assertRaises(ExecutionBlocked):publish_control(self.broker,self.packet)
        self.assertEqual(self.remote.writes,[])
        self.assertEqual(self.broker.ledger.pending('original'),[])

    def test_pause_during_object_creation_defers_ref_and_never_retries(self):
        original=self.remote.request
        def revoke(method,endpoint,payload=None):
            result=original(method,endpoint,payload)
            if endpoint=='git/blobs' and method=='POST':self.status['intent']='paused'
            return result
        self.remote.request=revoke
        with self.assertRaises(ExecutionBlocked):publish_control(self.broker,self.packet)
        self.assertEqual(self.remote.head,'a'*40)
        self.assertEqual(len(self.remote.writes),1)
        self.assertEqual(len(self.broker.ledger.pending('original')),1)

    def test_paths_and_payload_cannot_expand_project_or_admin_scope(self):
        for path in ('.github/workflows/ci.yml','../other','automation/do_again/requests/new.json',
                     'automation/do_again/cancellations/new.json','automation/do_again/receipts/x/../../main.json'):
            with self.subTest(path=path),self.assertRaises(ExecutionBlocked):
                publish_control(self.broker,{**self.packet,'path':path})
        with self.assertRaises(ExecutionBlocked):publish_control(self.broker,{**self.packet,'repository':'other'})
        self.assertEqual(self.remote.writes,[])

    def test_existing_claim_cannot_be_overwritten(self):
        self.packet['path']='automation/do_again/claims/task.json'
        publish_control(self.broker,self.packet)
        with self.assertRaises(ExecutionBlocked):
            publish_control(self.broker,{**self.packet,'request_id':'control-other-20261009','value':{'owner':'other'}})

    def test_canary_control_is_published_and_reconciled_on_original_branch_only(self):
        self.remote.control_branch='do-again/canary-'+('c'*24)+'/control'
        self.remote.lose_patch=True
        with self.assertRaises(ExecutionBlocked):publish_control(self.broker,self.packet)
        writes=copy.deepcopy(self.remote.writes)
        self.assertEqual(writes[-1][1],'git/refs/heads/'+self.remote.control_branch)
        self.remote.control_branch='operator-control'
        with self.assertRaises(ExecutionBlocked):
            reconcile_control(self.broker,{'operation':'control_reconcile','request_id':self.packet['request_id']})
        self.assertEqual(self.remote.writes,writes)
        self.assertEqual(len(self.broker.ledger.pending('original')),1)
        self.remote.control_branch='do-again/canary-'+('c'*24)+'/control'
        self.assertTrue(reconcile_control(self.broker,{'operation':'control_reconcile',
            'request_id':self.packet['request_id']})['reconciled_read_only'])
        self.assertEqual(self.remote.writes,writes)

    def test_sync_cache_is_bound_to_repository_and_control_branch(self):
        packet={'operation':'control_sync','epoch':2,'known':{}}
        first=sync_control(self.broker,packet)
        self.remote.control_branch='do-again/canary-'+('c'*24)+'/control'
        self.remote.head='d'*40
        self.remote.commits[self.remote.head]=dict(self.remote.commits['a'*40],sha=self.remote.head)
        second=sync_control(self.broker,packet)
        self.assertNotEqual(first['head'],second['head'])
        self.assertEqual(second['head'],self.remote.head)

    def test_canary_api_cannot_read_or_write_legacy_or_other_canary_branch(self):
        branch='do-again/canary-'+('c'*24)+'/control'
        api=GitHubRepository('Tran-Steven/do-again','x'*32,control=True,control_branch=branch)
        with patch('do_again.supervisor.github.https_bytes',return_value=(200,b'{}')) as network:
            api.request('GET','git/ref/heads/'+branch)
            api.request('PATCH','git/refs/heads/'+branch,{'sha':'a'*40,'force':False})
            self.assertEqual(network.call_count,2)
            for other in ('operator-control','main','do-again/canary-'+('d'*24)+'/control'):
                for method,endpoint,payload in (
                    ('GET','git/ref/heads/'+other,None),
                    ('PATCH','git/refs/heads/'+other,{'sha':'a'*40,'force':False})):
                    with self.subTest(other=other,method=method),self.assertRaises(ExecutionBlocked):
                        api.request(method,endpoint,payload)
            with self.assertRaises(ExecutionBlocked):
                api.request('PATCH','git/refs/heads/'+branch,{'sha':'a'*40,'force':True})
            self.assertEqual(network.call_count,2)
        for invalid in ('main','do-again/canary-x/control',branch+'/../main',None):
            with self.subTest(branch=invalid),self.assertRaises(ExecutionBlocked):
                GitHubRepository('Tran-Steven/do-again','x'*32,control=True,control_branch=invalid)
        with self.assertRaises(ExecutionBlocked):
            GitHubRepository('Tran-Steven/do-again','x'*32,control_branch=branch)

    def test_control_api_cannot_mutate_main_or_force_any_branch(self):
        api=GitHubRepository('Tran-Steven/do-again','x'*32,control=True)
        for endpoint,payload in [('git/refs/heads/main',{'sha':'a'*40,'force':False}),
                                 ('git/refs/heads/operator-control',{'sha':'a'*40,'force':True})]:
            with self.assertRaises(ExecutionBlocked):api.request('PATCH',endpoint,payload)
        with self.assertRaises(ExecutionBlocked):api.request('POST','git/refs',{})
        with self.assertRaises(ExecutionBlocked):api.request('GET','git/ref/heads/main')


if __name__=='__main__':unittest.main()
