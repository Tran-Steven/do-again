from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,160}$")
OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


class OperatorError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OperatorError(f"invalid UTC timestamp: {value!r}") from exc
    if result.tzinfo is None:
        raise OperatorError(f"timestamp must be timezone-aware: {value!r}")
    return result.astimezone(timezone.utc)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def request_fingerprint(request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(request)).hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def validate_request(
    request: Any,
    *,
    max_ttl_seconds: int,
    max_future_skew_seconds: int = 300,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise OperatorError("request must be an object")
    if request.get("schema_version") != 1:
        raise OperatorError("unsupported request schema")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        raise OperatorError("invalid request_id")
    operation = request.get("operation")
    if not isinstance(operation, str) or not OPERATION_RE.fullmatch(operation):
        raise OperatorError("invalid operation")
    issued = parse_utc(request.get("issued_at_utc"))
    expires = parse_utc(request.get("expires_at_utc"))
    now = utc_now()
    if issued > now + timedelta(seconds=max_future_skew_seconds):
        raise OperatorError(
            f"request issued_at_utc is more than {max_future_skew_seconds}s in the future"
        )
    if expires <= issued:
        raise OperatorError("request expiry must follow issuance")
    ttl = (expires - issued).total_seconds()
    if ttl > max_ttl_seconds:
        raise OperatorError(f"request TTL exceeds {max_ttl_seconds}s")
    if now > expires:
        raise OperatorError("request expired")
    args = request.get("args", {})
    expected = request.get("expected", {})
    limits = request.get("limits", {})
    if not isinstance(args, dict):
        raise OperatorError("args must be an object")
    if not isinstance(expected, dict):
        raise OperatorError("expected must be an object")
    if not isinstance(limits, dict):
        raise OperatorError("limits must be an object")
    continuation = request.get("continuation")
    if continuation is not None:
        if not isinstance(continuation, dict):
            raise OperatorError("continuation must be an object")
        acknowledged = continuation.get("acknowledged_receipts", [])
        if not isinstance(acknowledged, list) or any(
            not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value)
            for value in acknowledged
        ):
            raise OperatorError("continuation.acknowledged_receipts must be valid request IDs")
        if len(set(acknowledged)) != len(acknowledged):
            raise OperatorError("continuation.acknowledged_receipts must not contain duplicates")
        goal_state = continuation.get("goal_state", "in_progress")
        if goal_state not in {"in_progress", "completed", "blocked"}:
            raise OperatorError("continuation.goal_state must be in_progress, completed, or blocked")
        goal_id = continuation.get("goal_id")
        if goal_id is not None and (
            not isinstance(goal_id, str) or not REQUEST_ID_RE.fullmatch(goal_id)
        ):
            raise OperatorError("continuation.goal_id must be a valid identifier")
        summary = continuation.get("summary")
        if summary is not None and (
            not isinstance(summary, str) or len(summary.strip()) > 2000
        ):
            raise OperatorError("continuation.summary must be a string up to 2000 characters")
        normalized_continuation = {
            "acknowledged_receipts": acknowledged,
            "goal_state": goal_state,
        }
        if goal_id is not None:
            normalized_continuation["goal_id"] = goal_id
        if summary is not None:
            normalized_continuation["summary"] = summary.strip()
    else:
        normalized_continuation = None
    normalized = dict(request)
    normalized["args"] = args
    normalized["expected"] = expected
    normalized["limits"] = limits
    if normalized_continuation is not None:
        normalized["continuation"] = normalized_continuation
    return normalized


def expand_path(value: str, *, repo: Path) -> Path:
    text = str(value).replace("$REPO", str(repo)).replace("$HOME", str(Path.home()))
    return Path(os.path.expanduser(text)).resolve()


def path_within(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
