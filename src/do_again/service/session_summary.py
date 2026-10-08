"""Local, receipt-grounded, non-AI session recap.

This module deliberately never fetches ChatGPT conversation content or
presents a command's stdout as proof of a shipped feature.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..core.schema import atomic_json
from .runtime import RuntimeLayout


def _read(path: Path) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def _date(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except ValueError:
        return None


def session_start(layout: RuntimeLayout, now: datetime | None = None) -> datetime:
    """Use daemon's durable startup timestamp, otherwise a labeled 24h window."""
    now = now or datetime.now(timezone.utc)
    status = _read(layout.control_worktree / "automation/do_again/agent_status.json")
    started = _date(status.get("started_at_utc"))
    if started and started <= now:
        return started
    return now - timedelta(hours=24)


def _title(request_id: str) -> str:
    # This is a human-readable label for a request ID, *not* a feature claim.
    words = [x for x in re.split(r"[-_]+", request_id) if x]
    words = [w for w in words if not re.fullmatch(r"20\d{6}|\d{1,4}|p\d+", w)]
    if len(words) > 3:
        words = words[1:]  # omit project prefix
    return " ".join(words[:9]).strip() or request_id


def build_summary(
    layout: RuntimeLayout,
    *,
    since: datetime,
    now: datetime | None = None,
    max_items: int = 6,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if since.tzinfo is None or now.tzinfo is None or since > now:
        raise ValueError("session summary requires a valid timezone-aware interval")
    base = layout.control_worktree / "automation/do_again"
    requests = base / "requests"
    receipts = base / "receipts"
    completed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    for request in requests.glob("*.json") if requests.is_dir() else []:
        request_id = request.stem
        receipt = _read(receipts / request.name)
        raw = _read(request)
        timestamp = _date(receipt.get("finished_at_utc")) or _date(
            receipt.get("started_at_utc")
        ) or _date(raw.get("issued_at_utc"))
        entry = {
            "request_id": request_id,
            "title": _title(request_id),
            "state": str(receipt.get("state") or "pending"),
            "timestamp": timestamp.isoformat() if timestamp else "",
        }
        if not receipt:
            # Pending requests remain actionable even if created in a prior run.
            pending.append(entry)
        elif timestamp and since <= timestamp <= now:
            if entry["state"] == "succeeded":
                completed.append(entry)
            else:
                failed.append(entry)
    for group in (completed, failed, pending):
        group.sort(key=lambda x: x["timestamp"], reverse=True)

    # Git commits are evidence of code changes, but not necessarily by Do Again.
    # Never include diff contents, command stdout, applicant data, or credentials.
    commits: list[str] = []
    cmd = [
        "git", "-C", str(layout.repo), "log",
        "--since=" + since.isoformat(),
        "--until=" + now.isoformat(),
        "--no-merges", "--format=%s", "-n", str(max_items),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if proc.returncode == 0:
            commits = [line.strip()[:140] for line in proc.stdout.splitlines()
                       if line.strip()][:max_items]
    except (OSError, subprocess.TimeoutExpired):
        pass
    live = _read(layout.state_dir / "liveness.json")
    browser = _read(layout.state_dir / "browser_status.json")
    incidents = sorted(
        p.name for p in (layout.state_dir / "incidents").glob("*.json")
    ) if (layout.state_dir / "incidents").is_dir() else []
    return {
        "schema_version": 1,
        "project": layout.repo.name,
        "window_start_utc": since.isoformat(),
        "window_end_utc": now.isoformat(),
        "totals": {
            "succeeded": len(completed),
            "failed_or_blocked": len(failed),
            "pending": len(pending),
        },
        "recent_completed": completed[:max_items],
        "recent_failed": failed[:max_items],
        "recent_pending": pending[:max_items],
        "recent_project_commits": commits,
        "watchdog_state": str(live.get("state") or "not_reported"),
        "watchdog_issue_number": live.get("issue_number"),
        "browser_delivery_state": str(browser.get("state") or "not_reported"),
        "browser_pending_receipts": int(browser.get("pending_receipts") or 0),
        "local_incident_files": incidents[-max_items:],
        "disclaimer": (
            "Execution receipts show whether local requests completed; "
            "Git commits may include non-Do Again changes. Neither alone "
            "proves features deployed or end-to-end behavior."
        ),
    }


def render_summary(data: dict[str, Any]) -> str:
    totals = data["totals"]
    lines = [
        "# Do Again — Work recap",
        "",
        f"**Project:** {data['project']}",
        f"**Window:** {data['window_start_utc']} → {data['window_end_utc']}",
        "",
        "## At a glance",
        f"- {totals['succeeded']} successful request{'s' if totals['succeeded'] != 1 else ''} · "
        f"{totals['failed_or_blocked']} failed/blocked · "
        f"{totals['pending']} pending",
        f"- Watchdog: {data['watchdog_state']} · Browser delivery: "
        f"{data['browser_delivery_state']} ({data['browser_pending_receipts']} queued)",
        "",
        "## Code changes in this window",
    ]
    if data["recent_project_commits"]:
        lines.extend("- " + subject for subject in data["recent_project_commits"])
    else:
        lines.append("- No project commits recorded in this window")
    lines += ["", "## Completed requests (with receipts)"]
    if data["recent_completed"]:
        lines.extend("- " + item["title"] + " (" + item["request_id"] + ")"
                     for item in data["recent_completed"])
    else:
        lines.append("- None recorded")
    lines += ["", "## Remaining / needs attention"]
    if data["recent_pending"]:
        lines.extend("- Pending request: " + item["request_id"] for item in data["recent_pending"])
    if data["watchdog_state"].startswith("stalled"):
        lines.append("- Watchdog needs attention: " + data["watchdog_state"])
    if data.get("watchdog_issue_number"):
        lines.append(f"- Watchdog incident: #{data['watchdog_issue_number']}")
    if data["browser_pending_receipts"]:
        lines.append(f"- {data['browser_pending_receipts']} receipts need delivery/reconciliation")
    if data["local_incident_files"]:
        lines.append(f"- {len(data['local_incident_files'])} recent local incident record(s) require review")
    if not any((data["recent_pending"], data["watchdog_state"].startswith("stalled"),
                data.get("watchdog_issue_number"), data["browser_pending_receipts"],
                data["local_incident_files"])):
        lines.append("- No pending requests or local runtime alerts identified")
    lines += ["", "## Failed attempts (history, may have been superseded)"]
    if data["recent_failed"]:
        lines.extend("- " + item["request_id"] for item in data["recent_failed"])
    else:
        lines.append("- None recorded in this window")
    lines += ["", "## Suggested next step"]
    if data["browser_pending_receipts"]:
        lines.append("- Reconcile receipt delivery before resuming or upgrading")
    elif data["watchdog_state"].startswith("stalled"):
        lines.append("- Inspect the watchdog incident and verify a fresh execution receipt")
    elif data["recent_pending"]:
        lines.append("- Finish or inspect the oldest pending request before ending the work")
    else:
        lines.append("- Review completed changes and verify end-to-end behavior before calling features shipped")
    lines += ["", "## What this confirms", data["disclaimer"], ""]
    return "\n".join(lines)


def save_summary(layout: RuntimeLayout, data: dict[str, Any], rendered: str) -> Path:
    """Only write local state. Never upload private recaps to control/GitHub."""
    path = layout.state_dir / "session_reports"
    path.mkdir(parents=True, exist_ok=True)
    # Date suffices for the stable latest report; every stop also gets a
    # timestamped immutable snapshot for later inspection.
    name = datetime.fromisoformat(data["window_end_utc"]).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = path / f"{name}.md"
    report_path.write_text(rendered, encoding="utf-8")
    (path / "latest.md").write_text(rendered, encoding="utf-8")
    atomic_json(path / "latest.json", data)
    return report_path
