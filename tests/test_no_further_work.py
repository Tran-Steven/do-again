import json,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import Mock
from do_again.service.planner_contract import AuthorizedTransportPlanner,NoFurtherWork,completed_changes
from do_again.service.offline_continuation import OfflineContinuation,digest
from do_again.service.controller_loop import ControllerLoop
from do_again.service.browser_planner import PlanningNotReady
from do_again.supervisor.macos_execution import ExecutionBlocked

class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc);self.goal={'goal':'authorized source repair'}
        self.grant={'kind':'browser-planning-persistent-v1','source_sha':'a'*40,'goal_sha256':digest(self.goal)}
        self.state={'requests':[],'acknowledged':[],'outstanding':None}
        self.evidence={'cases':[{'returncode':0,'native_test_counts':{'collected':1,'passed':1,'failed':0,'skipped':0,'pytest_exit_code':0}}]}
        class Journal:
            def __init__(self):self.invalid=Mock()
            def reserve(self,*args):return {'call_id':'planner-'+'b'*20+'-1'}
            def answered_invalid(self,*args):self.invalid(*args)
        self.journal=Journal()
        self.builder=Mock();grant=self.grant
        class Planner(AuthorizedTransportPlanner):
            def _validated_grant(self,now):return grant
        self.Planner=Planner
    def select(self,answer):
        self.transport=Mock(return_value=answer)
        p=self.Planner(None,self.journal.reserve,self.transport,self.builder)
        # Simulate bounded goal context containing contract/truncated roadmap:
        return p.select(goal=dict(self.goal,execution_contract={'allowed':['source.py']}),
                        evidence=self.evidence,state=self.state,now=self.now)
    def test_assessment_is_source_goal_call_bound_without_building_request(self):
        result=self.select({'kind':'no_further_work','summary':'No further authorized work found.','acknowledged_receipts':[]})
        self.assertIsInstance(result,NoFurtherWork);self.builder.assert_not_called()
        self.assertEqual(result.goal_sha256,self.grant['goal_sha256']);self.assertEqual(result.source_sha,'a'*40)
        self.assertEqual(self.transport.call_args.args[1]['oneOf'][1]['properties']['kind']['const'],'no_further_work')
        self.assertFalse(result.record()['counted_as_coding_cycle']);self.assertFalse(result.record()['goal_independently_verified'])
    def test_extra_code_missing_ack_empty_or_overlong_summary_are_rejected(self):
        original={'kind':'no_further_work','summary':'No more work.','acknowledged_receipts':[]}
        for bad in (dict(original,script='print(1)'),dict(original,summary=''),dict(original,summary='x'*1001),
                    dict(original,acknowledged_receipts=['invented']),{'kind':'no_further_work','summary':'done'}):
            with self.subTest(bad=bad),self.assertRaises(PlanningNotReady):self.select(bad)
        self.builder.assert_not_called();self.assertEqual(self.journal.invalid.call_count,5)
    def test_failed_empty_or_missing_native_test_counts_cannot_finish(self):
        cases=[[],[{'returncode':1}], [{'returncode':0}],
               [{'returncode':0,'native_test_counts':{'collected':0,'passed':0,'failed':0,'skipped':0,'pytest_exit_code':0}}],
               [{'returncode':0,'native_test_counts':{'collected':1,'passed':1,'failed':0,'skipped':1,'pytest_exit_code':0}}]]
        for values in cases:
            self.evidence={'cases':values}
            with self.subTest(cases=values),self.assertRaises(PlanningNotReady):
                self.select({'kind':'no_further_work','summary':'done','acknowledged_receipts':[]})
            self.assertNotIn('oneOf',self.transport.call_args.args[1])
        self.builder.assert_not_called()

    def test_task_answer_remains_compatible(self):
        self.select({'gap':'real gap','acceptance':'real tests','script':'print(1)','acknowledged_receipts':[]})
        self.builder.assert_called_once()

class AssessmentJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'journal.json'
        self.now=datetime.now(timezone.utc)
        self.scope={'kind':'jobpipe-native-controller-v1','repository':'Tran-Steven/jobpipe','branch':'operator-control',
            'source_sha':'a'*40,'goal_sha256':digest({'goal':'g'}),'epoch':7,'issued_at_utc':self.now.isoformat(),
            'expires_at_utc':None,'max_requests':60,'max_cycles':60,'max_outstanding':1,'max_retries':0,
            'planner':'chatgpt-browser-cdp','integration':'proposal-only','network_access':False,
            'application_submissions':False,'browser_messages':False}
        self.binding={k:self.scope[k] for k in ('repository','branch','source_sha','goal_sha256','epoch')}
        self.c=OfflineContinuation(self.path,self.scope,self.binding);self.c.start(self.now)
        self.assessment=NoFurtherWork('planner-'+'b'*20+'-1','a'*40,self.scope['goal_sha256'],'c'*64,'No further authorized work found.',[])
    def test_loop_completes_without_execution_publication_or_cycle_count(self):
        history=Mock();history.pending.return_value=[];planner=Mock();planner.select.return_value=self.assessment
        loop=ControllerLoop(self.c,history,planner,{},lambda:{},Mock())
        result=loop.tick(self.now);self.assertEqual(result['state'],'completed')
        self.assertEqual(result['requests'],[]);self.assertEqual(result['acknowledged'],[])
        history.publish_reserved.assert_not_called();history.completion.assert_not_called()
        original=self.path.read_bytes();loop.tick(self.now);self.assertEqual(planner.select.call_count,1)
        self.assertEqual(self.path.read_bytes(),original)
        restarted=OfflineContinuation(self.path,self.scope,self.binding)
        self.assertEqual(restarted.start(self.now)['completion_reason'],'planner_no_further_work')
    def test_pending_source_or_goal_mismatch_cannot_complete(self):
        from dataclasses import replace
        for value,pending in [(self.assessment,['other-request']),
                (replace(self.assessment,source_sha='d'*40),[]),
                (replace(self.assessment,goal_sha256='d'*64),[]),
                (replace(self.assessment,acknowledged_receipts=['invented']),[])]:
            before=self.path.read_bytes()
            with self.assertRaises(ExecutionBlocked):self.c.no_further_work(value,pending_ids=pending,now=self.now)
            self.assertEqual(self.path.read_bytes(),before)
    def test_raw_model_dict_cannot_replace_validated_assessment(self):
        with self.assertRaises(ExecutionBlocked):self.c.no_further_work({'kind':'no_further_work'},pending_ids=[],now=self.now)
