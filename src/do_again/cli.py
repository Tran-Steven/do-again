from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .platforms.detect import detect_platform
from .service.runtime import (
    ServiceError,
    _default_policy,
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
    for key, ok in checks.items():
        print(f"{'OK' if ok else 'FAIL'} {key}")
    print(f"platform_name={info.name}")
    print(f"service_manager={info.service_manager}")
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
    except ServiceError as exc:
        print(f"do-again: {exc}", file=sys.stderr)
        return 1
    _print_service_status(value)
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
            'policy = "do-again-policy.json"\n',
            encoding="utf-8",
        )
    print(str(config))
    return 0


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


def setup_project(path: str = ".", *, install_background: bool = True) -> int:
    try:
        repo = find_repo(path)
        init_project(str(repo))
        layout = runtime_layout(repo)
        remote_url = _check_remote(repo, layout.remote)
        if install_background:
            value = install_service(repo)
        else:
            value = service_status(repo)
    except (ServiceError, subprocess.TimeoutExpired) as exc:
        print(f"do-again: setup failed: {exc}", file=sys.stderr)
        return 1

    print("SETUP_OK")
    print(f"repo={repo}")
    print(f"remote={layout.remote}")
    print(f"remote_url={remote_url}")
    print(f"control_branch={layout.branch}")
    print(f"config={repo / 'do-again.toml'}")
    print(f"policy={repo / 'do-again-policy.json'}")
    if install_background:
        print(f"background_installed={value.get('installed')}")
        print(f"background_running={value.get('running')}")
        print("next=do-again status")
    else:
        print("background_installed=false")
        print("next=do-again run")
    return 0


def _service_action(action: str, path: str) -> int:
    try:
        if action == "install":
            value = install_service(path)
        elif action == "stop":
            value = stop_service(path)
        elif action == "restart":
            value = restart_service(path)
        elif action == "uninstall":
            value = uninstall_service(path)
        else:
            raise ServiceError(f"unsupported service action: {action}")
    except ServiceError as exc:
        print(f"do-again: {exc}", file=sys.stderr)
        return 1
    _print_service_status(value)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="do-again",
        description="Policy-controlled local execution for AI coding agents.",
        epilog="Start here: do-again setup",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="Check runtime prerequisites")

    setup_parser = sub.add_parser(
        "setup",
        help="Initialize this repository and start the background agent",
    )
    setup_parser.add_argument("path", nargs="?", default=".")
    setup_parser.add_argument(
        "--no-service",
        action="store_true",
        help="Configure the project without installing a background service",
    )

    status_parser = sub.add_parser("status", help="Show project and service status")
    status_parser.add_argument("path", nargs="?", default=".")

    init_parser = sub.add_parser(
        "init",
        help="Create project config and policy files only",
    )
    init_parser.add_argument("path", nargs="?", default=".")

    for name, help_text in (
        ("install", "Install or refresh the background agent"),
        ("stop", "Stop the background agent"),
        ("restart", "Restart the background agent"),
        ("uninstall", "Remove the background agent"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("path", nargs="?", default=".")

    run_parser = sub.add_parser("run", help="Run the agent in the foreground")
    run_parser.add_argument("path", nargs="?", default=".")
    run_parser.add_argument("--once", action="store_true")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "doctor":
        return doctor()
    if args.command == "setup":
        return setup_project(args.path, install_background=not args.no_service)
    if args.command == "status":
        return status(args.path)
    if args.command == "init":
        return init_project(args.path)
    if args.command in {"install", "stop", "restart", "uninstall"}:
        return _service_action(args.command, args.path)
    if args.command == "run":
        try:
            return run_foreground(args.path, once=args.once)
        except ServiceError as exc:
            print(f"do-again: {exc}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
