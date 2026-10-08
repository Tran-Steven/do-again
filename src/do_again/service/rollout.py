from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..core.schema import atomic_json, utc_now
from ..platforms.process import pid_alive
from .runtime import (
    RuntimeLayout,
    ServiceError,
    find_repo,
    restart_service,
    runtime_layout,
    service_status,
    stop_service,
)


def _tree_hash(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
    )
    for path in files:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(rel).to_bytes(4, "big"))
        digest.update(rel)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _request_state(layout: RuntimeLayout) -> dict[str, Any]:
    control = layout.control_worktree
    requests = control / "automation/do_again/requests"
    receipts = control / "automation/do_again/receipts"
    pending: list[str] = []
    started: set[str] = set()
    invalid: list[str] = []
    if requests.is_dir():
        for request in sorted(requests.glob("*.json")):
            rid = request.stem
            if not (receipts / request.name).is_file():
                pending.append(rid)
    # Agent.write_ledger is authoritative. Inspect independently of requests
    # and receipts: a crash or partial publication can leave an orphan record.
    # Retain conservative compatibility with the former preflight directory.
    for directory in (layout.state_dir / "ledger", layout.state_dir / "requests"):
        for path in sorted(directory.glob("*.json")):
            try:
                if path.is_symlink():
                    raise ValueError("execution record is a symlink")
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or record.get("state") not in {"started", "terminal"}:
                    raise ValueError("invalid execution record")
                if record["state"] == "started":
                    started.add(path.stem)
            except (OSError, ValueError):
                invalid.append(f"{directory.name}/{path.name}")
    return {"pending_requests": pending, "started_requests": sorted(started),
            "invalid_execution_records": invalid}


def _process_rows() -> list[dict[str, Any]]:
    if os.name == "nt":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            return []
        proc = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | ForEach-Object { "
             "'{0}|{1}|{2}' -f $_.ProcessId,$_.ParentProcessId,$_.CommandLine }"],
            text=True, capture_output=True, check=False, timeout=15,
        )
        rows = []
        for line in proc.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "command": parts[2]})
        return rows
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,state=,command="],
        text=True, capture_output=True, check=False, timeout=15,
    )
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            rows.append({
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "state": parts[2],
                "command": parts[3],
            })
    return rows


def _descendants(pid: int | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not pid:
        return []
    children: list[dict[str, Any]] = []
    frontier = {pid}
    seen = {pid}
    while frontier:
        next_frontier: set[int] = set()
        for row in rows:
            if row["ppid"] in frontier and row["pid"] not in seen:
                children.append(row)
                seen.add(row["pid"])
                next_frontier.add(row["pid"])
        frontier = next_frontier
    return children


def _shared_browser_child_pids(rows: list[dict[str, Any]], *, repo: Path) -> set[int]:
    """Recognize our dedicated persistent Chrome, never arbitrary Chrome or executor work.

    A managed browser is allowed to survive this one project's restart only
    when another live Do Again project owns a browser lease. Normal child
    scripts and test/browser processes remain upgrade blockers.
    """
    root = Path(os.environ.get("DO_AGAIN_HOME", str(Path.home() / ".do_again"))).expanduser().resolve()
    browser_root = root / "browser"
    state = _json(browser_root / "state.json")
    try:
        browser_pid = int(state.get("pid") or 0)
        port = int(state.get("port") or 0)
    except (ValueError, TypeError):
        return set()
    if not browser_pid or not port:
        return set()
    expected_profile = (browser_root / "profile").resolve()
    if str(state.get("profile_dir") or "") != str(expected_profile):
        return set()
    process = next((row for row in rows if row["pid"] == browser_pid), None)
    if process is None:
        return set()
    command = str(process["command"])
    if (
        "--user-data-dir=" + str(expected_profile) not in command
        or "--remote-debugging-port=" + str(port) not in command
        or "Chrome" not in command
        or not pid_alive(browser_pid)
    ):
        return set()
    other_live_leases = False
    for lease_path in (browser_root / "leases").glob("*.json"):
        lease = _json(lease_path)
        lease_repo = str(lease.get("repo") or "")
        lease_pid = lease.get("daemon_pid")
        try:
            other_pid = int(lease_pid or 0)
        except (ValueError, TypeError):
            continue
        if (
            lease.get("active") is True
            and lease_repo
            and lease_repo != str(repo.resolve())
            and other_pid > 0
            and pid_alive(other_pid)
        ):
            other_live_leases = True
            break
    if not other_live_leases:
        return set()
    return {browser_pid, *(row["pid"] for row in _descendants(browser_pid, rows))}


def assess_upgrade(repo: Path, *, process_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    layout = runtime_layout(repo)
    status = service_status(repo)
    request_state = _request_state(layout)
    outbox = layout.state_dir / "browser_outbox"
    pending_outbox = sorted(p.stem for p in outbox.glob("*.json")) if outbox.is_dir() else []
    browser = _json(layout.state_dir / "browser_status.json")
    rows = process_rows if process_rows is not None else _process_rows()
    descendants = _descendants(status.get("pid"), rows)
    # A zombie has already exited and cannot be executing user work. macOS can
    # briefly retain reaped Git children as "(git)" between quiescence checks;
    # treating those rows as live executor work can indefinitely starve a safe
    # upgrade. Rows without a state field remain blocking for compatibility
    # with injected/platform process inventories.
    descendants = [
        row for row in descendants
        if not str(row.get("state") or "").upper().startswith("Z")
    ]
    shared_browser_pids = _shared_browser_child_pids(rows, repo=repo)
    ignored_browser_children = [row for row in descendants if row["pid"] in shared_browser_pids]
    descendants = [row for row in descendants if row["pid"] not in shared_browser_pids]
    blockers: list[dict[str, Any]] = []

    if not status.get("installed"):
        blockers.append({"kind": "service_not_installed", "detail": "install the project service before staged upgrade"})
    if request_state["pending_requests"]:
        blockers.append({"kind": "pending_requests", "request_ids": request_state["pending_requests"]})
    if request_state["started_requests"]:
        blockers.append({"kind": "started_requests", "request_ids": request_state["started_requests"]})
    if request_state["invalid_execution_records"]:
        blockers.append({"kind": "invalid_execution_records", "records": request_state["invalid_execution_records"]})
    if descendants:
        blockers.append({
            "kind": "executor_children",
            "processes": [
                {"pid": row["pid"], "ppid": row["ppid"], "command": str(row["command"])[:240]}
                for row in descendants
            ],
        })
    if pending_outbox or int(browser.get("pending_receipts") or 0) > 0:
        blockers.append({
            "kind": "browser_delivery_pending",
            "request_ids": pending_outbox,
            "state": browser.get("state"),
        })
    if (layout.state_dir / "browser_submission_uncertain.json").exists():
        blockers.append({"kind": "browser_submission_uncertain"})
    if browser.get("state") in {"submission_uncertain", "browser_unresponsive"} or (
        browser.get("state") in {"queued", "recovering"} and browser.get("error")
    ):
        blockers.append({"kind": "browser_delivery_unsettled", "state": browser.get("state")})

    live_package = layout.runtime_source / "do_again"
    return {
        "repo": str(repo),
        "service": status,
        "safe": not blockers,
        "blockers": blockers,
        "pending_requests": request_state["pending_requests"],
        "started_requests": request_state["started_requests"],
        "outbox_pending": pending_outbox,
        "runtime_hash": _tree_hash(live_package),
        "ignored_shared_browser_children": [row["pid"] for row in ignored_browser_children],
        "features": {
            "liveness": (live_package / "service" / "liveness.py").is_file(),
            "rollout": (live_package / "service" / "rollout.py").is_file(),
        },
    }


def _stage_source(layout: RuntimeLayout, transaction_id: str) -> tuple[Path, str]:
    package_root = Path(__file__).resolve().parents[1]
    stage_root = layout.root / "runtime-staging" / transaction_id
    stage_package = stage_root / "do_again"
    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        package_root,
        stage_package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    expected_hash = _tree_hash(stage_package)
    if not expected_hash:
        raise ServiceError("staged runtime hash could not be computed")
    required = [
        stage_package / "service" / "daemon.py",
        stage_package / "service" / "liveness.py",
        stage_package / "service" / "rollout.py",
    ]
    missing = [str(p.relative_to(stage_package)) for p in required if not p.is_file()]
    if missing:
        raise ServiceError(f"staged runtime is missing required features: {', '.join(missing)}")
    return stage_package, expected_hash


def _incident(layout: RuntimeLayout, value: dict[str, Any]) -> None:
    ledger = layout.state_dir / "incidents"
    ledger.mkdir(parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(
        f"runtime-upgrade:{value.get('stage')}:{value.get('error_type')}".encode("utf-8")
    ).hexdigest()[:20]
    payload = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "kind": "runtime_upgrade",
        "recorded_at_utc": utc_now().isoformat(),
        "stage": value.get("stage"),
        "error_type": value.get("error_type"),
        "action": value.get("action"),
    }
    atomic_json(ledger / f"runtime-upgrade-{fingerprint}.json", payload)


def _effective_upgrade_blockers(preflight: dict[str, Any], *, offline: bool) -> list[dict[str, Any]]:
    blockers = list(preflight.get("blockers", []))
    if not offline:
        return blockers
    if preflight["service"].get("running"):
        return [{"kind": "offline_requires_stopped_service"}]
    # Queued requests without a started local ledger do not execute while
    # the service remains stopped. Every other in-flight/uncertain signal
    # remains a hard block, including started requests and browser outbox.
    return [b for b in blockers if b.get("kind") != "pending_requests"]


def staged_upgrade(
    path: str | Path = ".",
    *,
    apply: bool = False,
    offline: bool = False,
    process_rows: list[dict[str, Any]] | None = None,
    stop: Callable[[str | Path], dict[str, Any]] = stop_service,
    restart: Callable[[str | Path], dict[str, Any]] = restart_service,
) -> dict[str, Any]:
    repo = find_repo(path)
    layout = runtime_layout(repo)
    preflight = assess_upgrade(repo, process_rows=process_rows)
    effective_blockers = _effective_upgrade_blockers(preflight, offline=offline)
    result: dict[str, Any] = {
        "preflight": preflight, "applied": False,
        "deferred": bool(effective_blockers), "offline": offline,
        "effective_blockers": effective_blockers,
    }
    if not apply or effective_blockers:
        return result

    transaction_id = f"upgrade-{uuid.uuid4().hex[:16]}"
    tx_path = layout.state_dir / "runtime_upgrade.json"
    stage_package, expected_hash = _stage_source(layout, transaction_id)
    live_package = layout.runtime_source / "do_again"
    backup_package = layout.root / "runtime-backups" / transaction_id / "do_again"
    backup_package.parent.mkdir(parents=True, exist_ok=True)

    transaction = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "state": "staged",
        "repo": str(repo),
        "created_at_utc": utc_now().isoformat(),
        "expected_runtime_hash": expected_hash,
        "previous_runtime_hash": _tree_hash(live_package),
        "offline": offline,
        "queued_requests_preserved": list(preflight["pending_requests"]) if offline else [],
        "stage_package": str(stage_package),
        "backup_package": str(backup_package),
    }
    atomic_json(tx_path, transaction)

    # Re-evaluate immediately before the disruptive step; a new claim can appear after staging.
    second = assess_upgrade(repo)
    second_blockers = _effective_upgrade_blockers(second, offline=offline)
    if second_blockers:
        transaction.update(state="deferred", blockers=second_blockers, deferred_at_utc=utc_now().isoformat())
        atomic_json(tx_path, transaction)
        return {"preflight": second, "applied": False, "deferred": True, "transaction_id": transaction_id}

    was_running = bool(second["service"].get("running"))
    try:
        if was_running:
            stop(repo)
        transaction.update(state="service_stopped", service_was_running=was_running)
        atomic_json(tx_path, transaction)

        # Refuse cutover if activity appeared after stop but before swap.
        post_stop = assess_upgrade(repo)
        unsafe_after_stop = [
            b for b in _effective_upgrade_blockers(post_stop, offline=offline)
            if b.get("kind") not in {"service_not_installed"}
        ]
        if unsafe_after_stop:
            if was_running:
                restart(repo)
            transaction.update(state="deferred_after_stop", blockers=unsafe_after_stop)
            atomic_json(tx_path, transaction)
            return {"preflight": post_stop, "applied": False, "deferred": True, "transaction_id": transaction_id}

        if live_package.exists():
            os.replace(live_package, backup_package)
        os.replace(stage_package, live_package)
        transaction.update(state="runtime_swapped", swapped_at_utc=utc_now().isoformat())
        atomic_json(tx_path, transaction)

        if was_running:
            restart(repo)
        live_hash = _tree_hash(live_package)
        verification = {
            "runtime_hash": live_hash,
            "hash_matches": live_hash == expected_hash,
            "liveness": (live_package / "service" / "liveness.py").is_file(),
            "rollout": (live_package / "service" / "rollout.py").is_file(),
            "service": service_status(repo),
        }
        if not verification["hash_matches"] or not verification["liveness"] or not verification["rollout"]:
            raise ServiceError("post-upgrade runtime feature/hash verification failed")
        if was_running and not verification["service"].get("running"):
            raise ServiceError("service did not return to running state after staged upgrade")
        if offline and verification["service"].get("running"):
            raise ServiceError("offline upgrade unexpectedly started the stopped service")

        transaction.update(
            state="verified",
            verified_at_utc=utc_now().isoformat(),
            verification=verification,
        )
        atomic_json(tx_path, transaction)
        result.update(
            applied=True,
            deferred=False,
            transaction_id=transaction_id,
            verification=verification,
        )
        return result
    except Exception as exc:
        rollback_error = None
        try:
            if live_package.exists():
                failed = layout.root / "runtime-failed" / transaction_id / "do_again"
                failed.parent.mkdir(parents=True, exist_ok=True)
                os.replace(live_package, failed)
            if backup_package.exists():
                live_package.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup_package, live_package)
            if was_running:
                restart(repo)
        except Exception as rollback_exc:
            rollback_error = f"{type(rollback_exc).__name__}: {rollback_exc}"
        transaction.update(
            state="rolled_back" if rollback_error is None else "rollback_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            rollback_error=rollback_error,
            action=(
                "Previous runtime restored and restarted; inspect runtime_upgrade.json before retry."
                if rollback_error is None
                else "ROLLBACK FAILED: leave service unchanged and inspect runtime directories before intervention."
            ),
            failed_at_utc=utc_now().isoformat(),
        )
        atomic_json(tx_path, transaction)
        _incident(layout, transaction)
        raise ServiceError(transaction["action"]) from exc
