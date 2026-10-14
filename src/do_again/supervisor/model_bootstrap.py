"""Prepare a fresh operator-owned Codex grant without inference or browser use."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime,timedelta,timezone
from pathlib import Path

from .macos_execution import ExecutionBlocked
from .canary_bootstrap import _exclusive_json

SHA40=re.compile(r"[0-9a-f]{40}\Z")
NONCE=re.compile(r"[0-9a-f]{24}\Z")


def bootstrap_codex_canary(installed:dict,*,baseline:str,output:Path,
                          nonce:str|None=None,rpc=None)->dict:
    """One exclusive grant, with zero Codex model calls and no GitHub mutation."""
    from .macos_client import broker_request
    if (not isinstance(installed,dict)
            or installed.get("production_ready") is not False
            or installed.get("operator_uid")!=os.getuid()
            or os.getuid()==0
            or not isinstance(baseline,str) or not SHA40.fullmatch(baseline)):
        raise ExecutionBlocked("Codex bootstrap requires verified operator maintenance and SHA")
    home=Path(installed.get("operator_home",""))
    if home.resolve()!=Path.home().resolve():
        raise ExecutionBlocked("Codex bootstrap home differs from the installed operator")
    projects=installed.get("projects")
    if (not isinstance(projects,list) or len(projects)!=2
            or {p.get("account") for p in projects}!={"_doagain_da","_doagain_jp"}):
        raise ExecutionBlocked("Codex bootstrap requires both separate installed parents")
    reader=rpc or broker_request
    epoch=None
    for account in ("_doagain_da","_doagain_jp"):
        project=next(p for p in projects if p["account"]==account)
        if (project.get("repo")!=str(home/("do-again" if account=="_doagain_da" else "jobpipe"))
                or project.get("github_repository")!=(
                    "Tran-Steven/do-again" if account=="_doagain_da" else "Tran-Steven/jobpipe")):
            raise ExecutionBlocked("Codex bootstrap project identity differs")
        status=reader(Path(project["repo"]),{"operation":"status"})
        if (not isinstance(status,dict) or status.get("operator_intent")!="maintenance"
                or status.get("source_sha")!=installed.get("source_sha")
                or status.get("production_ready") is not False
                or status.get("canary_authorized") is not False
                or status.get("enforcement_verified") is not True
                or status.get("enforcement_blocker") is not None
                or status.get("unresolved_executions")!=[]
                or status.get("inflight_request_ids")!=[]):
            raise ExecutionBlocked("Codex bootstrap parent is not native-quiescent")
        if account=="_doagain_da":
            epoch=status.get("epoch")
            if type(epoch) is not int or epoch<1:
                raise ExecutionBlocked("Codex parent epoch is invalid")
    root=(home/".do_again/codex-tools").resolve()
    binary=root/"node_modules"/".bin"/"codex"
    if not binary.exists():
        raise ExecutionBlocked("Codex isolated CLI executable is missing")
    target=binary.resolve()
    if (not target.is_relative_to(root) or not target.is_file()
            or target.stat().st_uid!=os.getuid()
            or target.stat().st_mode & 0o022
            or not 0<target.stat().st_size<=16*1024*1024):
        raise ExecutionBlocked("Codex isolated executable is mutable or outside the pinned installation")
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    candidate=nonce or secrets.token_hex(12)
    if not isinstance(candidate,str) or not NONCE.fullmatch(candidate):
        raise ExecutionBlocked("Codex bootstrap requires one fresh exact nonce")
    expiry=(datetime.now(timezone.utc)+timedelta(minutes=85)).replace(microsecond=0).isoformat()
    grant={
        "nonce":candidate,"baseline":baseline,"parent_epoch":epoch,
        "transport":"codex-cli","cli_sha256":digest,
        "expires_at_utc":expiry,"max_model_calls":2,
    }
    destination=Path(output).absolute()
    for path in (destination,*destination.parents):
        if path.is_symlink():
            raise ExecutionBlocked("Codex grant output contains an aliased path")
    if destination.exists():
        raise ExecutionBlocked("Codex bootstrap cannot overwrite a prior reserved grant")
    _exclusive_json(destination,grant)
    return {
        "nonce":candidate,"baseline":baseline,
        "parent_epoch":epoch,"transport":"codex-cli",
        "expires_at_utc":expiry,"model_calls":0,
        "browser_calls":0,"grant_path":str(destination),
        "production_ready":False,
    }
