from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from ..browser import runtime
from ..core.schema import OperatorError, atomic_json, utc_now
from .isolated_auto import run_isolated_auto
from .isolated_cycle import IMAGE_RE, _git
from .isolated_planner import _read_source


def _private_layout(source_repo: Path) -> dict:
    source = Path(source_repo).expanduser()
    if source.is_symlink():
        raise OperatorError("isolated enrollment cannot use a source alias")
    source = source.resolve()
    if not source.is_dir() or not (source / ".git").exists():
        raise OperatorError("isolated enrollment requires an existing Git checkout")
    key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
    home = Path(os.environ.get("DO_AGAIN_HOME", str(Path.home() / ".do_again"))).expanduser()
    if home.is_symlink():
        raise OperatorError("Do Again home must not be a symlink")
    home = home.resolve()
    root = home / "isolated-projects" / key
    if source == root or source in root.parents or root in source.parents:
        raise OperatorError("isolated runtime must be outside the source checkout")
    return {
        "source_repo": source,
        "root": root,
        "state_dir": root / "state",
        "control_worktree": root / "control",
        "config_path": root / "config.json",
        "journal_path": root / "enrollment.json",
        "branch": "do-again-isolated-" + key,
        "key": key,
    }


def _require_private_root(root: Path) -> None:
    if root.is_symlink():
        raise OperatorError("isolated runtime cannot be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "getuid"):
        stat = root.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise OperatorError("isolated runtime must be owned by the current user")


def _read_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise OperatorError("isolated metadata is missing or aliased")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperatorError("isolated metadata cannot be read") from exc
    if not isinstance(value, dict):
        raise OperatorError("isolated metadata must be a JSON object")
    return value


def _check_image(image: str) -> None:
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise OperatorError("an immutable pinned container image is required")
    try:
        test = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorError("Docker inspection is unavailable") from exc
    if test.returncode != 0:
        raise OperatorError("pinned offline test image is not installed")


def enrolled_config(source_repo: Path) -> tuple[dict, dict]:
    layout = _private_layout(source_repo)
    config = _read_object(layout["config_path"])
    if (config.get("schema_version") != 1
            or config.get("source_repo") != str(layout["source_repo"])
            or config.get("state_dir") != str(layout["state_dir"])
            or config.get("control_worktree") != str(layout["control_worktree"])
            or config.get("branch") != layout["branch"]
            or config.get("remote") != "origin"):
        raise OperatorError("isolated enrollment belongs to a different checkout or control path")
    if (not isinstance(config.get("files"), list)
            or not 1 <= len(config["files"]) <= 5
            or not isinstance(config.get("goal"), str)
            or not isinstance(config.get("cycles"), int)
            or not 1 <= config["cycles"] <= 10
            or config.get("test_suite") not in {"python", "npm"}):
        raise OperatorError("isolated enrollment has invalid bounded policy")
    journal = _read_object(layout["journal_path"])
    if (journal.get("state") != "configured"
            or journal.get("branch") != config["branch"]
            or journal.get("original_head") != config.get("original_head")):
        raise OperatorError("isolated enrollment is incomplete")
    return layout, config


def enroll_isolated_project(*, source_repo: Path, image: str, goal: str,
                            files: list[str], test_suite: str,
                            cycles: int = 10) -> dict:
    layout = _private_layout(source_repo)
    repo = layout["source_repo"]
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 2000:
        raise OperatorError("bounded isolated goal is required")
    if not isinstance(cycles, int) or isinstance(cycles, bool) or not 1 <= cycles <= 10:
        raise OperatorError("isolated cycle budget must be 1 to 10")
    if test_suite not in {"python", "npm"}:
        raise OperatorError("isolated tests must use a fixed supported suite")
    _check_image(image)
    head, _ = _read_source(repo, files)
    bound = str(runtime.project_record(repo).get("chat_url") or "")
    if not bound.startswith("https://chatgpt.com/c/") or not runtime._chat_id(bound):
        raise OperatorError("source project must already have a bound ChatGPT conversation")
    config = {
        "schema_version": 1,
        "source_repo": str(repo),
        "original_head": head,
        "control_worktree": str(layout["control_worktree"]),
        "state_dir": str(layout["state_dir"]),
        "remote": "origin",
        "branch": layout["branch"],
        "image": image,
        "goal": goal.strip(),
        "files": list(files),
        "test_suite": test_suite,
        "cycles": cycles,
        "conversation_id": runtime._chat_id(bound),
        "production_deployed": False,
        "automatic_merge": False,
        "application_submissions": False,
    }
    if layout["config_path"].exists() or layout["config_path"].is_symlink():
        previous_layout, previous = enrolled_config(repo)
        if previous != config:
            raise OperatorError("existing isolated enrollment differs; no overwrite")
        return {"state": "already_configured", "branch": config["branch"],
                "source_repo": str(repo), "ready": True}
    remote = _git(repo, "remote", "get-url", "origin").stdout.decode().strip()
    if not remote:
        raise OperatorError("origin remote is not configured")
    _git(repo, "ls-remote", "origin", "HEAD")
    branch = layout["branch"]
    remote_ref = "refs/heads/" + branch
    _require_private_root(layout["root"])
    _require_private_root(layout["state_dir"])
    intended = {
        "schema_version": 1,
        "state": "reserved",
        "source_repo": str(repo),
        "original_head": head,
        "branch": branch,
        "config_sha256": hashlib.sha256(
            json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "recorded_at_utc": utc_now().isoformat(),
    }
    journal_path = layout["journal_path"]
    if journal_path.exists() or journal_path.is_symlink():
        existing = _read_object(journal_path)
        if (existing.get("state") != "reserved"
                or any(existing.get(k) != intended.get(k)
                       for k in ("source_repo", "original_head", "branch", "config_sha256"))):
            raise OperatorError("prior isolated setup attempt is incomplete or inconsistent")
    else:
        atomic_json(journal_path, intended)
    result = _git(repo, "ls-remote", "--heads", "origin", branch).stdout.decode().strip()
    if result:
        remote_head = result.split("\t", 1)[0]
        if remote_head != head:
            raise OperatorError("dedicated control branch exists with unexpected history")
    else:
        _git(repo, "push", "origin", "HEAD:" + remote_ref)
    control = layout["control_worktree"]
    if control.is_symlink():
        raise OperatorError("isolated control worktree cannot be an alias")
    if control.exists():
        if _git(control, "status", "--porcelain").stdout.strip():
            raise OperatorError("existing isolated control worktree is dirty")
    else:
        _git(repo, "fetch", "--quiet", "origin", branch)
        _git(repo, "worktree", "add", "--detach", str(control), "FETCH_HEAD")
    if _git(control, "rev-parse", "HEAD").stdout.decode().strip() != head:
        raise OperatorError("isolated control worktree starts from an unexpected commit")
    atomic_json(layout["config_path"], config)
    intended["state"] = "configured"
    intended["configured_at_utc"] = utc_now().isoformat()
    atomic_json(journal_path, intended)
    return {
        "state": "configured", "branch": branch,
        "source_repo": str(repo), "control_worktree": str(control),
        "ready": True, "legacy_controller_modified": False,
    }


def run_enrolled_isolated(source_repo: Path) -> dict:
    layout, config = enrolled_config(source_repo)
    repo = layout["source_repo"]
    if runtime._chat_id(str(runtime.project_record(repo).get("chat_url") or "")) != config["conversation_id"]:
        raise OperatorError("bound project ChatGPT conversation changed after isolated enrollment")
    if _git(repo, "rev-parse", "HEAD").stdout.decode().strip() != config["original_head"]:
        raise OperatorError("original checkout moved after isolated enrollment")
    test_command = (
        ["python", "-m", "unittest", "discover", "-s", "tests"]
        if config["test_suite"] == "python" else ["npm", "test", "--", "--run"]
    )
    return run_isolated_auto(
        source_repo=repo,
        control_worktree=layout["control_worktree"],
        state_dir=layout["state_dir"],
        branch=config["branch"],
        image=config["image"],
        goal=config["goal"],
        files=config["files"],
        test_command=test_command,
        cycles=config["cycles"],
    )
