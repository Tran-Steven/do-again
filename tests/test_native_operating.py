"""Regression tests only. These fixtures never establish live V1 acceptance."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from test_offline_continuation import ContinuationTests
from test_controller_provider_scope import ProviderScopeTests
from do_again.service.offline_continuation import (OfflineContinuation, digest,
    validate_scope, controller_artifact)
from do_again.service.controller_planner import PlanningCallJournal
from do_again.service.browser_planner import BrowserPlanner, validate_reserved_browser_call
from do_again.service.planner_contract import completed_changes
from do_again.supervisor.controller_configuration import validate_controller_configuration
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.service import native_operating as runner


class PersistentAuthorityTests(unittest.TestCase):
    def setUp(self):
        ProviderScopeTests.setUp(self)
        self.config['controller_scope'].update(kind='jobpipe-native-controller-v1',
            expires_at_utc=None, max_requests=12, max_cycles=12)
        self.config['browser_planning_scope'].update(kind='browser-planning-persistent-v1',
            expires_at_utc=None, max_calls=12)
        self.config['development_scope']['kind'] = 'jobpipe-native-v1'

    def test_still_authorized_after_hours_without_canary_renewal(self):
        validate_controller_configuration(self.config, now=self.now+timedelta(days=30))

    def test_canary_cannot_borrow_persistent_browser_grant(self):
        self.config['controller_scope'].update(kind='jobpipe-offline-controller-v1',
            max_requests=3, max_cycles=3, expires_at_utc=(self.now+timedelta(hours=1)).isoformat())
        self.config['development_scope']['kind']='jobpipe-offline-v1'
        with self.assertRaises(ExecutionBlocked):
            validate_controller_configuration(self.config, now=self.now)

    def test_persistent_cannot_borrow_offline_or_codex_provider(self):
        for provider in ('sealed-offline', 'codex-cli'):
            with self.subTest(provider=provider):
                self.config['controller_scope']['planner']=provider
                with self.assertRaises(ExecutionBlocked):
                    validate_controller_configuration(self.config, now=self.now)

    def test_budget_network_epoch_and_implicit_expiry_rejected(self):
        for section, field, value in [
            ('controller_scope','max_retries',1),
            ('controller_scope','max_cycles',10001),
            ('controller_scope','network_access',True),
            ('browser_planning_scope','max_calls',13),
            ('browser_planning_scope','automatic_merge',True),
            ('browser_planning_scope','epoch',14),
            ('browser_planning_scope','expires_at_utc',(self.now+timedelta(days=1)).isoformat()),
        ]:
            candidate=copy.deepcopy(self.config)
            candidate[section][field]=value
            with self.subTest(section=section,field=field):
                with self.assertRaises(ExecutionBlocked):
                    validate_controller_configuration(candidate, now=self.now)

    def test_pause_resume_epochs_do_not_require_a_new_scope(self):
        validate_controller_configuration(self.config, now=self.now, epoch=20)
        with self.assertRaises(ExecutionBlocked):
            validate_controller_configuration(self.config, now=self.now, epoch=12)

    def test_call_budget_above_three_and_exact_grant_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=PlanningCallJournal(Path(tmp)/'calls.json')
            grant=self.config['browser_planning_scope']
            call=journal.reserve(grant,4,'prompt',{})
            validate_reserved_browser_call(call['call_id'],grant)
            with self.assertRaises(ExecutionBlocked):
                PlanningCallJournal(journal.path).reserve(grant,4,'prompt',{})
            with self.assertRaises(ExecutionBlocked):
                journal.reserve(grant,13,'prompt',{})
            with self.assertRaises(ExecutionBlocked):
                validate_reserved_browser_call('planner-'+digest(grant)[:20]+'-13',grant)
            with self.assertRaises(ExecutionBlocked):
                validate_reserved_browser_call('planner-'+'a'*20+'-1',grant)

    def test_model_must_acknowledge_original_verified_receipt(self):
        state={'acknowledged':['original'], 'requests':[{
            'phase':'acknowledged','request':{'request_id':'original'},
            'receipt_sha256':'c'*64,'implementation_commit':'d'*40,'test_commit':'d'*40,
            'gap':'fixed gap','acceptance':'verified test'}]}
        answer={'gap':'next useful change','acceptance':'regression passes','script':'pass'}
        grant=self.config['browser_planning_scope']
        calls=[]
        planner=BrowserPlanner(lambda now:grant,lambda *a:{'call_id':'reserved'},
            lambda *a,**kw:dict(answer,acknowledged_receipts=[]),
            lambda *a:calls.append(a),source_sha='a'*40,
            goal_sha256=grant['goal_sha256'],epoch=13)
        with self.assertRaisesRegex(ExecutionBlocked,'acknowledge'):
            planner.select(goal={},evidence={},state=state,now=self.now)
        self.assertEqual(calls,[])
        planner.transport=lambda *a,**kw:dict(answer,acknowledged_receipts=completed_changes(state))
        planner.select(goal={},evidence={},state=state,now=self.now)
        self.assertEqual(len(calls),1)


class PersistentJournalTests(unittest.TestCase):
    def setUp(self):
        ContinuationTests.setUp(self)
        self.scope.update(kind='jobpipe-native-controller-v1',planner='chatgpt-browser-cdp',
                          expires_at_utc=None,max_cycles=5,max_requests=5)
        self.path=Path(self.temp.name)/controller_artifact(self.scope,'controller.json')
        self.c=OfflineContinuation(self.path,self.scope,self.binding)

    def proposal(self): return ContinuationTests.proposal(self)
    def finish(self,record): return ContinuationTests.finish(self,record)

    def test_five_receipt_cycles_with_restart_preserve_canary_bytes(self):
        legacy=Path(self.temp.name)/'controller.json'
        legacy.write_bytes(b'original canary journal')
        self.c.start(self.now)
        for index in range(5):
            record=self.c.reserve(self.proposal(),pending_ids=[],now=self.now)
            self.c.publication(record['request'],self.now)
            receipt,evidence=self.finish(record)
            self.c.accept(receipt,evidence=evidence,now=self.now)
            self.c=OfflineContinuation(self.path,self.scope,self.binding)
        self.assertEqual(self.c.status(self.now+timedelta(hours=10))['state'],'completed')
        self.assertEqual(legacy.read_bytes(),b'original canary journal')
        with self.assertRaises(ExecutionBlocked):
            self.c.reserve(self.proposal(),pending_ids=[],now=self.now)

    def test_rebinding_scope_never_reuses_previous_journal(self):
        self.c.start(self.now)
        self.scope=dict(self.scope,max_cycles=6,max_requests=6)
        with self.assertRaisesRegex(ExecutionBlocked,'another scope'):
            OfflineContinuation(self.path,self.scope,self.binding).status(self.now)


class PersistentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.sha='a'*40
        self.authority=dict(operator_intent='active',production_ready=False,
                            enforcement_verified=True,source_sha=self.sha,epoch=18,
                            controller_mode='jobpipe-native-controller-v1')
        for name,value in [('_target',self.root),('state_directory',self.root)]:
            patcher=patch.object(runner,name,return_value=value)
            patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(runner,'_authorized',return_value=self.authority)
        patcher.start();self.addCleanup(patcher.stop)

    def test_crash_with_uncertain_planning_never_resends_tick(self):
        (self.root/('tick-intent.'+self.sha[:12]+'.18.json')).write_text(json.dumps(dict(
            source_sha=self.sha,epoch=18,acknowledged_cycles=0,state='tick_started')))
        (self.root/('tick-intent.'+self.sha[:12]+'.18.json')).chmod(0o600)
        with patch.object(runner,'broker_request',return_value={'state':'planning','acknowledged':[]}) as broker:
            result=runner.run_persistent(self.root,source=self.sha,epoch=18)
        self.assertEqual(result['state'],'blocked')
        self.assertIn('uncertain',result['blocker'])
        self.assertEqual(broker.call_count,1)

    def test_restart_waiting_reconciles_and_continues_without_second_start(self):
        (self.root/('tick-intent.'+self.sha[:12]+'.18.json')).write_text(json.dumps(dict(
            source_sha=self.sha,epoch=18,acknowledged_cycles=0,state='tick_started')))
        (self.root/('tick-intent.'+self.sha[:12]+'.18.json')).chmod(0o600)
        phases=[{'state':'waiting','outstanding':'original','acknowledged':[]},
                {'state':'completed','outstanding':None,'acknowledged':['original']}]
        with patch.object(runner,'broker_request',side_effect=phases) as broker, \
             patch.object(runner.time,'sleep'):
            result=runner.run_persistent(self.root,source=self.sha,epoch=18)
        self.assertEqual(result['state'],'completed')
        self.assertEqual(broker.call_args_list[1].args[1],{'operation':'controller_tick'})

    def test_root_stop_is_never_automatically_resumed(self):
        with patch.object(runner,'broker_request',return_value={'state':'stopped','acknowledged':[]}) as broker:
            result=runner.run_persistent(self.root,source=self.sha,epoch=18)
        self.assertEqual(result['state'],'stopped')
        self.assertEqual(broker.call_count,1)

    def test_qualification_scope_cannot_launch_persistent_runner(self):
        self.authority['controller_mode']='jobpipe-offline-controller-v1'
        with patch.object(runner,'broker_request') as broker:
            result=runner.run_persistent(self.root,source=self.sha,epoch=18)
        self.assertEqual(result['state'],'blocked')
        broker.assert_not_called()
