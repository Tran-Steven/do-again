"""Bounded, operator-owned V1A continuation. The root broker retains all authority."""
from __future__ import annotations

from datetime import datetime, timezone
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time

from ..core.schema import atomic_json
from ..supervisor.macos_client import broker_request
from ..supervisor.macos_execution import ExecutionBlocked


LABEL = "io.github.tran-steven.do-again.v1a-jobpipe-runner"
SOURCE = re.compile(r"[a-f0-9]{40}\Z")
POLL_SECONDS = 20
MAX_SECONDS = 6600


def _target(repo):
    repo = Path(repo).expanduser().resolve()
    if (sys.platform != "darwin" or os.geteuid() != os.getuid()
            or os.getuid() != 501 or repo != Path.home().resolve() / "jobpipe"
            or not repo.is_dir()):
        raise ExecutionBlocked("V1A background runner requires the original jobpipe operator identity")
    return repo


def _root():
    parent = Path.home() / ".do_again"
    root = parent / "v1a-jobpipe-runner"
    for directory in (parent, root):
        if directory.is_symlink():
            raise ExecutionBlocked("V1A runner state has an alias")
        if not directory.exists():
            directory.mkdir(mode=0o700)
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ExecutionBlocked("V1A runner state is not private")
    return root


def _authorized(repo, source, epoch):
    status = broker_request(repo, {"operation": "status"})
    if (status.get("operator_intent") != "active"
            or status.get("production_ready") is not False
            or status.get("enforcement_verified") is not True
            or status.get("source_sha") != source
            or status.get("epoch") != epoch):
        raise ExecutionBlocked("native controller must be active and qualified at the exact approved source/epoch")
    return status


def _write_status(root, state, source, epoch, cycles=0, error=None):
    record = {
        "state": state,
        "source_sha": source,
        "epoch": epoch,
        "acknowledged_cycles": cycles,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "production_deployed": False,
        "application_submissions": False,
        "automatic_merge": False,
    }
    if error is not None:
        record["blocker"] = error
    atomic_json(root / "status.json", record)
    return record


def run_bounded(repo, *, source, epoch, poll_seconds=POLL_SECONDS,
                max_seconds=MAX_SECONDS):
    repo = _target(repo)
    if fcntl is None:
        raise ExecutionBlocked("V1A runner lock is unsupported on this platform")
    if (not isinstance(source, str) or SOURCE.fullmatch(source) is None
            or type(epoch) is not int or epoch <= 0
            or type(poll_seconds) is not int or not 1 <= poll_seconds <= 300
            or type(max_seconds) is not int or not 1 <= max_seconds <= MAX_SECONDS):
        raise ExecutionBlocked("V1A runner requires an exact bounded source/epoch")
    root = _root()
    lock = root / "runner.lock"
    if lock.is_symlink():
        raise ExecutionBlocked("V1A runner lock has an alias")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid() or os.fstat(fd).st_nlink != 1:
            raise ExecutionBlocked("V1A runner lock ownership differs")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionBlocked("V1A runner already active") from exc
        started = time.monotonic()
        cycles = 0
        try:
            _authorized(repo, source, epoch)
            state = broker_request(repo, {"operation": "controller_status"})
            if state.get("state") == "stopped":
                state = broker_request(repo, {"operation": "controller_start"})
            while True:
                _authorized(repo, source, epoch)
                phase = state.get("state")
                cycles = len(state.get("acknowledged", []))
                if phase == "completed":
                    return _write_status(root, "completed", source, epoch, cycles)
                if phase in {"blocked", "stopped"}:
                    raise ExecutionBlocked("controller " + str(phase) + "; original journal preserved")
                if phase not in {"planning", "waiting"}:
                    raise ExecutionBlocked("unrecognized controller phase")
                if time.monotonic() - started >= max_seconds:
                    return _write_status(root, "time_budget_exhausted", source, epoch, cycles)
                _write_status(root, phase, source, epoch, cycles)
                state = broker_request(repo, {"operation": "controller_tick"})
                if state.get("state") in {"blocked", "stopped", "completed"}:
                    continue
                time.sleep(poll_seconds)
        except Exception as exc:
            _write_status(root, "blocked", source, epoch, cycles,
                          type(exc).__name__ + ": " + str(exc)[:300])
            raise
    finally:
        os.close(fd)


def background_start(repo):
    repo = _target(repo)
    root = _root()
    status = broker_request(repo, {"operation": "status"})
    source = status.get("source_sha")
    epoch = status.get("epoch")
    if (not isinstance(source, str) or SOURCE.fullmatch(source) is None
            or type(epoch) is not int):
        raise ExecutionBlocked("sealed source or epoch unavailable")
    _authorized(repo, source, epoch)
    controller = broker_request(repo, {"operation": "controller_status"})
    if controller.get("state") in {"blocked", "completed"}:
        raise ExecutionBlocked("controller cannot be restarted without explicit reconciliation")
    label = LABEL
    plist_path = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    if plist_path.is_symlink():
        raise ExecutionBlocked("V1A launchd service file has an alias")
    definition = {
        "Label": label,
        "ProgramArguments": [str(Path(sys.executable).resolve()), "-m",
                             "do_again.cli", "controller", "run", str(repo),
                             "--source-sha", source, "--epoch", str(epoch)],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"HOME": str(Path.home()),
                                 "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                                 "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "StandardOutPath": str(root / "runner.out.log"),
        "StandardErrorPath": str(root / "runner.err.log"),
    }
    if plist_path.exists():
        if plistlib.loads(plist_path.read_bytes()) != definition:
            raise ExecutionBlocked("previous V1A service has a different frozen source/epoch")
    else:
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        with plist_path.open("xb") as handle:
            handle.write(plistlib.dumps(definition))
        plist_path.chmod(0o644)
    domain = "gui/" + str(os.getuid())
    loaded = subprocess.run(["launchctl", "print", domain + "/" + label],
                            capture_output=True, timeout=20)
    if loaded.returncode:
        result = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)],
                                capture_output=True, timeout=20)
        if result.returncode:
            raise ExecutionBlocked("user V1A launchd bootstrap failed; existing state retained")
    else:
        result = subprocess.run(["launchctl", "kickstart", domain + "/" + label],
                                capture_output=True, timeout=20)
        if result.returncode:
            raise ExecutionBlocked("user V1A runner kickstart failed; state retained")
    return {"state": "launched", "source_sha": source, "epoch": epoch,
            "label": label, "status_file": str(root / "status.json")}


def background_status(repo):
    _target(repo)
    root = _root()
    status = root / "status.json"
    if status.is_symlink():
        raise ExecutionBlocked("V1A runner status has an alias")
    if not status.exists():
        return {"state": "not_started"}
    record = json.loads(status.read_text())
    if not isinstance(record, dict):
        raise ExecutionBlocked("V1A runner status is unreadable")
    return record


def background_stop(repo):
    _target(repo)
    domain = "gui/" + str(os.getuid())
    result = subprocess.run(["launchctl", "bootout", domain + "/" + LABEL],
                            capture_output=True, timeout=20)
    if result.returncode:
        raise ExecutionBlocked("V1A runner service was not loaded; no controller mutation")
    return {"state": "runner_stopped", "controller_stop_sent": False,
            "original_controller_journal_preserved": True}
