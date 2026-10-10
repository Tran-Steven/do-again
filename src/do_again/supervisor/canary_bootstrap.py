"""One-shot, operator-owned creation of a fresh ChatGPT canary conversation.

This prepares a private grant only. It neither installs a supervisor nor
activates a worker. A crashed or uncertain browser attempt consumes its nonce.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from ..core.schema import atomic_json
from .authority import AuthorityRegistry, project_identity
from .live_canary import require_fresh_conversation, scope
from .macos_execution import ExecutionBlocked


def _exclusive_json(path: Path, value: dict) -> None:
    """Exclusive, private and durable: never overwrite an earlier attempt."""
    if path.is_symlink():
        raise ExecutionBlocked("canary bootstrap path is aliased")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "posix":
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def bootstrap_live_canary(
    installed: dict,
    *,
    baseline: str,
    output: Path,
    nonce: str | None = None,
    browser=None,
) -> dict:
    """Create and verify a fresh chat entirely through the dedicated browser.

    Call only as the trusted operator, before the protected canary installer.
    Never retry an attempt with the same nonce, even after a pre-send failure.
    """
    from ..browser import runtime as default_browser, cdp
    browser = default_browser if browser is None else browser
    home = Path(installed["operator_home"])
    if (installed.get("production_ready") is not False
            or installed.get("operator_uid") != os.getuid()
            or not re.fullmatch(r"[0-9a-f]{40}", str(baseline))):
        raise ExecutionBlocked("canary bootstrap needs maintenance-only operator and exact baseline")
    parents = [p for p in installed.get("projects", []) if p.get("account") == "_doagain_da"]
    if len(parents) != 1 or parents[0].get("repo") != str(home / "do-again"):
        raise ExecutionBlocked("canary bootstrap has no unique trusted parent")
    parent = parents[0]
    status = AuthorityRegistry(Path(installed["legacy_authority_path"])).status(Path(parent["repo"]))
    if status["intent"] != "maintenance":
        raise ExecutionBlocked("canary bootstrap requires parent maintenance authority")
    if not isinstance(status.get("epoch"), int) or status["epoch"] < 1:
        raise ExecutionBlocked("canary bootstrap has no valid parent authority epoch")
    nonce = secrets.token_hex(12) if nonce is None else nonce
    if not re.fullmatch(r"[0-9a-f]{24}", str(nonce)):
        raise ExecutionBlocked("invalid canary bootstrap nonce")
    repo = home / ".do_again" / "live-canary" / nonce
    output = Path(output).absolute()
    ticket = home / ".do_again" / "canary-bootstrap" / (nonce + ".json")
    record = browser.browser_paths().projects / (project_identity(repo)[:12] + ".json")
    if (repo.exists() or repo.is_symlink() or record.exists() or record.is_symlink()
            or ticket.exists() or ticket.is_symlink()
            or output.exists() or output.is_symlink()):
        raise ExecutionBlocked("fresh canary identity, chat binding, or grant already exists")
    for path in (ticket, output, repo, record):
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ExecutionBlocked("canary bootstrap path contains an alias")
    branch = "do-again/canary-" + nonce + "/control"
    marker = "DO_AGAIN_CANARY_CHAT_READY_" + nonce
    prompt = ("New isolated Do Again development canary conversation. "
              "Production and real job applications are disabled. "
              "No development request is authorized by this message.\n"
              "Reply with exactly: " + marker)

    # Reserve before any browser effect; the same nonce can never be replayed.
    _exclusive_json(ticket, {"schema_version": 1, "state": "reserved",
                             "nonce": nonce, "baseline": baseline,
                             "parent_epoch": status["epoch"]})
    target = None
    dispatch_started = False
    try:
        session = browser.ensure_browser_running(verify_auth=True)
        target = cdp.create_target(int(session["port"]), browser.CHATGPT_URL, background=True)
        target, _ = browser.wait_for_authenticated(
            int(session["port"]), chat_url=target.url, timeout=30.0, target=target)
        atomic_json(ticket, {"schema_version": 1, "state": "target_created",
                             "nonce": nonce, "target_id": target.id})

        def mark_dispatch():
            nonlocal dispatch_started
            # A failed durable write is still potentially uncertain: never retry.
            dispatch_started = True
            atomic_json(ticket, {"schema_version": 1, "state": "dispatch_started",
                                 "nonce": nonce, "target_id": target.id})

        response = browser.send_message(
            target, prompt, timeout=180.0, before_dispatch=mark_dispatch)
        url = str(response.get("chat_url") or "")
        if (str(response.get("response") or "").strip() != marker
                or not re.fullmatch(r"https://chatgpt\.com/c/[A-Za-z0-9-]{16,80}", url)):
            raise ExecutionBlocked("fresh canary chat lacks exact readiness acknowledgment")
        grant = {"nonce": nonce, "baseline": baseline, "parent_epoch": status["epoch"],
                 "chat_url": url, "binding_identity": "0" * 64}
        # Reject the last installed chat even when the nonce is new.
        require_fresh_conversation(installed, {"live_canary": grant})
        atomic_json(ticket, {"schema_version": 1, "state": "chat_verified",
                             "nonce": nonce, "chat_url": url, "target_id": target.id})
        binding = browser.register_project(
            repo, remote_url="https://github.com/Tran-Steven/do-again.git",
            control_branch=branch, chat_url=url)
        grant["binding_identity"] = browser.binding_identity(binding)
        # Revalidate the exact scoped grant before sealing any usable file.
        scope(dict(installed, live_canary=grant))
        _exclusive_json(output, grant)
        atomic_json(ticket, {"schema_version": 1, "state": "grant_sealed",
                             "nonce": nonce, "chat_url": url,
                             "grant_path": str(output), "binding_identity": grant["binding_identity"]})
        return {"nonce": nonce, "grant_path": str(output), "chat_url": url,
                "control_branch": branch, "production_ready": False}
    except BaseException:
        # Keep the tab and exact ticket after any possibly-sent message for
        # read-only reconciliation. Never resubmit the original prompt.
        if dispatch_started:
            atomic_json(ticket, {"schema_version": 1, "state": "submission_unresolved",
                                 "nonce": nonce, "target_id": target.id if target else None})
        elif target is not None:
            cdp.close_target(int(session["port"]), target.id)
        raise
