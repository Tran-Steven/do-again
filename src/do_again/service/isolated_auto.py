from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..browser import runtime
from ..core.schema import OperatorError, atomic_json, utc_now
from .isolated_cycle import _git
from .isolated_flow import run_isolated_once
from .isolated_sandbox import (
    _private_state, _sandbox_record, advance_isolated_sandbox,
    create_isolated_sandbox,
)
from .isolated_worker import create_isolated_worker


def run_isolated_auto(*, source_repo: Path, control_worktree: Path,
                      state_dir: Path, branch: str, image: str,
                      goal: str, files: list[str], test_command: list[str],
                      cycles: int = 3) -> dict:
    if (not isinstance(cycles, int) or isinstance(cycles, bool)
            or not 1 <= cycles <= 10):
        raise OperatorError("isolated autonomous trial allows 1 to 10 bounded cycles")
    source_repo = Path(source_repo).resolve()
    state_dir = Path(state_dir).resolve()
    _private_state(state_dir)
    with runtime._file_lock(state_dir / "isolated-auto.lock", timeout=5.0):
        return _run_locked(
            source_repo=source_repo, control_worktree=control_worktree,
            state_dir=state_dir, branch=branch, image=image,
            goal=goal, files=files, test_command=test_command,
            cycles=cycles)


def _run_locked(*, source_repo: Path, control_worktree: Path,
                state_dir: Path, branch: str, image: str, goal: str,
                files: list[str], test_command: list[str],
                cycles: int) -> dict:
    create_isolated_sandbox(source_repo=source_repo, state_dir=state_dir)
    sandbox, marker = _sandbox_record(state_dir)
    identity = {
        "source_repo": str(source_repo),
        "original_head": marker["original_head"],
        "branch": branch,
        "image": image,
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        "files": files,
        "test_command": test_command,
        "cycles": cycles,
    }
    ledger_path = state_dir / "isolated-auto.json"
    if ledger_path.is_symlink():
        raise OperatorError("isolated auto ledger may not be a symlink")
    if ledger_path.exists():
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise OperatorError("isolated auto progress ledger is unreadable") from exc
        if (not isinstance(ledger, dict)
                or ledger.get("identity") != identity
                or not isinstance(ledger.get("completed_cycles"), int)
                or not 0 <= ledger["completed_cycles"] <= cycles
                or not isinstance(ledger.get("commits"), list)
                or len(ledger["commits"]) != ledger["completed_cycles"]):
            raise OperatorError("isolated auto configuration differs from durable progress")
    else:
        ledger = {
            "schema_version": 1,
            "identity": identity,
            "state": "running",
            "completed_cycles": 0,
            "active_request_id": None,
            "commits": [],
            "started_at_utc": utc_now().isoformat(),
        }
        atomic_json(ledger_path, ledger)
    while ledger["completed_cycles"] < cycles:
        request_id = ledger.get("active_request_id")
        if request_id is None:
            ledger["state"] = "planning"
            atomic_json(ledger_path, ledger)
            result = run_isolated_once(
                repo=sandbox, chat_repo=source_repo,
                control_worktree=control_worktree,
                state_dir=state_dir, branch=branch, image=image,
                goal=goal, files=files, test_command=test_command)
            if result["state"] != "completed":
                ledger["state"] = result["state"]
                ledger["last_request_id"] = result["request_id"]
                atomic_json(ledger_path, ledger)
                return {
                    "state": result["state"],
                    "completed_cycles": ledger["completed_cycles"],
                    "request_id": result["request_id"],
                    "original_repo_modified": False,
                    "production_deployed": False,
                    "automatic_merge": False,
                }
            request_id = result["request_id"]
            ledger["active_request_id"] = request_id
            ledger["state"] = "advancing"
            atomic_json(ledger_path, ledger)
        agent = create_isolated_worker(
            repo=sandbox, control_worktree=control_worktree,
            state_dir=state_dir, branch=branch, image=image)
        advanced = advance_isolated_sandbox(agent, request_id=request_id)
        if advanced["state"] not in {"advanced", "already_advanced"}:
            raise OperatorError("verified isolated patch did not advance")
        ledger["commits"].append(advanced["sandbox_commit"])
        ledger["completed_cycles"] += 1
        ledger["active_request_id"] = None
        ledger["state"] = "running"
        ledger["updated_at_utc"] = utc_now().isoformat()
        atomic_json(ledger_path, ledger)
    ledger["state"] = "completed"
    ledger["finished_at_utc"] = utc_now().isoformat()
    atomic_json(ledger_path, ledger)
    return {
        "state": "completed_bounded_cycles",
        "completed_cycles": ledger["completed_cycles"],
        "sandbox_head": _git(sandbox, "rev-parse", "HEAD").stdout.decode().strip(),
        "commits": list(ledger["commits"]),
        "original_repo_modified": False,
        "production_deployed": False,
        "automatic_merge": False,
    }
