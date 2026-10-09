"""Sealed operator-owned daemon entrypoint. No legacy source/runtime fallback.

The root broker's production-ready and active intent checks are mandatory.
This entrypoint never writes supervisor authority or starts a service itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from ..core.schema import OperatorError
from .macos_execution import INSTALL_ROOT, load_configuration
from .macos_client import broker_request

PROJECT_ACCOUNTS = {"do-again": "_doagain_da", "jobpipe": "_doagain_jp"}
WORKER_OPERATIONS = frozenset({
    "status", "run_tests", "repo_script", "scratch_script", "extended_exec",
    "git_commit", "git_publish", "dependency_install", "git_publication_reconcile",
})
SHA40 = re.compile(r"[0-9a-f]{40}\Z")


def validate_worker_context(
    project_name: str,
    config: dict[str, Any],
    status: dict[str, Any],
    policy: dict[str, Any],
    *,
    installed_root: Path,
    module_path: Path,
    interpreter: Path,
    uid: int,
    euid: int,
) -> tuple[Path, Path, Path, Path]:
    """Pure admission decision; zero disk/network/process effects."""
    if project_name not in PROJECT_ACCOUNTS:
        raise OperatorError("unrecognized sealed worker project")
    if type(uid) is not int or type(euid) is not int or uid == 0 or euid != uid:
        raise OperatorError("worker requires its unprivileged operator identity")
    if config.get("operator_uid") != uid:
        raise OperatorError("worker operator identity differs from the installed supervisor")
    current = installed_root / "current"
    if module_path != current / "package/do_again/supervisor/immutable_worker.py":
        raise OperatorError("worker module was not loaded from the sealed installation")
    if interpreter != current / "runtimes/python/bin/python3" or config.get("python") != str(interpreter):
        raise OperatorError("worker interpreter is not the immutable installed runtime")
    projects = config.get("projects")
    if not isinstance(projects, list) or len(projects) != 2:
        raise OperatorError("worker requires exactly two installed project scopes")
    accounts = [p.get("account") for p in projects if isinstance(p, dict)]
    if len(accounts) != 2 or set(accounts) != set(PROJECT_ACCOUNTS.values()):
        raise OperatorError("worker installation project identities differ")
    project = next(p for p in projects if p["account"] == PROJECT_ACCOUNTS[project_name])
    repo_value = project.get("repo")
    home_value = config.get("operator_home")
    if not isinstance(repo_value, str) or not Path(repo_value).is_absolute():
        raise OperatorError("worker repository is not an exact absolute installation binding")
    if not isinstance(home_value, str) or not Path(home_value).is_absolute():
        raise OperatorError("worker home is not an exact absolute installation binding")
    repo = Path(repo_value)
    key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    base = Path(home_value) / ".do_again" / "projects" / key
    control, state = base / "control", base / "state"
    policy_path = current / "package/do_again/worker_policy.json"
    if (policy.get("worker_policy_schema") != 1
            or policy.get("control_branch") != "operator-control"
            or not isinstance(policy.get("allowed_operations"), list)
            or len(policy["allowed_operations"]) != len(WORKER_OPERATIONS)
            or set(policy["allowed_operations"]) != WORKER_OPERATIONS):
        raise OperatorError("installed worker policy does not match the admitted typed capabilities")
    if (status.get("source_sha") != config.get("source_sha")
            or status.get("operator_intent") != "active"
            or status.get("production_ready") is not True
            or status.get("enforcement_verified") is not True
            or status.get("enforcement_blocker") is not None
            or status.get("unresolved_executions") != []):
        raise OperatorError("production admission or native proof is incomplete")
    goal_revision = status.get("goal_revision")
    if not isinstance(goal_revision, str) or not goal_revision.strip():
        raise OperatorError("worker has no accepted durable project goal")
    authority = status.get("authority")
    if (not isinstance(authority, dict)
            or not isinstance(authority.get("repo_head"), str)
            or SHA40.fullmatch(authority["repo_head"]) is None
            or status.get("worktree") != project.get("worktree")
            or status.get("uid") != project.get("uid")):
        raise OperatorError("worker repository or execution identity is unattested")
    return repo, control, state, policy_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Immutable Do Again operator worker")
    parser.add_argument("--project", choices=sorted(PROJECT_ACCOUNTS), required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise OperatorError("installed worker requires verified macOS confinement")
    if not hasattr(os, "getuid") or not hasattr(os, "geteuid"):
        raise OperatorError("operator identity is unavailable")
    config = load_configuration(INSTALL_ROOT / "current/config.json")
    from .macos_server import verify_installation
    verify_installation(config)
    project = next(
        (p for p in config.get("projects", []) if isinstance(p, dict)
         and p.get("account") == PROJECT_ACCOUNTS[args.project]),
        None,
    )
    if not isinstance(project, dict) or not isinstance(project.get("repo"), str):
        raise OperatorError("project is not sealed into the installation")
    repo = Path(project["repo"])
    status = broker_request(repo, {"operation": "status"})
    policy_path = INSTALL_ROOT / "current/package/do_again/worker_policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperatorError("installed worker policy unavailable") from exc
    repo, control, state, policy_path = validate_worker_context(
        args.project, config, status, policy,
        installed_root=INSTALL_ROOT, module_path=Path(__file__),
        interpreter=Path(sys.executable), uid=os.getuid(), euid=os.geteuid(),
    )
    for path in (control, state):
        if path.is_symlink() or not path.is_dir():
            raise OperatorError("pre-existing control or state directory is missing or aliased")
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OperatorError("operator control state ownership is unsafe")
    from ..service.daemon import main as daemon_main
    argv = [
        "--repo", str(repo),
        "--control-worktree", str(control),
        "--branch", "operator-control",
        "--remote", "origin",
        "--policy", str(policy_path),
        "--state-dir", str(state),
    ]
    if args.once:
        argv.append("--once")
    # Daemon repeats admission before browser/Git effects. All model-selected
    # commands are routed through the authenticated broker executor.
    return daemon_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
