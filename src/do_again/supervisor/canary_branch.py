"""One-shot, journaled provisioning of a single isolated canary GitHub control ref.

Runs only as the trusted operator. GitHub CLI owns the credentials, never Git or
an untrusted worker. Ambiguous GitHub effects cannot be replayed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..core.schema import atomic_json
from .macos_execution import ExecutionBlocked

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_NONCE = re.compile(r"[0-9a-f]{24}\Z")
_STATUS = re.compile(r"^HTTP/\S+\s+([0-9]{3})\b")


def _api(method: str, endpoint: str, *, body: dict | None = None) -> tuple[int, dict]:
    """Get a *verified HTTP status*, never infer 404 from gh's exit code."""
    if method not in {"GET", "POST"} or not endpoint.startswith(
            "repos/Tran-Steven/do-again/git/ref"):
        raise ExecutionBlocked("GitHub ref operation is outside canary setup")
    if method == "POST" and endpoint != "repos/Tran-Steven/do-again/git/refs":
        raise ExecutionBlocked("GitHub write endpoint is outside canary setup")
    command = ["gh", "api", "--hostname", "github.com", "--include",
               "--method", method, endpoint]
    if body is not None:
        if method != "POST":
            raise ExecutionBlocked("GitHub request body is not permitted")
        command += ["--input", "-"]
    try:
        proc = subprocess.run(
            command, input=(json.dumps(body) if body is not None else None),
            capture_output=True, text=True, timeout=30, check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(Path.home())})
    except (OSError, subprocess.TimeoutExpired):
        raise ExecutionBlocked("GitHub branch request uncertain; inspect remote ref") from None
    # gh --include emits the effective HTTP response status even for an
    # ordinary, expected 404. Never classify a generic CLI error as not found.
    statuses = [_STATUS.match(row.strip()) for row in proc.stdout.splitlines()]
    statuses = [int(m.group(1)) for m in statuses if m]
    if len(statuses) != 1:
        raise ExecutionBlocked("GitHub branch response lacks an unambiguous HTTP status")
    status = statuses[0]
    if proc.returncode and status != 404:
        raise ExecutionBlocked("GitHub branch request failed; remote state uncertain")
    if status == 404 and method == "GET":
        return status, {}
    if not 200 <= status < 300 or proc.returncode:
        raise ExecutionBlocked("GitHub branch request was not successful")
    separator = re.search(r"\r?\n\r?\n", proc.stdout)
    if separator is None:
        raise ExecutionBlocked("GitHub branch response body is unavailable")
    try:
        value = json.loads(proc.stdout[separator.end():])
    except (ValueError, UnicodeError):
        raise ExecutionBlocked("GitHub branch JSON is invalid") from None
    if not isinstance(value, dict):
        raise ExecutionBlocked("GitHub branch JSON has invalid shape")
    return status, value


def _reserve(ticket: Path, value: dict) -> None:
    ticket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(ticket, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        fd = os.open(ticket.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def provision_control_branch(*, nonce: str, baseline: str, grant: Path,
                             journal_root: Path) -> dict:
    """Create only an absent exact canary ref; resolve consumed attempts read-only."""
    if not (_NONCE.fullmatch(nonce) and _SHA.fullmatch(baseline)):
        raise ExecutionBlocked("control branch needs exact canary nonce and SHA")
    grant = Path(grant).absolute()
    if (grant.is_symlink() or not grant.is_file() or grant.stat().st_uid != os.getuid()
            or grant.stat().st_mode & 0o077 or grant.stat().st_nlink != 1):
        raise ExecutionBlocked("canary grant is not a private operator-owned file")
    try:
        value = json.loads(grant.read_text())
    except (OSError, ValueError):
        raise ExecutionBlocked("canary grant is not valid JSON") from None
    if (not isinstance(value, dict) or value.get("nonce") != nonce
            or value.get("baseline") != baseline or set(value) != {
                "nonce", "baseline", "parent_epoch", "chat_url", "binding_identity"}):
        raise ExecutionBlocked("canary branch authority differs from sealed grant")

    branch = "do-again/canary-" + nonce + "/control"
    name = "refs/heads/" + branch
    endpoint = "repos/Tran-Steven/do-again/git/ref/heads/" + branch
    ticket = Path(journal_root).absolute() / (nonce + ".json")
    if any(p.is_symlink() for p in (ticket, grant, *ticket.parents)):
        raise ExecutionBlocked("control branch path contains a symlink")
    if ticket.exists():
        try:
            history = json.loads(ticket.read_text())
        except (OSError, ValueError):
            raise ExecutionBlocked("control branch journal is invalid") from None
        if history.get("nonce") != nonce or history.get("baseline") != baseline:
            raise ExecutionBlocked("control branch journal identity changed")
        # An earlier request may have reached GitHub. Never POST on retry.
        status, remote = _api("GET", endpoint)
        if status != 200 or remote.get("ref") != name or remote.get("object", {}).get("sha") != baseline:
            raise ExecutionBlocked("prior branch publication remains unresolved")
        atomic_json(ticket, {"nonce": nonce, "baseline": baseline,
                             "phase": "verified", "branch": branch})
        return {"branch": branch, "sha": baseline, "reconciled": True}

    # We require a literal GitHub 404 *before* reserving the one-shot effect.
    status, remote = _api("GET", endpoint)
    if status != 404 or remote:
        raise ExecutionBlocked("fresh canary branch is already present or ambiguous")
    _reserve(ticket, {"nonce": nonce, "baseline": baseline,
                      "phase": "publication_reserved", "branch": branch})
    # A lost POST response leaves the journal reserved. Later calls only GET.
    status, posted = _api("POST", "repos/Tran-Steven/do-again/git/refs",
                          body={"ref": name, "sha": baseline})
    if status != 201 or posted.get("ref") != name or posted.get("object", {}).get("sha") != baseline:
        raise ExecutionBlocked("GitHub branch creation outcome is uncertain")
    status, verified = _api("GET", endpoint)
    if status != 200 or verified.get("ref") != name or verified.get("object", {}).get("sha") != baseline:
        raise ExecutionBlocked("GitHub control branch readback differs")
    atomic_json(ticket, {"nonce": nonce, "baseline": baseline,
                         "phase": "verified", "branch": branch})
    return {"branch": branch, "sha": baseline, "reconciled": False}
