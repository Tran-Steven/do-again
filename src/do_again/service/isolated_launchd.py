from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time

from ..core.schema import OperatorError, atomic_json, utc_now
from .isolated_enrollment import _private_layout, enrolled_config, run_enrolled_isolated


def _service_layout(source_repo: Path) -> tuple[dict, dict, str, Path]:
    layout, config = enrolled_config(source_repo)
    if sys.platform != "darwin" or not hasattr(os, "getuid"):
        raise OperatorError("isolated background installation currently requires macOS")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise OperatorError("isolated background service must not run as root")
    label = "io.github.tran-steven.do-again.isolated." + layout["key"]
    plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    if plist.is_symlink():
        raise OperatorError("isolated service plist must not be a symlink")
    return layout, config, label, plist


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorError("macOS user launchctl is unavailable") from exc
    if check and proc.returncode:
        raise OperatorError("isolated launchd action failed: " + (proc.stderr or proc.stdout).strip()[:300])
    return proc


def _domain() -> str:
    return "gui/" + str(os.getuid())


def _service_loaded(label: str) -> bool:
    return _launchctl("print", _domain() + "/" + label, check=False).returncode == 0


def _runtime_dir(layout: dict) -> Path:
    return layout["root"] / "runtime"


def _runtime_status(layout: dict) -> dict:
    path = layout["root"] / "run-status.json"
    if not path.exists():
        return {"state": "not_started"}
    if path.is_symlink():
        raise OperatorError("isolated runtime status is aliased")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperatorError("isolated runtime status is unreadable") from exc
    if not isinstance(value, dict):
        raise OperatorError("isolated runtime status must be an object")
    return value


def _verified_plist(path: Path, label: str) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise OperatorError("isolated launchd configuration is unsafe")
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise OperatorError("isolated launchd configuration is unreadable") from exc
    if not isinstance(value, dict) or value.get("Label") != label:
        raise OperatorError("existing launchd service is not owned by this isolated project")
    return value


def _expected_plist(layout: dict, label: str, python: str) -> dict:
    root = layout["root"]
    return {
        "Label": label,
        "ProgramArguments": [
            python, "-m", "do_again.cli", "isolated-service-run",
            "--source", str(layout["source_repo"]),
        ],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONPATH": str(_runtime_dir(layout)),
            "DO_AGAIN_HOME": str(root.parent.parent),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        },
        "ProcessType": "Background",
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(root / "logs" / "isolated.out.log"),
        "StandardErrorPath": str(root / "logs" / "isolated.err.log"),
    }


def _ensure_packaged_runtime(layout: dict) -> None:
    folder = _runtime_dir(layout)
    target = folder / "do_again"
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not (target / "__init__.py").is_file():
            raise OperatorError("existing isolated runtime snapshot is unsafe")
        return
    if folder.is_symlink():
        raise OperatorError("isolated runtime folder cannot be an alias")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    package = Path(__file__).resolve().parents[1]
    shutil.copytree(
        package, target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def install_isolated_service(source_repo: Path) -> dict:
    layout, config, label, plist = _service_layout(source_repo)
    python = str(Path(sys.executable).resolve())
    if not Path(python).is_file():
        raise OperatorError("isolated service Python executable is unavailable")
    expected = _expected_plist(layout, label, python)
    existing = _verified_plist(plist, label)
    if existing is not None and existing != expected:
        raise OperatorError("existing isolated service definition differs; no replacement")
    _ensure_packaged_runtime(layout)
    logs = layout["root"] / "logs"
    if logs.is_symlink():
        raise OperatorError("isolated service logs directory cannot be an alias")
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    if existing is None:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps(expected, sort_keys=True))
        os.chmod(plist, 0o644)
    if not _service_loaded(label):
        _launchctl("bootstrap", _domain(), str(plist))
    return isolated_service_status(source_repo)


def start_isolated_service(source_repo: Path) -> dict:
    layout, config, label, plist = _service_layout(source_repo)
    expected = _expected_plist(layout, label, str(Path(sys.executable).resolve()))
    installed = _verified_plist(plist, label)
    if installed is None:
        raise OperatorError("isolated service not installed; enroll and install first")
    if installed != expected:
        raise OperatorError("installed isolated service no longer matches its approved definition")
    status = isolated_service_status(source_repo)
    if status["process_running"]:
        return status
    if status["launchd_loaded"]:
        _launchctl("kickstart", _domain() + "/" + label)
    else:
        _launchctl("bootstrap", _domain(), str(plist))
    return isolated_service_status(source_repo)


def stop_isolated_service(source_repo: Path) -> dict:
    layout, config, label, plist = _service_layout(source_repo)
    if _service_loaded(label):
        _launchctl("bootout", _domain() + "/" + label)
    return isolated_service_status(source_repo)


def isolated_service_status(source_repo: Path) -> dict:
    layout, config, label, plist = _service_layout(source_repo)
    loaded = _service_loaded(label)
    pid = None
    if loaded:
        details = _launchctl("print", _domain() + "/" + label, check=False)
        found = re.search(r"\bpid\s*=\s*(\d+)", details.stdout)
        if found:
            pid = int(found.group(1))
    return {
        "state": "installed" if _verified_plist(plist, label) is not None else "not_installed",
        "label": label,
        "installed": plist.exists(),
        "launchd_loaded": loaded,
        "process_running": pid is not None,
        "pid": pid,
        "source_repo": str(layout["source_repo"]),
        "run": _runtime_status(layout),
    }


def serve_isolated_enrollment(source_repo: Path) -> dict:
    try:
        layout, config = enrolled_config(source_repo)
    except Exception as exc:
        layout = _private_layout(source_repo)
        status = layout["root"] / "run-status.json"
        blocked = {
            "schema_version": 1,
            "state": "blocked",
            "source_repo": str(layout["source_repo"]),
            "error": type(exc).__name__ + ": " + str(exc)[:500],
            "completed_at_utc": utc_now().isoformat(),
        }
        atomic_json(status, blocked)
        return blocked
    status = layout["root"] / "run-status.json"
    initial = {
        "schema_version": 1,
        "state": "running",
        "source_repo": str(layout["source_repo"]),
        "pid": os.getpid(),
        "started_at_utc": utc_now().isoformat(),
    }
    atomic_json(status, initial)
    try:
        result = run_enrolled_isolated(source_repo)
        latest = {
            **initial,
            "state": result["state"],
            "completed_cycles": result.get("completed_cycles", 0),
            "request_id": result.get("request_id"),
            "completed_at_utc": utc_now().isoformat(),
        }
    except Exception as exc:
        latest = {
            **initial,
            "state": "blocked",
            "error": type(exc).__name__ + ": " + str(exc)[:500],
            "completed_at_utc": utc_now().isoformat(),
        }
    atomic_json(status, latest)
    return latest
