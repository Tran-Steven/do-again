"""Deterministic next-stage request after an exact Codex edit receipt.

Only generates a bounded JSON control request; it does not send a model call,
write to GitHub, execute tests or activate the protected worker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .core.schema import validate_request
from .model_receipts import verify_model_edit_receipt, ModelReceiptBlocked


def prepare_model_canary_test(
    *,
    edit_request: dict,
    edit_receipt: dict,
    nonce: str,
    task: int,
    expected_head: str,
    issued_at: datetime | None = None,
) -> dict:
    """Trust no claimed success without the exact original broker receipt."""
    evidence = verify_model_edit_receipt(
        edit_request, edit_receipt,
        nonce=nonce, task=task, expected_head=expected_head,
    )
    if evidence["next_stage"] != "test":
        raise ModelReceiptBlocked("synthetic canary stage was not a completed edit")
    now = issued_at or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ModelReceiptBlocked("follow-up requires an aware UTC issuance")
    now = now.astimezone(timezone.utc)
    request = {
        "schema_version": 1,
        "request_id": "canary-" + nonce + "-" + str(task) + "-test",
        "operation": "run_tests",
        "issued_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=20)).isoformat(),
        "args": {
            "discover": True,
            "start_directory": "tests",
            "pattern": "test_live_canary_" + nonce + ".py",
            "cwd": ".",
        },
        "expected": {"repo_head": expected_head},
        "limits": {"timeout_seconds": 120},
        "continuation": {
            "acknowledged_receipts": [evidence["request_id"]],
            "goal_state": "in_progress",
            "summary": "Exact model edit receipt verified; confined synthetic regression test only.",
        },
    }
    return validate_request(request, max_ttl_seconds=3600)
