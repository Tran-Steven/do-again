from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from ..core.agent import Agent
from ..platforms.detect import detect_platform
from ..platforms.linux import service_definition_path as linux_service_definition_path
from ..platforms.macos import service_definition_path as macos_service_definition_path
from ..platforms.windows import service_name as windows_service_name


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeLayout:
    repo: Path
    branch: str
    remote: str
    policy_source: Path | None
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


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    except OSError as exc:
        if check:
            raise ServiceError(f"{argv[0]} is unavailable: {exc}") from exc
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
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


def _project_config(repo: Path) -> dict[str, Any]:
    config = repo / "do-again.toml"
    if not config.is_file():
        return {}
    try:
        value = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ServiceError(f"invalid Do Again config: {config}: {exc}") from exc
    section = value.get("do_again", {})
    if not isinstance(section, dict):
        raise ServiceError(f"invalid [do_again] config section: {config}")
    return section


def _config_branch(repo: Path) -> str:
    section = _project_config(repo)
    branch = str(section.get("control_branch", "operator-control")).strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch)
        or branch.startswith("-")
        or ".." in branch
        or branch in {"main", "master", "trunk"}
    ):
        raise ServiceError(f"invalid or unsafe control branch: {branch!r}")

    check = _run(["git", "check-ref-format", "--branch", branch], check=False)
    if check.returncode != 0:
        raise ServiceError(f"invalid control branch: {branch!r}")

    current = _run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        check=False,
    )
    if current.returncode == 0 and current.stdout.strip() == branch:
        raise ServiceError(
            f"control branch must be dedicated and cannot be the checked-out branch: {branch!r}"
        )
    return branch


def _config_remote(repo: Path) -> str:
    section = _project_config(repo)
    remote = str(section.get("remote", "origin")).strip()
    if (
        not remote
        or remote.startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", remote)
        or ".." in remote
    ):
        raise ServiceError(f"invalid Git remote: {remote!r}")
    return remote


def _config_policy_source(repo: Path) -> Path | None:
    section = _project_config(repo)
    raw = section.get("policy")
    if raw is None or not str(raw).strip():
        return None
    candidate = Path(str(raw)).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    source = candidate.resolve()
    try:
        source.relative_to(repo.resolve())
    except ValueError as exc:
        raise ServiceError("configured policy must be inside the repository") from exc
    if not source.is_file():
        raise ServiceError(f"configured policy does not exist: {source}")
    return source

def runtime_layout(repo: Path) -> RuntimeLayout:
    repo = repo.resolve()
    branch = _config_branch(repo)
    remote = _config_remote(repo)
    policy_source = _config_policy_source(repo)
    key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    label = f"io.github.tran-steven.do-again.{key}"
    home = Path(os.environ.get("DO_AGAIN_HOME", str(Path.home() / ".do_again"))).expanduser().resolve()
    root = home / "projects" / key
    return RuntimeLayout(
        repo=repo,
        branch=branch,
        remote=remote,
        policy_source=policy_source,
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
        ["git", "-C", str(layout.repo), "ls-remote", "--exit-code", "--heads", layout.remote, layout.branch],
        check=False,
    )
    if remote.returncode != 0:
        _run(["git", "-C", str(layout.repo), "push", layout.remote, f"HEAD:refs/heads/{layout.branch}"])
    _run(["git", "-C", str(layout.repo), "fetch", "--quiet", layout.remote, layout.branch])


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
        ["git", "-C", str(layout.repo), "fetch", "--quiet", layout.remote, layout.branch]
    )
    _run(
        [
            "git",
            "-C",
            str(layout.repo),
            "worktree",
            "add",
            "--detach",
            str(layout.control_worktree),
            "FETCH_HEAD",
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

    if layout.policy_source is not None:
        try:
            policy = json.loads(
                layout.policy_source.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceError(
                f"invalid configured policy: {layout.policy_source}: {exc}"
            ) from exc
        if not isinstance(policy, dict):
            raise ServiceError("configured policy must be a JSON object")
    else:
        policy = _default_policy()
    policy["control_branch"] = layout.branch
    policy["agent_launchd_label"] = layout.label
    policy["agent_service_label"] = layout.label
    layout.policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _copy_runtime(layout)

    metadata = {
        "schema_version": 1,
        "repo": str(layout.repo),
        "control_branch": layout.branch,
        "remote": layout.remote,
        "control_worktree": str(layout.control_worktree),
        "state_dir": str(layout.state_dir),
        "policy": str(layout.policy_path),
        "policy_source": (
            str(layout.policy_source) if layout.policy_source else None
        ),
        "service_label": layout.label,
    }
    layout.metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return layout


def _agent_argv(layout: RuntimeLayout) -> list[str]:
    return [
        sys.executable,
        "-m",
        "do_again.core.agent",
        "--repo",
        str(layout.repo),
        "--control-worktree",
        str(layout.control_worktree),
        "--branch",
        layout.branch,
        "--remote",
        layout.remote,
        "--policy",
        str(layout.policy_path),
        "--state-dir",
        str(layout.state_dir),
    ]


def _base_status(repo: Path, layout: RuntimeLayout) -> dict[str, Any]:
    info = detect_platform()
    return {
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


def _wait_for(
    probe: Callable[[], bool],
    expected: bool,
    description: str,
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe() is expected:
            return
        time.sleep(0.1)
    state = "ready" if expected else "stopped"
    raise ServiceError(f"{description} did not become {state}")


def _cleanup_runtime(repo: Path, layout: RuntimeLayout) -> None:
    if layout.control_worktree.exists():
        _run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(layout.control_worktree)],
            check=False,
        )
    if layout.root.exists():
        shutil.rmtree(layout.root)


# macOS / launchd

def _domain() -> str:
    if not hasattr(os, "getuid"):
        raise ServiceError("launchd user domain is unavailable on this platform")
    return f"gui/{os.getuid()}"


def _launchd_plist(layout: RuntimeLayout) -> dict[str, Any]:
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    return {
        "Label": layout.label,
        "ProgramArguments": _agent_argv(layout),
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


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["launchctl", *args], check=check)


def _macos_loaded(layout: RuntimeLayout) -> bool:
    return _launchctl("print", f"{_domain()}/{layout.label}", check=False).returncode == 0


def _macos_status(repo: Path, layout: RuntimeLayout) -> dict[str, Any]:
    result = _base_status(repo, layout)
    plist = macos_service_definition_path(layout.label)
    result["installed"] = plist.is_file()
    probe = _launchctl("print", f"{_domain()}/{layout.label}", check=False)
    if probe.returncode == 0:
        result["running"] = True
        match = re.search(r"\bpid\s*=\s*(\d+)", probe.stdout)
        if match:
            result["pid"] = int(match.group(1))
    return result


def _install_macos(repo: Path) -> dict[str, Any]:
    existing = runtime_layout(repo)
    if _macos_loaded(existing):
        proc = _launchctl("bootout", f"{_domain()}/{existing.label}", check=False)
        if proc.returncode != 0:
            raise ServiceError(f"launchctl bootout failed: {(proc.stderr or proc.stdout).strip()}")
        _wait_for(lambda: _macos_loaded(existing), False, f"launchd service {existing.label}")

    layout = prepare_runtime(repo)
    plist = macos_service_definition_path(layout.label)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(_launchd_plist(layout), sort_keys=True))
    os.chmod(plist, 0o644)

    boot = _launchctl("bootstrap", _domain(), str(plist), check=False)
    if boot.returncode != 0:
        raise ServiceError(f"launchctl bootstrap failed: {(boot.stderr or boot.stdout).strip()}")
    _wait_for(lambda: _macos_loaded(layout), True, f"launchd service {layout.label}")
    return _macos_status(repo, layout)


def _stop_macos(repo: Path) -> dict[str, Any]:
    layout = runtime_layout(repo)
    before = _macos_loaded(layout)
    proc = _launchctl("bootout", f"{_domain()}/{layout.label}", check=False)
    if before and proc.returncode != 0:
        raise ServiceError(f"launchctl bootout failed: {(proc.stderr or proc.stdout).strip()}")
    if before:
        _wait_for(lambda: _macos_loaded(layout), False, f"launchd service {layout.label}")
    return _macos_status(repo, layout)


def _restart_macos(repo: Path) -> dict[str, Any]:
    layout = runtime_layout(repo)
    plist = macos_service_definition_path(layout.label)
    if not plist.is_file():
        raise ServiceError("Do Again is not installed for this repository; run do-again install first")
    current = _macos_status(repo, layout)
    if current["running"]:
        proc = _launchctl("kickstart", "-k", f"{_domain()}/{layout.label}", check=False)
    else:
        proc = _launchctl("bootstrap", _domain(), str(plist), check=False)
    if proc.returncode != 0:
        raise ServiceError(f"launchctl restart failed: {(proc.stderr or proc.stdout).strip()}")
    _wait_for(lambda: _macos_loaded(layout), True, f"launchd service {layout.label}")
    return _macos_status(repo, layout)


def _uninstall_macos(repo: Path) -> dict[str, Any]:
    layout = runtime_layout(repo)
    if _macos_loaded(layout):
        proc = _launchctl("bootout", f"{_domain()}/{layout.label}", check=False)
        if proc.returncode != 0:
            raise ServiceError(f"launchctl bootout failed: {(proc.stderr or proc.stdout).strip()}")
        _wait_for(lambda: _macos_loaded(layout), False, f"launchd service {layout.label}")
    plist = macos_service_definition_path(layout.label)
    try:
        plist.unlink()
    except FileNotFoundError:
        pass
    _cleanup_runtime(repo, layout)
    return {"repo": str(repo), "label": layout.label, "installed": False, "running": False}


# Linux / systemd user service

def _linux_unit_name(layout: RuntimeLayout) -> str:
    return f"{layout.label}.service"


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", "--user", *args], check=check)


def _linux_running(layout: RuntimeLayout) -> bool:
    return _systemctl("is-active", "--quiet", _linux_unit_name(layout), check=False).returncode == 0


def _linux_pid(layout: RuntimeLayout) -> int | None:
    proc = _systemctl(
        "show",
        _linux_unit_name(layout),
        "--property=MainPID",
        "--value",
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        value = int(proc.stdout.strip())
    except ValueError:
        return None
    return value or None


def _posix_launcher_path(layout: RuntimeLayout) -> Path:
    return layout.root / "run-agent.sh"


def _posix_launcher_text(layout: RuntimeLayout) -> str:
    argv = " ".join(shlex.quote(value) for value in _agent_argv(layout))
    return (
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(str(layout.runtime_source))}\n"
        "export PYTHONDONTWRITEBYTECODE=1\n"
        "export PYTHONUNBUFFERED=1\n"
        f"cd {shlex.quote(str(layout.repo))}\n"
        f"exec {argv}\n"
    )


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _systemd_unit(layout: RuntimeLayout) -> str:
    launcher = _posix_launcher_path(layout)
    return (
        "[Unit]\n"
        f"Description=Do Again agent for {layout.repo.name}\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart=/bin/sh {_systemd_quote(str(launcher))}\n"
        f"WorkingDirectory={_systemd_quote(str(layout.repo))}\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _linux_status(repo: Path, layout: RuntimeLayout) -> dict[str, Any]:
    result = _base_status(repo, layout)
    unit = linux_service_definition_path(layout.label)
    result["installed"] = unit.is_file()
    if result["installed"] and _linux_running(layout):
        result["running"] = True
        result["pid"] = _linux_pid(layout)
    return result


def _require_systemctl() -> None:
    if shutil.which("systemctl") is None:
        raise ServiceError("systemctl is required for the Linux background service")


def _install_linux(repo: Path) -> dict[str, Any]:
    _require_systemctl()
    existing = runtime_layout(repo)
    if _linux_running(existing):
        _systemctl("stop", _linux_unit_name(existing))
        _wait_for(lambda: _linux_running(existing), False, f"systemd service {_linux_unit_name(existing)}")

    layout = prepare_runtime(repo)
    launcher = _posix_launcher_path(layout)
    launcher.write_text(_posix_launcher_text(layout), encoding="utf-8")
    os.chmod(launcher, 0o700)

    unit = linux_service_definition_path(layout.label)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(_systemd_unit(layout), encoding="utf-8")
    os.chmod(unit, 0o644)

    _systemctl("daemon-reload")
    _systemctl("enable", "--now", _linux_unit_name(layout))
    _wait_for(lambda: _linux_running(layout), True, f"systemd service {_linux_unit_name(layout)}")
    return _linux_status(repo, layout)


def _stop_linux(repo: Path) -> dict[str, Any]:
    layout = runtime_layout(repo)
    unit = linux_service_definition_path(layout.label)
    if unit.is_file() and _linux_running(layout):
        _systemctl("stop", _linux_unit_name(layout))
        _wait_for(lambda: _linux_running(layout), False, f"systemd service {_linux_unit_name(layout)}")
    return _linux_status(repo, layout)


def _restart_linux(repo: Path) -> dict[str, Any]:
    _require_systemctl()
    layout = runtime_layout(repo)
    unit = linux_service_definition_path(layout.label)
    if not unit.is_file():
        raise ServiceError("Do Again is not installed for this repository; run do-again install first")
    _systemctl("restart", _linux_unit_name(layout))
    _wait_for(lambda: _linux_running(layout), True, f"systemd service {_linux_unit_name(layout)}")
    return _linux_status(repo, layout)


def _uninstall_linux(repo: Path) -> dict[str, Any]:
    layout = runtime_layout(repo)
    unit = linux_service_definition_path(layout.label)
    if shutil.which("systemctl") is not None:
        _systemctl("disable", "--now", _linux_unit_name(layout), check=False)
    try:
        unit.unlink()
    except FileNotFoundError:
        pass
    if shutil.which("systemctl") is not None:
        _systemctl("daemon-reload", check=False)
    _cleanup_runtime(repo, layout)
    return {"repo": str(repo), "label": layout.label, "installed": False, "running": False}


# Windows / Task Scheduler

def _windows_task(layout: RuntimeLayout) -> str:
    return windows_service_name(layout.label)


def _windows_launcher_path(layout: RuntimeLayout) -> Path:
    return layout.root / "run-agent.cmd"


def _windows_launcher_text(layout: RuntimeLayout) -> str:
    command = subprocess.list2cmdline(_agent_argv(layout))
    pythonpath = str(layout.runtime_source).replace("%", "%%")
    repo = str(layout.repo).replace("%", "%%")
    return (
        "@echo off\r\n"
        f'set "PYTHONPATH={pythonpath}"\r\n'
        'set "PYTHONDONTWRITEBYTECODE=1"\r\n'
        'set "PYTHONUNBUFFERED=1"\r\n'
        f'cd /d "{repo}"\r\n'
        f"{command}\r\n"
    )


def _schtasks(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["schtasks", *args], check=check)


def _windows_task_exists(layout: RuntimeLayout) -> bool:
    return _schtasks("/Query", "/TN", _windows_task(layout), check=False).returncode == 0


def _windows_task_state(layout: RuntimeLayout) -> str:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell:
        task = _windows_task(layout).replace("'", "''")
        proc = _run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"$t=Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
                "if ($null -ne $t) { [Console]::Out.Write($t.State) }",
            ],
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().lower()

    proc = _schtasks("/Query", "/TN", _windows_task(layout), "/FO", "LIST", "/V", check=False)
    if proc.returncode != 0:
        return ""
    match = re.search(r"(?im)^Status:\s*(.+?)\s*$", proc.stdout)
    return match.group(1).strip().lower() if match else ""


def _windows_running(layout: RuntimeLayout) -> bool:
    return _windows_task_state(layout) == "running"


def _windows_status(repo: Path, layout: RuntimeLayout) -> dict[str, Any]:
    result = _base_status(repo, layout)
    result["installed"] = _windows_task_exists(layout)
    if result["installed"]:
        result["running"] = _windows_running(layout)
    return result


def _require_schtasks() -> None:
    if shutil.which("schtasks") is None:
        raise ServiceError("schtasks is required for the Windows background service")


def _install_windows(repo: Path) -> dict[str, Any]:
    _require_schtasks()
    existing = runtime_layout(repo)
    if _windows_task_exists(existing):
        if _windows_running(existing):
            _schtasks("/End", "/TN", _windows_task(existing), check=False)
            _wait_for(lambda: _windows_running(existing), False, f"scheduled task {_windows_task(existing)}")
        _schtasks("/Delete", "/TN", _windows_task(existing), "/F", check=False)

    layout = prepare_runtime(repo)
    launcher = _windows_launcher_path(layout)
    launcher.parent.mkdir(parents=True, exist_ok=True)
    with launcher.open("w", encoding="utf-8", newline="") as handle:
        handle.write(_windows_launcher_text(layout))

    _schtasks(
        "/Create",
        "/TN",
        _windows_task(layout),
        "/SC",
        "ONLOGON",
        "/TR",
        f'"{launcher}"',
        "/F",
    )
    _schtasks("/Run", "/TN", _windows_task(layout))
    _wait_for(lambda: _windows_running(layout), True, f"scheduled task {_windows_task(layout)}")
    return _windows_status(repo, layout)


def _stop_windows(repo: Path) -> dict[str, Any]:
    layout = runtime_layout(repo)
    if _windows_task_exists(layout) and _windows_running(layout):
        proc = _schtasks("/End", "/TN", _windows_task(layout), check=False)
        if proc.returncode != 0:
            raise ServiceError(f"schtasks /End failed: {(proc.stderr or proc.stdout).strip()}")
        _wait_for(lambda: _windows_running(layout), False, f"scheduled task {_windows_task(layout)}")
    return _windows_status(repo, layout)


def _restart_windows(repo: Path) -> dict[str, Any]:
    _require_schtasks()
    layout = runtime_layout(repo)
    if not _windows_task_exists(layout):
        raise ServiceError("Do Again is not installed for this repository; run do-again install first")
    if _windows_running(layout):
        _schtasks("/End", "/TN", _windows_task(layout))
        _wait_for(lambda: _windows_running(layout), False, f"scheduled task {_windows_task(layout)}")
    _schtasks("/Run", "/TN", _windows_task(layout))
    _wait_for(lambda: _windows_running(layout), True, f"scheduled task {_windows_task(layout)}")
    return _windows_status(repo, layout)


def _uninstall_windows(repo: Path) -> dict[str, Any]:
    layout = runtime_layout(repo)
    if _windows_task_exists(layout):
        _schtasks("/End", "/TN", _windows_task(layout), check=False)
        _schtasks("/Delete", "/TN", _windows_task(layout), "/F", check=False)
    _cleanup_runtime(repo, layout)
    return {"repo": str(repo), "label": layout.label, "installed": False, "running": False}


def _require_supported_background_platform() -> str:
    info = detect_platform()
    if info.name not in {"macos", "linux", "windows"}:
        raise ServiceError(f"background service lifecycle is unsupported on {info.name}")
    return info.name


def service_status(path: str | Path = ".") -> dict[str, Any]:
    repo = find_repo(path)
    layout = runtime_layout(repo)
    platform_name = detect_platform().name
    if platform_name == "macos":
        return _macos_status(repo, layout)
    if platform_name == "linux":
        return _linux_status(repo, layout)
    if platform_name == "windows":
        return _windows_status(repo, layout)
    return _base_status(repo, layout)


def install_service(path: str | Path = ".") -> dict[str, Any]:
    repo = find_repo(path)
    platform_name = _require_supported_background_platform()
    if platform_name == "macos":
        return _install_macos(repo)
    if platform_name == "linux":
        return _install_linux(repo)
    return _install_windows(repo)


def stop_service(path: str | Path = ".") -> dict[str, Any]:
    repo = find_repo(path)
    platform_name = _require_supported_background_platform()
    if platform_name == "macos":
        return _stop_macos(repo)
    if platform_name == "linux":
        return _stop_linux(repo)
    return _stop_windows(repo)


def restart_service(path: str | Path = ".") -> dict[str, Any]:
    repo = find_repo(path)
    platform_name = _require_supported_background_platform()
    if platform_name == "macos":
        return _restart_macos(repo)
    if platform_name == "linux":
        return _restart_linux(repo)
    return _restart_windows(repo)


def uninstall_service(path: str | Path = ".") -> dict[str, Any]:
    repo = find_repo(path)
    platform_name = _require_supported_background_platform()
    if platform_name == "macos":
        return _uninstall_macos(repo)
    if platform_name == "linux":
        return _uninstall_linux(repo)
    return _uninstall_windows(repo)


def run_foreground(path: str | Path = ".", *, once: bool = False) -> int:
    repo = find_repo(path)
    if service_status(repo)["running"]:
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
