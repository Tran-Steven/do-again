"""Sealed non-browser canary child identity for the macOS root broker.

Separate from the legacy chat-bound canary. A Codex broker cannot be created
with an old browser grant, and no child authority is active merely because
a root-owned config includes a Codex grant.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from ..model_grant import sealed_codex_canary, CodexCanaryScope, bound_model_request
from .authority import project_identity
from .macos_execution import EXECUTION_ROOT, ExecutionBlocked


def codex_project(config):
    scope=sealed_codex_canary(config)
    parent=next(p for p in config["projects"] if p["account"]=="_doagain_da")
    home=Path(config["operator_home"])
    repo=home/".do_again/codex-canary"/scope.nonce
    if any(part.is_symlink() for part in (repo,*repo.parents)):
        raise ExecutionBlocked("Codex canary worktree binding is aliased")
    key=project_identity(repo)
    project=dict(parent,repo=str(repo),key=key,
        worktree=str(EXECUTION_ROOT/key/"worktree"),
        source_sha=scope.baseline,goal_revision="codex-canary-"+scope.nonce,
        control_branch=scope.control_branch,bundle="snapshots/canary.bundle",
        github_repository="Tran-Steven/do-again")
    return scope,parent,project


class CodexCanaryAuthority:
    def __init__(self,broker,parent,scope:CodexCanaryScope):
        self.broker,self.parent,self.scope=broker,parent,scope

    def parent_check(self):
        state=self.parent.registry.status(self.parent.project.repo)
        if (self.parent.config.get("production_ready") is not False
                or state["intent"]!="maintenance" or state["epoch"]!=self.scope.parent_epoch
                or not self.parent._verified()
                or self.parent.ledger.pending(self.parent.project.key)
                or self.parent._inflight()):
            raise ExecutionBlocked("Codex parent maintenance authority changed")
        projects=self.parent.config["projects"]
        sibling=next(p for p in projects if p.get("account")=="_doagain_jp")
        # The second installed parent remains a maintenance-only dependency.
        # The durable parent-check cannot authorize work in jobpipe.
        sibling_repo=Path(sibling["repo"])
        sibling_state=self.parent.registry.status(sibling_repo)
        if (sibling_state["intent"]!="maintenance"
                or self.parent.ledger.pending(project_identity(sibling_repo))):
            raise ExecutionBlocked("Codex canary sibling project left quiescent maintenance")

    def check(self):
        self.parent_check()
        if time.time()>=self.scope.expires_at_utc.timestamp():
            raise ExecutionBlocked("Codex canary sealed expiry reached")
        path=self.broker.state/"codex-canary-activation.json"
        if not path.is_file() or path.is_symlink():
            raise ExecutionBlocked("Codex canary has no root activation record")
        try: activation=json.loads(path.read_text())
        except (ValueError,OSError):
            raise ExecutionBlocked("Codex canary activation evidence is unreadable") from None
        status=self.broker.registry.status(self.broker.project.repo)
        if (activation.get("nonce")!=self.scope.nonce
                or activation.get("source_sha")!=self.scope.source_sha
                or activation.get("epoch")!=status["epoch"]
                or status["intent"]!="active"
                or status["goal_revision"]!="codex-canary-"+self.scope.nonce
                or type(activation.get("started_at")) not in (int,float)
                or not activation["started_at"]<=time.time()<activation["started_at"]+7200
                or not self.broker._verified()):
            raise ExecutionBlocked("Codex child native activation is incomplete or revoked")

    def request(self,value,path):
        if not isinstance(path,str) or not path.startswith("automation/do_again/requests/"):
            raise ExecutionBlocked("Codex child request path is excluded")
        rid=path.rsplit("/",1)[-1].removesuffix(".json")
        for task in (1,2):
            for stage in ("edit","test","commit","publish","ci"):
                if rid==self.scope.request_prefix+str(task)+"-"+stage:
                    bound_model_request(self.scope,task=task,stage=stage,request=value)
                    if task==2:
                        raise ExecutionBlocked("second Codex task awaits a separately verified model checkpoint")
                    if stage=="edit":
                        from ..model_canary import validate_prepared_edit
                        validate_prepared_edit(value,nonce=self.scope.nonce,
                            task=task,expected_head=self.scope.baseline)
                    return
        raise ExecutionBlocked("Codex child request ID exceeds two-task plan")

    def packet(self,packet):
        operation=packet.get("operation")
        if operation in {"status","probe","execution_observe","control_sync",
                         "control_reconcile","git_publication_reconcile","worker_register"}:
            return
        if operation=="codex_request_publish":
            if packet.get("request_id")!="codex-publish-"+self.scope.nonce+"-1-edit":
                raise ExecutionBlocked("Codex publisher packet ID differs from sealed one-shot")
            self.request(packet.get("value"),"automation/do_again/requests/canary-"
                         +self.scope.nonce+"-1-edit.json")
            return
        if operation=="control_publish":
            path=packet.get("path","")
            if path=="automation/do_again/agent_status.json":return
            if path.startswith("automation/do_again/receipts/") or path.startswith("automation/do_again/claims/"):
                # Reuse the existing typed stage set, not an arbitrary worker
                # recipient. Terminal broker checks bind each receipt.
                rid=path.rsplit("/",1)[-1].removesuffix(".json")
                if rid not in {
                    self.scope.request_prefix+"1-"+stage
                    for stage in ("edit","test","commit","publish","ci")
                } or not path.endswith(".json"):
                    raise ExecutionBlocked("Codex child receipt is outside the fixed task-one stages")
                value=packet.get("value")
                if not isinstance(value,dict) or value.get("request_id")!=rid:
                    raise ExecutionBlocked("Codex child control record mismatches its original request")
                return
            raise ExecutionBlocked("Codex child cannot write requests through ordinary control_publish")
        if operation in {"execute","git_commit","git_publish","ci_observe"}:
            from .model_stage_gate import admit_first_task_packet
            admit_first_task_packet(self.broker,self.scope,packet)
            return
        # Any native operation outside the exact root-read original request
        # and successful preceding broker stages is blocked, not degraded.
        raise ExecutionBlocked("Codex child capability is not part of the sealed canary")


def create_codex_broker(config,parent):
    from .macos_server import ProjectBroker
    scope,_,project=codex_project(config)
    sibling=next(p for p in config["projects"] if p["account"]=="_doagain_jp")
    child_config=dict(config,projects=[project,sibling],dependency_artifacts={})
    child=ProjectBroker(child_config,project)
    child.codex=CodexCanaryAuthority(child,parent,scope)
    parent.codex_canary_child=child
    @contextmanager
    def admission():
        with parent.admission():
            child.codex.parent_check()
            with ProjectBroker.admission(child):
                yield
    child.admission=admission
    return child
