from __future__ import annotations

import json
import re
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from ..browser import cdp
from ..browser import runtime as browser
from ..core.schema import atomic_json


def _settings(repo: Path) -> dict[str, Any]:
    path = repo / "do-again.toml"
    if not path.is_file():
        return {"continuous": False}
    content = tomllib.loads(path.read_text(encoding="utf-8"))
    section = content.get("do_again", {})
    if not isinstance(section, dict):
        raise ValueError("invalid do-again configuration")
    enabled = section.get("continuous", False)
    report = section.get("report_stalls", False)
    if not isinstance(enabled, bool) or not isinstance(report, bool):
        raise ValueError("continuous and report_stalls must be boolean")
    issue_repo = str(section.get("stall_issue_repo", "")).strip()
    if report and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", issue_repo):
        raise ValueError("stall_issue_repo must identify an owner/repository")
    idle_seconds = int(section.get("idle_seconds", 1800))
    recovery_seconds = int(section.get("recovery_seconds", 900))
    if idle_seconds < 60 or recovery_seconds < 60:
        raise ValueError("liveness intervals must be at least 60 seconds")
    return {
        "continuous": enabled,
        "report_stalls": report,
        "issue_repo": issue_repo,
        "idle_seconds": idle_seconds,
        "recovery_seconds": recovery_seconds,
    }


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("invalid liveness state")
    return value


def _activity(control: Path) -> tuple[str, float, bool]:
    requests = control / "automation/do_again/requests"
    receipts = control / "automation/do_again/receipts"
    done = {p.stem for p in receipts.glob("*.json")} if receipts.is_dir() else set()
    pending = any(p.stem not in done for p in requests.glob("*.json")) if requests.is_dir() else False
    latest = max(receipts.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, default=None) if receipts.is_dir() else None
    return (latest.stem, latest.stat().st_mtime, pending) if latest is not None else ("", 0.0, pending)


def _decision(
    state: dict[str, Any],
    *,
    receipt_id: str,
    receipt_time: float,
    pending: bool,
    busy: bool,
    now: float,
    idle_seconds: int,
    recovery_seconds: int,
) -> tuple[dict[str, Any], str]:
    value = dict(state)
    if receipt_id != str(value.get("receipt_id", "")):
        value = {
            "receipt_id": receipt_id,
            "progress_at": receipt_time or now,
            "attempts": 0,
            "state": "working",
        }
    if "progress_at" not in value:
        value["progress_at"] = now
    if pending:
        value["state"] = "working"
        return value, "wait"
    if busy:
        value.setdefault("busy_since", now)
        if now - float(value["busy_since"]) >= max(1800, 2 * idle_seconds):
            value["state"] = "stalled_generating"
            return value, "report_busy"
        value["state"] = "generating"
        return value, "wait"
    value.pop("busy_since", None)
    if now - float(value["progress_at"]) < idle_seconds:
        value["state"] = "idle_grace"
        return value, "wait"
    attempts = int(value.get("attempts", 0))
    if attempts >= 2:
        if now - float(value.get("last_resume_at", 0)) < recovery_seconds:
            value["state"] = "recovering"
            return value, "wait"
        value["state"] = "stalled"
        return value, "report"
    if attempts and now - float(value.get("last_resume_at", 0)) < recovery_seconds:
        value["state"] = "recovering"
        return value, "wait"
    value["state"] = "continuation_due"
    return value, "resume"


def _report_stall(repo: Path, config: dict[str, Any], state: dict[str, Any], reason: str = "idle") -> dict[str, Any]:
    now = time.time()
    if not config["report_stalls"] or now < float(state.get("next_report_at", 0)):
        return state
    slug = re.sub(r"[^a-z0-9-]", "-", repo.name.lower())[:60]
    types = {
        "idle": "No-progress after bounded continuation",
        "busy": "Conversation generation stuck",
        "pending": "Unfinished request stuck",
    }
    title = f"[do-again] {types[reason]} ({slug})"
    repository = config["issue_repo"]
    try:
        lookup = subprocess.run(
            ["gh", "issue", "list", "--repo", repository, "--state", "open", "--limit", "100", "--search", f"{title} in:title", "--json", "number,title"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        state["next_report_at"] = now + 3600
        state["issue_report"] = "unavailable"
        return state
    state["next_report_at"] = now + 3600
    if lookup.returncode != 0:
        state["issue_report"] = "unavailable"
        return state
    try:
        matches = json.loads(lookup.stdout)
    except (ValueError, TypeError):
        state["issue_report"] = "unavailable"
        return state
    for item in matches:
        if item.get("title") == title:
            state["issue_number"] = item["number"]
            state["issue_report"] = "existing"
            return state
    body = (
        "Do Again's native progress watchdog observed an active continuous project "
        "without verified development progress for a prolonged period.\n\n"
        f"Project: `{slug}`\n"
        f"Failure class: `{reason}`.\n"
        "The local watchdog identified a persistent no-progress condition.\n"
        "The watchdog has stopped prompting until new progress or an operator intervention.\n\n"
        "No candidate records, browser contents, or secrets are included. "
        "Please inspect the private runtime ledger locally and add a deterministic regression."
    )
    try:
        created = subprocess.run(
            ["gh", "issue", "create", "--repo", repository, "--title", title, "--body", body],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        state["issue_report"] = "unavailable"
        return state
    if created.returncode == 0:
        state["issue_report"] = "created"
        match = re.search(r"/issues/(\d+)", created.stdout)
        if match:
            state["issue_number"] = int(match.group(1))
    else:
        state["issue_report"] = "unavailable"
    return state


def _pending_stale(control: Path, now: float, timeout: float = 7200.0) -> bool:
    requests = control / "automation/do_again/requests"
    receipts = control / "automation/do_again/receipts"
    if not requests.is_dir():
        return False
    return any(
        not (receipts / request.name).exists()
        and now - request.stat().st_mtime > timeout
        for request in requests.glob("*.json")
    )


def check_liveness(repo: Path, control: Path, state_dir: Path) -> str:
    config = _settings(repo)
    if not config["continuous"]:
        return "disabled"
    path = state_dir / "liveness.json"
    value = _read_state(path)
    receipt_id, receipt_time, pending = _activity(control)
    if pending:
        new_state, _ = _decision(
            value, receipt_id=receipt_id, receipt_time=receipt_time, pending=True,
            busy=False, now=time.time(), idle_seconds=config["idle_seconds"],
            recovery_seconds=config["recovery_seconds"],
        )
        if _pending_stale(control, time.time()):
            new_state["state"] = "stalled_pending"
            new_state = _report_stall(repo, config, new_state, reason="pending")
        atomic_json(path, new_state)
        return str(new_state["state"])
    session = browser.ensure_browser_running(verify_auth=True)
    record = browser.project_record(repo)
    url = str(record.get("chat_url") or "")
    if not url:
        raise browser.BrowserError("project has no bound automation chat")
    target = browser._find_chatgpt_target(int(session["port"]), url)
    if target is None:
        target = cdp.create_target(int(session["port"]), url, background=True)
        target, _ = browser.wait_for_authenticated(int(session["port"]), target=target, chat_url=url, timeout=30.0)
    busy = bool(browser._assistant_snapshot(target).get("busy"))
    value, action = _decision(
        value, receipt_id=receipt_id, receipt_time=receipt_time, pending=False,
        busy=busy, now=time.time(), idle_seconds=config["idle_seconds"],
        recovery_seconds=config["recovery_seconds"],
    )
    if action == "resume":
        if browser._context_limit_warning(target):
            raise browser.BrowserError("context limit requires rollover before continuation")
        prompt = (
            "DO_AGAIN_LIVENESS_CONTINUE: This project has an active continuous development "
            "goal, but no new Do Again execution receipt has appeared within the configured "
            "idle window. Re-read your current task, operator-control evidence, and goal; "
            "choose exactly one scoped high-value next development step with an acceptance "
            "test and issue a Do Again request. Do not claim progress without its receipt. "
            "If truly complete, explicitly explain the proof and tell the operator to disable "
            "continuous mode. If blocked, identify the precise blocker and do not repeat an "
            "unchanged action. Never submit real applications without separate authorization."
        )
        value["attempts"] = int(value.get("attempts", 0)) + 1
        value["last_resume_at"] = time.time()
        value["state"] = "recovering"
        atomic_json(path, value)
        browser.send_message(target, prompt, wait_for_response=False)
    elif action in {"report", "report_busy"}:
        value = _report_stall(repo, config, value, reason="busy" if action == "report_busy" else "idle")
    atomic_json(path, value)
    return str(value["state"])
