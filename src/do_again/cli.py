from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .platforms.detect import detect_platform


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


def status() -> int:
    info = detect_platform()
    print(f"platform={info.name}")
    print(f"service_manager={info.service_manager}")
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="do-again")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("status")
    init_parser = sub.add_parser("init")
    init_parser.add_argument("path", nargs="?", default=".")
    args = parser.parse_args()
    if args.command == "doctor":
        return doctor()
    if args.command == "status":
        return status()
    if args.command == "init":
        return init_project(args.path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
