"""Root broker only: publish one canonical Codex canary edit request.

The existing worker's control_publish intentionally denies requests/. This
separate capability is disabled by default and usable only from an installed,
root-created Codex child broker with an active sealed canary grant.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from ..core.schema import canonical_json
from ..model_canary import validate_prepared_edit
from .control_history import api_for, blob_sha, snapshot
from .macos_execution import ExecutionBlocked


def publish_codex_edit(broker, packet: dict, *, api=None) -> dict:
    if sys.platform != "darwin" or os.geteuid() != 0:
        raise ExecutionBlocked("Codex GitHub request publication requires the immutable macOS root broker")
    authority = getattr(broker, "codex", None)
    if authority is None:
        raise ExecutionBlocked("no separately sealed Codex authority exists")
    if (not isinstance(packet, dict)
            or set(packet) != {"operation", "request_id", "epoch", "value"}
            or packet.get("operation") != "codex_request_publish"
            or type(packet.get("epoch")) is not int
            or not isinstance(packet.get("value"), dict)):
        raise ExecutionBlocked("Codex request publisher accepts only a fixed exact-stage value")
    from .macos_server import verify_installation
    from .model_native import CodexCanaryAuthority
    if not isinstance(authority, CodexCanaryAuthority):
        raise ExecutionBlocked("Codex publisher requires a root-constructed sealed authority")
    scope = authority.scope
    original = packet["value"]
    rid = "canary-" + scope.nonce + "-1-edit"
    if packet["request_id"] != "codex-publish-" + scope.nonce + "-1-edit":
        raise ExecutionBlocked("Codex publisher has no matching one-shot identity")
    try:
        validate_prepared_edit(original, nonce=scope.nonce, task=1,
                               expected_head=scope.baseline)
    except Exception:
        raise ExecutionBlocked("Codex candidate is not the canonical bounded synthetic edit") from None
    path = "automation/do_again/requests/" + rid + ".json"
    data = canonical_json(original) + b"\n"
    if len(data) > 65536:
        raise ExecutionBlocked("Codex publication exceeded the two-file synthetic budget")
    digest = hashlib.sha256(canonical_json(packet)).hexdigest()
    sha = blob_sha(data)
    with broker.lock:
        with broker.admission():
            authority.check()
            if packet["epoch"] != broker.registry.status(broker.project.repo)["epoch"]:
                raise ExecutionBlocked("Codex publication epoch is stale")
            verify_installation(broker.config)
            api = api or api_for(broker)
            if (api.repository != "Tran-Steven/do-again"
                    or api.control_branch != scope.control_branch):
                raise ExecutionBlocked("Codex publisher repository/control branch differs")
            existing = broker.ledger.lookup(broker.project.key, packet["request_id"], digest)
            if existing is not None:
                return existing
            before, commit, entries = snapshot(api)
            if path in entries:
                raise ExecutionBlocked("Codex original request ID already exists remotely")
            if any(
                p.startswith("automation/do_again/requests/canary-" + scope.nonce + "-")
                for p in entries
            ):
                raise ExecutionBlocked("Codex request history already contains this canary; reconcile")
            stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            message = "Do Again sealed Codex canary request " + rid
            broker.ledger.reserve(broker.project.key, packet["request_id"], digest,
                intent={"operation": "codex_request_publish",
                        "source_sha":broker.config["source_sha"],
                        "base":before,"path":path,"blob":sha,
                        "message":message,"control_branch":scope.control_branch,
                        "nonce":scope.nonce,"epoch":packet["epoch"]})
        @contextmanager
        def effect():
            with broker.admission():
                authority.check()
                if packet["epoch"] != broker.registry.status(broker.project.repo)["epoch"]:
                    raise ExecutionBlocked("Codex parent epoch changed during publication")
                yield
        # Every outbound effect follows the durable ledger reservation.
        with effect():
            blob = api.request("POST", "git/blobs", {
                "encoding":"base64","content":base64.b64encode(data).decode("ascii")})
            if blob.get("sha") != sha:
                raise ExecutionBlocked("Codex publication blob does not match original payload")
        with effect():
            tree = api.request("POST", "git/trees", {
                "base_tree":commit["tree"]["sha"],
                "tree":[{"path":path,"mode":"100644","type":"blob","sha":sha}]})
            if not isinstance(tree,dict) or not isinstance(tree.get("sha"),str) or len(tree["sha"])!=40:
                raise ExecutionBlocked("Codex publication tree is malformed")
        with effect():
            author={"name":"Do Again","email":"do-again@users.noreply.github.com","date":stamp}
            created=api.request("POST","git/commits",{
                "message":message,"tree":tree["sha"],"parents":[before],
                "author":author,"committer":author})
            head=created.get("sha") if isinstance(created,dict) else None
            if not isinstance(head,str) or len(head)!=40:
                raise ExecutionBlocked("Codex publication commit is malformed")
        with effect():
            observed=api.request("GET","git/ref/heads/"+scope.control_branch)
            if not isinstance(observed,dict) or observed.get("object",{}).get("sha")!=before:
                raise ExecutionBlocked("Codex publication control ref moved; no retry")
            api.request("PATCH","git/refs/heads/"+scope.control_branch,
                        {"sha":head,"force":False})
        observed_head, observed_commit, observed_entries=snapshot(api)
        if (observed_head!=head or observed_commit.get("message")!=message
                or [p["sha"] for p in observed_commit.get("parents",[])]!=[before]
                or observed_entries.get(path)!=sha):
            raise ExecutionBlocked("Codex publication has no exact remote readback; no retry")
        result={"state":"succeeded","returncode":0,
                "request_id":rid,"head":head,"path":path,"blob":sha,
                "executed":False,"acknowledged":False}
        broker.ledger.finish(broker.project.key,packet["request_id"],result)
        broker.control_cache=None
        return result
