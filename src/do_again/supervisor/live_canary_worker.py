"""Canary admission for the actual sealed Agent and browser/broker loop."""
from __future__ import annotations

from argparse import Namespace
import os
import sys
import uuid
from pathlib import Path

from .live_canary import scope
from .macos_execution import INSTALL_ROOT, ExecutionBlocked
from .macos_client import broker_request


def main(config,args):
    from .immutable_worker import validate_control_paths,read_admission_status
    from .macos_server import verify_installation
    from ..core.broker_executor import BrokerExecutor
    from ..core.control_transport import BrokerControlHistory
    from ..service.daemon import _run_admitted_worker
    verify_installation(config)
    grant,_,project=scope(config)
    if (args.project!='do-again' or Path(__file__)!=INSTALL_ROOT/'current/package/do_again/supervisor/live_canary_worker.py'
            or Path(sys.executable)!=Path(config['python'])
            or args.expected_source!=config['source_sha'] or args.expected_epoch is None):
        raise ExecutionBlocked('canary worker does not match its sealed service identity')
    repo=Path(project['repo']);base=Path(config['operator_home'])/'.do_again/projects'/project['key'][:12]
    control,state=base/'sealed-control',base/'state'
    policy=INSTALL_ROOT/'current/package/do_again/worker_policy.json'
    validate_control_paths(control,state,os.getuid())
    def check():
        result=read_admission_status(repo)
        unresolved=result.get('unresolved_executions')
        inflight=result.get('inflight_request_ids',[])
        if (result.get('source_sha')!=config['source_sha'] or result.get('epoch')!=args.expected_epoch
                or result.get('production_ready') is not False or result.get('canary_authorized') is not True
                or result.get('operator_intent')!='active' or result.get('enforcement_verified') is not True
                or result.get('worktree')!=project['worktree'] or result.get('uid')!=project['uid']
                or result.get('goal_revision')!=project['goal_revision']
                or not isinstance(unresolved,list)
                or any(row.get('request_id') not in inflight for row in unresolved)):
            raise ExecutionBlocked('sealed canary admission is incomplete or revoked')
        return result
    broker_request(repo,{'operation':'canary_reconcile'})
    check()
    from ..browser.runtime import project_record,binding_identity
    record=project_record(repo)
    if record.get('chat_url')!=grant['chat_url'] or binding_identity(record)!=grant['binding_identity']:
        raise ExecutionBlocked('canary conversation binding changed before worker startup')
    from ..service.daemon import _queue_continuation
    from .live_canary import objective
    from ..core.schema import atomic_json,read_json,utc_now
    offered=state/'canary-objective.json'
    if not offered.exists():
        atomic_json(offered,{'source_sha':config['source_sha'],'prompt':objective(config)+
                   '\nCurrent UTC for your first request: '+utc_now().isoformat()})
    initial=read_json(offered)
    if initial.get('source_sha')!=config['source_sha']:raise ExecutionBlocked('canary objective source changed')
    _queue_continuation(state,initial['prompt'],purpose='idle_continuation',
                        marker='DO_AGAIN_LIVE_CANARY_'+grant['nonce'],binding=record)
    broker_request(repo,{'operation':'worker_register','pid':os.getpid(),'epoch':args.expected_epoch,
                         'source_sha':config['source_sha']})
    def browser_effect():
        current=check()
        if current.get('inflight_request_ids'):return {'delivered':0,'liveness':'running'}
        return broker_request(repo,{'operation':'browser_tick','request_id':'browser-'+uuid.uuid4().hex,
                                   'epoch':args.expected_epoch})
    worker_args=Namespace(control_worktree=str(control),branch=project['control_branch'],remote='origin',
                          policy=str(policy),once=args.once)
    return _run_admitted_worker(worker_args,repo,state,True,admission_check=check,
        control_transport=BrokerControlHistory(repo,control,args.expected_epoch),
        executor=BrokerExecutor(repo=repo,policy_path=policy,state_dir=state),browser_effect=browser_effect)
