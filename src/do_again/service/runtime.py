from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from ..core.agent import Agent
from ..platforms.detect import detect_platform
from ..platforms.macos import service_definition_path


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeLayout:
    repo: Path
    branch: str
    key: str
    label: str
    root: Path
    control_worktree: Path
    state_dir: Path
    policy_path: Path
    runtime_source: Path
    metadata_path: Path
    stdout_log: Path
    stderr_log: Path


def _run(argv: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ServiceError(f"{' '.join(argv)} failed ({proc.returncode}): {detail}")
    return proc


def find_repo(path: str | Path = ".") -> Path:
    candidate = Path(path).expanduser().resolve()
    proc = _run(["git", "-C", str(candidate), "rev-parse", "--show-toplevel"], check=False)
    if proc.returncode != 0:
        raise ServiceError(f"not inside a Git repository: {candidate}")
    return Path(proc.stdout.strip()).resolve()


def _config_branch(repo: Path) -> str:
    config = repo / "do-again.toml"
    if not config.is_file():
        return "operator-control"
    try:
        value = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ServiceError(f"invalid Do Again config: {config}: {exc}") from exc
    branch = str(value.get("do_again", {}).get("control_branch", "operator-control")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch) or branch.startswith("-") or ".." in branch:
        raise ServiceError(f"invalid control branch: {branch!r}")
    return branch


def runtime_layout(repo: Path) -> RuntimeLayout:
    repo = repo.resolve()
    branch = _config_branch(repo)
    key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    label = f"io.github.tran-steven.do-again.{key}"
    home = Path(os.environ.get("DO_AGAIN_HOME", str(Path.home() / ".do_again"))).expanduser().resolve()
    root = home / "projects" / key
    return RuntimeLayout(
        repo=repo,
        branch=branch,
        key=key,
        label=label,
        root=root,
        control_worktree=root / "control",
        state_dir=root / "state",
        policy_path=root / "policy.json",
        runtime_source=root / "runtime",
        metadata_path=root / "runtime.json",
        stdout_log=root / "logs" / "agent.out.log",
        stderr_log=root / "logs" / "agent.err.log",
    )


def _default_policy() -> dict[str, Any]:
    text = resources.files("do_again").joinpath("default_policy.json").read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ServiceError("bundled default policy is invalid")
    return value


def _ensure_remote_control_branch(layout: RuntimeLayout) -> None:
    remote = _run(
        ["git", "-C", str(layout.repo), "ls-remote", "--exit-code", "--heads", "origin", layout.branch],
        check=False,
    )
    if remote.returncode != 0:
        _run(["git", "-C", str(layout.repo), "push", "origin", f"HEAD:refs/heads/{layout.branch}"])
    _run(["git", "-C", str(layout.repo), "fetch", "--quiet", "origin", layout.branch])


def _ensure_control_worktree(layout: RuntimeLayout) -> None:
    if layout.control_worktree.exists():
        check = _run(
            ["git", "-C", str(layout.control_worktree), "rev-parse", "--show-toplevel"],
            check=False,
        )
        if check.returncode != 0:
            raise ServiceError(f"control path exists but is not a Git worktree: {layout.control_worktree}")
        dirty = _run(["git", "-C", str(layout.control_worktree), "status", "--porcelain"])
        if dirty.stdout.strip():
            raise ServiceError(f"control worktree is dirty: {layout.control_worktree}")
        return
    layout.control_worktree.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "git",
            "-C",
            str(layout.repo),
            "worktree",
            "add",
            "--detach",
            str(layout.control_worktree),
            f"origin/{layout.branch}",
        ]
    )


def _copy_runtime(layout: RuntimeLayout) -> None:
    package_root = Path(__file__).resolve().parents[1]
    target = layout.runtime_source / "do_again"
    if target.exists():
        shutil.rmtree(target)
    layout.runtime_source.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        package_root,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def prepare_runtime(repo: Path) -> RuntimeLayout:
    layout = runtime_layout(repo)
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    layout.stdout_log.parent.mkdir(parents=True, exist_ok=True)
    _ensure_remote_control_branch(layout)
    _ensure_control_worktree(layout)

    policy = _default_policy()
    policy["control_branch"] = layout.branch
    policy["agent_launchd_label"] = layout.label
    layout.policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _copy_runtime(layout)

    metadata = {
        "schema_version": 1,
        "repo": str(layout.repo),
        "control_branch": layout.branch,
        "control_worktree": str(layout.control_worktree),
        "state_dir": str(layout.state_dir),
        "policy": str(layout.policy_path),
        "service_label": layout.label,
    }
    layout.metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return layout


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_plist(layout: RuntimeLayout) -> dict[str, Any]:
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    return {
        "Label": layout.label,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "do_again.core.agent",
            "--repo",
            str(layout.repo),
            "--control-worktree",
            str(layout.control_worktree),
            "--branch",
            layout.branch,
            "--policy",
            str(layout.policy_path),
            "--state-dir",
            str(layout.state_dir),
        ],
        "WorkingDirectory": str(layout.repo),
        "EnvironmentVariables": {
            "PATH": path,
            "PYTHONPATH": str(layout.runtime_source),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 5,
        "StandardOutPath": str(layout.stdout_log),
        "StandardErrorPath": str(layout.stderr_log),
    }


def _require_macos() -> None:
    info = detect_platform()
    if info.name != "macos":
        raise ServiceError(
            f"background service lifecycle is currently supported on macOS only; detected {info.name}"
        )


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["launchctl", *args], check=check)


def _loaded(layout: RuntimeLayout) -> bool:
    return _launchctl("print", f"{_domain()}/{layout.label}", check=False).returncode == 0


def _wait_loaded(layout: RuntimeLayout, expected: bool, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _loaded(layout) is expected:
            return
        time.sleep(0.1)
    state = "loaded" if expected else "unloaded"
    raise ServiceError(f"launchd service did not become {state}: {layout.label}")


def service_status(path: str | Path = ".") -> dict[str, Any]:
    repo = find_repo(path)
    layout = runtime_layout(repo)
    info = detect_platform()
    result: dict[str, Any] = {
        "repo": str(repo),
        "platform": info.name,
        "service_manager": info.service_manager,
        "label": layout.label,
        "runtime_dir": str(layout.root),
        "control_worktree": str(layout.control_worktree),
        "installed": False,
        "running": False,
        "pid": None,
    }
    if info.name != "macos":
        return result
    plist = service_definition_path(layout.label)
    result["installed"] = plist.is_file()
    probe = _launchctl("print", f"{_domain()}/{layout.label}", check=False)
    if probe.returncode == 0:
        result["running"] = True
        match = re.search(r"\bpid\s*=\s*(\d+)", probe.stdout)
        if match:
            result["pid"] = int(match.group(1))
    return result


def install_service(path: str | Path = ".") -> dict[str, Any]:
    _require_macos()
    repo = find_repo(path)
    existing = runtime_layout(repo)
    if _loaded(existing):
        proc = _launchctl("bootout", f"{_domain()}/{existing.label}", check=False)
        if proc.returncode != 0:
            raise ServiceError(f"launchctl bootout failed: {(proc.stderr or proc.stdout).strip()}")
        _wait_loaded(existing, False)

    layout = prepare_runtime(repo)
    plist = service_definition_path(layout.label)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(_launchd_plist(layout), sort_keys=True))
    os.chmod(plist, 0o644)

    boot = _launchctl("bootstrap", _domain(), str(plist), check=False)
    if boot.returncode != 0:
        raise ServiceError(f"launchctl bootstrap failed: {(boot.stderr or boot.stdout).strip()}")
    _wait_loaded(layout, True)
    return service_status(repo)


def stop_service(path: str | Path = ".") -> dict[str, Any]:
    _require_macos()
    repo = find_repo(path)
    layout = runtime_layout(repo)
    before = _loaded(layout)
    proc = _launchctl("bootout", f"{_domain()}/{layout.label}", check=False)
    if before and proc.returncode != 0:
        raise ServiceError(f"launchctl bootout failed: {(proc.stderr or proc.stdout).strip()}")
    if before:
        _wait_loaded(layout, False)
    return service_status(repo)


def restart_service(path: str | Path = ".") -> dict[str, Any]:
    _require_macos()
    repo = find_repo(path)
    layout = runtime_layout(repo)
    plist = service_definition_path(layout.label)
    if not plist.is_file():
        raise ServiceError("Do Again is not installed for this repository; run do-again install first")
    current = service_status(repo)
    if current["running"]:
        proc = _launchctl("kickstart", "-k", f"{_domain()}/{layout.label}", check=False)
    else:
        proc = _launchctl("bootstrap", _domain(), str(plist), check=False)
    if proc.returncode != 0:
        raise ServiceError(f"launchctl restart failed: {(proc.stderr or proc.stdout).strip()}")
    _wait_loaded(layout, True)
    return service_status(repo)


def uninstall_service(path: str | Path = ".") -> dict[str, Any]:
    _require_macos()
    repo = find_repo(path)
    layout = runtime_layout(repo)
    if _loaded(layout):
        proc = _launchctl("bootout", f"{_domain()}/{layout.label}", check=False)
        if proc.returncode != 0:
            raise ServiceError(f"launchctl bootout failed: {(proc.stderr or proc.stdout).strip()}")
        _wait_loaded(layout, False)
    plist = service_definition_path(layout.label)
    try:
        plist.unlink()
    except FileNotFoundError:
        pass
    if layout.control_worktree.exists():
        _run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(layout.control_worktree)],
            check=False,
        )
    if layout.root.exists():
        shutil.rmtree(layout.root)
    return {
        "repo": str(repo),
        "label": layout.label,
        "installed": False,
        "running": False,
    }


def run_foreground(path: str | Path = ".", *, once: bool = False) -> int:
    repo = find_repo(path)
    if detect_platform().name == "macos" and service_status(repo)["running"]:
        raise ServiceError("background service is already running; stop it before using foreground mode")
    layout = prepare_runtime(repo)
    agent = Agent(
        repo=repo,
        control_worktree=layout.control_worktree,
        branch=layout.branch,
        policy_path=layout.policy_path,
        state_dir=layout.state_dir,
    )
    return agent.run(once=once)


def layout_as_dict(path: str | Path = ".") -> dict[str, Any]:
    return asdict(runtime_layout(find_repo(path)))
