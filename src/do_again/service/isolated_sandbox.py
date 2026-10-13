from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..core.schema import OperatorError, atomic_json, utc_now
from .isolated_cycle import _export, _git
from .isolated_receipts import _load_verified_receipt


def _private_state(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = state_dir.stat()
    if hasattr(os, "getuid") and (
        info.st_uid != os.getuid() or info.st_mode & 0o077
    ):
        raise OperatorError("isolated development state must be user-private")


def _sandbox_path(state_dir: Path) -> Path:
    workspace = state_dir / "isolated-source"
    if workspace.is_symlink():
        raise OperatorError("isolated source must not be a symlink")
    return workspace


def _sandbox_record(state_dir: Path) -> tuple[Path, dict]:
    record_file = state_dir / "isolated-sandbox.json"
    if record_file.is_symlink() or not record_file.is_file():
        raise OperatorError("isolated source marker is missing or aliased")
    try:
        marker = json.loads(record_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise OperatorError("isolated source marker is unreadable") from exc
    workspace = _sandbox_path(state_dir)
    if (not isinstance(marker, dict)
            or marker.get("schema_version") != 1
            or marker.get("workspace") != str(workspace)
            or not workspace.is_dir()
            or not (workspace / ".git").is_dir()):
        raise OperatorError("isolated source does not match its durable marker")
    return workspace, marker


def create_isolated_sandbox(*, source_repo: Path, state_dir: Path) -> dict:
    if Path(source_repo).is_symlink() or Path(state_dir).is_symlink():
        raise OperatorError("sandbox paths must not be symlink aliases")
    source_repo = Path(source_repo).resolve()
    state_dir = Path(state_dir).resolve()
    if (source_repo == state_dir or source_repo in state_dir.parents
            or state_dir in source_repo.parents
            or not source_repo.is_dir() or not (source_repo / ".git").exists()):
        raise OperatorError("sandbox requires an independent source Git checkout")
    _private_state(state_dir)
    workspace = _sandbox_path(state_dir)
    record_file = state_dir / "isolated-sandbox.json"
    if record_file.exists() or record_file.is_symlink():
        existing, marker = _sandbox_record(state_dir)
        if marker.get("original_repo") != str(source_repo):
            raise OperatorError("isolated state is owned by a different source")
        if _git(existing, "status", "--porcelain", "--untracked-files=all").stdout.strip():
            raise OperatorError("existing isolated source has uncommitted changes")
        return {
            "state": "existing",
            "workspace": str(existing),
            "head": _git(existing, "rev-parse", "HEAD").stdout.decode().strip(),
            "original_repo_unchanged": True,
        }
    if workspace.exists() or workspace.is_symlink():
        raise OperatorError("isolated source exists without a durable marker")
    if _git(source_repo, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise OperatorError("original source must be clean before isolated snapshot")
    head = _git(source_repo, "rev-parse", "HEAD").stdout.decode().strip()
    workspace.mkdir(mode=0o700)
    _export(source_repo, workspace, head)
    _git(workspace, "init", "-q", "-b", "isolated-development")
    _git(workspace, "add", "-f", "--all")
    _git(
        workspace, "-c", "user.name=Do Again Sandbox",
        "-c", "user.email=do-again@invalid.example",
        "commit", "-qm", "isolated source baseline")
    isolated_head = _git(workspace, "rev-parse", "HEAD").stdout.decode().strip()
    marker = {
        "schema_version": 1,
        "original_repo": str(source_repo),
        "original_head": head,
        "workspace": str(workspace),
        "initial_head": isolated_head,
        "created_at_utc": utc_now().isoformat(),
        "remote_push_enabled": False,
    }
    atomic_json(record_file, marker)
    return {
        "state": "created", "workspace": str(workspace),
        "head": isolated_head, "original_head": head,
        "original_repo_unchanged": True,
    }


def advance_isolated_sandbox(agent, *, request_id: str) -> dict:
    state_dir = Path(agent.state_dir).resolve()
    workspace, marker = _sandbox_record(state_dir)
    if Path(agent.repo).resolve() != workspace:
        raise OperatorError("worker source is not the isolated development workspace")
    receipt = _load_verified_receipt(agent, request_id)
    if receipt is None or receipt.get("state") != "succeeded":
        raise OperatorError("cannot advance without a successful isolated coding receipt")
    report = state_dir / "receipt-reports" / (request_id + ".json")
    if report.is_symlink() or not report.is_file():
        raise OperatorError("ChatGPT has not acknowledged the verified receipt")
    try:
        acknowledged = json.loads(report.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise OperatorError("receipt acknowledgment journal is unreadable") from exc
    receipt_digest = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (not isinstance(acknowledged, dict)
            or acknowledged.get("state") != "acknowledged"
            or acknowledged.get("request_id") != request_id
            or acknowledged.get("receipt_sha256") != receipt_digest):
        raise OperatorError("acknowledgment does not attest this exact receipt")
    patch = receipt["result"]["result"]["isolated_patch"]
    output = state_dir / "isolated-advances"
    journal = output / (request_id + ".json")
    head = _git(workspace, "rev-parse", "HEAD").stdout.decode().strip()
    if journal.exists() or journal.is_symlink():
        if journal.is_symlink() or not journal.is_file():
            raise OperatorError("isolated advancement journal is unsafe")
        try:
            previous = json.loads(journal.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise OperatorError("isolated advancement journal is unreadable") from exc
        if (not isinstance(previous, dict)
                or previous.get("state") != "completed"
                or previous.get("request_id") != request_id
                or previous.get("source_head") != patch["source_head"]
                or previous.get("patch_sha256") != patch["patch_sha256"]
                or previous.get("sandbox_commit") != head):
            raise OperatorError("prior isolated advancement is uncertain or changed")
        return {
            "state": "already_advanced", "request_id": request_id,
            "sandbox_commit": head, "original_repo_unchanged": True,
            "production_deployed": False, "automatic_merge": False,
        }
    if head != patch["source_head"]:
        raise OperatorError("isolated source HEAD differs from tested patch base")
    if _git(workspace, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise OperatorError("isolated source is dirty; no patch application")
    patch_file = state_dir / "isolated-patches" / patch["patch_file"]
    changed = set()
    for line in _git(workspace, "apply", "--numstat", str(patch_file)).stdout.decode().splitlines():
        values = line.split("\t", 2)
        if (len(values) != 3 or not values[0].isdigit()
                or not values[1].isdigit() or not values[2]):
            raise OperatorError("isolated patch has unsupported or binary changes")
        changed.add(values[2])
    if changed != set(patch["files"]):
        raise OperatorError("patch affects unapproved isolated source paths")
    _git(workspace, "apply", "--check", "--index", str(patch_file))
    output.mkdir(mode=0o700, exist_ok=True)
    progress = {
        "schema_version": 1, "state": "reserved",
        "request_id": request_id,
        "source_head": head,
        "patch_sha256": patch["patch_sha256"],
        "receipt_sha256": receipt_digest,
        "reserved_at_utc": utc_now().isoformat(),
    }
    atomic_json(journal, progress)
    _git(workspace, "apply", "--index", str(patch_file))
    staged = set(_git(workspace, "diff", "--cached", "--name-only").stdout.decode().splitlines())
    if staged != changed:
        raise OperatorError("staged isolated files differ from the tested patch")
    _git(
        workspace, "-c", "user.name=Do Again Sandbox",
        "-c", "user.email=do-again@invalid.example",
        "commit", "-qm", "Do Again isolated verified change " + request_id)
    commit = _git(workspace, "rev-parse", "HEAD").stdout.decode().strip()
    progress["state"] = "completed"
    progress["sandbox_commit"] = commit
    progress["completed_at_utc"] = utc_now().isoformat()
    atomic_json(journal, progress)
    return {
        "state": "advanced", "request_id": request_id,
        "sandbox_commit": commit, "original_repo_unchanged": True,
        "production_deployed": False, "automatic_merge": False,
    }
