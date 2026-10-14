"""Opt-in, unprivileged Git-backed isolated patch worker.

This is an experimental one-project executor, NOT the legacy shell runner,
the privileged V1A controller, or an autonomous ChatGPT planning service.
Git's remote control branch supplies scoped patch manifests and receives
idempotent claims/receipts using the existing Agent journal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from ..core.agent import Agent
from ..core.schema import OperatorError, atomic_json, request_fingerprint
from .isolated_cycle import IMAGE_RE, run_isolated_cycle


class IsolatedPatchExecutor:
    """Never execute a caller-selected shell command or touch the live checkout."""

    def __init__(self, *, repo: Path, image: str, state_dir: Path):
        self.repo = Path(repo).resolve()
        self.state_dir = Path(state_dir).resolve()
        if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
            raise OperatorError("isolated worker requires immutable container image digest")
        self.image = image

    def execute(self, request: dict) -> dict:
        if (not isinstance(request, dict)
                or request.get("operation") != "isolated_patch"
                or not isinstance(request.get("args"), dict)
                or set(request["args"]) != {"manifest"}):
            raise OperatorError("isolated worker permits only an inline isolated_patch manifest")
        if request.get("expected") not in (None, {}):
            raise OperatorError("isolated patch uses the manifest's exact source HEAD fence")
        manifest = request["args"]["manifest"]
        if (not isinstance(manifest, dict)
                or len(json.dumps(manifest, separators=(",", ":")).encode()) > 500_000):
            raise OperatorError("isolated patch manifest is missing or oversized")
        receipt = run_isolated_cycle(
            self.repo, manifest, image=self.image,
            output_dir=self.state_dir / "isolated-patches",
        )
        return {
            "operation": "isolated_patch",
            "request_fingerprint": request_fingerprint(request),
            "result": {"returncode": 0, "isolated_patch": receipt},
        }


def create_isolated_worker(*, repo: Path, control_worktree: Path, state_dir: Path,
                           branch: str, image: str, remote: str = "origin") -> Agent:
    """Build patch-only Agent without the disabled unrestricted runtime path.

    Requires an already configured isolated control worktree, deliberately
    separate from the legacy operator-control branch. No daemon installation,
    root permissions, browser session, or initial remote branch creation.
    """
    inputs = [Path(repo), Path(control_worktree), Path(state_dir)]
    # Resolve only AFTER checking the provided final path component. A
    # caller-selected alias must not redirect private worker state.
    if any(item.is_symlink() for item in inputs):
        raise OperatorError("isolated worker paths must not be symlink aliases")
    repo, control_worktree, state_dir = tuple(item.resolve() for item in inputs)
    if os.name != "posix" and os.name != "nt":
        raise OperatorError("unsupported isolated-worker platform")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise OperatorError("isolated worker must not run as administrator/root")
    if (not repo.is_dir() or not (repo / ".git").exists()
            or not control_worktree.is_dir()
            or not (control_worktree / ".git").exists()
            or repo == control_worktree or state_dir == repo or repo in state_dir.parents
            or state_dir == control_worktree or control_worktree in state_dir.parents):
        raise OperatorError("isolated worker requires distinct source, control and private state")
    if (not isinstance(branch, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,80}", branch)
            or branch in {"main", "master", "operator-control"}
            or branch.startswith("refs/")
            or ".." in branch):
        raise OperatorError("worker requires a dedicated non-production control branch")
    if remote != "origin":
        raise OperatorError("experimental worker only accepts origin remote")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = state_dir.stat()
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise OperatorError("worker state must be private to the current user")
    policy_file = state_dir / "isolated-policy.json"
    policy = {
        "schema_version": 1,
        "allowed_operations": ["isolated_patch"],
        "max_request_ttl_seconds": 3600,
        "max_future_skew_seconds": 300,
        "poll_seconds": 5,
        "request_lock_stale_seconds": 7200,
        "remote_claims": True,
    }
    if policy_file.exists() or policy_file.is_symlink():
        if policy_file.is_symlink() or json.loads(policy_file.read_text()) != policy:
            raise OperatorError("existing isolated-worker policy differs; no overwrite")
    else:
        atomic_json(policy_file, policy)
        os.chmod(policy_file, stat.S_IRUSR | stat.S_IWUSR)
    return Agent(
        repo=repo, control_worktree=control_worktree, branch=branch,
        remote=remote, policy_path=policy_file, state_dir=state_dir,
        executor=IsolatedPatchExecutor(repo=repo, image=image, state_dir=state_dir),
    )
