"""Regular ChatGPT conversation bridge. No browser, Codex, or model API required.

Preparing a handoff creates only an original private local intent. ChatGPT
publishes the exact request to the existing Git control branch using its
available GitHub integration; the existing Do Again worker publishes a receipt.
Observation fetches Git history read-only (remote) and validates identity.
This is *transport evidence*, never a substitute for root/native confinement.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core.schema import canonical_json, request_fingerprint, validate_request
from .service.runtime import ServiceError, RuntimeLayout

_ID = re.compile(r"chatgpt-verify-[0-9a-f]{24}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _journal_dir(layout: RuntimeLayout) -> Path:
    root = layout.state_dir / "chatgpt_handoffs"
    if root.is_symlink() or layout.state_dir.is_symlink():
        raise ServiceError("ChatGPT handoff state is aliased")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ServiceError("ChatGPT handoff journal is not an ordinary directory")
    return root


def _write_original(path: Path, value: dict) -> None:
    # No replacement, even after a crash. One request ID has one identity.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _github_slug(layout: RuntimeLayout) -> str:
    """Resolve the actual configured repository; do not expose local paths."""
    try:
        result = subprocess.run(
            ["git", "-C", str(layout.repo), "remote", "get-url", layout.remote],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError("cannot resolve the configured GitHub remote") from exc
    if result.returncode != 0:
        raise ServiceError("configured GitHub remote is unavailable")
    raw = result.stdout.strip()
    found = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", raw)
    if found is None:
        raise ServiceError("regular ChatGPT GitHub handoff requires a GitHub repository remote")
    return found[1]


def prepare(layout: RuntimeLayout, *, nonce: str | None = None,
            now: datetime | None = None) -> dict[str, Any]:
    """Generate a harmless status request to paste into an ordinary ChatGPT chat."""
    if not layout.branch or layout.branch in {"main", "master", "trunk"}:
        raise ServiceError("ChatGPT handoff requires a separate control branch")
    slug = _github_slug(layout)
    nonce = secrets.token_hex(12) if nonce is None else nonce
    if not re.fullmatch(r"[0-9a-f]{24}", nonce):
        raise ServiceError("ChatGPT verification identity must be 24 hex characters")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rid = "chatgpt-verify-" + nonce
    request = {
        "schema_version": 1, "request_id": rid, "operation": "status",
        "issued_at_utc": current.isoformat(),
        "expires_at_utc": (current + timedelta(minutes=10)).isoformat(),
        "args": {}, "expected": {}, "limits": {"timeout_seconds": 30},
    }
    # Native requests are validated the same way as normal manual requests.
    validate_request(request, max_ttl_seconds=1200)
    witness = {
        "schema_version": 1, "transport": "regular_chatgpt",
        "repository": str(layout.repo), "github_repository": slug,
        "remote": layout.remote,
        "control_branch": layout.branch, "request": request,
        "request_fingerprint": request_fingerprint(request),
        "status": "prepared_not_submitted",
    }
    _write_original(_journal_dir(layout) / (rid + ".json"), witness)
    path = "automation/do_again/requests/" + rid + ".json"
    prompt = (
        "Do Again regular-ChatGPT verification (no Codex CLI or browser automation). "
        "Use the connected GitHub tools to create exactly ONE file at " + path
        + " on the dedicated branch " + layout.branch + " of the Git repository "
        + slug + ". The file content must be this exact JSON: "
        + json.dumps(request, sort_keys=True, separators=(",", ":"))
        + ". Do not edit the worktree, apply to production, submit real applications, "
        "or invent a receipt. After publishing, inspect the matching "
        "automation/do_again/receipts/" + rid + ".json in that same control "
        "branch. Report the actual receipt state, or say that it is not yet present. "
        "Do not retry this request ID or publish a second request for this check."
    )
    return {"request_id": rid, "transport": "regular_chatgpt",
            "control_branch": layout.branch, "request_path": path,
            "request_fingerprint": witness["request_fingerprint"],
            "prompt": prompt, "submitted": False}


def _git(control: Path, *args: str, runner=None) -> str:
    command = ["git", "-C", str(control), *args]
    if runner is None:
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError("Git control observation unavailable") from exc
    else:
        result = runner(command)
    if result.returncode != 0:
        raise ServiceError("Git control observation failed; no receipt was assumed")
    return result.stdout.strip()


def observe(layout: RuntimeLayout, request_id: str, *, runner=None) -> dict[str, Any]:
    """Check the exact original request and receipt on a fresh remote Git head."""
    if not isinstance(request_id, str) or not _ID.fullmatch(request_id):
        raise ServiceError("ChatGPT verification request identity is invalid")
    path = _journal_dir(layout) / (request_id + ".json")
    if not path.is_file() or path.is_symlink():
        raise ServiceError("original ChatGPT handoff intent does not exist")
    try:
        original = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ServiceError("original ChatGPT handoff intent is unreadable") from exc
    request = original.get("request")
    if (original.get("transport") != "regular_chatgpt"
            or original.get("repository") != str(layout.repo)
            or original.get("github_repository") != _github_slug(layout)
            or original.get("remote") != layout.remote
            or original.get("control_branch") != layout.branch
            or not isinstance(request, dict)
            or request.get("request_id") != request_id
            or original.get("request_fingerprint") != request_fingerprint(request)):
        raise ServiceError("ChatGPT handoff identity changed; do not trust remote evidence")
    if not layout.control_worktree.is_dir():
        raise ServiceError("control worktree missing; no remote verification was performed")
    _git(layout.control_worktree, "fetch", "--no-tags", "--quiet",
         layout.remote, layout.branch, runner=runner)
    head = _git(layout.control_worktree, "rev-parse", "FETCH_HEAD", runner=runner)
    if not _SHA.fullmatch(head):
        raise ServiceError("fetched Git control head is invalid")
    # Confirm a branch movement during fetch is not confused with proven state.
    observed = _git(layout.control_worktree, "ls-remote", "--heads",
                    layout.remote, "refs/heads/" + layout.branch, runner=runner)
    if observed != head + "\trefs/heads/" + layout.branch:
        raise ServiceError("Git remote control branch changed during verification")
    def remote_record(kind: str):
        try:
            raw = _git(layout.control_worktree, "show",
                       head + ":automation/do_again/" + kind + "/"
                       + request_id + ".json", runner=runner)
        except ServiceError:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            raise ServiceError("remote ChatGPT record is malformed")
        if not isinstance(value, dict):
            raise ServiceError("remote ChatGPT record is not an object")
        return value
    published_request = remote_record("requests")
    base = {"request_id": request_id, "transport": "regular_chatgpt",
            "remote_head": head, "verification": "git_receipt_only"}
    if published_request is None:
        return dict(base, state="request_not_published", completed=False)
    if canonical_json(published_request) != canonical_json(request):
        raise ServiceError("published ChatGPT request differs from original private intent")
    receipt = remote_record("receipts")
    if receipt is None:
        return dict(base, state="awaiting_worker_receipt", completed=False)
    if (receipt.get("request_id") != request_id
            or receipt.get("operation") != "status"
            or receipt.get("request_fingerprint") != original["request_fingerprint"]):
        raise ServiceError("remote receipt is not bound to original ChatGPT request")
    if receipt.get("state") != "succeeded":
        return dict(base, state="worker_" + str(receipt.get("state")),
                    completed=False)
    result = receipt.get("result")
    if (not isinstance(result, dict)
            or result.get("operation") != "status"
            or result.get("request_fingerprint") != original["request_fingerprint"]):
        raise ServiceError("status receipt lacks a matching executed transport result")
    return dict(base, state="receipt_verified", completed=True)
