from __future__ import annotations

import argparse
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
from .browser.runtime import send_message, use_background_fallback
from .service.runtime import (
    ServiceError,
    _default_policy,
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


def doctor() -> int:
    info = detect_platform()
    checks = {
        "python": sys.version_info >= (3, 11),
        "git": shutil.which("git") is not None,
        "platform": info.supported,
    }
    browser_error: str | None = None
    try:
        repo = find_repo(".")
        layout = runtime_layout(repo)
    except ServiceError:
        layout = None
    if layout is not None and layout.browser_enabled:
        try:
            browser_binary = discover_browser()
            checks["browser"] = True
        except BrowserError as exc:
            checks["browser"] = False
            browser_binary = None
            browser_error = str(exc)

    for key, ok in checks.items():
        print(f"{'OK' if ok else 'FAIL'} {key}")
    print(f"platform_name={info.name}")
    print(f"service_manager={info.service_manager}")
    if layout is not None:
        print(f"repo={layout.repo}")
        print(f"browser_enabled={layout.browser_enabled}")
    if layout is not None and layout.browser_enabled:
        if browser_binary is not None:
            print(f"browser_binary={browser_binary}")
        if browser_error:
            print(f"browser_fix={browser_error}")
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

        target = cdp.create_target(port, chat_url, background=True)
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
                reset = subprocess.run(
                    ["git", "-C", str(control), "reset", "--hard", "FETCH_HEAD"],
                    text=True,
                    capture_output=True,
                )
                if reset.returncode != 0:
                    last_sync_error = (reset.stderr or reset.stdout).strip()
                else:
                    request_seen = request_seen or (control / request_path).is_file()
                    receipt_file = control / receipt_path
                    if receipt_file.is_file():
                        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
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


def _service_action(action: str, path: str) -> int:
    try:
        repo = find_repo(path)
        layout = runtime_layout(repo)

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
    return 0


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
        metavar="{setup,verify,start,status,stop,restart,list,doctor,browser}",
    )

    sub.add_parser("doctor", help="Check runtime prerequisites")

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
        return doctor()
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
    if args.command == "list":
        return list_projects()
    if args.command == "init":
        return init_project(args.path)
    if args.command in {"start", "stop", "restart", "install", "uninstall"}:
        return _service_action(args.command, args.path)
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
