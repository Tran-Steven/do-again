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
from .macos_execution import INSTALL_ROOT, EXECUTION_ROOT, private_root_file
from .macos_client import broker_request

PROJECT_ACCOUNTS = {"do-again": "_doagain_da", "jobpipe": "_doagain_jp"}
WORKER_OPERATIONS = frozenset({
    "status", "run_tests", "repo_script", "scratch_script", "extended_exec",
    "git_commit", "git_publish", "dependency_install", "git_publication_reconcile", "ci_observe",
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
    ongoing: bool = False,
) -> tuple[Path, Path, Path, Path]:
    """Pure admission decision; zero disk/network/process effects."""
    if project_name not in PROJECT_ACCOUNTS:
        raise OperatorError("unrecognized sealed worker project")
    if type(uid) is not int or type(euid) is not int or uid == 0 or euid != uid:
        raise OperatorError("worker requires its unprivileged operator identity")
    if config.get("operator_uid") != uid:
        raise OperatorError("worker operator identity differs from the installed supervisor")
    if (config.get('schema_version') != 1 or config.get('production_ready') is not True
            or not isinstance(config.get('source_sha'), str)
            or SHA40.fullmatch(config['source_sha']) is None):
        raise OperatorError('installed production authority or source identity is invalid')
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
    if repo != Path(home_value) / project_name or '..' in repo.parts:
        raise OperatorError('worker repository differs from its canonical project scope')
    from .authority import project_identity
    full_key = project_identity(repo)
    if (project.get('key') != full_key or project.get('worktree') != str(EXECUTION_ROOT/full_key/'worktree')
            or type(project.get('uid')) is not int or project.get('gid') != project['uid']
            or not 400 <= project['uid'] < 500 or project['uid'] == uid
            or len({p.get('uid') for p in projects}) != 2):
        raise OperatorError('dedicated project identity or workspace binding differs')
    key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    base = Path(home_value) / ".do_again" / "projects" / key
    control, state = base / "sealed-control", base / "state"
    policy_path = current / "package/do_again/worker_policy.json"
    if (policy.get("worker_policy_schema") != 1
            or policy.get("control_branch") != "operator-control"
            or not isinstance(policy.get("allowed_operations"), list)
            or len(policy["allowed_operations"]) != len(WORKER_OPERATIONS)
            or set(policy["allowed_operations"]) != WORKER_OPERATIONS):
        raise OperatorError("installed worker policy does not match the admitted typed capabilities")
    unresolved = status.get('unresolved_executions')
    inflight = status.get('inflight_request_ids')
    known_inflight = (ongoing and isinstance(unresolved,list) and unresolved
        and isinstance(inflight,list) and all(isinstance(row,dict)
            and isinstance(row.get('request_id'),str) and row['request_id'] in inflight for row in unresolved))
    if (status.get("source_sha") != config.get("source_sha")
            or status.get("operator_intent") != "active"
            or status.get("production_ready") is not True
            or status.get("enforcement_verified") is not True
            or status.get("enforcement_blocker") is not None
            or (unresolved != [] and not known_inflight)):
        raise OperatorError("production admission or native proof is incomplete")
    goal_revision = status.get("goal_revision")
    if (not isinstance(goal_revision, str) or not goal_revision.strip()
            or type(status.get('epoch')) is not int or status['epoch'] < 1):
        raise OperatorError("worker has no accepted durable project goal")
    authority = status.get("authority")
    if (not isinstance(authority, dict)
            or not isinstance(authority.get("repo_head"), str)
            or SHA40.fullmatch(authority["repo_head"]) is None
            or status.get("worktree") != project.get("worktree")
            or status.get("uid") != project.get("uid")):
        raise OperatorError("worker repository or execution identity is unattested")
    return repo, control, state, policy_path


def read_worker_configuration() -> dict:
    """Read sealed public configuration as the operator, without root authority."""
    path = INSTALL_ROOT/'current/config.json'
    private_root_file(path)
    try:
        value = json.loads(path.read_text())
    except (OSError,ValueError) as exc:
        raise OperatorError('sealed worker configuration is unavailable') from exc
    if not isinstance(value,dict):raise OperatorError('sealed worker configuration is invalid')
    return value


def validate_control_paths(control: Path, state: Path, uid: int) -> None:
    for path in (control,state):
        for ancestor in (path,*path.parents):
            if ancestor.is_symlink():raise OperatorError('control state ancestor is aliased')
            info = ancestor.stat()
            if info.st_uid not in {0,uid} or info.st_mode & 0o022:
                raise OperatorError('control state ancestor ownership is unsafe')
        if not path.is_dir() or path.stat().st_uid != uid:
            raise OperatorError('pre-existing operator control state is missing or unsafe')


def read_admission_status(repo: Path) -> dict[str, Any]:
    """Resolve lost browser-helper responses using protected terminal receipts only."""
    status = broker_request(repo, {'operation':'status'})
    pending = status.get('unresolved_executions')
    if not isinstance(pending,list) or len(pending)>16:
        raise OperatorError('unresolved admission evidence is invalid or exceeds its bound')
    for record in pending:
        if (str(record.get('request_id') or '').startswith('browser-')
                and record['request_id'] not in status.get('inflight_request_ids', [])):
            broker_request(repo, {'operation':'browser_reconcile','request_id':record['request_id']})
    return broker_request(repo, {'operation':'status'}) if pending else status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Immutable Do Again operator worker")
    parser.add_argument("--project", choices=sorted(PROJECT_ACCOUNTS), required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--canary", action="store_true")
    parser.add_argument("--codex-canary", action="store_true")
    parser.add_argument("--expected-source")
    parser.add_argument("--expected-epoch",type=int)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise OperatorError("installed worker requires verified macOS confinement")
    if not hasattr(os, "getuid") or not hasattr(os, "geteuid"):
        raise OperatorError("operator identity is unavailable")
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise OperatorError('worker requires isolated Python without host site imports or bytecode writes')
    config = read_worker_configuration()
    if (os.getgid() != config.get('operator_gid') or os.getegid() != os.getgid()):
        raise OperatorError('worker operator group differs from the sealed identity')
    from .macos_server import verify_installation
    verify_installation(config)
    if args.canary:
        from .live_canary_worker import main as canary_main
        return canary_main(config,args)
    project = next(
        (p for p in config.get("projects", []) if isinstance(p, dict)
         and p.get("account") == PROJECT_ACCOUNTS[args.project]),
        None,
    )
    if not isinstance(project, dict) or not isinstance(project.get("repo"), str):
        raise OperatorError("project is not sealed into the installation")
    repo = Path(project["repo"])
    status = read_admission_status(repo)
    if ((args.expected_source is not None and args.expected_source!=config.get("source_sha"))
            or (args.expected_epoch is not None and args.expected_epoch!=status.get("epoch"))):
        raise OperatorError("worker service source or authority epoch changed")
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
    validate_control_paths(control,state,os.getuid())
    # Recheck the original authority after filesystem validation. A pause/resume
    # or changed goal during startup invalidates this worker admission.
    latest = read_admission_status(repo)
    validate_worker_context(args.project,config,latest,policy,
        installed_root=INSTALL_ROOT,module_path=Path(__file__),
        interpreter=Path(sys.executable),uid=os.getuid(),euid=os.geteuid())
    if any(latest.get(k) != status.get(k) for k in ('epoch','goal_revision','authority')):
        raise OperatorError('worker authority changed during admission')
    broker_request(repo,{"operation":"worker_register","pid":os.getpid(),
                         "epoch":status["epoch"],"source_sha":config["source_sha"]})
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
    def admission_check():
        from .authority import AuthorityDenied
        try:
            current = read_admission_status(repo)
            validate_worker_context(args.project,config,current,policy,
                installed_root=INSTALL_ROOT,module_path=Path(__file__),
                interpreter=Path(sys.executable),uid=os.getuid(),euid=os.geteuid(),ongoing=True)
            if any(current.get(k) != status.get(k) for k in ('epoch','goal_revision')):
                raise OperatorError('worker admission epoch or goal changed')
        except OperatorError as exc:
            raise AuthorityDenied(str(exc)) from exc
    from ..core.control_transport import BrokerControlHistory
    transport=BrokerControlHistory(repo,control,status["epoch"])
    def browser_effect():
        import uuid
        current = read_admission_status(repo)
        if current.get('inflight_request_ids'):
            # An admitted operation may drain. Do not reserve another browser
            # effect while that writer holds the project execution journal.
            return {'delivered':0,'liveness':'running'}
        return broker_request(repo,{'operation':'browser_tick','request_id':'browser-'+uuid.uuid4().hex,
                                    'epoch':status['epoch']})
    return daemon_main(argv,admission_check=admission_check,control_transport=transport,browser_effect=browser_effect)


if __name__ == "__main__":
    raise SystemExit(main())
