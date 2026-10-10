"""Root-only continuation publisher for the fixed first Codex canary task.

Never accepts model-authored subsequent GitHub requests. Every follow-up
request is reconstructed from the preceding *original* request, its exact
remote receipt, and a matching terminal native broker ledger result.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from ..core.schema import canonical_json,request_fingerprint
from ..model_stages import (
    prepare_model_canary_test,prepare_model_canary_commit,
    prepare_model_canary_publish,prepare_model_canary_ci,
)
from .control_history import api_for,blob_sha,read_json_blob,snapshot
from .macos_execution import ExecutionBlocked
from .model_stage_gate import _terminal

_PRECEDING={"test":"edit","commit":"test","publish":"commit","ci":"publish"}
_BUILDERS={
    "test":prepare_model_canary_test,
    "commit":prepare_model_canary_commit,
    "publish":prepare_model_canary_publish,
    "ci":prepare_model_canary_ci,
}
_INPUTS={
    "test":("edit_request","edit_receipt"),
    "commit":("test_request","test_receipt"),
    "publish":("commit_request","commit_receipt"),
    "ci":("publish_request","publish_receipt"),
}


def _derive(broker,scope,api,entries,stage,task=1):
    if type(task) is not int or task not in (1,2):
        raise ExecutionBlocked("Codex continuation task is outside the two-task plan")
    if task==2:
        broker.codex.second_ready()
    previous=_PRECEDING[stage]
    rid=scope.request_prefix+str(task)+"-"+previous
    req_path="automation/do_again/requests/"+rid+".json"
    receipt_path="automation/do_again/receipts/"+rid+".json"
    req_sha,receipt_sha=entries.get(req_path),entries.get(receipt_path)
    if not isinstance(req_sha,str) or not isinstance(receipt_sha,str):
        raise ExecutionBlocked("Codex follow-up requires original request and durable broker receipt")
    original=read_json_blob(api,req_sha)
    if (blob_sha(canonical_json(original)+b"\n")!=req_sha
            or original.get("request_id")!=rid):
        raise ExecutionBlocked("Codex preceding request blob does not match its canonical identity")
    receipt=read_json_blob(api,receipt_sha)
    native=_terminal(broker,scope,task,previous)
    fingerprint=request_fingerprint(original)
    intent=broker.ledger.intent(broker.project.key,rid)
    if intent.get("request_fingerprint")!=fingerprint:
        raise ExecutionBlocked("Codex native predecessor fingerprint differs from remote original")
    envelope=receipt.get("result")
    if (receipt.get("request_id")!=rid or receipt.get("request_fingerprint")!=fingerprint
            or receipt.get("state")!="succeeded"
            or not isinstance(envelope,dict)
            or envelope.get("request_fingerprint")!=fingerprint
            or not isinstance(envelope.get("result"),dict)):
        raise ExecutionBlocked("Codex remote receipt lacks matching native result identity")
    remote_result=envelope["result"]
    if remote_result.get("returncode")!=native.get("returncode"):
        raise ExecutionBlocked("Codex remote receipt outcome differs from root ledger")
    if previous=="commit" and remote_result.get("authority")!=native.get("authority"):
        raise ExecutionBlocked("Codex commit receipt does not match root-published head")
    if previous=="publish" and any(remote_result.get(k)!=native.get(k)
                                   for k in ("head","pull_request","repository")):
        raise ExecutionBlocked("Codex publication receipt does not match native GitHub evidence")
    expected_head=original.get("expected",{}).get("repo_head")
    if not isinstance(expected_head,str) or not re.fullmatch(r"[0-9a-f]{40}",expected_head):
        raise ExecutionBlocked("Codex preceding request lost the exact Git HEAD")
    inputs=dict(zip(_INPUTS[stage],(original,receipt)))
    try:
        generated=_BUILDERS[stage](
            **inputs,nonce=scope.nonce,task=task,expected_head=expected_head)
    except Exception:
        raise ExecutionBlocked("Codex follow-up cannot be admitted from preceding receipt") from None
    if generated.get("request_id")!=scope.request_prefix+str(task)+"-"+stage:
        raise ExecutionBlocked("Codex follow-up builder generated a different identity")
    return generated


def publish_codex_followup(broker,packet:dict,*,api=None)->dict:
    if sys.platform!="darwin" or os.geteuid()!=0:
        raise ExecutionBlocked("Codex continuation publication requires sealed macOS root broker")
    from .macos_server import verify_installation
    from .model_native import CodexCanaryAuthority
    authority=getattr(broker,"codex",None)
    if not isinstance(authority,CodexCanaryAuthority):
        raise ExecutionBlocked("Codex follow-up has no root-constructed canary authority")
    scope=authority.scope
    if (not isinstance(packet,dict) or set(packet)!={"operation","request_id","epoch"}
            or packet.get("operation")!="codex_followup_publish"
            or type(packet.get("epoch")) is not int):
        raise ExecutionBlocked("Codex follow-up refuses model-selected payloads and refs")
    request_id=packet["request_id"]
    if not isinstance(request_id,str):
        raise ExecutionBlocked("Codex follow-up identity is missing")
    match=re.fullmatch("codex-publish-"+scope.nonce+r"-([12])-(test|commit|publish|ci)",request_id)
    if match is None:
        raise ExecutionBlocked("Codex follow-up stage is outside the bounded two-task plan")
    task=int(match[1])
    stage=match[2]
    if task==2:
        authority.second_ready()
    digest=hashlib.sha256(canonical_json(packet)).hexdigest()
    with broker.lock:
        with broker.admission():
            authority.check()
            if packet["epoch"]!=broker.registry.status(broker.project.repo)["epoch"]:
                raise ExecutionBlocked("Codex follow-up worker epoch changed")
            verify_installation(broker.config)
            api=api or api_for(broker)
            if (getattr(api,"repository",None)!="Tran-Steven/do-again"
                    or getattr(api,"control_branch",None)!=scope.control_branch):
                raise ExecutionBlocked("Codex follow-up repository and branch binding differ")
            cached=broker.ledger.lookup(broker.project.key,request_id,digest)
            if cached is not None:return cached
            base,commit,entries=snapshot(api)
            proposal=_derive(broker,scope,api,entries,stage,task=task)
            rid=proposal["request_id"]
            path="automation/do_again/requests/"+rid+".json"
            if path in entries:
                raise ExecutionBlocked("Codex follow-up original request already exists")
            data=canonical_json(proposal)+b"\n"
            if len(data)>65536:
                raise ExecutionBlocked("Codex deterministic continuation exceeds request limit")
            sha=blob_sha(data)
            stamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            message="Do Again sealed Codex canary request "+rid
            broker.ledger.reserve(broker.project.key,request_id,digest,
                intent={"operation":"codex_followup_publish",
                        "source_sha":scope.source_sha,"base":base,
                        "path":path,"blob":sha,"message":message,
                        "control_branch":scope.control_branch,
                        "nonce":scope.nonce,"stage":stage,
                        "epoch":packet["epoch"]})
        @contextmanager
        def effect():
            with broker.admission():
                authority.check()
                if packet["epoch"]!=broker.registry.status(broker.project.repo)["epoch"]:
                    raise ExecutionBlocked("Codex follow-up authority epoch changed")
                yield
        with effect():
            blob=api.request("POST","git/blobs",{
                "encoding":"base64","content":base64.b64encode(data).decode("ascii")})
            if blob.get("sha")!=sha:
                raise ExecutionBlocked("Codex follow-up blob identity differs")
        with effect():
            tree=api.request("POST","git/trees",{
                "base_tree":commit["tree"]["sha"],
                "tree":[{"path":path,"mode":"100644","type":"blob","sha":sha}]})
            new_tree=tree.get("sha") if isinstance(tree,dict) else None
            if not isinstance(new_tree,str) or not re.fullmatch(r"[0-9a-f]{40}",new_tree):
                raise ExecutionBlocked("Codex follow-up tree identity is invalid")
        with effect():
            author={"name":"Do Again","email":"do-again@users.noreply.github.com","date":stamp}
            result=api.request("POST","git/commits",{
                "message":message,"tree":new_tree,"parents":[base],
                "author":author,"committer":author})
            head=result.get("sha") if isinstance(result,dict) else None
            if not isinstance(head,str) or not re.fullmatch(r"[0-9a-f]{40}",head):
                raise ExecutionBlocked("Codex follow-up commit identity is invalid")
        with effect():
            observed=api.request("GET","git/ref/heads/"+scope.control_branch)
            if not isinstance(observed,dict) or observed.get("object",{}).get("sha")!=base:
                raise ExecutionBlocked("Codex follow-up branch moved; do not replay")
            api.request("PATCH","git/refs/heads/"+scope.control_branch,
                        {"sha":head,"force":False})
        observed_head,observed_commit,observed_entries=snapshot(api)
        if (observed_head!=head or observed_commit.get("message")!=message
                or [p["sha"] for p in observed_commit.get("parents",[])]!=[base]
                or observed_entries.get(path)!=sha):
            raise ExecutionBlocked("Codex follow-up publication readback is uncertain")
        success={"state":"succeeded","returncode":0,"head":head,
                 "request_id":rid,"path":path,"blob":sha,
                 "executed":False,"acknowledged":False}
        broker.ledger.finish(broker.project.key,request_id,success)
        broker.control_cache=None
        return success
