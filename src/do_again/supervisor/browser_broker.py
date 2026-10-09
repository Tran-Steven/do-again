"""Fixed immutable browser helper; pause and effect initiation share one fence."""
from __future__ import annotations
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from ..core.schema import canonical_json
from .macos_execution import INSTALL_ROOT,ExecutionBlocked,REQUEST_ID
from .control_history import gate


def browser_tick(broker,packet):
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('browser effects require the immutable macOS broker')
    if set(packet)!={'operation','request_id','epoch'} or packet['operation']!='browser_tick':
        raise ExecutionBlocked('browser effect cannot select a target, profile, command or project')
    if not isinstance(packet['request_id'],str) or not REQUEST_ID.fullmatch(packet['request_id']):
        raise ExecutionBlocked('invalid browser effect identity')
    from .macos_server import verify_installation
    from .authority import project_identity
    project=next(p for p in broker.config['projects'] if p['key']==broker.project.key)
    repo=Path(project['repo']);base=Path(broker.config['operator_home'])/'.do_again/projects'/project_identity(repo)[:12]
    fingerprint=hashlib.sha256(canonical_json(packet)).hexdigest()
    with broker.lock:
        verify_installation(broker.config)
        with broker.admission():
            gate(broker,packet['epoch'])
            recovered=broker.ledger.lookup(broker.project.key,packet['request_id'],fingerprint)
            if recovered is not None:return recovered
            from .worker_service import verified_worker_pid
            worker_pid=verified_worker_pid(broker,packet['epoch'])
            ci=ci_evidence(broker,base/'sealed-control')
            broker.ledger.reserve(broker.project.key,packet['request_id'],fingerprint,
                intent={'operation':'browser_tick','source_sha':broker.config['source_sha'],'repo':str(repo)})
            # Fixed trusted code, operator identity, clean environment. Never use
            # dedicated-process draining for the operator UID or kill user Chrome.
            code=('import sys;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
                  'from do_again.supervisor.browser_runner import main;main()')
            proc=subprocess.Popen([broker.config['python'],'-I','-S','-B','-c',code,str(repo),str(base/'state'),str(base/'sealed-control'),json.dumps(ci),str(worker_pid)],
                user=broker.config['operator_uid'],group=broker.config['operator_gid'],extra_groups=[],
                cwd=str(INSTALL_ROOT/'current'),env={'PATH':'/usr/bin:/bin','HOME':broker.config['operator_home']},
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)
            # Hold the effect fence through the trusted helper's synchronous
            # browser work. A queued pause cannot overtake a later click.
            try:
                stdout,_=proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=10)
                raise ExecutionBlocked('browser helper timed out; effect remains uncertain')
            if proc.returncode or len(stdout)>65536:
                raise ExecutionBlocked('browser effect lacks terminal evidence; reconciliation required')
            try:result=json.loads(stdout)
            except ValueError:raise ExecutionBlocked('browser helper evidence is invalid') from None
            if result.get('state')!='completed':
                raise ExecutionBlocked('browser effect remains uncertain; original outbox is preserved')
            broker.ledger.finish(broker.project.key,packet['request_id'],result)
            return result


def ci_evidence(broker,control):
    """Exact repository/run/head, including current engineering authority."""
    from ..service.liveness import _latest_goal
    from .control_history import api_for
    from .macos_server import worktree_authority
    goal=_latest_goal(control)
    ci=goal.get('ci') if goal.get('goal_state')=='waiting_for_ci' else None
    if not isinstance(ci,dict):return {'state':'invalid','error':'no durable CI wait'}
    configured=next(p for p in broker.config['projects'] if p['key']==broker.project.key)
    if (ci.get('repository')!=configured.get('github_repository') or type(ci.get('run_id')) is not int
            or ci['run_id']<=0 or not isinstance(ci.get('head_sha'),str)
            or ci['head_sha']!=worktree_authority(broker.project.worktree)['repo_head']):
        return {'state':'mismatch','error':'CI scope or current head differs'}
    result=api_for(broker).request('GET','actions/runs/'+str(ci['run_id']))
    if not result or result.get('id')!=ci['run_id'] or result.get('head_sha')!=ci['head_sha']:
        return {'state':'mismatch','error':'CI run identity differs'}
    return {'state':'terminal' if result.get('status')=='completed' else 'waiting',
            'status':result.get('status'),'conclusion':result.get('conclusion'),'head_sha':result.get('head_sha')}
