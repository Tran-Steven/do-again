from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .platforms.detect import detect_platform
from .service.runtime import (
    ServiceError,
    find_repo,
    install_service,
    restart_service,
    run_foreground,
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
    if config.exists():
        print(str(config))
        return 0
    config.write_text('[do_again]\ncontrol_branch = "operator-control"\n')
    print(str(config))
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
    parser = argparse.ArgumentParser(prog="do-again")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")

    status_parser = sub.add_parser("status")
    status_parser.add_argument("path", nargs="?", default=".")

    init_parser = sub.add_parser("init")
    init_parser.add_argument("path", nargs="?", default=".")

    for name in ("install", "stop", "restart", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("path", nargs="?", default=".")

    run_parser = sub.add_parser("run")
    run_parser.add_argument("path", nargs="?", default=".")
    run_parser.add_argument("--once", action="store_true")

    args = parser.parse_args()
    if args.command == "doctor":
        return doctor()
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
