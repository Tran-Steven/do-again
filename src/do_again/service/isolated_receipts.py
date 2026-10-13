from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from ..browser import runtime
from ..core.schema import OperatorError, atomic_json, request_fingerprint, utc_now
from .isolated_planner import _private_exclusive_json
from .isolated_worker import create_isolated_worker


_REQUEST_RE = re.compile(r"isolated-[a-f0-9]{24}")
_PATCH_RE = re.compile(r"[a-f0-9]{24}[.]patch")
_TERMINAL = {"succeeded", "failed", "blocked", "error"}


def _load_verified_receipt(agent, request_id: str) -> dict | None:
    agent.sync()
    request_file = agent.requests_dir / (request_id + ".json")
    if not request_file.is_file():
        raise OperatorError("isolated request is absent from the Git control branch")
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperatorError("isolated request is unreadable") from exc
    if (not isinstance(request, dict)
            or request.get("request_id") != request_id
            or request.get("operation") != "isolated_patch"):
        raise OperatorError("control-branch request identity does not match")
    receipt = agent.receipt_payload(request_id)
    if receipt is None:
        return None
    if (receipt.get("request_id") != request_id
            or receipt.get("operation") != "isolated_patch"
            or receipt.get("request_fingerprint") != request_fingerprint(request)
            or receipt.get("state") not in _TERMINAL):
        raise OperatorError("Git receipt does not match the original isolated request")
    if receipt["state"] == "succeeded":
        result = receipt.get("result")
        execution = result.get("result") if isinstance(result, dict) else None
        patch = execution.get("isolated_patch") if isinstance(execution, dict) else None
        manifest = request.get("args", {}).get("manifest", {})
        if (not isinstance(patch, dict)
                or execution.get("returncode") != 0
                or patch.get("state") != "verified_isolated_patch"
                or patch.get("tested_offline") is not True
                or patch.get("automatic_merge") is not False
                or patch.get("production_deployed") is not False
                or patch.get("source_head") != manifest.get("expected_head")):
            raise OperatorError("success receipt lacks required isolated test attestation")
        name = patch.get("patch_file")
        digest = patch.get("patch_sha256")
        if (not isinstance(name, str) or not _PATCH_RE.fullmatch(name)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise OperatorError("success receipt references an invalid patch identity")
        path = agent.state_dir / "isolated-patches" / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 800000:
            raise OperatorError("tested isolated patch is not available locally")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise OperatorError("tested isolated patch digest mismatch")
    return receipt


def report_isolated_receipt(*, repo: Path, control_worktree: Path,
                            state_dir: Path, branch: str, image: str,
                            request_id: str) -> dict:
    if not isinstance(request_id, str) or not _REQUEST_RE.fullmatch(request_id):
        raise OperatorError("receipt delivery requires a bounded isolated request ID")
    repo = Path(repo).resolve()
    agent = create_isolated_worker(
        repo=repo, control_worktree=control_worktree,
        state_dir=state_dir, branch=branch, image=image)
    with runtime._file_lock(agent.state_dir / "isolated-planner.lock", timeout=5.0):
        return _report_with_agent(agent, repo=repo, request_id=request_id)


def _report_with_agent(agent, *, repo: Path, request_id: str) -> dict:
    intent = agent.state_dir / "planning-intents" / (request_id + ".json")
    if not intent.is_file() or intent.is_symlink():
        raise OperatorError("receipt has no durable ChatGPT planning identity")
    try:
        planned = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OperatorError("planning intent is unreadable") from exc
    if (not isinstance(planned, dict) or planned.get("state") != "published"
            or planned.get("request_id") != request_id):
        raise OperatorError("planning request has not been safely published")
    report_dir = agent.state_dir / "receipt-reports"
    if report_dir.exists():
        for prior in report_dir.glob("isolated-*.json"):
            if prior.is_symlink():
                raise OperatorError("receipt-report journal is not an ordinary file")
            try:
                entry = json.loads(prior.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise OperatorError("receipt-report journal is unreadable") from exc
            if not isinstance(entry, dict) or entry.get("state") != "acknowledged":
                raise OperatorError("previous receipt delivery is uncertain; no further sends")
    report_path = report_dir / (request_id + ".json")
    if report_path.exists() or report_path.is_symlink():
        raise OperatorError("this receipt was already delivered or attempted")
    receipt = _load_verified_receipt(agent, request_id)
    if receipt is None:
        return {"state": "awaiting_receipt", "request_id": request_id, "sent": False}
    record = runtime.project_record(repo)
    bound = str(record.get("chat_url") or "")
    conversation = runtime._chat_id(bound)
    if (not bound.startswith("https://chatgpt.com/c/")
            or not conversation or conversation != planned.get("conversation_id")):
        raise OperatorError("bound ChatGPT conversation changed since planning")
    status = runtime.ensure_browser_running(verify_auth=True)
    port = int(status["port"])
    target = runtime._find_chatgpt_target(port, bound)
    if target is None:
        target = runtime.cdp.create_target(port, bound, background=True)
    target, _ = runtime.wait_for_authenticated(
        port, chat_url=bound, target=target, timeout=30.0)
    if runtime._chat_id(target.url) != conversation:
        raise OperatorError("receipt target is not the authorized project conversation")
    patch = {}
    if receipt["state"] == "succeeded":
        patch = receipt["result"]["result"]["isolated_patch"]
    body = {
        "task": "Acknowledge this verified execution receipt; return exactly one JSON object with request_id and acknowledged=true.",
        "request_id": request_id,
        "receipt_state": receipt["state"],
        "source_head": planned.get("source_head"),
        "patch_sha256": patch.get("patch_sha256"),
        "changed_files": patch.get("files", []),
        "tested_offline": bool(patch.get("tested_offline", False)),
        "production_deployed": False,
        "automatic_merge": False,
        "error": str(receipt.get("error") or "")[:500],
        "restrictions": "Do not run commands, deploy, merge, submit applications, or initiate another task in this reply.",
    }
    prompt = json.dumps(body, separators=(",", ":"), sort_keys=True)
    journal = {
        "schema_version": 1,
        "request_id": request_id,
        "state": "reserved",
        "conversation_id": conversation,
        "receipt_sha256": hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "recorded_at_utc": utc_now().isoformat(),
    }
    _private_exclusive_json(report_path, journal)

    def before_dispatch() -> None:
        journal["state"] = "dispatch_started"
        journal["dispatched_at_utc"] = utc_now().isoformat()
        atomic_json(report_path, journal)

    response = runtime.send_message(
        target, prompt, timeout=120.0, wait_for_response=True,
        before_dispatch=before_dispatch)
    if runtime._chat_id(str(response.get("chat_url") or "")) != conversation:
        raise OperatorError("receipt acknowledgment came from the wrong conversation")
    try:
        ack = json.loads(response.get("response"))
    except (TypeError, ValueError) as exc:
        raise OperatorError("receipt acknowledgment was not a JSON object") from exc
    if (not isinstance(ack, dict)
            or set(ack) != {"request_id", "acknowledged"}
            or ack.get("request_id") != request_id
            or ack.get("acknowledged") is not True):
        raise OperatorError("ChatGPT did not acknowledge this exact receipt")
    journal["state"] = "acknowledged"
    journal["acknowledged_at_utc"] = utc_now().isoformat()
    atomic_json(report_path, journal)
    return {
        "state": "acknowledged", "request_id": request_id,
        "receipt_state": receipt["state"], "sent": True,
        "production_deployed": False, "automatic_merge": False,
    }
