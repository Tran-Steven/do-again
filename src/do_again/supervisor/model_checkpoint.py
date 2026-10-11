"""Root-owned Codex task-one CI checkpoint; no model/browser acknowledgment.

A task-two grant is never derived from the worker's claims, a successful
read-only CI polling operation, or a synthetic assistant acknowledgment. Only
the native broker's verified original publication and terminal passing GitHub
CI can create this immutable checkpoint under the root-owned broker state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from ..core.schema import atomic_json, canonical_json
from .macos_execution import ExecutionBlocked

_SHA = re.compile(r"[0-9a-f]{40}\Z")


def checkpoint_filename(broker) -> Path:
    return broker.state / "codex-ci-task-one.json"


def validate_checkpoint(broker, scope, value: dict) -> dict:
    if not isinstance(value, dict):
        raise ExecutionBlocked("Codex first checkpoint is malformed")
    expected = {
        "schema_version": 1,
        "kind": "codex_native_ci_checkpoint",
        "nonce": scope.nonce,
        "source_sha": scope.source_sha,
        "baseline": scope.baseline,
        "parent_epoch": scope.parent_epoch,
        "task": 1,
        "repository": "Tran-Steven/do-again",
        "operation": "ci_observe",
        "state": "terminal",
        "status": "completed",
        "conclusion": "success",
        "returncode": 0,
        "replay": False,
        "model_acknowledged": False,
        "browser_acknowledged": False,
    }
    if any(value.get(k) != v or type(value.get(k)) is not type(v)
           for k, v in expected.items()):
        raise ExecutionBlocked("Codex checkpoint does not match its sealed source and native CI proof")
    if (set(value) != set(expected) | {
            "head_sha", "pull_request", "run_id", "run_attempt",
            "url", "publication_request_id", "ci_sha256"
        } or not isinstance(value.get("head_sha"),str)
            or not _SHA.fullmatch(value["head_sha"])
            or value["head_sha"] == scope.baseline
            or type(value.get("pull_request")) is not int or value["pull_request"] < 1
            or type(value.get("run_id")) is not int or value["run_id"] < 1
            or type(value.get("run_attempt")) is not int or value["run_attempt"] < 1
            or value.get("publication_request_id") != scope.request_prefix+"1-publish"
            or value.get("url") != "https://github.com/Tran-Steven/do-again/actions/runs/"+str(value["run_id"])):
        raise ExecutionBlocked("Codex checkpoint lacks exact published PR/run/head binding")
    base = {k:v for k,v in value.items() if k!="ci_sha256"}
    if (not isinstance(value.get("ci_sha256"),str)
            or value["ci_sha256"] != hashlib.sha256(canonical_json(base)).hexdigest()):
        raise ExecutionBlocked("Codex checkpoint evidence digest differs")
    return value


def observe_checkpoint(broker, scope) -> dict:
    """Read only a root-owned terminal checkpoint; never observe/poll again."""
    path = checkpoint_filename(broker)
    if path.is_symlink():
        raise ExecutionBlocked("Codex checkpoint path contains an alias")
    if not path.exists():
        return {"state": "not_verified", "task": 1, "replay": False}
    info=path.stat()
    if (not path.is_file() or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o077 or info.st_size > 4096):
        raise ExecutionBlocked("Codex checkpoint is not a private root-owned regular file")
    try:
        record=json.loads(path.read_text(encoding="utf-8"))
    except (ValueError,OSError,UnicodeError):
        raise ExecutionBlocked("Codex checkpoint cannot be parsed") from None
    return validate_checkpoint(broker,scope,record)


def certify_first_task_ci(broker, packet: dict) -> dict:
    """Explicit authenticated root RPC: one terminal CI observation, no effects."""
    if sys.platform!="darwin" or os.geteuid()!=0:
        raise ExecutionBlocked("Codex CI checkpoint requires the installed macOS root broker")
    from .macos_server import verify_installation
    from .ci_observation import observe_ci
    if packet!={"operation":"codex_ci_checkpoint"}:
        raise ExecutionBlocked("Codex CI checkpoint accepts no worker-provided CI success values")
    authority=getattr(broker,"codex",None)
    if authority is None:
        raise ExecutionBlocked("Codex root-installed canary scope is unavailable")
    scope=authority.scope
    authority.check()
    verify_installation(broker.config)
    existing=observe_checkpoint(broker,scope)
    if existing.get("state")=="terminal":
        return dict(existing,already_verified=True)
    native=broker.ledger.observe_request(
        broker.project.key,scope.request_prefix+"1-publish",
        _publication_fingerprint(broker,scope))
    if native.get("state")!="terminal" or native.get("replay") is not False:
        raise ExecutionBlocked("Codex first publication native effect is ambiguous")
    published=native.get("result")
    if (not isinstance(published,dict) or published.get("state")!="succeeded"
            or published.get("returncode")!=0
            or published.get("repository")!="Tran-Steven/do-again"
            or not isinstance(published.get("head"),str) or not _SHA.fullmatch(published["head"])
            or published["head"]==scope.baseline
            or type(published.get("pull_request")) is not int):
        raise ExecutionBlocked("Codex first publication did not finish with exact draft evidence")
    result=observe_ci(broker,{"operation":"ci_observe",
                              "request_id":scope.request_prefix+"1-publish"})
    if (not isinstance(result,dict) or result.get("state")!="terminal"
            or result.get("status")!="completed" or result.get("conclusion")!="success"
            or result.get("returncode")!=0 or result.get("replay") is not False
            or result.get("repository")!="Tran-Steven/do-again"
            or result.get("head_sha")!=published["head"]
            or result.get("pull_request")!=published["pull_request"]):
        raise ExecutionBlocked("Codex first CI has not completed successfully at the published head")
    proof={
        "schema_version":1,"kind":"codex_native_ci_checkpoint",
        "nonce":scope.nonce,"source_sha":scope.source_sha,
        "baseline":scope.baseline,"parent_epoch":scope.parent_epoch,
        "task":1,"repository":"Tran-Steven/do-again",
        "operation":"ci_observe",
        "state":result["state"],"status":result["status"],
        "conclusion":result["conclusion"],"returncode":result["returncode"],
        "replay":False,"head_sha":result["head_sha"],
        "pull_request":result["pull_request"],
        "run_id":result["run_id"],"run_attempt":result["run_attempt"],
        "url":result["url"],"publication_request_id":scope.request_prefix+"1-publish",
        "model_acknowledged":False,"browser_acknowledged":False,
    }
    proof["ci_sha256"]=hashlib.sha256(canonical_json(proof)).hexdigest()
    validate_checkpoint(broker,scope,proof)
    # An authenticated operator may request the observation, but may not
    # provide any content for this root-constructed checkpoint.
    with broker.lock,broker.admission():
        authority.check()
        previous=observe_checkpoint(broker,scope)
        if previous.get("state")=="terminal":
            if previous!=proof:
                raise ExecutionBlocked("Codex original checkpoint is already sealed with different CI evidence")
            return dict(previous,already_verified=True)
        atomic_json(checkpoint_filename(broker),proof)
        os.chmod(checkpoint_filename(broker),0o600)
    return proof


def _publication_fingerprint(broker,scope):
    rid=scope.request_prefix+"1-publish"
    intent=broker.ledger.intent(broker.project.key,rid)
    if (not isinstance(intent,dict) or intent.get("operation")!="git_publish"
            or intent.get("source_sha")!=scope.source_sha
            or not isinstance(intent.get("request_fingerprint"),str)
            or not re.fullmatch(r"[0-9a-f]{64}",intent["request_fingerprint"])):
        raise ExecutionBlocked("Codex native first publication intent is missing")
    return intent["request_fingerprint"]
