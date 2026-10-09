"""Restart crosses the real SQLite broker/Agent boundary; no effect is replayed."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.agent import Agent
from do_again.core.broker_executor import BrokerExecutor
from do_again.core.schema import atomic_json, read_json, request_fingerprint, utc_now
from do_again.supervisor.macos_execution import ExecutionLedger, ExecutionBlocked
from do_again.supervisor.macos_server import ProjectBroker


class BrokerReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.db=self.root/'supervisor.sqlite'
        self.ledger=ExecutionLedger(self.db);self.request={'request_id':'canary-recovery-0001',
            'operation':'scratch_script','args':{'language':'python','content':'print(1)'}}
        self.fp=request_fingerprint(self.request);self.sha='a'*40
        self.intent={'operation':'execute','request_fingerprint':self.fp,'source_sha':self.sha}
        self.ledger.reserve('scope',self.request['request_id'],'b'*64,intent=self.intent)
        self.policy=self.root/'policy.json';atomic_json(self.policy,{'allowed_operations':['scratch_script']})
        self.broker=object.__new__(ProjectBroker)
        self.broker.config={'operator_uid':501,'source_sha':self.sha,'production_ready':False}
        self.broker.project=SimpleNamespace(key='scope');self.broker.ledger=ExecutionLedger(self.db)
        self.calls=[]
        def rpc(repo,packet):
            self.calls.append(packet)
            return self.broker.dispatch(packet,501)
        self.executor=BrokerExecutor(repo=self.root,policy_path=self.policy,state_dir=self.root,rpc=rpc)
        for target,kwargs in [('do_again.core.broker_executor.sys.platform',{'new':'darwin'}),
                             ('do_again.supervisor.macos_server.verify_installation',{})]:
            ctx=patch(target,**kwargs);ctx.start();self.addCleanup(ctx.stop)

    def finish(self,code=0):
        self.ledger.finish('scope',self.request['request_id'],
            {'returncode':code,'timed_out':False,'source_sha':self.sha,'stdout':'verified'})

    def test_lost_response_restart_recovers_original_result_read_only_while_closed(self):
        self.finish();before=self.ledger.evidence('scope')
        recovered=self.executor.recover(self.request)
        self.assertTrue(recovered['recovered_read_only'])
        self.assertEqual(recovered['result']['returncode'],0)
        self.assertEqual(self.calls,[{'operation':'execution_observe','request_id':self.request['request_id'],
                                     'request_fingerprint':self.fp}])
        self.assertEqual(ExecutionLedger(self.db).evidence('scope'),before)

    def test_started_execution_missing_record_and_wrong_identity_never_replay(self):
        self.assertIsNone(self.executor.recover(self.request))
        with self.assertRaises(ExecutionBlocked):
            self.executor.recover(dict(self.request,args={'language':'python','content':'different'}))
        self.assertIsNone(self.executor.recover(dict(self.request,request_id='canary-absent-0001')))
        self.assertTrue(all(p['operation']=='execution_observe' for p in self.calls))
        self.assertEqual(len(self.ledger.pending('scope')),1)

    def test_wrong_project_missing_provenance_and_corrupt_result_fail_closed(self):
        self.finish()
        self.assertEqual(self.ledger.observe_request('other',self.request['request_id'],self.fp)['state'],'not_started')
        self.ledger.reserve('legacy','legacy-request-0001','c'*64)
        self.ledger.finish('legacy','legacy-request-0001',{'returncode':0,'source_sha':self.sha})
        with self.assertRaises(ExecutionBlocked):self.ledger.observe_request('legacy','legacy-request-0001',self.fp)
        self.ledger.reserve('wrong','wrong-request-0001','d'*64,intent=self.intent)
        self.ledger.finish('wrong','wrong-request-0001',{'returncode':0,'source_sha':'e'*40})
        with self.assertRaises(ExecutionBlocked):self.ledger.observe_request('wrong','wrong-request-0001',self.fp)

    def test_terminal_result_requires_typed_execution_outcome(self):
        for index,result in enumerate(({'source_sha':self.sha},
                {'source_sha':self.sha,'returncode':False},
                {'source_sha':self.sha,'returncode':0,'timed_out':'false'})):
            request='corrupt-outcome-'+str(index)
            self.ledger.reserve('corrupt',request,'e'*64,intent=self.intent)
            self.ledger.finish('corrupt',request,result)
            with self.assertRaises(ExecutionBlocked):self.ledger.observe_request('corrupt',request,self.fp)

    def test_observer_rejects_untrusted_peer_and_extra_authority_fields(self):
        packet={'operation':'execution_observe','request_id':self.request['request_id'],'request_fingerprint':self.fp}
        with self.assertRaises(ExecutionBlocked):self.broker.dispatch(packet,401)
        with self.assertRaises(ExecutionBlocked):self.broker.dispatch(dict(packet,project='other'),501)
        with self.assertRaises(ExecutionBlocked):self.broker.dispatch(dict(packet,request_id='../other'),501)

    def restarted_agent(self):
        agent=object.__new__(Agent);agent.executor=self.executor;agent.instance_id='restarted'
        ledger=self.root/'agent-ledger.json';atomic_json(ledger,{'state':'started',
            'request_fingerprint':self.fp,'started_at_utc':utc_now().isoformat()})
        agent.local_ledger=lambda rid:read_json(ledger)
        agent.write_ledger=lambda rid,value:atomic_json(ledger,value)
        agent.receipt_payload=lambda rid:None
        agent.acquire_request_lock=lambda rid:True;agent.release_request_lock=lambda rid:None
        published=[];notified=[];agent.publish_receipt=published.append;agent.notify_receipt=notified.append
        path=self.root/(self.request['request_id']+'.json');atomic_json(path,self.request)
        return agent,path,ledger,published,notified

    def test_agent_repairs_receipt_then_second_restart_only_republishes_terminal_record(self):
        for code,state in ((0,'succeeded'),(1,'failed')):
            with self.subTest(code=code):
                # One original execution result, not a second native operation.
                if code==1:
                    self.request=dict(self.request,request_id='canary-recovery-0002')
                    self.fp=request_fingerprint(self.request)
                    self.ledger.reserve('scope',self.request['request_id'],'c'*64,
                        intent=dict(self.intent,request_fingerprint=self.fp))
                self.finish(code);before=self.ledger.evidence('scope')
                agent,path,ledger,published,notified=self.restarted_agent()
                with patch.object(self.executor,'execute',side_effect=AssertionError('replayed effect')):
                    self.assertTrue(agent.process_path(path));self.assertTrue(agent.process_path(path))
                self.assertEqual(read_json(ledger)['receipt']['state'],state)
                self.assertTrue(published[0]['result']['recovered_read_only'])
                self.assertEqual(len(notified),1)
                self.assertEqual(self.ledger.evidence('scope'),before)

    def test_unavailable_broker_preserves_started_agent_record(self):
        agent,path,ledger,published,notified=self.restarted_agent()
        with patch.object(self.executor,'recover',side_effect=ExecutionBlocked('unavailable')):
            with self.assertRaises(ExecutionBlocked):agent.process_path(path)
        self.assertEqual(read_json(ledger)['state'],'started');self.assertEqual(published,[])

    def test_crash_after_publication_recovers_pending_notification_without_execution(self):
        from types import MethodType
        from do_again.service.daemon import _queue_receipt,_pending_outbox
        self.finish();agent,path,ledger,published,_=self.restarted_agent()
        agent.state_dir=self.root/'operator-state';agent.state_dir.mkdir()
        agent.ledger_path=lambda rid:ledger
        agent.write_ledger=MethodType(Agent.write_ledger,agent)
        agent.notify_receipt=MethodType(Agent.notify_receipt,agent)
        notifications=[]
        def notify(receipt):
            notifications.append(receipt)
            if len(notifications)==1:raise OSError('crash before queue')
            _queue_receipt(agent.state_dir,receipt)
        agent.receipt_callback=notify
        agent.receipt_payload=lambda rid:published[0] if published else None
        with patch.object(self.executor,'execute',side_effect=AssertionError('execution replay')):
            self.assertTrue(agent.process_path(path))
            self.assertTrue(read_json(ledger)['notification_pending'])
            self.assertFalse(agent.process_path(path))
            self.assertFalse(read_json(ledger)['notification_pending'])
            self.assertFalse(agent.process_path(path))
        self.assertEqual(len(notifications),2);self.assertEqual(len(_pending_outbox(agent.state_dir)),1)
        self.assertEqual(notifications[0],notifications[1])

    def test_recovered_notification_never_requeues_a_reconciled_delivery(self):
        from do_again.service.daemon import _queue_receipt,_pending_outbox
        state=self.root/'delivered-state';receipt={'request_id':'completed-receipt','state':'succeeded'}
        path=_queue_receipt(state,receipt)
        reservation=state/'browser_reservations'/path.name
        value=read_json(reservation);atomic_json(reservation,dict(value,state='reconciled'))
        path.unlink()
        self.assertEqual(_queue_receipt(state,receipt),reservation)
        self.assertEqual(_pending_outbox(state),[])
