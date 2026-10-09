from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import sys
import threading
import hashlib
from types import SimpleNamespace
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from do_again.supervisor.git_export import export_commit
from do_again.supervisor.github import GitHubRepository, publish_commit
from do_again.supervisor.macos_execution import ExecutionBlocked, ExecutionLedger, ProjectExecution


class FakeRepository:
    repository = 'Tran-Steven/do-again'
    def __init__(self, export, branch):
        self.export=export;self.branch=branch;self.head=None;self.pr=None;self.effects=[]
        self.lose_pr_response=False
    def request(self, method, endpoint, payload=None):
        if method=='GET':
            if endpoint.startswith('git/ref/'):
                return None if self.head is None else {'object':{'sha':self.head}}
            if endpoint.startswith('git/commits/'):
                return {'sha':self.export['parent'],'tree':{'sha':'a'*40}}
            if endpoint.startswith('pulls?'):return [] if self.pr is None else [self.pr]
            if endpoint.startswith('pulls/'):return self.pr
        self.effects.append((method,endpoint,payload))
        if endpoint=='git/blobs':
            import base64,hashlib
            data=base64.b64decode(payload['content'])
            return {'sha':hashlib.sha1(b'blob '+str(len(data)).encode()+b'\x00'+data).hexdigest()}
        if endpoint=='git/trees':return {'sha':self.export['tree']}
        if endpoint=='git/commits':return {'sha':self.export['head']}
        if endpoint.startswith('git/refs'):
            self.head=payload['sha'];return {'object':{'sha':self.head}}
        if method=='PATCH' and endpoint.startswith('pulls/'):
            self.pr.update(payload);return self.pr
        if endpoint=='pulls':
            self.pr={'number':7,'state':'open','title':payload['title'],'body':payload['body'],
                     'html_url':'https://github.com/'+self.repository+'/pull/7',
                     'head':{'sha':self.head,'ref':self.branch,'repo':{'full_name':self.repository}},
                     'base':{'ref':'main','repo':{'full_name':self.repository}}}
            if self.lose_pr_response:raise OSError('synthetic response lost after PR creation')
            return self.pr
        raise AssertionError((method,endpoint))


@unittest.skipUnless(shutil.which('git'),'real Git fixture required')
class PublicationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();self.git=Path(shutil.which('git'))
        self.env=dict(os.environ,GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull)
        self.command('init','-q',str(self.root))
        self.command('config','user.name','Do Again');self.command('config','user.email','fixture@example.invalid')
        (self.root/'selected').write_bytes(b'before\n');(self.root/'deleted').write_bytes(b'delete\n')
        self.command('add','.');self.command('commit','-qm','base')
        self.base=self.command('rev-parse','HEAD').strip()
        (self.root/'selected').write_bytes(b'after\x00binary\xff\n');(self.root/'deleted').unlink()
        self.command('add','-A');self.command('commit','-qm','selected change')
        self.head=self.command('rev-parse','HEAD').strip()
        self.export=export_commit(self.git,self.root/'.git',self.head)
        self.branch='do-again/task-'+'b'*20
        self.api=FakeRepository(self.export,self.branch)
        @contextmanager
        def admission():yield
        self.admission=admission
    def command(self,*args):
        return subprocess.check_output([str(self.git),'-C',str(self.root),*args],env=self.env,text=True,stderr=subprocess.DEVNULL)
    def publish(self):
        return publish_commit(self.api,self.export,self.branch,'Useful change','Acceptance evidence',effect_guard=self.admission)

    def test_real_binary_and_deleted_file_export_publishes_exact_commit_and_draft(self):
        self.assertEqual(self.export['parent'],self.base)
        deleted=next(e for e in self.export['entries'] if e['path']=='deleted')
        self.assertIsNone(deleted['sha']);self.assertEqual(deleted['mode'],'100644')
        result=self.publish()
        self.assertEqual(result['head'],self.head)
        self.assertEqual(self.api.head,self.head)
        self.assertTrue(next(p for m,e,p in self.api.effects if e=='pulls')['draft'])
        self.assertFalse(any(e.startswith('git/refs/heads/main') for m,e,p in self.api.effects))
        before=list(self.api.effects);self.assertEqual(self.publish(),result);self.assertEqual(self.api.effects,before)

    def test_changed_remote_tip_and_unreserved_branch_have_no_effect(self):
        self.api.head='f'*40
        with self.assertRaises(ExecutionBlocked):self.publish()
        self.assertEqual(self.api.effects,[])

        self.api.head=None;self.branch='main'
        with self.assertRaises(ExecutionBlocked):self.publish()
        self.assertEqual(self.api.effects,[])

    def test_workflow_changes_rejected_before_any_github_operation(self):
        for path in ('.github/workflows/ci.yml','.GitHub/Workflows/other.yml',
                     '.github/workflows','../.github/workflows/ci.yml'):
            with self.subTest(path=path):
                self.export['entries'][0]['path']=path
                with patch.object(self.api,'request',side_effect=AssertionError('network must not run')):
                    with self.assertRaises(ExecutionBlocked):self.publish()

    def test_pause_before_reference_mutation_does_not_publish_branch_or_pr(self):
        calls=0
        @contextmanager
        def admission():
            nonlocal calls
            calls+=1
            if calls==4:raise ExecutionBlocked('synthetic operator pause')
            yield
        self.admission=admission
        with self.assertRaises(ExecutionBlocked):self.publish()
        self.assertIsNone(self.api.head);self.assertIsNone(self.api.pr)

    def test_reference_update_never_force_pushes(self):
        self.api.head=self.base;self.publish()
        changes=[p for m,e,p in self.api.effects if m=='PATCH']
        self.assertEqual(changes,[{'sha':self.head,'force':False}])

    def test_lost_pr_response_is_uncertain_and_readback_proves_existing_effect(self):
        self.api.lose_pr_response=True
        with self.assertRaises(OSError):self.publish()
        self.assertIsNotNone(self.api.pr)
        self.api.lose_pr_response=False;before=list(self.api.effects)
        self.assertEqual(self.publish()['pull_request'],7)
        self.assertEqual(self.api.effects,before)

    def test_export_rejects_large_blobs_before_reading_their_contents(self):
        (self.root/'selected').write_bytes(b'x'*(256*1024));self.command('add','selected');self.command('commit','-qm','large')
        head=self.command('rev-parse','HEAD').strip()
        with self.assertRaises(ExecutionBlocked):export_commit(self.git,self.root/'.git',head)


class GitHubScopeTests(unittest.TestCase):
    def test_repository_scope_and_redacted_http_failures(self):
        with self.assertRaises(ExecutionBlocked):GitHubRepository('Tran-Steven/sonary','s'*40)
        api=GitHubRepository('Tran-Steven/do-again','s'*40)
        with patch('do_again.supervisor.github.https_bytes',return_value=(401,b'synthetic credential failure')) as fetch:
            with self.assertRaises(ExecutionBlocked) as error:api.request('GET','git/ref/heads/main')
            self.assertNotIn('synthetic credential',str(error.exception))
            self.assertTrue(fetch.call_args.args[0].startswith('https://api.github.com/repos/Tran-Steven/do-again/'))

    def test_arbitrary_admin_endpoints_and_main_updates_cannot_contact_network(self):
        api=GitHubRepository('Tran-Steven/do-again','s'*40)
        with patch('do_again.supervisor.github.https_bytes') as fetch:
            for method, endpoint in (('DELETE','git/refs/heads/main'),('PATCH','git/refs/heads/main'),
                                      ('POST','../../sonary/pulls'),('GET','actions/secrets'),
                                      ('GET','pulls?state=all&head=other:do-again/task-'+ 'b'*20+'&base=main&per_page=100')):
                with self.subTest(endpoint=endpoint),self.assertRaises(ExecutionBlocked):api.request(method,endpoint)
            fetch.assert_not_called()


@unittest.skipUnless(shutil.which('git'),'real Git fixture required')
class PublicationBrokerTests(unittest.TestCase):
    command=PublicationTests.command
    def setUp(self):
        PublicationTests.setUp(self)
        from do_again.supervisor import publication
        self.module=publication
        project=ProjectExecution(self.root,401,401,'_doagain_da',self.root,(self.git,Path(sys.executable)))
        self.branch='do-again/task-'+hashlib.sha256((project.key+':approved-fixture').encode()).hexdigest()[:20]
        self.command('branch','-m',self.branch)
        self.api=FakeRepository(self.export,self.branch)
        self.state={'intent':'active','epoch':1,'goal_revision':'approved-fixture'}
        state_dir=self.root/'protected-state';state_dir.mkdir()
        (state_dir/'github-token').write_text('s'*40);(state_dir/'github-token').chmod(0o600)
        self.broker=SimpleNamespace(project=project,state=state_dir,lock=threading.Lock(),
                    admission=self.admission,registry=SimpleNamespace(status=lambda repo:dict(self.state)),
                    ledger=ExecutionLedger(state_dir/'ledger.sqlite'),_verified=lambda:True,
                    config={'production_ready':True,'source_sha':'fixture-source','python':sys.executable,
                    'git':str(self.git),'projects':[{'key':project.key,'github_repository':self.api.repository}]})
        self.packet={'operation':'git_publish','request_id':'publication-request-1','expected_head':self.head,
                     'expected_epoch':1,'title':'Scoped change','body':'Acceptance evidence','request_fingerprint':'b'*64}
        self.exports=0
        def capture(spec,timeout,*,start_guard):
            with start_guard():self.exports+=1
            return {'returncode':0,'timed_out':False,'stdout':json.dumps(self.export)}
        # Protocol fixtures substitute execution/transport only. Real Git export
        # is tested above; installed native/network qualification is separate.
        patches=[patch.object(publication.sys,'platform','darwin'),
                 patch.object(publication.os,'geteuid',return_value=0,create=True),
                 patch.object(publication.os,'chown',create=True),
                 patch.object(publication,'EXECUTION_ROOT',self.root/'execution'),
                 patch.object(publication,'read_credential',return_value='s'*40),
                 patch.object(publication,'GitHubRepository',return_value=self.api),
                 patch.object(publication,'MacOSProcesses',return_value=SimpleNamespace(owned=lambda uid:[])),
                 patch.object(publication,'launch_spec',return_value={}),
                 patch.object(publication,'capture',side_effect=capture),
                 patch('do_again.supervisor.macos_server.verify_installation')]
        for item in patches:item.start();self.addCleanup(item.stop)

    def test_completed_publication_receipt_replays_without_export_or_remote_effect(self):
        result=self.module.publish_via_broker(self.broker,self.packet)
        self.assertEqual(result['state'],'succeeded')
        effects=list(self.api.effects)
        self.assertEqual(self.module.publish_via_broker(self.broker,self.packet),result)
        self.assertEqual(self.exports,1);self.assertEqual(self.api.effects,effects)

    def test_lost_pr_response_blocks_after_journal_restart_without_second_trigger(self):
        self.api.lose_pr_response=True
        with self.assertRaises(OSError):self.module.publish_via_broker(self.broker,self.packet)
        self.broker.ledger=ExecutionLedger(self.broker.state/'ledger.sqlite')
        effects=list(self.api.effects)
        with self.assertRaisesRegex(ExecutionBlocked,'ambiguous started'):
            self.module.publish_via_broker(self.broker,self.packet)
        self.assertEqual(self.api.effects,effects)
        self.assertEqual(len([e for m,e,p in effects if e=='pulls']),1)
        self.state['intent']='maintenance'
        result=self.module.reconcile_publication(self.broker,
                          {'operation':'git_publication_reconcile','request_id':self.packet['request_id']})
        self.assertEqual(result['state'],'succeeded');self.assertTrue(result['reconciled_read_only'])
        self.assertEqual(self.api.effects,effects)
        self.assertEqual(self.broker.ledger.pending(self.broker.project.key),[])

    def test_missing_pr_or_changed_payload_stays_uncertain_without_effect(self):
        self.api.lose_pr_response=True
        with self.assertRaises(OSError):self.module.publish_via_broker(self.broker,self.packet)
        effects=list(self.api.effects)
        self.api.pr['body']='different payload'
        result=self.module.reconcile_publication(self.broker,
                          {'operation':'git_publication_reconcile','request_id':self.packet['request_id']})
        self.assertEqual(result['state'],'post_dispatch_uncertain')
        self.assertEqual(self.api.effects,effects)
        self.assertEqual(len(self.broker.ledger.pending(self.broker.project.key)),1)

    def test_forged_repository_fields_and_paused_authority_have_no_effect(self):
        with self.assertRaises(ExecutionBlocked):
            self.module.publish_via_broker(self.broker,self.packet|{'repository':'Tran-Steven/sonary'})
        self.state['intent']='paused'
        with self.assertRaises(ExecutionBlocked):self.module.publish_via_broker(self.broker,self.packet)
        self.assertEqual(self.api.effects,[]);self.assertEqual(self.exports,0)


@unittest.skipUnless(os.name=='posix','native POSIX credential modes required')
class CredentialPrivacyTests(unittest.TestCase):
    def test_open_or_oversized_credentials_are_rejected_before_read(self):
        from do_again.supervisor.publication import read_credential
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);secret=root/'github-token';secret.write_text('s'*40)
            broker=SimpleNamespace(state=root)
            with patch('do_again.supervisor.publication.private_root_file'):
                secret.chmod(0o644)
                with self.assertRaises(ExecutionBlocked):read_credential(broker)
                secret.chmod(0o600);secret.write_text('s'*300)
                with self.assertRaises(ExecutionBlocked):read_credential(broker)
                secret.write_text('s'*40)
                self.assertEqual(read_credential(broker),'s'*40)


class RuntimeAttestationTests(unittest.TestCase):
    def test_changed_manifest_invalidates_same_source_native_proof(self):
        from do_again.supervisor import macos_server
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'current').mkdir();(root/'state').mkdir()
            manifest=root/'current/manifest.json';manifest.write_bytes(b'first build')
            python=root/'python';python.write_bytes(b'fixed interpreter')
            original=Path.read_bytes
            def read(path):
                return b'fixture sandbox' if path==Path('/usr/bin/sandbox-exec') else original(path)
            with patch.object(macos_server,'INSTALL_ROOT',root), \
                    patch.object(macos_server.sys,'executable',str(python)), \
                    patch.object(Path,'read_bytes',read):
                config={'source_sha':'same source'}
                broker=SimpleNamespace(config=config,state=root/'state')
                proof={'identity':macos_server.machine_identity(config),'result':{'verified':True}}
                (broker.state/'enforcement.json').write_text(json.dumps(proof))
                self.assertTrue(macos_server.ProjectBroker._verified(broker))
                manifest.write_bytes(b'changed Git or certificate bundle')
                self.assertFalse(macos_server.ProjectBroker._verified(broker))
