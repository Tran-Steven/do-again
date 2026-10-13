"""Attended, single-send ChatGPT -> isolated Git patch request adapter.

An existing authorized project conversation plans only fenced source changes.
The model never chooses a command, target repository, control branch, or
execution policy. All sends are durably reserved; uncertain outcomes are never
automatically replayed. This is NOT an unattended continuation service.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
import re

from ..browser import runtime
from ..core.schema import OperatorError, atomic_json, utc_now
from .isolated_cycle import _git, _path, TEST_COMMANDS
from .isolated_worker import create_isolated_worker


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _private_exclusive_json(path: Path, payload: dict) -> None:
    """Claim a planning identity durably; never overwrite a previous attempt."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = path.parent.stat()
    if hasattr(os, "getuid") and (
        directory.st_uid != os.getuid() or directory.st_mode & 0o077
    ):
        raise OperatorError("planning intents must be user-private")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise OperatorError("planning identity already reserved; never resend it") from exc
    try:
        with os.fdopen(fd, "wb", closefd=False) as file:
            file.write(json.dumps(payload, sort_keys=True).encode() + b"\n")
            file.flush()
        os.fsync(fd)
    finally:
        os.close(fd)
    if hasattr(os, "O_DIRECTORY"):
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _read_source(repo: Path, paths: list[str]) -> tuple[str, list[dict]]:
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 5
            or len(set(paths)) != len(paths)):
        raise OperatorError("planning requires 1 to 5 distinct explicit source files")
    head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    if _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise OperatorError("planning requires a clean isolated source checkout")
    sources = []
    total_bytes = 0
    for value in paths:
        relative = _path(value)
        if any(segment.startswith(".") for segment in relative.parts):
            raise OperatorError("hidden source files are not eligible for planning")
        _git(repo, "ls-files", "--error-unmatch", "--", relative.as_posix())
        file = repo.joinpath(*relative.parts)
        if file.is_symlink() or not file.is_file():
            raise OperatorError("planning file must be tracked, ordinary and non-aliased")
        raw = file.read_bytes()
        total_bytes += len(raw)
        if len(raw) > 10000 or total_bytes > 25000:
            raise OperatorError("planning source context exceeds private-code budget")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OperatorError("planning only accepts UTF-8 text") from exc
        sources.append({
            "path": relative.as_posix(),
            "before_sha256": hashlib.sha256(raw).hexdigest(),
            "content": content,
        })
    return head, sources


def _validate_plan(answer: str, *, call_id: str, head: str,
                   source: list[dict], command: list[str]) -> dict | None:
    if not isinstance(answer, str) or len(answer.encode()) > 65536:
        raise OperatorError("ChatGPT planner response is missing or oversized")
    try:
        result = json.loads(answer)
    except (ValueError, TypeError) as exc:
        raise OperatorError("ChatGPT planner response is not exactly one JSON object") from exc
    if not isinstance(result, dict) or result.get("call_id") != call_id:
        raise OperatorError("planner response is not bound to the reserved call identity")
    if set(result) == {"call_id", "status"} and result["status"] == "goal_complete":
        return None
    if set(result) != {"call_id", "manifest"}:
        raise OperatorError("planner response is not a bounded change or completion")
    manifest = result["manifest"]
    if (not isinstance(manifest, dict)
            or set(manifest) != {"expected_head", "changes", "test_command"}
            or manifest["expected_head"] != head
            or manifest["test_command"] != command):
        raise OperatorError("planner manifest changed source HEAD or fixed test command")
    changes = manifest["changes"]
    authorized = {row["path"]: row["before_sha256"] for row in source}
    if not isinstance(changes, list) or not 1 <= len(changes) <= len(source):
        raise OperatorError("planner changes exceed permitted file count")
    seen: set[str] = set()
    for row in changes:
        if (not isinstance(row, dict)
                or set(row) != {"path", "before_sha256", "content"}
                or row["path"] not in authorized
                or row["path"] in seen
                or row["before_sha256"] != authorized[row["path"]]
                or not isinstance(row["content"], str)
                or len(row["content"].encode()) > 128000):
            raise OperatorError("planner returned an unapproved source modification")
        seen.add(row["path"])
    return manifest


def plan_isolated_request(*, repo: Path, control_worktree: Path, state_dir: Path,
                          branch: str, image: str, goal: str,
                          files: list[str], test_command: list[str],
                          chat_repo: Path | None = None) -> dict:
    """Send once and publish a request, not an execution or source branch merge."""
    if (not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 2000
            or not isinstance(test_command, list)
            or tuple(test_command) not in TEST_COMMANDS):
        raise OperatorError("planning goal or fixed test command is invalid")
    repo = Path(repo).resolve()
    agent = create_isolated_worker(
        repo=repo, control_worktree=control_worktree, state_dir=state_dir,
        branch=branch, image=image)
    # Serialize planning for this one project. Another project has its own
    # private lock and can proceed independently; the lock does not grant
    # permission to replay a prior uncertain browser send.
    with runtime._file_lock(agent.state_dir / "isolated-planner.lock", timeout=5.0):
        return _plan_with_agent(
            agent, repo=repo, branch=branch, goal=goal,
            files=files, test_command=test_command,
            chat_repo=Path(chat_repo).resolve() if chat_repo is not None else repo)


def _plan_with_agent(agent, *, repo: Path, branch: str, goal: str,
                     files: list[str], test_command: list[str],
                     chat_repo: Path) -> dict:
    head, context = _read_source(repo, files)
    basis = {
        "project": str(repo), "head": head, "goal": goal.strip(),
        "files": [{k: row[k] for k in ("path", "before_sha256")} for row in context],
        "test_command": test_command,
    }
    call_id = "isolated-" + _digest(basis)[:24]
    intent_dir = agent.state_dir / "planning-intents"
    if intent_dir.exists():
        # A previous request with an unknown send/publish outcome cannot be
        # bypassed by changing the user's goal text.
        for file in intent_dir.glob("isolated-*.json"):
            try:
                previous = json.loads(file.read_text())
            except (OSError, ValueError) as exc:
                raise OperatorError("unreadable prior planning intent") from exc
            state = previous.get("state")
            if state != "published":
                raise OperatorError("previous browser planning outcome uncertain; no new sends")
            agent.sync()
            if not agent.receipt_exists(str(previous.get("request_id") or "")):
                raise OperatorError("previous isolated plan is still awaiting its execution receipt")
    intent = intent_dir / (call_id + ".json")
    if intent.exists() or intent.is_symlink():
        raise OperatorError("same planning request already attempted; no duplicate send")

    record = runtime.project_record(chat_repo)
    bound = str(record.get("chat_url") or "")
    conversation = runtime._chat_id(bound)
    if not bound.startswith("https://chatgpt.com/c/") or not conversation:
        raise OperatorError("project needs an existing bound ChatGPT conversation")
    status = runtime.ensure_browser_running(verify_auth=True)
    port = int(status["port"])
    target = runtime._find_chatgpt_target(port, bound)
    if target is None:
        # Open only the already-bound conversation before reserving any send.
        target = runtime.cdp.create_target(port, bound, background=True)
    target, _ = runtime.wait_for_authenticated(
        port, chat_url=bound, target=target, timeout=30.0)
    if runtime._chat_id(target.url) != conversation:
        raise OperatorError("authenticated tab differs from bound project conversation")

    envelope = {
        "call_id": call_id,
        "task": "Return one JSON object: either {call_id,manifest} for needed changes or {call_id,status:'goal_complete'} only if the goal is already satisfied by the provided sources.",
        "goal": goal.strip(), "source_head": head, "files": context,
        "manifest_contract": {
            "expected_head": head, "allowed_paths": [row["path"] for row in context],
            "fixed_test_command": test_command,
            "changes": "1-5 complete UTF-8 file replacements with original before_sha256",
        },
        "restrictions": "Do not create GitHub requests, run commands, submit applications, merge, or deploy. Only return JSON.",
    }
    prompt = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    if len(prompt.encode()) > 32768:
        raise OperatorError("bounded planning prompt is too large")

    intent_info = {
        "schema_version": 1, "request_id": call_id,
        "state": "reserved", "conversation_id": conversation,
        "source_head": head, "goal_sha256": _digest(basis),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "recorded_at_utc": utc_now().isoformat(),
    }
    _private_exclusive_json(intent, intent_info)

    def before_dispatch() -> None:
        intent_info["state"] = "dispatch_started"
        intent_info["dispatched_at_utc"] = utc_now().isoformat()
        atomic_json(intent, intent_info)

    # Never catch and retry; a browser failure after Enter is uncertain.
    result = runtime.send_message(
        target, prompt, timeout=120.0, wait_for_response=True,
        before_dispatch=before_dispatch)
    if runtime._chat_id(str(result.get("chat_url") or "")) != conversation:
        raise OperatorError("ChatGPT answered in another conversation")
    manifest = _validate_plan(
        result.get("response"), call_id=call_id,
        head=head, source=context, command=test_command)
    intent_info["response_sha256"] = hashlib.sha256(
        result["response"].encode()).hexdigest()
    if manifest is None:
        intent_info["state"] = "goal_complete"
        intent_info["completed_at_utc"] = utc_now().isoformat()
        atomic_json(intent, intent_info)
        return {
            "state": "model_declared_complete", "request_id": call_id,
            "source_head": head, "branch": branch,
            "application_submissions": False, "production_deployed": False,
            "executed": False,
        }
    intent_info["state"] = "reply_received"
    atomic_json(intent, intent_info)

    now = utc_now()
    request = {
        "schema_version": 1, "request_id": call_id, "operation": "isolated_patch",
        "issued_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=30)).isoformat(),
        "args": {"manifest": manifest}, "expected": {},
        "limits": {"timeout_seconds": 300},
    }
    # A failure to push is uncertain. Never automatically reissue the model
    # call or create another request ID to hide an ambiguous Git publication.
    agent.publish_json(
        Path("automation/do_again/requests") / (call_id + ".json"),
        request, "Do Again isolated plan " + call_id)
    intent_info["state"] = "published"
    intent_info["published_at_utc"] = utc_now().isoformat()
    atomic_json(intent, intent_info)
    return {
        "state": "published", "request_id": call_id,
        "source_head": head, "branch": branch,
        "application_submissions": False, "production_deployed": False,
        "executed": False,
    }
