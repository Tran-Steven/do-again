"""One-shot, journaled creation of an isolated canary GitHub control ref.

Runs only as the trusted operator. GitHub CLI handles credentials internally;
no credential reaches a restricted worker or Git subprocess.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from ..core.schema import atomic_json
from .macos_execution import ExecutionBlocked

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_NONCE = re.compile(r"[0-9a-f]{24}\Z")


def _api(args: list[str], *, body: dict | None = None) -> dict | None:
    command = ["gh", "api", "--hostname", "github.com", *args]
    try:
        proc = subprocess.run(
            command, input=(json.dumps(body) if body is not None else None),
            capture_output=True, text=True, timeout=30, check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(Path.home())})
    except (OSError, subprocess.TimeoutExpired):
        raise ExecutionBlocked("GitHub branch operation result is uncertain; inspect its remote ref") from None
    if proc.returncode:
        # gh returns 1 for both 404 and authentication/network errors. Do not
        # infer absence from that status; do not replay uncertain creations.
        raise ExecutionBlocked("GitHub control branch could not be verified; remote inspection required")
    try:
        value = json.loads(proc.stdout)
    except (TypeError, ValueError):
        raise ExecutionBlocked("GitHub control branch response is invalid") from None
    if not isinstance(value, dict):
        raise ExecutionBlocked("GitHub control branch response has invalid shape")
    return value


def provision_control_branch(*, nonce: str, baseline: str, grant: Path,
                             journal_root: Path) -> dict:
    """Provision once, or reconcile a recorded uncertain POST with read-only GET.

    The control ref is a narrow namespace, not a general publication capability.
    The operator has already authorized use of their configured gh identity.
    """
    if not (_NONCE.fullmatch(nonce) and _SHA.fullmatch(baseline)):
        raise ExecutionBlocked("control branch requires exact canary nonce and commit")
    grant = Path(grant).absolute()
    if grant.is_symlink() or not grant.is_file() or grant.stat().st_uid != os.getuid():
        raise ExecutionBlocked("canary grant is unavailable or not owned by operator")
    if grant.stat().st_mode & 0o077 or grant.stat().st_nlink != 1:
        raise ExecutionBlocked("canary grant is not private")
    try:
        value = json.loads(grant.read_text())
    except (OSError, ValueError):
        raise ExecutionBlocked("canary grant JSON is invalid") from None
    if (not isinstance(value, dict) or value.get("nonce") != nonce
            or value.get("baseline") != baseline or set(value) != {
                "nonce", "baseline", "parent_epoch", "chat_url", "binding_identity"}):
        raise ExecutionBlocked("canary control branch does not match sealed grant")

    branch = "do-again/canary-" + nonce + "/control"
    name = "refs/heads/" + branch
    endpoint = "repos/Tran-Steven/do-again/git/ref/heads/" + branch
    ticket = Path(journal_root).absolute() / (nonce + ".json")
    if any(p.is_symlink() for p in (ticket, grant, *ticket.parents)):
        raise ExecutionBlocked("branch journal path is aliased")
    if ticket.exists():
        history = json.loads(ticket.read_text())
        if history.get("nonce") != nonce or history.get("baseline") != baseline:
            raise ExecutionBlocked("branch journal authority differs")
        # A previous attempt may have reached GitHub, regardless of error.
        # Inspect only; do not send another POST for this consumed identity.
        remote = _api([endpoint])
        if remote.get("ref") != name or remote.get("object", {}).get("sha") != baseline:
            raise ExecutionBlocked("original branch publication remains unresolved")
        atomic_json(ticket, {"nonce": nonce, "baseline": baseline,
                             "phase": "verified", "branch": branch})
        return {"branch": branch, "sha": baseline, "reconciled": True}

    # A collision, even at the same hash, is not evidence that this fresh
    # identity was operator-created. Read first and deny any pre-existing ref.
    try:
        existing = _api([endpoint])
    except ExecutionBlocked:
        # A remote GET error is not proof of absence.
        raise ExecutionBlocked("cannot prove fresh branch absence; no POST issued") from None
    if existing is not None:
        raise ExecutionBlocked("fresh canary control branch already exists")

    # NOTE: this branch is only reached when an authenticated API wrapper
    # supports an explicit 404-as-absent response, never a guessed GET failure.
    raise ExecutionBlocked("GitHub absence proof is unavailable; no POST issued")
