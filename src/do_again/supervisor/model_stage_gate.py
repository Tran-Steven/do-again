"""Read-only root admission for the first Codex synthetic edit-to-CI chain.

Stage request identity comes from an exact blob on the sealed control branch.
A worker-controlled packet cannot name a different repo, select its executable,
skip a preceding native terminal result, or replay started executions.
Only the existing fenced broker launches or publishes effects afterward.
"""
from __future__ import annotations

import re

from ..core.schema import canonical_json, request_fingerprint, validate_request
from .control_history import api_for, blob_sha, read_json_blob, snapshot
from .macos_execution import ExecutionBlocked

STAGES=("edit","test","commit","publish","ci")
OPERATIONS={"edit":"scratch_script","test":"run_tests",
            "commit":"git_commit","publish":"git_publish","ci":"ci_observe"}
PREVIOUS={"test":"edit","commit":"test","publish":"commit","ci":"publish"}


def read_sealed_request(broker, scope, task: int, stage: str) -> dict:
    if task not in (1,2) or stage not in STAGES:
        raise ExecutionBlocked("Codex root stage exceeds the bounded two-task plan")
    if task==2:
        authority=getattr(broker,"codex",None)
        if authority is None:
            raise ExecutionBlocked("second task has no root checkpoint authority")
        authority.second_ready()
    api=api_for(broker)
    if (getattr(api,"repository",None)!="Tran-Steven/do-again"
            or getattr(api,"control_branch",None)!=scope.control_branch):
        raise ExecutionBlocked("Codex stage read differs from the sealed control branch")
    _,_,entries=snapshot(api)
    rid=scope.request_prefix+str(task)+"-"+stage
    path="automation/do_again/requests/"+rid+".json"
    sha=entries.get(path)
    if not isinstance(sha,str):
        raise ExecutionBlocked("Codex stage lacks the original remote request blob")
    value=read_json_blob(api,sha)
    if blob_sha(canonical_json(value)+b"\n")!=sha:
        raise ExecutionBlocked("Codex stage request is not canonical immutable JSON")
    if (value.get("request_id")!=rid
            or value.get("operation")!=OPERATIONS[stage]):
        raise ExecutionBlocked("Codex stage request identity differs from its blob")
    try:
        validated=validate_request(value,max_ttl_seconds=3600)
    except Exception:
        raise ExecutionBlocked("Codex stage request no longer has valid time and schema") from None
    if value!=validated:
        raise ExecutionBlocked("Codex stage request is not normalized")
    return value


def _terminal(broker,scope,task,stage):
    """Read a preceding confined effect from the original root ledger only."""
    rid=scope.request_prefix+str(task)+"-"+stage
    entry=broker.ledger.intent(broker.project.key,rid)
    expected={"edit":"execute","test":"execute",
              "commit":"git_commit","publish":"git_publish"}
    if (not isinstance(entry,dict) or entry.get("operation")!=expected[stage]
            or entry.get("source_sha")!=scope.source_sha
            or not isinstance(entry.get("request_fingerprint"),str)):
        raise ExecutionBlocked("Codex preceding stage lacks original root broker intent")
    result=broker.ledger.observe_request(
        broker.project.key,rid,entry["request_fingerprint"])
    if (result.get("state")!="terminal" or result.get("replay") is not False
            or result.get("request_fingerprint")!=entry["request_fingerprint"]
            or result.get("source_sha")!=scope.source_sha
            or not isinstance(result.get("result"),dict)):
        raise ExecutionBlocked("Codex preceding stage is not a proven native terminal effect")
    terminal=result["result"]
    if (terminal.get("source_sha")!=scope.source_sha
            or terminal.get("returncode")!=0
            or terminal.get("timed_out",False) is not False):
        raise ExecutionBlocked("Codex preceding confined stage did not succeed")
    if stage in ("commit","publish") and terminal.get("state")!="succeeded":
        raise ExecutionBlocked("Codex preceding native Git stage did not succeed")
    return terminal


def admit_codex_task_packet(broker, scope, packet):
    """Bind two tasks to their exact original GitHub requests and native outcomes."""
    if not isinstance(packet,dict):
        raise ExecutionBlocked("Codex worker packet must be a bounded object")
    operation=packet.get("operation")
    allowed={"execute","git_commit","git_publish","ci_observe"}
    if operation not in allowed:
        raise ExecutionBlocked("Codex stage effect has no bound native capability")
    rid=packet.get("request_id")
    if not isinstance(rid,str):
        raise ExecutionBlocked("Codex native stage has no exact request ID")
    if operation=="ci_observe":
        match=re.fullmatch(re.escape(scope.request_prefix)+r"([12])-publish",rid)
        if match is None or packet!={"operation":"ci_observe","request_id":rid}:
            raise ExecutionBlocked("Codex CI may only observe an exact native publication")
        task=int(match[1])
        stage="ci"
    else:
        match=re.fullmatch(re.escape(scope.request_prefix)+r"([12])-(edit|test|commit|publish)",rid)
        if match is None:
            raise ExecutionBlocked("Codex native request exceeds its two-task plan")
        task=int(match[1])
        stage=match[2]
    checkpoint=None
    if task==2:
        authority=getattr(broker,"codex",None)
        if authority is None:
            raise ExecutionBlocked("second task lacks root checkpoint authority")
        checkpoint=authority.second_ready()
    baseline=checkpoint["head_sha"] if checkpoint else scope.baseline
    request=read_sealed_request(broker,scope,task,stage)
    nonce=scope.nonce
    paths=["canary_live_"+nonce+".py","tests/test_live_canary_"+nonce+".py"]
    if stage=="edit":
        from ..model_canary import validate_prepared_edit
        try:
            validate_prepared_edit(request,nonce=nonce,task=task,expected_head=baseline)
        except Exception:
            raise ExecutionBlocked("Codex original edit is not the canonical model proposal") from None
        head=baseline
        if (set(request)!={"schema_version","request_id","operation","issued_at_utc",
                            "expires_at_utc","args","expected","limits"}
                or request["args"].get("language")!="python"
                or request["args"].get("cwd")!="."):
            raise ExecutionBlocked("Codex edit request expands the fixed writer capability")
    else:
        prior=PREVIOUS[stage]
        terminal=_terminal(broker,scope,task,prior)
        if stage=="publish":
            prior_head=terminal.get("authority",{}).get("repo_head")
            if not isinstance(prior_head,str) or len(prior_head)!=40 or prior_head==baseline:
                raise ExecutionBlocked("Codex publication lacks an exact committed new head")
            head=prior_head
        elif stage=="ci":
            head=terminal.get("head")
            if (not isinstance(head,str) or len(head)!=40
                    or type(terminal.get("pull_request")) is not int
                    or terminal["pull_request"]<1):
                raise ExecutionBlocked("Codex CI lacks exact published native PR evidence")
        else:
            head=baseline
        if (set(request)!={"schema_version","request_id","operation","issued_at_utc",
                            "expires_at_utc","args","expected","limits","continuation"}
                or request["continuation"].get("acknowledged_receipts")!=
                      [scope.request_prefix+str(task)+"-"+prior]
                or request["continuation"].get("goal_state")!="in_progress"):
            raise ExecutionBlocked("Codex stage lacks exact preceding receipt continuation")
    if (request.get("expected")!={"repo_head":head}
            or request.get("limits")!={"timeout_seconds":120}):
        raise ExecutionBlocked("Codex native stage Git head and timeout differ")
    if stage=="test":
        expected={"discover":True,"start_directory":"tests",
                  "pattern":"test_live_canary_"+nonce+".py","cwd":"."}
        if request.get("args")!=expected:
            raise ExecutionBlocked("Codex test pattern differs from fixed synthetic files")
    if stage=="commit":
        if request.get("args")!={"paths":paths,
                                "message":"Validate synthetic Codex canary task "+str(task)}:
            raise ExecutionBlocked("Codex Git stage exceeds exact two-file commit")
    if stage=="publish":
        if request.get("args")!={"title":"Do Again synthetic two-task Codex canary",
                                  "body":"Bounded, draft-only isolated validation. Production remains disabled."}:
            raise ExecutionBlocked("Codex publication PR metadata expands fixed scope")
    if stage=="ci":
        if request.get("args")!={"original_request_id":scope.request_prefix+str(task)+"-publish"}:
            raise ExecutionBlocked("Codex CI selects an unapproved original publication")
        return {"stage":stage,"request_id":request["request_id"],"expected_head":head}
    fingerprint=request_fingerprint(request)
    if packet.get("request_fingerprint")!=fingerprint:
        raise ExecutionBlocked("Codex native broker packet differs from the original request blob")
    if stage in ("edit","test"):
        executable=broker.config.get("python")
        expected_argv=(
            [executable,"-c",request["args"]["content"]]
            if stage=="edit"
            else [executable,"-m","unittest","discover","-s","tests","-p",
                  "test_live_canary_"+nonce+".py"]
        )
        expected_packet={
            "operation":"execute","request_id":request["request_id"],
            "argv":expected_argv,"cwd":".","timeout":120,
            "request_fingerprint":fingerprint,"expected_head":head,
            "expected_authority":{"repo_head":head},
        }
    elif stage=="commit":
        expected_packet={
            "operation":"git_commit","request_id":request["request_id"],
            "paths":paths,"message":request["args"]["message"],
            "expected_head":head,"expected_epoch":broker.registry.status(broker.project.repo)["epoch"],
            "request_fingerprint":fingerprint,
        }
    else:
        expected_packet={
            "operation":"git_publish","request_id":request["request_id"],
            "title":request["args"]["title"],"body":request["args"]["body"],
            "expected_head":head,"expected_epoch":broker.registry.status(broker.project.repo)["epoch"],
            "request_fingerprint":fingerprint,
        }
    if packet!=expected_packet:
        raise ExecutionBlocked("Codex native effect packet is not the canonical worker request")
    return {"stage":stage,"request_id":request["request_id"],"expected_head":head}



def admit_first_task_packet(broker, scope, packet):
    """Backward-compatible first-task-only check; never authorize task two."""
    rid=packet.get("request_id") if isinstance(packet,dict) else None
    if not isinstance(rid,str) or not rid.startswith(scope.request_prefix+"1-"):
        raise ExecutionBlocked("first-task entrypoint cannot admit task two")
    return admit_codex_task_packet(broker,scope,packet)
