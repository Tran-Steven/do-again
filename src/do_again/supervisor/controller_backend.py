"""Sealed backend factory; no caller supplied adapters or capability selection."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import threading
from ..core.schema import canonical_json
from ..service.offline_continuation import OfflineContinuation, digest, controller_artifact, persistent_scope
from ..service.controller_loop import ControllerLoop, DeterministicPlanner
from ..service.controller_planner import CodexPlanner, PlanningCallJournal
from ..service.planner_selection import select_controller_planner
from .controller_configuration import validate_controller_configuration
from .controller_receipts import NativeDevelopmentVerifier
from .controller_transport import ControllerHistory
from .development_attestation import DevelopmentAttestations
from .development_pipeline import DevelopmentPipeline
from .controller_native_effects import NativeControllerEffects
from .macos_execution import ExecutionBlocked

class ControllerBackend:
    def __init__(self,broker):
        self.broker=broker;self.local=threading.local()
        scope=validate_controller_configuration(broker.config)
        if scope is None or broker.project.account!='_doagain_jp':
            raise ExecutionBlocked('no sealed controller authority for this execution identity')
        self.controller=OfflineContinuation(broker.state/controller_artifact(scope,'controller.json'),scope,
            {k:scope[k] for k in ('source_sha','goal_sha256','epoch','repository','branch')})
        self.profile=broker.config['controller_profile']
        self.attestations=DevelopmentAttestations(Path(broker.config['authority_path']))
        self.effects=NativeControllerEffects(broker,self.profile,self.admission,self.select_tests)
        self.pipeline=DevelopmentPipeline(controller=self.controller,ledger=broker.ledger,
            attestations=self.attestations,project=broker.project.key,source_sha=broker.config['source_sha'],
            directory=broker.state,admission=self.admission,effects=self.effects)
        self.verifier=NativeDevelopmentVerifier(source_sha=broker.config['source_sha'],
            observe_execution=lambda rid,fp:broker.ledger.observe_request(broker.project.key,rid,fp),
            observe_development=lambda rid,fp:self.attestations.observe(broker.project.key,rid,fp))
        def codex_planner():
            from .controller_codex_helper import SealedCodexHelper
            helper=SealedCodexHelper(broker.config,broker.state,self.admission)
            journal=PlanningCallJournal(broker.state/'planning-calls.json')
            return CodexPlanner(helper.authorize,journal.reserve,helper.transport,self.build_request)
        def browser_planner():
            from .controller_browser_helper import SealedBrowserPlannerHelper
            from ..service.browser_planner import BrowserPlanner
            helper=SealedBrowserPlannerHelper(broker.config,broker.state,self.admission)
            journal=PlanningCallJournal(broker.state/controller_artifact(scope,'browser-planning-calls.json'))
            return BrowserPlanner(
                helper.authorize,journal.reserve,helper.transport,self.build_request,
                source_sha=broker.config['source_sha'],
                goal_sha256=scope['goal_sha256'],epoch=scope['epoch'])
        self.planner=select_controller_planner(
            scope['planner'],
            offline_factory=lambda:DeterministicPlanner(self.offline_select),
            codex_factory=codex_planner,
            browser_factory=browser_planner)

    @contextmanager
    def admission(self):
        from .macos_server import verify_installation
        from .jobpipe_development import require_broker_jobpipe_development
        with self.broker.admission():
            verify_installation(self.broker.config)
            status=self.broker.registry.status(self.broker.project.repo)
            validate_controller_configuration(self.broker.config,epoch=(status['epoch'] - 1 if status['intent'] == 'active' and status['epoch'] == self.controller.scope['epoch'] + 1 else status['epoch']))
            require_broker_jobpipe_development(self.broker)
            if status['intent']!='active' or not self.broker._verified():
                raise ExecutionBlocked('controller admission inactive or unqualified')
            yield

    def select_tests(self,request):
        state=self.controller.status(datetime.now(timezone.utc))
        if state['requests'][-1]['request']!=request:raise ExecutionBlocked('test selection request differs')
        # Retest completed priorities and the next bounded priority. The planner
        # cannot replace tests or select a passing-only subset.
        return self.profile['test_cases'] if persistent_scope(self.controller.scope) else self.profile['test_cases'][:len(state['requests'])]

    def collect(self):
        state=self.controller.status(datetime.now(timezone.utc));cycle=len(state['requests'])+1
        sid='offline-'+digest(self.controller.scope)[:20]+'-'+str(cycle)+'-probe'
        fp=digest({'scope':self.controller.scope,'cycle':cycle,'kind':'planning-probe'})
        with self.internal():return self.effects.collect(sid,fp)

    def build_request(self,answer,evidence,now):
        scope=self.controller.scope
        return {'request':{'schema_version':1,'operation':'scratch_script',
            'args':{'language':'python','content':answer['script']},
            'expected':{'repo_head':evidence['repo_head']},'limits':{'timeout_seconds':600},
            'issued_at_utc':now.isoformat(),'expires_at_utc':min(now+timedelta(minutes=15),
                datetime.fromisoformat(scope['expires_at_utc'].replace('Z','+00:00')) if scope['expires_at_utc'] is not None else now+timedelta(minutes=15)).isoformat()},
            'gap':answer['gap'],'acceptance':answer['acceptance'],'evidence_sha256':digest(evidence)}

    def offline_select(self,goal,evidence,state,now):
        failures={c['case'] for c in evidence['cases'] if c['returncode']!=0}
        tasks=[t for t in self.profile['offline_tasks'] if t['case'] in failures]
        if not tasks:raise ExecutionBlocked('no measured failing offline priority remains')
        return self.build_request({k:tasks[0][k] for k in ('gap','acceptance','script')},evidence,now)

    @contextmanager
    def internal(self):
        previous=getattr(self.local,'internal',False);self.local.internal=True
        try:yield
        finally:self.local.internal=previous

    def guard_packet(self,packet):
        if not getattr(self.local,'internal',False):
            raise ExecutionBlocked('controller execution capabilities require original staged reservation')

    def execute(self,packet):
        if set(packet)!={'operation','request'} or packet['operation']!='development_execute':
            raise ExecutionBlocked('development entrypoint accepts exact reserved request only')
        with self.internal():return self.pipeline.execute(packet['request'],datetime.now(timezone.utc))

    def tick(self,now):
        from .control_history import api_for
        with self.admission():api=api_for(self.broker)
        history=ControllerHistory(self.controller,api,self.admission)
        return ControllerLoop(self.controller,history,self.planner,planning_goal(self.broker.config['controller_goal']),
            self.collect,self.verifier).tick(now)


def require_sealed_factory(broker):
    """Authenticate the installed root runtime before any backend construction."""
    import os,sys
    from .macos_server import verify_installation
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('backend construction requires sealed root macOS runtime')
    verify_installation(broker.config)
    from .jobpipe_development import require_jobpipe_grant
    require_jobpipe_grant(broker.config,broker.project,broker.registry.status(broker.project.repo))


def create_controller_backend(broker):
    require_sealed_factory(broker)
    return ControllerBackend(broker)


def planning_goal(goal):
    """Bound public roadmap context while retaining the complete sealed goal hash."""
    value={k:v for k,v in goal.items() if k!='roadmap_snapshot'}
    snapshot=goal.get('roadmap_snapshot')
    if isinstance(snapshot,dict):
        body=snapshot.get('body','')
        value['roadmap_snapshot']={'title':snapshot.get('title'),'state':snapshot.get('state'),
            'body_prefix':body[:4096] if isinstance(body,str) else '',
            'truncated':isinstance(body,str) and len(body)>4096,'snapshot_sha256':digest(snapshot)}
    value['sealed_goal_sha256']=digest(goal)
    return value
