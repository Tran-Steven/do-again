"""One-shot GitHub control-ref provisioning for the separately sealed Codex canary."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..core.schema import atomic_json
from .canary_branch import _api,_reserve
from .macos_execution import ExecutionBlocked

NONCE=re.compile(r"[0-9a-f]{24}\Z")
SHA40=re.compile(r"[0-9a-f]{40}\Z")
SHA64=re.compile(r"[0-9a-f]{64}\Z")



def _check_operator_authority(installed:dict,grant:dict,*,rpc=None)->None:
    """Use exact root-broker epochs, never the older browser authority DB."""
    from .macos_client import broker_request
    if (not isinstance(installed,dict)
            or installed.get("production_ready") is not False
            or installed.get("operator_uid")!=os.getuid()
            or os.getuid()==0):
        raise ExecutionBlocked("Codex branch requires its installed maintenance operator")
    home=Path(installed.get("operator_home",""))
    projects=installed.get("projects")
    if (not isinstance(projects,list) or len(projects)!=2
            or {p.get("account") for p in projects if isinstance(p,dict)}
                  !={"_doagain_da","_doagain_jp"}
            or type(grant.get("parent_epoch")) is not int):
        raise ExecutionBlocked("Codex branch parent identities and epoch are missing")
    reader=rpc or broker_request
    for account in ("_doagain_da","_doagain_jp"):
        project=next(p for p in projects if p.get("account")==account)
        expected=home/("do-again" if account=="_doagain_da" else "jobpipe")
        expected_name=("Tran-Steven/do-again" if account=="_doagain_da"
                       else "Tran-Steven/jobpipe")
        if (project.get("repo")!=str(expected)
                or project.get("github_repository")!=expected_name):
            raise ExecutionBlocked("Codex branch native project scope differs")
        state=reader(expected,{"operation":"status"})
        if (not isinstance(state,dict)
                or state.get("source_sha")!=installed.get("source_sha")
                or state.get("operator_intent")!="maintenance"
                or state.get("production_ready") is not False
                or state.get("canary_authorized") is not False
                or state.get("enforcement_verified") is not True
                or state.get("enforcement_blocker") is not None
                or state.get("unresolved_executions")!=[]
                or state.get("inflight_request_ids")!=[]):
            raise ExecutionBlocked("Codex ref requires quiescent protected maintenance")
        if (account=="_doagain_da" and (
                type(state.get("epoch")) is not int
                or state["epoch"]!=grant["parent_epoch"])):
            raise ExecutionBlocked("Codex ref protected parent epoch changed")


def provision_codex_control_branch(
    *,grant:Path,installed:dict,journal_root:Path,
    reconcile_only:bool=False,
)->dict:
    """Never repeat an uncertain GitHub POST. Browser model calls are absent."""
    origin=Path(grant).absolute()
    if (origin.is_symlink() or not origin.is_file()
            or origin.stat().st_uid!=os.getuid()
            or origin.stat().st_nlink!=1 or origin.stat().st_mode & 0o077):
        raise ExecutionBlocked("Codex GitHub branch requires a private original operator grant")
    try:
        value=json.loads(origin.read_text())
    except (OSError,ValueError):
        raise ExecutionBlocked("Codex branch grant cannot be parsed") from None
    if (not isinstance(value,dict)
            or set(value)!={"nonce","baseline","parent_epoch","transport",
                             "cli_sha256","expires_at_utc","max_model_calls"}
            or value.get("transport")!="codex-cli"
            or type(value.get("max_model_calls")) is not int or value["max_model_calls"]!=2
            or not isinstance(value.get("nonce"),str) or not NONCE.fullmatch(value["nonce"])
            or not isinstance(value.get("baseline"),str) or not SHA40.fullmatch(value["baseline"])
            or not isinstance(value.get("cli_sha256"),str)
            or not SHA64.fullmatch(value["cli_sha256"])):
        raise ExecutionBlocked("Codex branch grant is outside the sealed two-task identity")
    _check_operator_authority(installed,value)
    nonce,baseline=value["nonce"],value["baseline"]
    branch="do-again/canary-"+nonce+"/control"
    name="refs/heads/"+branch
    endpoint="repos/Tran-Steven/do-again/git/ref/heads/"+branch
    ticket=Path(journal_root).absolute()/(nonce+".json")
    for path in (ticket,*ticket.parents):
        if path.is_symlink():
            raise ExecutionBlocked("Codex branch journal contains an alias")
    if ticket.exists():
        if (not ticket.is_file() or ticket.stat().st_uid!=os.getuid()
                or ticket.stat().st_nlink!=1 or ticket.stat().st_mode & 0o077):
            raise ExecutionBlocked("Codex existing branch journal is not trusted")
        try:
            record=json.loads(ticket.read_text())
        except (OSError,ValueError):
            raise ExecutionBlocked("Codex branch journal cannot be parsed") from None
        if record.get("nonce")!=nonce or record.get("baseline")!=baseline:
            raise ExecutionBlocked("Codex branch earlier publication identity differs")
        code,remote=_api("GET",endpoint)
        if code!=200 or remote.get("ref")!=name or remote.get("object",{}).get("sha")!=baseline:
            raise ExecutionBlocked("Codex branch original GitHub effect is unresolved")
        atomic_json(ticket,{"nonce":nonce,"baseline":baseline,
                            "phase":"verified","branch":branch})
        return {"branch":branch,"sha":baseline,"reconciled":True}
    if reconcile_only:
        raise ExecutionBlocked("no original reserved Codex branch effect to observe")
    code,remote=_api("GET",endpoint)
    if code!=404 or remote:
        raise ExecutionBlocked("Codex control branch exists or its remote state is uncertain")
    _check_operator_authority(installed,value)
    _reserve(ticket,{"nonce":nonce,"baseline":baseline,
                     "phase":"publication_reserved","branch":branch})
    code,posted=_api("POST","repos/Tran-Steven/do-again/git/refs",
        body={"ref":name,"sha":baseline})
    if code!=201 or posted.get("ref")!=name or posted.get("object",{}).get("sha")!=baseline:
        raise ExecutionBlocked("Codex branch creation is uncertain; do not reissue POST")
    code,remote=_api("GET",endpoint)
    if code!=200 or remote.get("ref")!=name or remote.get("object",{}).get("sha")!=baseline:
        raise ExecutionBlocked("Codex branch creation lacks exact remote readback")
    atomic_json(ticket,{"nonce":nonce,"baseline":baseline,
                        "phase":"verified","branch":branch})
    return {"branch":branch,"sha":baseline,"reconciled":False}
