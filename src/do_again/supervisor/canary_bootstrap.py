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
from .authority import project_identity
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


def _protected_parent_state(installed: dict, *, rpc=None) -> dict:
    """Verify the actual root-broker authority, never stale legacy SQLite.

    All installed protected projects must remain native-verified and quiescent in
    maintenance, including optional Sonary. A missing third sibling is a denial.
    The root authority's epoch is the only valid sealed browser grant epoch.
    """
    from .macos_client import broker_request
    reader = rpc or broker_request
    home = Path(installed["operator_home"])
    projects = installed.get("projects")
    expected = {"_doagain_da": "do-again", "_doagain_jp": "jobpipe"}
    if isinstance(projects,list) and len(projects)==3:
        expected["_doagain_so"] = "Sonary"
    if (not isinstance(projects, list) or len(projects) not in (2,3)
            or any(not isinstance(p,dict) for p in projects)
            or {p.get("account") for p in projects} != set(expected)):
        raise ExecutionBlocked("ChatGPT canary requires every protected project identity")
    statuses = {}
    for account, name in expected.items():
        project = next(p for p in projects if p.get("account")==account)
        repo = home / name
        if (project.get("repo") != str(repo)
                or project.get("github_repository") != "Tran-Steven/" + name):
            raise ExecutionBlocked("ChatGPT canary protected project path changed")
        state = reader(repo, {"operation":"status"})
        if (not isinstance(state,dict)
                or state.get("source_sha")!=installed.get("source_sha")
                or state.get("operator_intent")!="maintenance"
                or state.get("production_ready") is not False
                or state.get("canary_authorized") is not False
                or state.get("enforcement_verified") is not True
                or state.get("enforcement_blocker") is not None
                or state.get("unresolved_executions")!=[]
                or state.get("inflight_request_ids")!=[]
                or type(state.get("epoch")) is not int
                or state["epoch"]<1):
            raise ExecutionBlocked("ChatGPT canary parent is not protected quiescent maintenance")
        statuses[account] = state
    return statuses["_doagain_da"]



def preflight_background_delivery(*, browser=None, cdp_module=None) -> dict:
    """Read-only feasibility check before consuming any live canary nonce.

    Inspect ONLY existing Chrome/ChatGPT targets. Never launch Chrome, create
    a tab, activate a target/window, write a journal, or attempt a Send.
    A focused existing tab is only provisional: a fresh tab may lose focus.
    """
    from ..browser import runtime as default_browser, cdp as default_cdp
    browser = default_browser if browser is None else browser
    cdp_module = default_cdp if cdp_module is None else cdp_module

    def outcome(state, reason):
        return {"state": state, "reason": reason, "mode": "background",
                "read_only": True, "nonce_consumed": False,
                "message_submitted": False, "delivery_verified": False}

    try:
        config = browser.load_config()
        status = browser.browser_status(verify_session=False)
        if (config.get("preferred_mode") != "background"
                or status.get("mode") != "background"
                or status.get("running") is not True
                or status.get("authenticated") is not True
                or status.get("auth_required") is True):
            return outcome("blocked", "dedicated background Chrome is not authenticated and running")
        port = status.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            return outcome("blocked", "dedicated Chrome CDP port is unavailable")
        targets = [target for target in cdp_module.targets(port)
                   if target.url.startswith("https://chatgpt.com/")]
        if not targets:
            return outcome("blocked", "no existing ChatGPT tab to inspect without opening a tab")
        expression = """(() => ({
            focused: document.hasFocus() === true,
            visible: document.visibilityState === 'visible',
            composer: !!document.querySelector(
                '#prompt-textarea,[data-testid="prompt-textarea"],textarea,div[contenteditable="true"]'),
            challenge: !!document.querySelector('iframe[src*="challenges.cloudflare.com"],.cf-turnstile')
                || /just a moment|verify you are human|checking your browser/i.test(document.title)
        }))()"""
        for target in targets:
            observed = cdp_module.evaluate(target, expression, timeout=10.0)
            if (isinstance(observed, dict)
                    and observed.get("focused") is True
                    and observed.get("visible") is True
                    and observed.get("composer") is True
                    and observed.get("challenge") is False):
                return outcome("preflight_eligible",
                    "existing tab has document focus; fresh-tab dispatch is NOT yet proven")
        return outcome("blocked",
            "existing ChatGPT tabs are unfocused, hidden, challenged or lack composer; "
            "background Send would fail its immutable focus gate")
    except Exception:
        return outcome("blocked",
            "read-only Chrome/CDP observation failed; no effect was attempted")


def bootstrap_live_canary(
    installed: dict,
    *,
    baseline: str,
    output: Path,
    nonce: str | None = None,
    browser=None,
    allow_background: bool = False,
) -> dict:
    """Create and verify a fresh chat entirely through the dedicated browser.

    Background mode is explicitly opt-in and retains the no-activation focus gate.
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
    status = _protected_parent_state(installed)
    if allow_background:
        # This is an early NEGATIVE gate, not send readiness. It may reject
        # an unfocused GUI session without reserving a disposable identity.
        # send_message independently checks the actual fresh target.
        preflight = preflight_background_delivery(browser=browser)
        if preflight["state"] != "preflight_eligible":
            raise ExecutionBlocked("background Chrome preflight blocked before canary nonce: "
                                   + preflight["reason"])
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
        expected_mode = "background" if allow_background else "headless"
        if (session.get("mode") != expected_mode
                or session.get("session_ready") is not True
                or session.get("authenticated") is not True):
            raise ExecutionBlocked("live canary requires authenticated explicitly selected browser mode; no automatic GUI fallback")
        target = cdp.create_target(int(session["port"]), browser.CHATGPT_URL, background=True)
        target, _ = browser.wait_for_authenticated(
            int(session["port"]), chat_url=target.url, timeout=30.0, target=target)
        # send_message owns its focus/visibility check before any composer
        # mutation or durable dispatch. Target.activateTarget alone only made
        # a disposable background tab visible, NOT focused on macOS, so never
        # treat that as permission to click Send.
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
        # Reject a parent pause, resume, or epoch change during browser I/O.
        # A new chat is not authority to issue a canary grant.
        current = _protected_parent_state(installed)
        if current.get("epoch") != status["epoch"]:
            raise ExecutionBlocked("parent authority changed during canary bootstrap")
        grant = {"nonce": nonce, "baseline": baseline, "parent_epoch": status["epoch"],
                 "chat_url": url, "binding_identity": "0" * 64}
        if allow_background:
            grant["browser_mode"] = "background"
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
                "control_branch": branch, "browser_mode": expected_mode,
                "production_ready": False}
    except BaseException:
        # Keep the tab and exact ticket after any possibly-sent message for
        # read-only reconciliation. Never resubmit the original prompt.
        if dispatch_started:
            atomic_json(ticket, {"schema_version": 1, "state": "submission_unresolved",
                                 "nonce": nonce, "target_id": target.id if target else None})
        elif target is not None:
            cdp.close_target(int(session["port"]), target.id)
        raise
