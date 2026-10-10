from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .browser import (
    BrowserAuthRequired,
    BrowserError,
    archive_queue_status,
    chat_cleanup_inventory,
    queue_verified_archives,
    browser_self_test,
    browser_status,
    deactivate_project,
    discover_browser,
    ensure_browser_running,
    ensure_project_chat,
    process_archive_queue,
    verify_candidate_chat,
    project_record,
    setup_browser,
    stop_browser,
    stop_if_unused,
)
from .browser import cdp
from .platforms.detect import detect_platform
from .browser.runtime import send_message, use_background_fallback, _find_chatgpt_target, _assistant_snapshot
from .core.schema import REQUEST_ID_RE, request_fingerprint
from .service.rollout import staged_upgrade
from .service.session_summary import build_summary, render_summary, save_summary, session_start
from .service.runtime import (
    ServiceError,
    _default_policy,
    _ensure_control_worktree,
    _ensure_remote_control_branch,
    find_repo,
    install_service,
    restart_service,
    run_foreground,
    runtime_layout,
    service_status,
    stop_service,
    uninstall_service,
)


def doctor(path: str = ".", *, fix: bool = False) -> int:
    info = detect_platform()
    checks: dict[str, bool] = {
        "python": sys.version_info >= (3, 11),
        "git": shutil.which("git") is not None,
        "platform": info.supported,
    }
    repairs: list[str] = []
    actions: list[str] = []
    browser_error: str | None = None
    layout = None
    repo = None

    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)
        checks["repo"] = True
    except ServiceError as exc:
        checks["repo"] = False
        actions.append(f"project: {exc}")

    if layout is not None:
        service: dict[str, object] = {}
        try:
            service = service_status(repo)
            checks["service_status"] = True
        except ServiceError as exc:
            checks["service_status"] = False
            actions.append(f"service: {exc}")

        runtime_dirs = [
            layout.root,
            layout.state_dir,
            layout.stdout_log.parent,
        ]
        missing_dirs = [p for p in runtime_dirs if not p.is_dir()]
        checks["runtime_dirs"] = not missing_dirs
        if fix and missing_dirs:
            for directory in missing_dirs:
                directory.mkdir(parents=True, exist_ok=True)
            repairs.append("created missing runtime directories")
            checks["runtime_dirs"] = True

        control_ok = False
        if layout.control_worktree.exists():
            probe = subprocess.run(
                ["git", "-C", str(layout.control_worktree), "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
            )
            if probe.returncode == 0:
                dirty = subprocess.run(
                    ["git", "-C", str(layout.control_worktree), "status", "--porcelain"],
                    text=True,
                    capture_output=True,
                )
                control_ok = dirty.returncode == 0 and not dirty.stdout.strip()
                if not control_ok:
                    actions.append("control_worktree: existing control worktree is dirty or unhealthy")
        checks["control_worktree"] = control_ok

        if fix and not control_ok and not layout.control_worktree.exists():
            try:
                _ensure_remote_control_branch(layout)
                _ensure_control_worktree(layout)
                repairs.append("created missing control worktree")
                checks["control_worktree"] = True
            except ServiceError as exc:
                actions.append(f"control_worktree: {exc}")
        elif not control_ok and not layout.control_worktree.exists():
            actions.append("control_worktree: run do-again doctor --fix to create it")

        if service:
            installed = bool(service.get("installed"))
            running = bool(service.get("running"))
            checks["service_installed"] = installed
            checks["service_running"] = running
            if not installed:
                actions.append("service: run do-again install")
            elif not running:
                actions.append("service: run do-again start")
            # Never auto-install/restart here: those copy/replace runtime and are explicit lifecycle actions.

        if layout.browser_enabled:
            try:
                browser_binary = discover_browser()
                checks["browser"] = True
            except BrowserError as exc:
                checks["browser"] = False
                browser_binary = None
                browser_error = str(exc)
                actions.append(f"browser: {exc}")

            if browser_binary is not None:
                try:
                    shared = browser_status(verify_session=False)
                    if shared.get("running"):
                        session = browser_status(verify_session=True)
                        authenticated = bool(session.get("authenticated"))
                        checks["browser_auth"] = authenticated
                        if not authenticated:
                            actions.append("browser_auth: run do-again browser login")
                    else:
                        checks["browser_auth"] = False
                        actions.append("browser_auth: browser is stopped; run do-again browser start")
                except BrowserError as exc:
                    checks["browser_auth"] = False
                    actions.append(f"browser_auth: {exc}")

    for key, ok in checks.items():
        print(f"{'OK' if ok else 'FAIL'} {key}")
    print(f"platform_name={info.name}")
    print(f"service_manager={info.service_manager}")
    if layout is not None:
        print(f"repo={layout.repo}")
        print(f"browser_enabled={layout.browser_enabled}")
    if layout is not None and layout.browser_enabled and 'browser_binary' in locals():
        if browser_binary is not None:
            print(f"browser_binary={browser_binary}")
        if browser_error:
            print(f"browser_fix={browser_error}")
    for value in repairs:
        print(f"FIXED {value}")
    for value in actions:
        print(f"ACTION {value}")

    return 0 if all(checks.values()) else 1


def _print_service_status(value: dict[str, object]) -> None:
    for key in (
        "platform",
        "service_manager",
        "repo",
        "label",
        "installed",
        "running",
        "pid",
        "runtime_dir",
        "control_worktree",
    ):
        if key in value:
            print(f"{key}={value[key]}")


def status(path: str = ".") -> int:
    info = detect_platform()
    try:
        repo = find_repo(path)
    except ServiceError:
        print(f"platform={info.name}")
        print(f"service_manager={info.service_manager}")
        return 0
    try:
        value = service_status(repo)
        layout = runtime_layout(repo)
    except ServiceError as exc:
        print(f"do-again: {exc}", file=sys.stderr)
        return 1

    _print_service_status(value)
    print(f"browser_enabled={layout.browser_enabled}")
    if layout.browser_enabled:
        shared = browser_status(verify_session=False)
        browser = browser_status(verify_session=bool(shared.get("running")))
        record = project_record(repo)
        print(f"browser_running={browser.get('running')}")
        print(f"browser_mode={browser.get('mode')}")
        print(f"browser_authenticated={browser.get('authenticated')}")
        print(f"browser_session_ready={browser.get('session_ready')}")
        if record.get("chat_url"):
            print(f"automation_chat={record.get('chat_url')}")
        delivery_path = layout.state_dir / "browser_status.json"
        if delivery_path.is_file():
            try:
                delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                delivery = {}
            if isinstance(delivery, dict):
                print(f"browser_delivery_state={delivery.get('state')}")
                print(f"browser_pending_receipts={delivery.get('pending_receipts', 0)}")
                if delivery.get("error"):
                    print(f"browser_delivery_error={delivery.get('error')}")
        liveness_path = layout.state_dir / "liveness.json"
        if liveness_path.is_file():
            try:
                liveness = json.loads(liveness_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                liveness = {}
            if isinstance(liveness, dict):
                print(f"liveness_state={liveness.get('state', 'unknown')}")
                print(f"liveness_last_progress={liveness.get('progress_at')}")
                print(f"liveness_recovery_attempts={liveness.get('attempts', 0)}")
                if liveness.get("issue_report"):
                    print(f"liveness_issue_report={liveness['issue_report']}")
                if liveness.get("issue_number"):
                    print(f"liveness_issue_number={liveness['issue_number']}")
        stale_path = layout.state_dir / "stale_request_blocked.json"
        if stale_path.is_file():
            try:
                stale = json.loads(stale_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stale = {}
            if isinstance(stale, dict) and stale.get("request_id"):
                print("stale_request_blocked=true")
                print(f"stale_request_id={stale['request_id']}")
                print(f"stale_request_reason={stale.get('reason', 'unknown')}")
        if browser.get("auth_required"):
            print("browser_action=run do-again setup")
    return 0


def init_project(path: str) -> int:
    target = Path(path).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    config = target / "do-again.toml"
    policy = target / "do-again-policy.json"

    if not policy.exists():
        policy.write_text(
            json.dumps(_default_policy(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if not config.exists():
        config.write_text(
            '[do_again]\n'
            'control_branch = "operator-control"\n'
            'remote = "origin"\n'
            'policy = "do-again-policy.json"\n'
            'browser = true\n',
            encoding="utf-8",
        )
    print(str(config))
    return 0


def _set_browser_enabled(repo: Path, enabled: bool) -> None:
    config = repo / "do-again.toml"
    text = config.read_text(encoding="utf-8")
    value = "true" if enabled else "false"
    if re.search(r"(?m)^browser\s*=\s*(?:true|false)\s*$", text):
        text = re.sub(
            r"(?m)^browser\s*=\s*(?:true|false)\s*$",
            f"browser = {value}",
            text,
            count=1,
        )
    else:
        marker = "[do_again]\n"
        if marker not in text:
            raise ServiceError("do-again.toml is missing [do_again]")
        text = text.replace(marker, marker + f"browser = {value}\n", 1)
    config.write_text(text, encoding="utf-8")


def _check_remote(repo: Path, remote: str) -> str:
    url = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", remote],
        text=True,
        capture_output=True,
    )
    if url.returncode != 0 or not url.stdout.strip():
        raise ServiceError(
            f"Git remote {remote!r} is not configured; add a remote before running setup"
        )

    probe = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", remote, "HEAD"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip()
        raise ServiceError(f"cannot reach Git remote {remote!r}: {detail}")
    return url.stdout.strip()


def setup_project(
    path: str = ".",
    *,
    install_background: bool = True,
    browser: bool = True,
    browser_mode: str = "auto",
) -> int:
    try:
        repo = find_repo(path)
        init_project(str(repo))
        _set_browser_enabled(repo, browser)
        layout = runtime_layout(repo)
        remote_url = _check_remote(repo, layout.remote)
        _ensure_remote_control_branch(layout)

        browser_info: dict[str, object] | None = None
        chat_info: dict[str, object] | None = None
        if browser:
            browser_info = setup_browser(
                mode=browser_mode,
                run_iteration_test=False,
            )
            try:
                chat_info = ensure_project_chat(repo, remote_url=remote_url, control_branch=layout.branch)
            except BrowserError:
                if browser_mode != "auto" or browser_info.get("mode") != "headless":
                    raise
                browser_info = use_background_fallback()
                chat_info = ensure_project_chat(repo, remote_url=remote_url, control_branch=layout.branch)

        if install_background:
            value = install_service(repo)
            if not value.get("installed") or not value.get("running"):
                raise ServiceError("background service did not start; run do-again status for diagnostics")
            if browser:
                browser_info = ensure_browser_running(verify_auth=True)
        else:
            value = service_status(repo)
    except (
        ServiceError,
        BrowserError,
        BrowserAuthRequired,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"do-again: setup failed: {exc}", file=sys.stderr)
        return 1

    print("SETUP_CONFIGURED")
    print("verification=not_run")
    print(f"repo={repo}")
    print(f"remote={layout.remote}")
    print(f"remote_url={remote_url}")
    print(f"control_branch={layout.branch}")
    print(f"config={repo / 'do-again.toml'}")
    print(f"policy={repo / 'do-again-policy.json'}")
    print(f"browser_enabled={browser}")
    if browser_info is not None:
        print(f"browser_mode={browser_info.get('mode') or browser_info.get('resolved_mode')}")
        print(f"browser_running={browser_info.get('running')}")
        print(f"browser_authenticated={browser_info.get('authenticated')}")
    if chat_info is not None:
        print(f"automation_chat={chat_info.get('chat_url')}")
    if install_background:
        print(f"background_installed={value.get('installed')}")
        print(f"background_running={value.get('running')}")
        print("next=do-again status")
    else:
        print("background_installed=false")
        print("next=do-again run")
    return 0


def verify_project(path: str = ".", *, timeout_seconds: float = 90.0) -> int:
    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)
        if not layout.browser_enabled:
            raise ServiceError(
                "end-to-end verification requires browser automation; "
                "enable it with do-again setup"
            )
        service = service_status(repo)
        if not service.get("running"):
            raise ServiceError("background service is not running; run do-again start")
        record = project_record(repo)
        chat_url = str(record.get("chat_url") or "")
        if not chat_url:
            raise ServiceError("project has no bound automation chat; run do-again setup")
        browser = ensure_browser_running(verify_auth=True)
        port = int(browser.get("port") or 0)
        if not port:
            raise BrowserError("automation browser did not report a CDP port")

        request_id = "verify-" + uuid.uuid4().hex[:16]
        issued = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        expires = issued + __import__("datetime").timedelta(minutes=5)
        request = {
            "schema_version": 1,
            "request_id": request_id,
            "operation": "status",
            "issued_at_utc": issued.isoformat(),
            "expires_at_utc": expires.isoformat(),
            "args": {},
            "expected": {},
            "limits": {"timeout_seconds": 30},
        }
        request_path = f"automation/do_again/requests/{request_id}.json"
        receipt_path = f"automation/do_again/receipts/{request_id}.json"
        prompt = (
            "Do Again setup verification. Create exactly one control request at "
            + request_path
            + " on "
            + layout.branch
            + " with this exact JSON, then do not create any other request for this verification: "
            + json.dumps(request, sort_keys=True)
        )

        # Prefer the existing bound project tab. Creating a fresh target
        # navigates asynchronously; sending before it has a JS context fails
        # with 'Cannot find default execution context' even when authenticated.
        target = _find_chatgpt_target(port, chat_url)
        if target is None:
            target = cdp.create_target(port, chat_url, background=True)
        context_deadline = time.monotonic() + 20
        while True:
            try:
                ready = cdp.evaluate(target, "document.readyState", timeout=5.0)
                if ready in {"interactive", "complete"}:
                    break
            except BrowserError as exc:
                # Retry only the read-only readiness probe; never resend a
                # potentially dispatched ChatGPT prompt.
                message = str(exc)
                if not any(part in message for part in (
                    "Cannot find default execution context",
                    "Execution context was destroyed",
                )):
                    raise
            if time.monotonic() >= context_deadline:
                raise BrowserError(
                    "ChatGPT tab did not expose a ready execution context; "
                    "no verification prompt was sent"
                )
            time.sleep(0.4)
        # Do not interrupt an existing coding response. Poll only the
        # conversation state until its composer is available for a new turn.
        idle_deadline = time.monotonic() + min(max(timeout_seconds, 0.0), 45.0)
        while True:
            if not _assistant_snapshot(target).get("busy", False):
                break
            if time.monotonic() >= idle_deadline:
                raise BrowserError(
                    "ChatGPT is still generating; no verification request was sent"
                )
            time.sleep(1.0)
        send_message(target, prompt, timeout=180.0, wait_for_response=False)

        control = layout.control_worktree
        deadline = time.monotonic() + timeout_seconds
        request_seen = False
        last_sync_error = None
        while time.monotonic() < deadline:
            sync = subprocess.run(
                ["git", "-C", str(control), "fetch", "--quiet", layout.remote, layout.branch],
                text=True,
                capture_output=True,
            )
            if sync.returncode == 0:
                # Never reset or switch the live agent's checkout during a
                # verification. Inspect immutable fetched Git objects only.
                fetched = subprocess.run(
                    ["git", "-C", str(control), "rev-parse", "FETCH_HEAD"],
                    text=True, capture_output=True, timeout=10,
                )
                if fetched.returncode != 0:
                    last_sync_error = (fetched.stderr or fetched.stdout).strip()
                    time.sleep(1.0)
                    continue
                head_sha = fetched.stdout.strip()
                if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head_sha):
                    last_sync_error = "Git fetch returned an invalid control commit"
                    time.sleep(1.0)
                    continue
                read_request = subprocess.run(
                    ["git", "-C", str(control), "show", f"{head_sha}:{request_path}"],
                    text=True, capture_output=True, timeout=10,
                )
                request_seen = request_seen or read_request.returncode == 0
                read_receipt = subprocess.run(
                    ["git", "-C", str(control), "show", f"{head_sha}:{receipt_path}"],
                    text=True, capture_output=True, timeout=10,
                )
                if read_receipt.returncode == 0:
                    receipt = json.loads(read_receipt.stdout)
                    if receipt.get("request_id") != request_id:
                        raise ServiceError("verification receipt request_id mismatch")
                    if receipt.get("state") != "succeeded":
                        raise ServiceError(
                            "verification request completed with state "
                            + str(receipt.get("state"))
                            + ": "
                            + str(receipt.get("error") or "no error detail")
                        )
                    print("SETUP_OK")
                    print("verification=end_to_end")
                    print(f"verification_request_id={request_id}")
                    print(f"automation_chat={chat_url}")
                    return 0
            else:
                last_sync_error = (sync.stderr or sync.stdout).strip()
            time.sleep(1.0)

        if not request_seen:
            raise ServiceError(
                "verification timed out before ChatGPT published the control request; "
                "check the bound automation chat and browser session"
            )
        detail = f": {last_sync_error}" if last_sync_error else ""
        raise ServiceError(
            "verification request reached the control branch but no matching receipt "
            "was published before timeout; check do-again status and agent logs" + detail
        )
    except (
        ServiceError,
        BrowserError,
        BrowserAuthRequired,
        subprocess.TimeoutExpired,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"do-again: verify failed: {exc}", file=sys.stderr)
        return 1


def _ensure_project_browser(repo: Path) -> None:
    layout = runtime_layout(repo)
    if not layout.browser_enabled:
        return
    record = project_record(repo)
    if not record.get("chat_url"):
        raise BrowserError(
            "this project has no automation chat yet; run do-again setup first"
        )
    try:
        ensure_browser_running(verify_auth=True)
    except BrowserError as exc:
        print(f"do-again: browser unavailable; local execution will continue: {exc}", file=sys.stderr)

def _read_json_if_file(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def request_history(path: str = ".", *, limit: int = 20) -> int:
    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)
        control = layout.control_worktree
        requests_dir = control / "automation/do_again/requests"
        receipts_dir = control / "automation/do_again/receipts"
        rows: list[dict[str, object]] = []
        ids: set[str] = set()
        if requests_dir.is_dir():
            ids.update(p.stem for p in requests_dir.glob("*.json"))
        if receipts_dir.is_dir():
            ids.update(p.stem for p in receipts_dir.glob("*.json"))
        for request_id in ids:
            request = _read_json_if_file(requests_dir / f"{request_id}.json") or {}
            receipt = _read_json_if_file(receipts_dir / f"{request_id}.json") or {}
            stamp = str(
                receipt.get("finished_at_utc")
                or receipt.get("started_at_utc")
                or request.get("issued_at_utc")
                or ""
            )
            rows.append(
                {
                    "request_id": request_id,
                    "operation": receipt.get("operation") or request.get("operation"),
                    "state": receipt.get("state") or "pending",
                    "timestamp": stamp or None,
                }
            )
        rows.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
        if limit > 0:
            rows = rows[:limit]
        print(json.dumps({"repo": str(repo), "history": rows}, indent=2, sort_keys=True))
        return 0
    except ServiceError as exc:
        print(f"do-again: history: {exc}", file=sys.stderr)
        return 1


def trace_request(request_id: str, path: str = ".") -> int:
    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)
        control = layout.control_worktree
        base = control / "automation/do_again"
        ledger_path = layout.state_dir / "ledger" / f"{request_id}.json"
        conflicts_dir = base / "conflicts"
        conflicts = []
        if conflicts_dir.is_dir():
            for p in sorted(conflicts_dir.glob(f"{request_id}-*.json")):
                value = _read_json_if_file(p)
                if value is not None:
                    conflicts.append(value)
        payload = {
            "repo": str(repo),
            "request_id": request_id,
            "request": _read_json_if_file(base / "requests" / f"{request_id}.json"),
            "claim": _read_json_if_file(base / "claims" / f"{request_id}.json"),
            "ledger": _read_json_if_file(ledger_path),
            "receipt": _read_json_if_file(base / "receipts" / f"{request_id}.json"),
            "conflicts": conflicts,
        }
        if not any(
            payload[key]
            for key in ("request", "claim", "ledger", "receipt", "conflicts")
        ):
            print(f"do-again: trace: unknown request id: {request_id}", file=sys.stderr)
            return 2
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except ServiceError as exc:
        print(f"do-again: trace: {exc}", file=sys.stderr)
        return 1


def _tail_text(path: Path, lines: int) -> str:
    if not path.is_file():
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if lines <= 0:
        return "\n".join(data)
    return "\n".join(data[-lines:])


def show_logs(
    path: str = ".",
    *,
    follow: bool = False,
    lines: int = 100,
    stream: str = "both",
) -> int:
    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)
        selected = []
        if stream in {"stdout", "both"}:
            selected.append(("stdout", layout.stdout_log))
        if stream in {"stderr", "both"}:
            selected.append(("stderr", layout.stderr_log))
        for label, log_path in selected:
            print(f"== {label}: {log_path} ==")
            tail = _tail_text(log_path, lines)
            if tail:
                print(tail)
        if not follow:
            return 0

        positions = {}
        for _, log_path in selected:
            try:
                positions[log_path] = log_path.stat().st_size
            except OSError:
                positions[log_path] = 0
        try:
            while True:
                for label, log_path in selected:
                    try:
                        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                            handle.seek(positions[log_path])
                            chunk = handle.read()
                            positions[log_path] = handle.tell()
                    except OSError:
                        continue
                    if chunk:
                        for line in chunk.rstrip("\n").splitlines():
                            print(f"[{label}] {line}", flush=True)
                time.sleep(0.5)
        except KeyboardInterrupt:
            return 0
    except ServiceError as exc:
        print(f"do-again: logs: {exc}", file=sys.stderr)
        return 1




def _git_control(layout, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(layout.control_worktree), *args],
        text=True,
        capture_output=True,
    )


def _sync_control(layout) -> None:
    dirty = _git_control(layout, "status", "--porcelain")
    if dirty.returncode != 0:
        raise ServiceError((dirty.stderr or dirty.stdout).strip())
    if dirty.stdout.strip():
        raise ServiceError("operator control worktree is dirty")
    fetch = _git_control(layout, "fetch", "--quiet", layout.remote, layout.branch)
    if fetch.returncode != 0:
        raise ServiceError((fetch.stderr or fetch.stdout).strip())
    rebase = _git_control(layout, "rebase", "FETCH_HEAD")
    if rebase.returncode != 0:
        raise ServiceError((rebase.stderr or rebase.stdout).strip())


def _publish_control_json(layout, relative: str, value: dict[str, object], message: str) -> None:
    _sync_control(layout)
    target = layout.control_worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    add = _git_control(layout, "add", relative)
    if add.returncode != 0:
        raise ServiceError((add.stderr or add.stdout).strip())
    commit = _git_control(layout, "commit", "-m", message)
    if commit.returncode != 0:
        raise ServiceError((commit.stderr or commit.stdout).strip())
    push = _git_control(layout, "push", layout.remote, f"HEAD:{layout.branch}")
    if push.returncode != 0:
        raise ServiceError((push.stderr or push.stdout).strip())


def cancel_request(request_id: str, path: str = ".", *, reason: str | None = None) -> int:
    try:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ServiceError("invalid request id")
        repo = find_repo(path)
        layout = runtime_layout(repo)
        _sync_control(layout)
        base = layout.control_worktree / "automation/do_again"
        request = _read_json_if_file(base / "requests" / f"{request_id}.json")
        if request is None:
            raise ServiceError(f"unknown request id: {request_id}")
        receipt = _read_json_if_file(base / "receipts" / f"{request_id}.json")
        claim = _read_json_if_file(base / "claims" / f"{request_id}.json")
        ledger = _read_json_if_file(layout.state_dir / "ledger" / f"{request_id}.json")
        if receipt is not None:
            raise ServiceError("request is already terminal and cannot be cancelled")
        if claim is not None:
            raise ServiceError("request is already claimed and cannot be safely cancelled")
        if isinstance(ledger, dict) and ledger.get("state") in {"started", "terminal"}:
            raise ServiceError(
                "request has local execution state and cannot be safely cancelled"
            )
        existing = _read_json_if_file(base / "cancellations" / f"{request_id}.json")
        if existing is not None:
            print(json.dumps(existing, indent=2, sort_keys=True))
            return 0
        payload = {
            "schema_version": 1,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint(request),
            "state": "cancelled_before_execution",
            "reason": reason or "cancelled by operator",
            "cancelled_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        _publish_control_json(
            layout,
            f"automation/do_again/cancellations/{request_id}.json",
            payload,
            f"Do Again cancel {request_id}",
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except ServiceError as exc:
        print(f"do-again: cancel: {exc}", file=sys.stderr)
        return 1


def retry_request(request_id: str, path: str = ".") -> int:
    try:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise ServiceError("invalid request id")
        repo = find_repo(path)
        layout = runtime_layout(repo)
        _sync_control(layout)
        base = layout.control_worktree / "automation/do_again"
        request = _read_json_if_file(base / "requests" / f"{request_id}.json")
        if request is None:
            raise ServiceError(f"unknown request id: {request_id}")
        receipt = _read_json_if_file(base / "receipts" / f"{request_id}.json")
        ledger = _read_json_if_file(layout.state_dir / "ledger" / f"{request_id}.json")
        if receipt is None:
            raise ServiceError("request is not terminal; use trace before deciding to retry")
        state = str(receipt.get("state") or "")
        if state == "succeeded":
            raise ServiceError("successful requests are not retryable")
        if state == "blocked_ambiguous_replay":
            raise ServiceError(
                "ambiguous replay requires a fresh manually scoped request after inspection"
            )
        if isinstance(ledger, dict) and ledger.get("state") != "terminal":
            raise ServiceError("local ledger is not terminal; refusing retry")

        issued = datetime.now(timezone.utc)
        old_issued = request.get("issued_at_utc")
        old_expires = request.get("expires_at_utc")
        ttl = timedelta(minutes=10)
        try:
            oi = datetime.fromisoformat(str(old_issued).replace("Z", "+00:00"))
            oe = datetime.fromisoformat(str(old_expires).replace("Z", "+00:00"))
            candidate = oe - oi
            if timedelta(seconds=1) < candidate <= timedelta(hours=24):
                ttl = candidate
        except Exception:
            pass
        new_id = f"retry-{uuid.uuid4().hex[:16]}"
        clone = dict(request)
        clone["request_id"] = new_id
        clone["issued_at_utc"] = issued.isoformat()
        clone["expires_at_utc"] = (issued + ttl).isoformat()
        continuation = dict(clone.get("continuation") or {})
        acked = list(continuation.get("acknowledged_receipts") or [])
        if request_id not in acked:
            acked.append(request_id)
        continuation["acknowledged_receipts"] = acked
        continuation["goal_state"] = "in_progress"
        summary = str(continuation.get("summary") or "").strip()
        linkage = f"Retry of {request_id} after terminal state {state}."
        continuation["summary"] = (summary + " " + linkage).strip()[:2000]
        clone["continuation"] = continuation

        _publish_control_json(
            layout,
            f"automation/do_again/requests/{new_id}.json",
            clone,
            f"Do Again retry {request_id} as {new_id}",
        )
        print(json.dumps({
            "original_request_id": request_id,
            "new_request_id": new_id,
            "previous_state": state,
            "request": clone,
        }, indent=2, sort_keys=True))
        return 0
    except ServiceError as exc:
        print(f"do-again: retry: {exc}", file=sys.stderr)
        return 1


def _service_action(action: str, path: str) -> int:
    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)
        report_since = session_start(layout) if action == "stop" else None

        if action in {"install", "start", "restart"}:
            _ensure_project_browser(repo)

        if action == "install":
            value = install_service(repo)
        elif action == "start":
            current = service_status(repo)
            if current.get("running"):
                value = current
            elif current.get("installed"):
                value = restart_service(repo)
            else:
                value = install_service(repo)
        elif action == "stop":
            value = stop_service(repo)
            if layout.browser_enabled:
                deactivate_project(repo)
                stop_if_unused()
        elif action == "restart":
            value = restart_service(repo)
        elif action == "uninstall":
            value = uninstall_service(repo)
            if layout.browser_enabled:
                deactivate_project(repo)
                stop_if_unused()
        else:
            raise ServiceError(f"unsupported service action: {action}")
    except (ServiceError, BrowserError, BrowserAuthRequired) as exc:
        print(f"do-again: {exc}", file=sys.stderr)
        return 1
    _print_service_status(value)
    if action == "stop" and report_since is not None:
        try:
            data = build_summary(layout, since=report_since)
            content = render_summary(data)
            saved = save_summary(layout, data, content)
            print("\n" + content + f"Saved recap: {saved}")
        except (OSError, ValueError) as exc:
            # Stopping safely always takes priority over a best-effort recap.
            print(f"do-again: session recap unavailable: {exc}", file=sys.stderr)
    return 0


def project_summary(path: str = ".", *, hours: float = 24.0, json_output: bool = False) -> int:
    if not (0 < hours <= 24 * 365):
        print("do-again: summary: hours must be between 0 and 8760", file=sys.stderr)
        return 2
    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)
        now = datetime.now(timezone.utc)
        data = build_summary(layout, since=now - timedelta(hours=hours), now=now)
        print(json.dumps(data, indent=2, sort_keys=True) if json_output else render_summary(data))
        return 0
    except (ServiceError, OSError, ValueError) as exc:
        print(f"do-again: summary: {exc}", file=sys.stderr)
        return 1


def list_projects() -> int:
    home = Path(
        os.environ.get("DO_AGAIN_HOME", str(Path.home() / ".do_again"))
    ).expanduser().resolve()
    projects_root = home / "projects"
    rows: list[tuple[str, str, str, str]] = []
    if projects_root.is_dir():
        for metadata_path in sorted(projects_root.glob("*/runtime.json")):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            repo_text = str(metadata.get("repo") or "")
            if not repo_text:
                continue
            repo = Path(repo_text)
            name = repo.name or repo_text
            service = "missing"
            try:
                value = service_status(repo)
                if value.get("running"):
                    service = "running"
                elif value.get("installed"):
                    service = "stopped"
                else:
                    service = "not-installed"
            except Exception:
                service = "unavailable"

            browser = "off"
            if bool(metadata.get("browser_enabled")):
                record = project_record(repo)
                shared = browser_status(verify_session=False)
                if record.get("chat_url") and shared.get("running"):
                    browser = str(shared.get("mode") or "running")
                elif record.get("chat_url"):
                    browser = "stopped"
                else:
                    browser = "not-configured"
            rows.append((name, service, browser, repo_text))

    if not rows:
        print("No Do Again projects found.")
        return 0

    print("PROJECT\tSERVICE\tBROWSER\tREPOSITORY")
    for name, service, browser, repo in rows:
        print(f"{name}\t{service}\t{browser}\t{repo}")
    return 0


def _browser_action(action: str, *, mode: str = "auto", run_test: bool = True) -> int:
    try:
        if action == "status":
            value = browser_status(verify_session=True)
            for key in (
                "configured", "running", "pid", "port", "mode",
                "profile_dir", "browser_binary", "authenticated",
                "session_ready", "auth_required", "chat_url", "error",
            ):
                if key in value:
                    print(f"{key}={value.get(key)}")
            return 0 if not value.get("auth_required") else 2
        if action == "login":
            value = setup_browser(mode=mode, run_iteration_test=run_test)
            print(f"browser_mode={value.get('mode') or value.get('resolved_mode')}")
            print(f"browser_running={value.get('running')}")
            print(f"browser_authenticated={value.get('authenticated')}")
            return 0
        if action == "start":
            value = ensure_browser_running(verify_auth=True)
            print(f"browser_mode={value.get('mode')}")
            print(f"browser_running={value.get('running')}")
            print(f"browser_authenticated={value.get('authenticated')}")
            return 0
        if action == "stop":
            stop_browser(force=True)
            print("browser_running=False")
            return 0
        if action == "test":
            value = browser_self_test()
            print("BROWSER_TEST_OK")
            print(f"chat_url={value.get('chat_url')}")
            return 0
        raise BrowserError(f"unsupported browser action: {action}")
    except (BrowserError, BrowserAuthRequired) as exc:
        print(f"do-again: browser: {exc}", file=sys.stderr)
        return 1


def _model_action(action: str, *, allow_model_call: bool = False,
                  nonce: str | None = None, task: int | None = None,
                  expected_head: str | None = None) -> int:
    from .model_transport import (
        CodexTransportBlocked,
        discover_codex,
        generate_structured,
        login_ready,
    )
    binary = discover_codex()
    if action == "status":
        status = {"transport": "codex", "installed": binary is not None,
                  "authenticated": login_ready(binary) if binary else False,
                  "browser_required": False, "production_enabled": False}
        print(json.dumps(status, sort_keys=True))
        return 0 if status["authenticated"] else 2
    if action == "canary-draft":
        from .model_canary import (
            CodexCanaryProposalRejected,
            canary_model_schema,
            canary_task_prompt,
            prepare_canary_edit,
        )
        try:
            if not allow_model_call:
                raise CodexTransportBlocked("canary draft requires explicit model-call authorization")
            prompt = canary_task_prompt(nonce=nonce, task=task)
            proposal = generate_structured(
                prompt, canary_model_schema(),
                allow_model_call=True, binary=binary, timeout_seconds=180,
            )
            request = prepare_canary_edit(
                proposal, nonce=nonce, task=task, expected_head=expected_head,
            )
        except (CodexTransportBlocked, CodexCanaryProposalRejected) as exc:
            print(f"do-again: model: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({
            "state": "proposal_only",
            "published": False,
            "executed": False,
            "request": request,
        }, indent=2, sort_keys=True))
        return 0
    if action == "smoke":
        try:
            response = generate_structured(
                "Return exactly the word ready in the reply field. Do not read files, use tools, or perform other actions.",
                {"type": "object",
                 "properties": {"reply": {"type": "string", "maxLength": 64}},
                 "required": ["reply"], "additionalProperties": False},
                allow_model_call=allow_model_call,
                binary=binary,
                timeout_seconds=120,
            )
        except CodexTransportBlocked as exc:
            print(f"do-again: model: {exc}", file=sys.stderr)
            return 1
        if response.get("reply", "").strip().lower() != "ready":
            print("do-again: model: structured response did not match smoke marker", file=sys.stderr)
            return 1
        print("CODEX_READ_ONLY_SMOKE_OK")
        return 0
    print("do-again: model: unknown transport action", file=sys.stderr)
    return 2


def _chat_cleanup_action(
    action: str,
    path: str,
    *,
    candidates: list[str] | None = None,
    apply: bool = False,
) -> int:
    try:
        repo = find_repo(path)
        if action == "discover":
            value = chat_cleanup_inventory(repo, candidate_ids=candidates or [])
        elif action == "status":
            value = archive_queue_status(repo)
        elif action == "cleanup":
            value = queue_verified_archives(
                repo,
                candidate_ids=candidates or [],
                apply=apply,
            )
        elif action == "verify":
            rows = [
                verify_candidate_chat(repo, chat_id)
                for chat_id in (candidates or [])
            ]
            value = {"repo": str(repo), "results": rows}
        elif action == "run":
            value = process_archive_queue(repo)
        else:
            raise BrowserError(f"unsupported chats action: {action}")
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    except (BrowserError, ServiceError) as exc:
        print(f"do-again: chats: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="do-again",
        description="Policy-controlled local execution for AI coding agents.",
        epilog="Start here: do-again setup",
    )
    sub = parser.add_subparsers(
        dest="command",
        metavar="{setup,verify,start,status,summary,history,trace,logs,cancel,retry,stop,restart,upgrade,list,doctor,browser}",
    )

    doctor_parser = sub.add_parser(
        "doctor", help="Check runtime prerequisites and optionally repair safe local state"
    )
    doctor_parser.add_argument("path", nargs="?", default=".")
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="Repair safe deterministic state only; never reinstall/restart services or bypass browser auth",
    )

    setup_parser = sub.add_parser(
        "setup",
        help="Set up the project, ChatGPT browser runtime, and background agent",
    )
    setup_parser.add_argument("path", nargs="?", default=".")
    setup_parser.add_argument(
        "--no-service",
        action="store_true",
        help="Configure the project without installing a background service",
    )
    setup_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip the ChatGPT browser runtime",
    )
    setup_parser.add_argument(
        "--browser-mode",
        choices=("auto", "headless", "background"),
        default="auto",
        help="Advanced: choose browser runtime mode (default: auto)",
    )
    verify_parser = sub.add_parser(
        "verify",
        help="Verify the ChatGPT-to-control-to-local-to-receipt round trip",
    )
    verify_parser.add_argument("path", nargs="?", default=".")
    verify_parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Maximum seconds to wait for the end-to-end verification receipt",
    )
    status_parser = sub.add_parser("status", help="Show project, service, and browser status")
    status_parser.add_argument("path", nargs="?", default=".")
    summary_parser = sub.add_parser(
        "summary", help="Read a concise, receipt-grounded work recap without stopping the service"
    )
    summary_parser.add_argument("path", nargs="?", default=".")
    summary_parser.add_argument("--hours", type=float, default=24.0)
    summary_parser.add_argument("--json", action="store_true", help="Machine-readable local report")
    upgrade_parser = sub.add_parser(
        "upgrade",
        help="Safely stage and roll out this project's copied service runtime",
    )
    upgrade_parser.add_argument("path", nargs="?", default=".")
    upgrade_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only when the project is provably quiescent; otherwise defer without restart",
    )
    upgrade_parser.add_argument(
        "--offline",
        action="store_true",
        help="Stage only a stopped project's runtime, preserving unstarted queued requests without starting service",
    )
    history_parser = sub.add_parser(
        "history", help="Show recent durable Do Again request history"
    )
    history_parser.add_argument("path", nargs="?", default=".")
    history_parser.add_argument("--limit", type=int, default=20)
    trace_parser = sub.add_parser(
        "trace", help="Show request, claim, ledger, receipt, and conflict state"
    )
    trace_parser.add_argument("request_id")
    trace_parser.add_argument("path", nargs="?", default=".")
    logs_parser = sub.add_parser(
        "logs", help="Show agent logs; use --follow to stream new lines"
    )
    logs_parser.add_argument("path", nargs="?", default=".")
    logs_parser.add_argument("--follow", action="store_true")
    logs_parser.add_argument("--lines", type=int, default=100)
    logs_parser.add_argument(
        "--stream", choices=("stdout", "stderr", "both"), default="both"
    )
    cancel_parser = sub.add_parser(
        "cancel", help="Cancel a request only if it is still demonstrably unclaimed"
    )
    cancel_parser.add_argument("request_id")
    cancel_parser.add_argument("path", nargs="?", default=".")
    cancel_parser.add_argument("--reason")
    retry_parser = sub.add_parser(
        "retry", help="Clone a terminal failed/blocked request into a fresh request ID"
    )
    retry_parser.add_argument("request_id")
    retry_parser.add_argument("path", nargs="?", default=".")

    init_parser = sub.add_parser("init")
    init_parser.add_argument("path", nargs="?", default=".")

    for name, help_text in (
        ("start", "Start this project's Do Again runtime"),
        ("stop", "Stop this project's Do Again runtime"),
        ("restart", "Restart this project's Do Again runtime"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("path", nargs="?", default=".")

    sub.add_parser("list", help="Show all configured Do Again projects")

    for name in ("install", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("path", nargs="?", default=".")

    chats_parser = sub.add_parser(
        "chats",
        help="Discover and safely clean up Do Again-owned automation chats",
    )
    chats_sub = chats_parser.add_subparsers(dest="chats_command", required=True)
    chats_discover = chats_sub.add_parser(
        "discover",
        help="Dry-run chat ownership/provenance inventory",
    )
    chats_discover.add_argument("path", nargs="?", default=".")
    chats_discover.add_argument("--candidate", action="append", default=[])
    chats_status = chats_sub.add_parser(
        "status",
        help="Show persisted archive queue status",
    )
    chats_status.add_argument("path", nargs="?", default=".")
    chats_verify = chats_sub.add_parser(
        "verify",
        help="Verify historical candidates using bootstrap markers plus receipts",
    )
    chats_verify.add_argument("path", nargs="?", default=".")
    chats_verify.add_argument("--candidate", action="append", required=True)
    chats_run = chats_sub.add_parser(
        "run",
        help="Process the persisted archive queue for verified-owned inactive chats",
    )
    chats_run.add_argument("path", nargs="?", default=".")
    chats_cleanup = chats_sub.add_parser(
        "cleanup",
        help="Queue verified-owned inactive chats; dry-run unless --apply is used",
    )
    chats_cleanup.add_argument("path", nargs="?", default=".")
    chats_cleanup.add_argument("--candidate", action="append", default=[])
    chats_cleanup.add_argument("--apply", action="store_true")

    run_parser = sub.add_parser("run")
    run_parser.add_argument("path", nargs="?", default=".")
    run_parser.add_argument("--once", action="store_true")

    model_parser = sub.add_parser(
        "model",
        help="Optional non-browser Codex transport readiness and read-only inference smoke",
    )
    model_sub = model_parser.add_subparsers(dest="model_command", required=True)
    model_sub.add_parser("status", help="Check Codex CLI and ChatGPT login without model usage")
    model_smoke = model_sub.add_parser("smoke", help="One explicit, read-only Codex inference")
    model_smoke.add_argument("--allow-model-call", action="store_true",
        help="Explicitly authorize one model call against your Codex usage allowance")
    model_canary = model_sub.add_parser("canary-draft",
        help="Prepare but never publish one exact-head synthetic canary edit request")
    model_canary.add_argument("--nonce", required=True)
    model_canary.add_argument("--task", type=int, choices=(1, 2), required=True)
    model_canary.add_argument("--head", required=True)
    model_canary.add_argument("--allow-model-call", action="store_true",
        help="Explicitly authorize one non-browser model proposal")

    browser_parser = sub.add_parser(
        "browser",
        help="Advanced browser runtime diagnostics and controls",
    )
    browser_sub = browser_parser.add_subparsers(dest="browser_command", required=True)
    browser_sub.add_parser("status", help="Inspect browser/session health")
    browser_sub.add_parser("start", help="Start the shared automation browser")
    browser_sub.add_parser("stop", help="Stop the shared automation browser")
    browser_sub.add_parser("test", help="Run a one-message ChatGPT browser self-test")
    browser_login = browser_sub.add_parser(
        "login",
        help="Open the dedicated profile for interactive authentication",
    )
    browser_login.add_argument(
        "--mode",
        choices=("auto", "headless", "background"),
        default="auto",
    )
    browser_login.add_argument("--skip-test", action="store_true")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return doctor(args.path, fix=args.fix)
    if args.command == "setup":
        return setup_project(
            args.path,
            install_background=not args.no_service,
            browser=not args.no_browser,
            browser_mode=args.browser_mode,
        )
    if args.command == "verify":
        return verify_project(args.path, timeout_seconds=args.timeout)
    if args.command == "status":
        return status(args.path)
    if args.command == "summary":
        return project_summary(args.path, hours=args.hours, json_output=args.json)
    if args.command == "upgrade":
        try:
            value = staged_upgrade(args.path, apply=args.apply, offline=args.offline)
        except ServiceError as exc:
            print(f"do-again: upgrade: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0 if value.get("applied") or not args.apply else 2
    if args.command == "history":
        return request_history(args.path, limit=args.limit)
    if args.command == "trace":
        return trace_request(args.request_id, args.path)
    if args.command == "logs":
        return show_logs(
            args.path,
            follow=args.follow,
            lines=args.lines,
            stream=args.stream,
        )
    if args.command == "cancel":
        return cancel_request(args.request_id, args.path, reason=args.reason)
    if args.command == "retry":
        return retry_request(args.request_id, args.path)
    if args.command == "list":
        return list_projects()
    if args.command == "init":
        return init_project(args.path)
    if args.command in {"start", "stop", "restart", "install", "uninstall"}:
        return _service_action(args.command, args.path)
    if args.command == "model":
        return _model_action(
            args.model_command,
            allow_model_call=bool(getattr(args, "allow_model_call", False)),
            nonce=getattr(args, "nonce", None),
            task=getattr(args, "task", None),
            expected_head=getattr(args, "head", None),
        )
    if args.command == "browser":
        return _browser_action(
            args.browser_command,
            mode=getattr(args, "mode", "auto"),
            run_test=not getattr(args, "skip_test", False),
        )
    if args.command == "chats":
        return _chat_cleanup_action(
            args.chats_command,
            args.path,
            candidates=getattr(args, "candidate", []),
            apply=bool(getattr(args, "apply", False)),
        )
    if args.command == "run":
        try:
            return run_foreground(args.path, once=args.once)
        except ServiceError as exc:
            print(f"do-again: {exc}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
