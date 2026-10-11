"""Root-only two-task Codex canary completion; never enables production."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

from ..core.schema import atomic_json,canonical_json
from .macos_execution import ExecutionBlocked
from .model_checkpoint import observe_checkpoint

SHA40=re.compile(r"[0-9a-f]{40}\Z")


def finalize_codex_canary(broker, packet:dict)->dict:
    """Accept only exact native task-two terminal CI on the first draft PR."""
    if sys.platform!="darwin" or os.geteuid()!=0:
        raise ExecutionBlocked("Codex completion needs the protected macOS root broker")
    if packet!={"operation":"codex_canary_complete"}:
        raise ExecutionBlocked("Codex completion accepts no worker-supplied success or head")
    authority=getattr(broker,"codex",None)
    if authority is None:
        raise ExecutionBlocked("no root-installed Codex canary is present")
    scope=authority.scope
    authority.check()
    from .macos_server import verify_installation
    from .ci_observation import observe_ci
    from .model_stage_gate import _terminal
    verify_installation(broker.config)
    first=observe_checkpoint(broker,scope)
    if first.get("state")!="terminal" or first.get("conclusion")!="success":
        raise ExecutionBlocked("second CI cannot complete without the sealed first CI checkpoint")
    output=broker.state/"codex-two-task-complete.json"
    if output.exists() or output.is_symlink():
        raise ExecutionBlocked("Codex completion certificate already exists; no duplicate completion")
    published=_terminal(broker,scope,2,"publish")
    if (published.get("state")!="succeeded"
            or published.get("repository")!="Tran-Steven/do-again"
            or published.get("head")==first["head_sha"]
            or not isinstance(published.get("head"),str)
            or not SHA40.fullmatch(published["head"])
            or published.get("pull_request")!=first["pull_request"]):
        raise ExecutionBlocked("second publication did not update the original draft PR")
    observed=observe_ci(broker,{
        "operation":"ci_observe",
        "request_id":scope.request_prefix+"2-publish"})
    if (not isinstance(observed,dict) or observed.get("state")!="terminal"
            or observed.get("status")!="completed" or observed.get("conclusion")!="success"
            or observed.get("returncode")!=0 or observed.get("replay") is not False
            or observed.get("repository")!="Tran-Steven/do-again"
            or observed.get("head_sha")!=published["head"]
            or observed.get("pull_request")!=first["pull_request"]
            or type(observed.get("run_id")) is not int or observed["run_id"]<1
            or type(observed.get("run_attempt")) is not int or observed["run_attempt"]<1):
        raise ExecutionBlocked("task-two CI is waiting, failed, or bound to a different PR/head")
    certificate={
        "schema_version":1,"kind":"codex_two_task_native_acceptance",
        "nonce":scope.nonce,"source_sha":scope.source_sha,
        "parent_epoch":scope.parent_epoch,"production_ready":False,
        "model_acknowledged":False,"browser_acknowledged":False,
        "first_ci_sha256":first["ci_sha256"],
        "first_head":first["head_sha"],"second_head":observed["head_sha"],
        "repository":"Tran-Steven/do-again","pull_request":first["pull_request"],
        "second_run_id":observed["run_id"],"second_run_attempt":observed["run_attempt"],
        "first_result":"terminal_success","second_result":"terminal_success",
        "replay":False,
    }
    certificate["certificate_sha256"]=hashlib.sha256(canonical_json(certificate)).hexdigest()
    with broker.lock,broker.admission():
        authority.check()
        if output.exists() or output.is_symlink():
            raise ExecutionBlocked("Codex canary completion raced another operator gesture")
        # A task-two checkpoint never grants or resumes parent production.
        atomic_json(output,certificate)
        os.chmod(output,0o600)
        current=broker.registry.status(broker.project.repo)
        broker.registry.set_intent(
            broker.project.repo,"maintenance",goal_revision=current["goal_revision"])
    return certificate
