"""Dedicated sealed Codex canary worker: non-browser two-task native loop.

The root broker decides all executable, GitHub, CI and checkpoint effects.
The operator worker can submit only the first/second original model proposal
and four fixed stage selectors per task. It never uses Chrome/CDP, general
shell, raw GitHub writes, or a production-enabled parent project.
"""
from __future__ import annotations

import os
import re
import sys
import time
from argparse import Namespace
from pathlib import Path

from ..model_attempt import (
    observe_original_codex_attempt,observe_second_codex_attempt,
    run_first_codex_canary_proposal,run_second_codex_canary_proposal,
)
from .macos_execution import INSTALL_ROOT,ExecutionBlocked
from .model_native import codex_project

_WAIT_PREFIXES=(
    "Codex first CI has not completed successfully",
    "task-two CI is waiting",
)


def _admission(config, args, project, *, rpc):
    state=rpc(Path(project["repo"]),{"operation":"status"})
    if (not isinstance(state,dict)
            or state.get("source_sha")!=config["source_sha"]
            or state.get("epoch")!=args.expected_epoch
            or state.get("production_ready") is not False
            or state.get("canary_authorized") is not True
            or state.get("operator_intent")!="active"
            or state.get("enforcement_verified") is not True
            or state.get("enforcement_blocker") is not None
            or state.get("worktree")!=project["worktree"]
            or state.get("uid")!=project["uid"]
            or state.get("goal_revision")!=project["goal_revision"]
            or state.get("unresolved_executions")!=[]
            or state.get("inflight_request_ids")!=[]):
        raise ExecutionBlocked("Codex worker native grant or exact project identity changed")
    return state


def _receipt_driver(nonce, task, repo, epoch, *, rpc, status:dict):
    """One immediate, idempotent typed follow-up per fully published receipt."""
    prefix="canary-"+nonce+"-"+str(task)+"-"
    transitions={"edit":"test","test":"commit","commit":"publish","publish":"ci"}
    def on_receipt(receipt):
        rid=receipt.get("request_id") if isinstance(receipt,dict) else None
        if not isinstance(rid,str) or not rid.startswith(prefix):
            status["failure"]="unexpected synthetic receipt identity"
            return True
        stage=rid[len(prefix):]
        if stage not in {*transitions,"ci"}:
            status["failure"]="synthetic stage receipt is outside the two-task plan"
            return True
        if receipt.get("state")!="succeeded":
            status["failure"]="synthetic "+str(task)+"-"+stage+" stage did not succeed"
            return True
        if stage=="ci":
            status["ci_receipt"]=True
            return True
        next_stage=transitions[stage]
        try:
            result=rpc(repo,{
                "operation":"codex_followup_publish",
                "request_id":"codex-publish-"+nonce+"-"+str(task)+"-"+next_stage,
                "epoch":epoch,
            })
            if not isinstance(result,dict) or result.get("state")!="succeeded":
                raise ExecutionBlocked("root-owned follow-up publication is not terminal")
        except Exception:
            # A lost GitHub response may already have committed. Stop, never
            # create a fresh stage identity or replay an uncertain mutation.
            status["failure"]="root follow-up publication uncertain; reconcile the original ledger"
            return True
        return False
    return on_receipt


def _poll_terminal_ci(repo,task,scope,*,rpc,check,sleep=time.sleep,clock=time.time):
    """Poll only root-proven CI. Never create repeated CI execution requests."""
    operation="codex_ci_checkpoint" if task==1 else "codex_canary_complete"
    while clock()<scope.expires_at_utc.timestamp():
        check()
        try:
            result=rpc(repo,{"operation":operation})
        except Exception as exc:
            message=str(exc)
            if not any(fragment in message for fragment in _WAIT_PREFIXES):
                raise ExecutionBlocked("protected CI qualification is not a safe waiting state") from None
            sleep(20)
            continue
        if not isinstance(result,dict):
            raise ExecutionBlocked("root CI checkpoint result is not a bounded record")
        if task==1:
            if result.get("state")!="terminal" or result.get("conclusion")!="success":
                raise ExecutionBlocked("first CI checkpoint is not terminal success")
        else:
            if (result.get("kind")!="codex_two_task_native_acceptance"
                    or result.get("production_ready") is not False
                    or result.get("first_result")!="terminal_success"
                    or result.get("second_result")!="terminal_success"):
                raise ExecutionBlocked("two-task completion has no root-sealed positive evidence")
        return result
    raise ExecutionBlocked("Codex canary expired while awaiting exact passing CI; no retry")


def _candidate(config,task,*,rpc):
    if task==1:
        observation=observe_original_codex_attempt(config)
        if observation["state"]=="not_reserved":
            return run_first_codex_canary_proposal(config,allow_model_call=True)["request"]
    else:
        observation=observe_second_codex_attempt(config,rpc=rpc)
        if observation["state"]=="not_reserved":
            return run_second_codex_canary_proposal(
                config,allow_model_call=True,rpc=rpc)["request"]
    if observation["state"]!="candidate_ready":
        raise ExecutionBlocked("previous model call is uncertain and must not be replayed")
    return observation["request"]


def main(config,args):
    from .macos_client import broker_request
    from .macos_server import verify_installation
    from .immutable_worker import validate_control_paths
    from ..core.broker_executor import BrokerExecutor
    from ..core.control_transport import BrokerControlHistory
    from ..service.daemon import _run_admitted_worker
    verify_installation(config)
    scope,_,project=codex_project(config)
    installed=INSTALL_ROOT/"current/package/do_again/supervisor/model_canary_worker.py"
    if (sys.platform!="darwin" or args.project!="do-again"
            or args.canary or not args.codex_canary
            or Path(__file__)!=installed
            or Path(sys.executable)!=Path(config["python"])
            or args.expected_source!=config["source_sha"]
            or type(args.expected_epoch) is not int
            or os.getuid()!=config["operator_uid"]
            or os.geteuid()!=os.getuid()):
        raise ExecutionBlocked("Codex worker is not the sealed isolated non-browser service")
    repo=Path(project["repo"])
    base=Path(config["operator_home"])/".do_again/projects"/project["key"][:12]
    control,state=base/"sealed-control",base/"state"
    validate_control_paths(control,state,os.getuid())
    policy=INSTALL_ROOT/"current/package/do_again/worker_policy.json"
    def check():
        return _admission(config,args,project,rpc=broker_request)
    check()
    broker_request(repo,{"operation":"worker_register","pid":os.getpid(),
                         "epoch":args.expected_epoch,"source_sha":config["source_sha"]})
    for task in (1,2):
        check()
        proposal=_candidate(config,task,rpc=broker_request)
        published=broker_request(repo,{
            "operation":"codex_request_publish",
            "request_id":"codex-publish-"+scope.nonce+"-"+str(task)+"-edit",
            "epoch":args.expected_epoch,"value":proposal,
        })
        if not isinstance(published,dict) or published.get("state")!="succeeded":
            raise ExecutionBlocked("model proposal was not durably published by root broker")
        status={"failure":None,"ci_receipt":False}
        worker_args=Namespace(control_worktree=str(control),
            branch=scope.control_branch,remote="origin",
            policy=str(policy),once=False)
        result=_run_admitted_worker(
            worker_args,repo,state,False,
            admission_check=check,
            control_transport=BrokerControlHistory(
                repo,control,args.expected_epoch),
            executor=BrokerExecutor(repo=repo,policy_path=policy,state_dir=state),
            receipt_observer=_receipt_driver(
                scope.nonce,task,repo,args.expected_epoch,
                rpc=broker_request,status=status),
        )
        if status["failure"] is not None:
            raise ExecutionBlocked(status["failure"])
        if result!=0 or not status["ci_receipt"]:
            raise ExecutionBlocked("synthetic worker ended without a terminal CI-stage receipt")
        _poll_terminal_ci(repo,task,scope,rpc=broker_request,check=check)
    return 0
