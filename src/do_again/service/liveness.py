from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from ..browser import cdp
from ..browser import runtime as browser
from ..core.schema import atomic_json, parse_utc, OperatorError


def _queue_continuation(state_dir, prompt, *, purpose, marker, binding):
    from .daemon import _queue_continuation as enqueue
    return enqueue(state_dir, prompt, purpose=purpose, marker=marker, binding=binding)


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
    def completed(path):
        try:
            value=json.loads(path.read_text())
            stamp=value.get('finished_at_utc')
            if stamp:return parse_utc(stamp).timestamp()
        except (OSError,ValueError,TypeError,OperatorError):return 0.0
        return path.stat().st_mtime
    latest = max(receipts.glob("*.json"), key=completed, default=None) if receipts.is_dir() else None
    return (latest.stem, completed(latest), pending) if latest is not None else ("", 0.0, pending)


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
        # Execution completion is activity, not acceptance-verified progress.
        # Preserve continuation uncertainty and retry budget across no-op,
        # failed, or newly imported receipts.
        value.update(receipt_id=receipt_id, last_execution_at=receipt_time,
                     state="working")
    if "progress_at" not in value:
        # Initial watchdog baseline; subsequent receipts never advance it.
        value["progress_at"] = receipt_time or now
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
    # A browser send can have succeeded before its CDP response timed out.
    # Never submit the same idle-continuation intent a second time merely
    # because no Git receipt has arrived; escalate for operator inspection.
    if attempts:
        if now - float(value.get("last_resume_at", 0)) < recovery_seconds:
            value["state"] = "recovering"
            return value, "wait"
        value["state"] = "stalled_idle_handoff"
        return value, "report"
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
        "ci": "CI handoff unacknowledged after browser submission",
    }
    title = f"[do-again] {types[reason]} ({slug})"
    repository = config["issue_repo"]
    try:
        lookup = subprocess.run(
            ["gh", "issue", "list", "--repo", repository, "--state", "open", "--limit", "1000", "--json", "number,title"],
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


def _latest_goal(control: Path) -> dict[str, Any]:
    requests = control / "automation/do_again/requests"
    if not requests.is_dir():
        return {}
    candidates = []
    for path in requests.glob("*.json"):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                continue
            issued = parse_utc(request["issued_at_utc"]) if request.get("issued_at_utc") else None
            # Broker imports change mtimes; use original causal chronology.
            timestamp = issued.timestamp() if issued else path.stat().st_mtime
            candidates.append((timestamp, path.name, path, request))
        except (OSError, ValueError, TypeError, OperatorError):
            continue
    for _, _, path, request in sorted(candidates, key=lambda item: item[:2], reverse=True):
        continuation = request.get("continuation")
        if not isinstance(continuation, dict):
            continue
        goal_state = str(continuation.get("goal_state") or "in_progress")
        result = {
            "request_id": str(request.get("request_id") or path.stem),
            "goal_state": goal_state,
            "goal_id": continuation.get("goal_id"),
        }
        if isinstance(continuation.get("ci"), dict):
            result["ci"] = dict(continuation["ci"])
        return result
    return {}


def _github_actions_run(ci: dict[str, Any]) -> dict[str, Any]:
    repository = ci.get("repository")
    run_id = ci.get("run_id")
    head_sha = ci.get("head_sha")
    if (
        not isinstance(repository, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository)
        or any(part in {".", ".."} for part in repository.split("/"))
        or type(run_id) is not int
        or run_id <= 0
        or not isinstance(head_sha, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha)
    ):
        return {"state": "invalid", "error": "invalid durable CI wait metadata"}
    expected_head = head_sha.lower()
    try:
        proc = subprocess.run(
            [
                "gh", "api",
                f"repos/{repository}/actions/runs/{run_id}",
                "--jq", "{status:.status,conclusion:.conclusion,head_sha:.head_sha}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "unavailable", "error": type(exc).__name__}
    if proc.returncode != 0:
        return {"state": "unavailable", "error": "github_actions_query_failed"}
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"state": "unavailable", "error": "github_actions_invalid_response"}
    if not isinstance(value, dict):
        return {"state": "unavailable", "error": "github_actions_invalid_response"}
    actual_head = str(value.get("head_sha") or "").lower()
    if actual_head != expected_head:
        return {
            "state": "mismatch",
            "status": value.get("status"),
            "conclusion": value.get("conclusion"),
            "head_sha": actual_head,
        }
    status = str(value.get("status") or "")
    conclusion = value.get("conclusion")
    if status != "completed":
        return {"state": "waiting", "status": status, "head_sha": actual_head}
    return {
        "state": "terminal",
        "status": status,
        "conclusion": conclusion,
        "head_sha": actual_head,
    }


def _handle_goal_lifecycle(
    repo: Path,
    control: Path,
    state_dir: Path,
    state: dict[str, Any],
    config: dict[str, Any],
    *, ci_probe=None, reporter=None,
) -> tuple[dict[str, Any], str | None]:
    report = reporter or _report_stall
    goal = _latest_goal(control)
    goal_state = str(goal.get("goal_state") or "in_progress")
    state["goal_state"] = goal_state
    if goal.get("goal_id") is not None:
        state["goal_id"] = goal.get("goal_id")
    else:
        state.pop("goal_id", None)

    if goal_state in {"completed", "validated_complete", "paused"}:
        state["state"] = goal_state
        return state, "stop"

    if goal_state == "blocked":
        state["state"] = "blocked"
        return state, "stop"

    if goal_state == "waiting_for_execution":
        # This lifecycle metadata lives in an old request. It cannot freeze
        # the entire continuous project forever after its own receipt and
        # later requests have completed. Check the actual Git-backed queue.
        goal_request_id = str(goal.get("request_id") or "")
        receipt_path = control / "automation/do_again/receipts" / (goal_request_id + ".json")
        latest_receipt_id, latest_receipt_time, pending = _activity(control)
        if pending:
            if _pending_stale(control, time.time()):
                state["state"] = "stalled_pending"
                state = report(repo, config, state, reason="pending")
                return state, "stop"
            state["state"] = "waiting_for_execution"
            return state, "wait"
        if receipt_path.is_file():
            completed_at = receipt_path.stat().st_mtime
            if (latest_receipt_id != goal_request_id and latest_receipt_time > completed_at) or (
                time.time() - completed_at >= config["recovery_seconds"]
            ):
                # No requests outstanding and the supposed execution has
                # already finished. Resume normal idle-watchdog accounting.
                state["stale_goal_superseded"] = goal_request_id
                state["goal_state"] = "in_progress"
                return state, None
        else:
            # An incoherent control snapshot is not authority to replay work.
            state["state"] = "stalled_execution_handoff"
            state["execution_error"] = "Waiting goal has no matching receipt or unfinished request"
            state = report(repo, config, state, reason="pending")
            return state, "stop"
        state["state"] = "waiting_for_execution"
        return state, "wait"

    if goal_state != "waiting_for_ci":
        return state, None

    ci = goal.get("ci")
    if not isinstance(ci, dict):
        state["state"] = "blocked"
        state["ci_error"] = "waiting_for_ci has no durable CI metadata"
        return state, "stop"

    probe = (ci_probe or _github_actions_run)(ci)
    state["ci"] = {
        "repository": ci.get("repository"),
        "run_id": ci.get("run_id"),
        "head_sha": ci.get("head_sha"),
        "probe_state": probe.get("state"),
        "status": probe.get("status"),
        "conclusion": probe.get("conclusion"),
    }
    state["ci_checked_at"] = time.time()

    if probe["state"] == "waiting":
        state["state"] = "waiting_for_ci"
        return state, "wait"
    if probe["state"] in {"unavailable", "mismatch", "invalid"}:
        state["state"] = "blocked"
        state["ci_error"] = probe.get("error") or probe["state"]
        return state, "stop"

    fingerprint = f"{ci.get('repository')}:{ci.get('run_id')}:{ci.get('head_sha')}:{probe.get('conclusion')}"
    now = time.time()
    if state.get("ci_terminal_fingerprint") == fingerprint:
        # A terminal CI result is NOT evidence that the wake-up message was
        # accepted, acknowledged, or acted on. Never blindly replay it.
        # Persisted unknown delivery survives daemon restarts.
        age = now - float(state.get("ci_terminal_at", now))
        if age >= max(60, int(config["recovery_seconds"])):
            state["state"] = "stalled_ci_handoff"
            state["ci_error"] = "CI is terminal, but no subsequent goal transition was verified"
            state = report(repo, config, state, reason="ci")
            return state, "stop"
        phase = str(state.get("ci_delivery_phase") or "unknown")
        state["state"] = ("ci_delivery_uncertain" if phase == "uncertain"
                          else "ci_handoff_observed" if phase == "observed"
                          else "recovering")
        return state, "reconcile_ci"

    # Persist before attempting any side-effect. A crash or uncertain send
    # can never trigger a second submission of the same terminal CI event.
    state["ci_terminal_fingerprint"] = fingerprint
    state["ci_terminal_at"] = now
    state["ci_conclusion"] = probe.get("conclusion")
    state["ci_delivery_phase"] = "uncertain"
    state["ci_delivery_marker"] = "DO_AGAIN_CI_GATE_COMPLETE token=" + hashlib.sha256(
        fingerprint.encode("utf-8")
    ).hexdigest()[:20]
    state["state"] = "ci_continuation_due"
    atomic_json(state_dir / "liveness.json", state)
    return state, "resume_ci"


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


def check_liveness(repo: Path, control: Path, state_dir: Path, *, ci_probe=None, reporter=None, settings=None) -> str:
    # Decisions serialize separately from outbox effects; queueing must never
    # recursively take the same non-reentrant delivery lock.
    with browser._file_lock(state_dir / 'liveness.lock', timeout=5.0):
        return _check_liveness_locked(repo, control, state_dir, ci_probe=ci_probe,
                                      reporter=reporter, settings=settings)


def _check_liveness_locked(repo: Path, control: Path, state_dir: Path, *, ci_probe=None, reporter=None, settings=None) -> str:
    config = settings if settings is not None else _settings(repo)
    report = reporter or _report_stall
    if not config["continuous"]:
        return "disabled"
    path = state_dir / "liveness.json"
    value = _read_state(path)
    receipt_id, receipt_time, pending = _activity(control)
    value.update(last_receipt_id=receipt_id, last_execution_at=receipt_time)
    # In-flight local execution has priority over any external CI event.
    # Never wake the agent to mutate the repo while a claimed request runs.
    # A claimed execution must not be interrupted by a CI wake-up, but an
    # explicit paused/completed/blocked goal still needs to be honored.
    if pending and _latest_goal(control).get("goal_state") == "waiting_for_ci":
        lifecycle_action = None
    else:
        value, lifecycle_action = _handle_goal_lifecycle(repo, control, state_dir, value, config,ci_probe=ci_probe,reporter=reporter)
    if lifecycle_action in {"stop", "wait"}:
        atomic_json(path, value)
        return str(value["state"])
    if lifecycle_action == "reconcile_ci":
        # Read-only evidence check of the one registered project chat.
        # A visible wake-up marker is not model acknowledgement; without a
        # newer request or goal transition, bounded escalation still applies.
        try:
            session = browser.ensure_browser_running(verify_auth=True)
            record = browser.project_record(repo)
            url = str(record.get("chat_url") or "")
            if url:
                target = browser._find_chatgpt_target(int(session["port"]), url)
                if target and browser._page_contains(target, str(value["ci_delivery_marker"])):
                    value["ci_delivery_phase"] = "observed"
                    value["state"] = "ci_handoff_observed"
                    value.pop("ci_probe_error", None)
        except (browser.BrowserError, TimeoutError, OSError) as exc:
            value["ci_probe_error"] = type(exc).__name__
        atomic_json(path, value)
        return str(value["state"])
    if lifecycle_action == "resume_ci":
        session = browser.ensure_browser_running(verify_auth=True)
        record = browser.project_record(repo)
        url = str(record.get("chat_url") or "")
        if not url:
            raise browser.BrowserError("project has no bound automation chat")
        target = browser._find_chatgpt_target(int(session["port"]), url)
        if target is None:
            target = cdp.create_target(int(session["port"]), url, background=True)
            target, _ = browser.wait_for_authenticated(
                int(session["port"]), target=target, chat_url=url, timeout=30.0
            )
        conclusion = str(value.get("ci_conclusion") or "unknown")
        prompt = (
            str(value["ci_delivery_marker"]) + ": The exact GitHub Actions run recorded in the durable "
            f"waiting_for_ci goal reached terminal conclusion {conclusion!r}. Re-read the "
            "existing goal and CI evidence, verify the expected head/PR state, and continue "
            "exactly once. If CI failed, diagnose before mutation. If CI succeeded, perform "
            "the already-approved next gated action. Do not create a parallel goal and do not "
            "claim completion without receipts/live proof."
        )
        value["state"] = "ci_delivery_uncertain"
        value["last_resume_at"] = time.time()
        atomic_json(path, value)
        try:
            # This only proves the browser submit step, never consumption.
            _queue_continuation(state_dir, prompt, purpose='ci_continuation', marker=str(value['ci_delivery_marker']), binding=record)
        except Exception as exc:
            # The browser may have clicked before disconnecting. Reconcile
            # the bound conversation; do NOT send again on restart.
            value["ci_delivery_phase"] = "uncertain"
            value["ci_delivery_error"] = type(exc).__name__
            atomic_json(path, value)
            raise
        value["ci_delivery_phase"] = "queued"
        value["state"] = "recovering"
        atomic_json(path, value)
        return str(value["state"])
    if pending:
        new_state, _ = _decision(
            value, receipt_id=receipt_id, receipt_time=receipt_time, pending=True,
            busy=False, now=time.time(), idle_seconds=config["idle_seconds"],
            recovery_seconds=config["recovery_seconds"],
        )
        if _pending_stale(control, time.time()):
            new_state["state"] = "stalled_pending"
            new_state = report(repo, config, new_state, reason="pending")
        atomic_json(path, new_state)
        return str(new_state["state"])
    # Escalation must not depend on a healthy browser/CDP connection. An
    # unknown send can remain invisible to all DOM probes indefinitely.
    if value.get("idle_resume_marker") or int(value.get("attempts", 0)):
        marker = str(value.get("idle_resume_marker") or "")
        age = time.time() - float(value.get("idle_resume_at") or time.time())
        if marker and age >= config["recovery_seconds"]:
            value["state"] = "stalled_idle_handoff"
            value["idle_resume_error"] = "No receipt arrived before browser-handoff deadline"
            value = report(repo, config, value, reason="idle")
            atomic_json(path, value)
            return str(value["state"])
        if int(value.get("attempts", 0)) and not marker:
            value["state"] = "stalled_legacy_continuation_unverified"
            value["idle_resume_error"] = "Old continuation lacks delivery marker; manual reconciliation required"
            value = report(repo, config, value, reason="idle")
            atomic_json(path, value)
            return str(value["state"])
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
    # A prior intent can have been accepted despite an unknown CDP result.
    # Read only the exact bound conversation, never submit again on restart.
    marker = str(value.get("idle_resume_marker") or "")
    if marker:
        try:
            if browser._page_contains(target, marker):
                value["idle_resume_phase"] = "observed"
                value.pop("idle_resume_probe_error", None)
        except (browser.BrowserError, TimeoutError, OSError) as exc:
            value["idle_resume_probe_error"] = type(exc).__name__
        age = time.time() - float(value.get("idle_resume_at") or time.time())
        if age >= config["recovery_seconds"]:
            value["state"] = "stalled_idle_handoff"
            value["idle_resume_error"] = "No new execution receipt or explicit goal transition after continuation"
            value = report(repo, config, value, reason="idle")
        else:
            value["state"] = ("idle_handoff_observed" if value.get("idle_resume_phase") == "observed"
                              else "idle_delivery_uncertain" if value.get("idle_resume_phase") == "uncertain"
                              else "recovering")
        atomic_json(path, value)
        return str(value["state"])
    if int(value.get("attempts", 0)) and not marker:
        # Legacy in-flight sends predate durable markers. Their delivery is
        # indeterminate; do not issue a duplicate on upgrade.
        value["state"] = "stalled_legacy_continuation_unverified"
        value["idle_resume_error"] = "Old continuation lacks delivery marker; manual reconciliation required"
        value = report(repo, config, value, reason="idle")
        atomic_json(path, value)
        return str(value["state"])
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
        # Persist a deterministic marker before any uncertain browser side
        # effect, so a crash/restart cannot duplicate this continuation.
        fingerprint = f"{repo.resolve()}:{receipt_id}:{value.get('progress_at')}:{value.get('goal_id')}"
        marker = "DO_AGAIN_LIVENESS_CONTINUE token=" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]
        value["attempts"] = 1
        value["last_resume_at"] = time.time()
        value["idle_resume_at"] = value["last_resume_at"]
        value["idle_resume_marker"] = marker
        value["idle_resume_phase"] = "uncertain"
        value["state"] = "idle_delivery_uncertain"
        atomic_json(path, value)
        try:
            _queue_continuation(state_dir, prompt, purpose='idle_continuation', marker=marker, binding=record)
        except Exception as exc:
            value["idle_resume_probe_error"] = type(exc).__name__
            atomic_json(path, value)
            raise
        value["idle_resume_phase"] = "queued"
        value["state"] = "recovering"
    elif action in {"report", "report_busy"}:
        value = report(repo, config, value, reason="busy" if action == "report_busy" else "idle")
    atomic_json(path, value)
    return str(value["state"])
