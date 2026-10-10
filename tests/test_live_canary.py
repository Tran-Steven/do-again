"""Canary admission regressions, not live browser/native acceptance evidence."""
import copy
import os
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

from do_again.core.schema import atomic_json
from do_again.supervisor.authority import AuthorityRegistry,project_identity
from do_again.supervisor.live_canary import scope,CanaryAuthority,effect_authorized,activate
from do_again.supervisor.macos_execution import ExecutionBlocked,ExecutionLedger
from do_again.supervisor.control_history import canary_entries,gate


class LiveCanaryTests(unittest.TestCase):
    def test_worker_reconciles_lost_control_response_read_only_before_admission(self):
        from do_again.supervisor.live_canary_worker import reconciled_status
        repo=Path('/synthetic/canary')
        pending={'operator_intent':'active','inflight_request_ids':[],
                 'unresolved_executions':[{'request_id':'original-control','state':'started'}]}
        complete={**pending,'unresolved_executions':[]}
        read=Mock(side_effect=[pending,complete]);rpc=Mock(return_value={'replay':False})
        self.assertEqual(reconciled_status(repo,read_status=read,rpc=rpc),complete)
        rpc.assert_called_once_with(repo,{'operation':'canary_reconcile'})
        self.assertEqual(read.call_count,2)

    def test_worker_reconciliation_never_replays_uncertainty_or_runs_after_pause(self):
        from do_again.supervisor.live_canary_worker import reconciled_status
        pending={'operator_intent':'active','inflight_request_ids':[],
                 'unresolved_executions':[{'request_id':'uncertain-execution','state':'started'}]}
        rpc=Mock(return_value={'replay':False,'remaining':pending['unresolved_executions']})
        self.assertEqual(reconciled_status(Path('/scope'),read_status=Mock(return_value=pending),rpc=rpc),pending)
        rpc.assert_called_once()
        for status in ({**pending,'operator_intent':'paused'},
                       {**pending,'inflight_request_ids':['uncertain-execution']},
                       {**pending,'unresolved_executions':[]}):
            rpc=Mock()
            self.assertEqual(reconciled_status(Path('/scope'),read_status=Mock(return_value=status),rpc=rpc),status)
            rpc.assert_not_called()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.nonce='a'*24;self.source='b'*40;self.head='c'*40
        self.grant={'nonce':self.nonce,'baseline':self.head,'parent_epoch':1,
                    'chat_url':'https://chatgpt.com/c/'+'d'*36,'binding_identity':'e'*64}
        self.config={'production_ready':False,'source_sha':self.source,'operator_home':str(self.root),
                     'operator_uid':501,'operator_gid':20,'python':'/sealed/python3',
                     'live_canary':self.grant,'projects':[{'repo':str(self.root/'do-again'),
                        'account':'_doagain_da','key':'parent','uid':401,'gid':401,
                        'worktree':'/original','executables':['/sealed/python3']}]}
        _,parent,project=scope(self.config)
        self.assertEqual(project['bundle'],'snapshots/canary.bundle')
        self.registry=AuthorityRegistry(self.root/'authority.sqlite',owner_uid=os.getuid() if os.name=='posix' else None)
        self.registry.initialize();self.registry.set_intent(Path(parent['repo']),'maintenance',goal_revision='original')
        self.repo=Path(project['repo']);self.registry.set_intent(self.repo,'maintenance',goal_revision=project['goal_revision'])
        self.ledger=ExecutionLedger(self.registry.path)
        self.parent=SimpleNamespace(config=self.config,registry=self.registry,
            project=SimpleNamespace(repo=Path(parent['repo']),key='parent'),ledger=self.ledger)
        self.broker=SimpleNamespace(config={**self.config,'projects':[project]},registry=self.registry,
            project=SimpleNamespace(repo=self.repo,key=project['key']),ledger=self.ledger,
            state=self.root/'root-state',lock=threading.Lock(),admission=nullcontext,_verified=lambda:True)
        self.broker.state.mkdir();self.authority=CanaryAuthority(self.broker,self.parent,self.grant)
        self.broker.canary=self.authority
        context=patch('do_again.supervisor.macos_server.verify_installation');context.start();self.addCleanup(context.stop)
        with patch('do_again.supervisor.live_canary.time.time',return_value=100):activate(self.broker)
        context=patch('do_again.supervisor.live_canary.time.time',return_value=110);context.start();self.addCleanup(context.stop)

    def request(self,task,stage,operation,**kwargs):
        return {'request_id':self.authority.rid(task,stage),'operation':operation,**kwargs}

    def test_no_general_production_authority_or_scope_expansion(self):
        self.assertTrue(effect_authorized(self.broker));self.assertFalse(self.config['production_ready'])
        self.assertFalse(effect_authorized(SimpleNamespace(config=self.config)))
        for field,value in [('nonce','../escape'),('parent_epoch',True),('baseline','main'),
                            ('chat_url','https://chatgpt.com/c/short')]:
            config=copy.deepcopy(self.config);config['live_canary'][field]=value
            with self.subTest(field=field),self.assertRaises(ExecutionBlocked):scope(config)
        config=copy.deepcopy(self.config);config['production_ready']=True
        with self.assertRaises(ExecutionBlocked):scope(config)

    def test_parent_pause_epoch_change_or_execution_revokes_all_effects(self):
        for intent in ('paused','maintenance','stopped','active'):
            with self.subTest(intent=intent):
                self.registry.set_intent(self.parent.project.repo,intent,goal_revision='original')
                with self.assertRaises(ExecutionBlocked):effect_authorized(self.broker)

    def test_deadline_source_goal_and_child_pause_fail_closed(self):
        with patch('do_again.supervisor.live_canary.time.time',return_value=7300):
            with self.assertRaises(ExecutionBlocked):effect_authorized(self.broker)
        self.broker.config['source_sha']='f'*40
        with self.assertRaises(ExecutionBlocked):effect_authorized(self.broker)
        self.broker.config['source_sha']=self.source
        self.registry.set_intent(self.repo,'paused',goal_revision='live-canary-'+self.nonce)
        with self.assertRaises(ExecutionBlocked):effect_authorized(self.broker)

    def test_atomic_activation_cannot_be_replayed(self):
        self.registry.set_intent(self.repo,'maintenance',goal_revision='live-canary-'+self.nonce)
        with self.assertRaises(ExecutionBlocked):activate(self.broker)
        self.assertFalse(self.broker.config['production_ready'])

    def test_typed_request_budget_and_exact_test_command(self):
        self.authority.packet(self.request(1,'edit','execute',argv=['/sealed/python3','-c','pass'],cwd='.',timeout=20))
        self.authority.packet(self.request(1,'test','execute',argv=['/sealed/python3','-m','unittest','discover',
            '-s','tests','-p','test_live_canary_'+self.nonce+'.py'],timeout=20))
        for packet in [self.request(3,'edit','execute'),self.request(1,'edit','dependency_install'),
            self.request(1,'edit','execute',argv=['/bin/bash','-c','pass'],cwd='.',timeout=20),
            self.request(1,'test','execute',argv=['/sealed/python3','-c','pass'],timeout=20),
            self.request(1,'edit','execute',argv=['/sealed/python3','-c','pass'],cwd='.',timeout=3600),
            {'operation':'operator_intent','intent':'active'},self.request(1,'publish','git_commit')]:
            with self.subTest(packet=packet),self.assertRaises(ExecutionBlocked):self.authority.packet(packet)

    def test_request_filename_and_stage_cannot_hide_a_noop(self):
        rid=self.authority.rid(1,'edit')
        self.authority.request({'request_id':rid,'operation':'scratch_script'},'automation/do_again/requests/'+rid+'.json')
        for value in ({'request_id':rid,'operation':'status'},
                      {'request_id':self.authority.rid(2,'edit'),'operation':'scratch_script'}):
            with self.assertRaises(ExecutionBlocked):
                self.authority.request(value,'automation/do_again/requests/'+rid+'.json')

    def execution_stage(self,stage,returncode=None):
        import hashlib
        rid=self.authority.rid(1,stage);fp=hashlib.sha256(rid.encode()).hexdigest()
        self.ledger.reserve(self.broker.project.key,rid,fp,
            intent={'operation':'execute','source_sha':self.source,'request_fingerprint':fp})
        if returncode is not None:self.ledger.finish(self.broker.project.key,rid,
            {'returncode':returncode,'source_sha':self.source,'timed_out':False})

    def test_one_repair_requires_known_failure_and_repaired_tests_before_commit(self):
        edit=self.request(1,'edit-repair','execute',argv=['/sealed/python3','-c','pass'],cwd='.',timeout=20)
        test=self.request(1,'test-repair','execute',argv=['/sealed/python3','-m','unittest','discover',
            '-s','tests','-p','test_live_canary_'+self.nonce+'.py'],cwd='.',timeout=20)
        commit=self.request(1,'commit','git_commit',paths=self.authority.paths())
        with self.assertRaises(ExecutionBlocked):self.authority.packet(edit)
        self.execution_stage('test')
        with self.assertRaises(ExecutionBlocked):self.authority.packet(edit)
        self.ledger.finish(self.broker.project.key,self.authority.rid(1,'test'),
            {'returncode':1,'source_sha':self.source,'timed_out':False})
        self.authority.packet(edit)
        with self.assertRaises(ExecutionBlocked):self.authority.packet(test)
        self.execution_stage('edit-repair',0);self.authority.packet(test)
        with self.assertRaises(ExecutionBlocked):self.authority.packet(commit)
        self.execution_stage('test-repair',0);self.authority.packet(commit)
        with self.assertRaises(ExecutionBlocked):self.authority.packet(dict(edit,request_id=edit['request_id']+'-again'))

    def test_success_or_other_runtime_never_grants_repair_and_failed_repair_blocks_commit(self):
        self.execution_stage('test',0)
        with self.assertRaises(ExecutionBlocked):self.authority.repair_gate(1,'edit-repair')
        self.broker.config['source_sha']='f'*40
        with self.assertRaises(ExecutionBlocked):self.authority.repair_gate(1,'edit-repair')
        self.broker.config['source_sha']=self.source
        self.execution_stage('edit-repair',0);self.execution_stage('test-repair',1)
        with self.assertRaises(ExecutionBlocked):self.authority.packet(self.request(1,'commit','git_commit',paths=self.authority.paths()))

    def test_control_sync_reserves_repair_only_after_causal_terminal_evidence(self):
        edit='automation/do_again/requests/'+self.authority.rid(1,'edit-repair')+'.json'
        test='automation/do_again/requests/'+self.authority.rid(1,'test-repair')+'.json'
        entries={edit:'a'*40,test:'b'*40}
        self.assertEqual(canary_entries(self.broker,entries),{})
        self.execution_stage('test',1)
        self.assertEqual(canary_entries(self.broker,entries),{edit:'a'*40})
        self.execution_stage('edit-repair',0)
        self.assertEqual(canary_entries(self.broker,entries),entries)

    def test_commits_require_real_terminal_test_evidence_and_exact_files(self):
        commit=self.request(1,'commit','git_commit',paths=self.authority.paths())
        with self.assertRaises(ExecutionBlocked):self.authority.packet(commit)
        rid=self.authority.rid(1,'test');fp='f'*64
        self.ledger.reserve(self.broker.project.key,rid,'g'*64,
            intent={'operation':'execute','source_sha':self.source,'request_fingerprint':fp})
        with self.assertRaises(ExecutionBlocked):self.authority.packet(commit)
        self.ledger.finish(self.broker.project.key,rid,{'returncode':0,'source_sha':self.source,'timed_out':False})
        self.authority.packet(commit)
        with self.assertRaises(ExecutionBlocked):self.authority.packet(dict(commit,paths=['.github/workflows/ci.yml']))

    def first_proof(self):
        rid=self.authority.rid(1,'publish')
        self.ledger.reserve(self.broker.project.key,rid,'f'*64,
            intent={'operation':'git_publish','head':self.head,'source_sha':self.source,'request_fingerprint':'f'*64})
        self.ledger.finish(self.broker.project.key,rid,{'state':'succeeded','returncode':0,'source_sha':self.source})
        self.authority.ci({'state':'terminal','conclusion':'success','head_sha':self.head},rid)
        self.ack={'request_ids':[rid],'chat_url':self.grant['chat_url'],'binding_identity':self.grant['binding_identity'],
                  'message_visible':True,'assistant_acknowledged':True}
        self.authority.browser({'acknowledgments':[self.ack]})

    def test_second_task_requires_both_original_ci_and_exact_browser_ack(self):
        packet=self.request(2,'edit','execute',argv=['/sealed/python3','-c','pass'],cwd='.',timeout=20)
        with self.assertRaises((ExecutionBlocked,OSError)):self.authority.packet(packet)
        self.first_proof();self.authority.packet(packet)
        saved=self.broker.state/'canary-ack-1.json'
        atomic_json(saved,{**self.ack,'source_sha':self.source,'assistant_acknowledged':False})
        with self.assertRaises(ExecutionBlocked):self.authority.packet(packet)
        atomic_json(saved,{**self.ack,'source_sha':self.source,'binding_identity':'changed'})
        with self.assertRaises(ExecutionBlocked):self.authority.packet(packet)

    @unittest.skipUnless(os.name=='posix','operator record ownership is a POSIX production gate')
    def test_root_lookup_uses_configured_operator_record_without_home_override(self):
        from do_again.browser.runtime import binding_identity
        path=self.root/'.do_again/browser/projects'/(self.broker.project.key[:12]+'.json')
        path.parent.mkdir(parents=True)
        record={'chat_url':self.grant['chat_url'],'binding_generation':'original'}
        atomic_json(path,record);path.chmod(0o600)
        self.broker.config['operator_uid']=os.getuid()
        self.grant['binding_identity']=binding_identity(record)
        with patch.dict(os.environ,{'HOME':'/var/root','DO_AGAIN_HOME':'/wrong/authority'}), \
             patch('do_again.browser.runtime.project_record',side_effect=AssertionError('root HOME must not select browser state')):
            self.authority.check_binding()
            atomic_json(path,{**record,'binding_generation':'changed'});path.chmod(0o600)
            with self.assertRaises(ExecutionBlocked):self.authority.check_binding()
        path.unlink();path.symlink_to(self.broker.state/'absent')
        with self.assertRaises(ExecutionBlocked):self.authority.check_binding()

    def test_changed_browser_binding_and_wrong_ack_never_release_second_task(self):
        with patch('do_again.browser.runtime.project_record',return_value={'chat_url':self.grant['chat_url'],
                                                                         'binding_generation':'changed'}):
            with self.assertRaises(ExecutionBlocked):self.authority.check_binding()
        with self.assertRaises(ExecutionBlocked):self.authority.browser({'acknowledgments':[{'assistant_acknowledged':False}]})

    def test_control_sync_withholds_premature_second_task_instead_of_claiming_it(self):
        one='automation/do_again/requests/'+self.authority.rid(1,'edit')+'.json'
        two='automation/do_again/requests/'+self.authority.rid(2,'edit')+'.json'
        other='automation/do_again/requests/other-project-task.json'
        entries={one:'a'*40,two:'b'*40,other:'c'*40}
        self.assertEqual(canary_entries(self.broker,entries),{one:'a'*40})
        self.first_proof();self.assertEqual(canary_entries(self.broker,entries),{one:'a'*40,two:'b'*40})

    def test_export_rejects_unrelated_files_workflows_deletions_and_aliases(self):
        export={'entries':[{'path':p,'sha':'a'*40,'mode':'100644'} for p in self.authority.paths()]}
        self.authority.export(export)
        for field,value in [('path','src/do_again/supervisor/live_canary.py'),('path','.github/workflows/ci.yml'),
                            ('mode','120000'),('sha',None)]:
            changed=copy.deepcopy(export);changed['entries'][0][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ExecutionBlocked):self.authority.export(changed)

    def test_control_gate_admits_only_canary_without_production_switch(self):
        gate(self.broker,2)
        ordinary=SimpleNamespace(config=self.config,registry=self.registry,project=self.parent.project,_verified=lambda:True)
        with self.assertRaises(ExecutionBlocked):gate(ordinary,1)

    def test_parent_effect_journal_is_unchanged_by_canary_evidence(self):
        before=self.ledger.evidence('parent');self.first_proof()
        self.assertEqual(self.ledger.evidence('parent'),before)
