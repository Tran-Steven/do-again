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


def _require_stage_receipt(*, request: dict, receipt: dict, nonce: str,
                           task: int, stage: str, expected_head: str) -> dict:
    """Require the exact, successful broker result; no assistant ACK inference."""
    from .core.schema import request_fingerprint
    from .model_receipts import _HEAD, _NONCE
    operations = {"test": "run_tests", "commit": "git_commit", "publish": "git_publish"}
    if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(task) is not int or task not in (1, 2)
            or stage not in operations or not isinstance(expected_head, str)
            or not _HEAD.fullmatch(expected_head)
            or not isinstance(request, dict) or not isinstance(receipt, dict)):
        raise ModelReceiptBlocked("invalid synthetic stage or source identity")
    rid = "canary-" + nonce + "-" + str(task) + "-" + stage
    sha = request_fingerprint(request)
    if (request.get("request_id") != rid
            or request.get("operation") != operations[stage]
            or request.get("expected") != {"repo_head": expected_head}
            or receipt.get("request_id") != rid
            or receipt.get("request_fingerprint") != sha
            or receipt.get("operation") != operations[stage]
            or receipt.get("state") != "succeeded"
            or receipt.get("schema_version") != 1):
        raise ModelReceiptBlocked("stage receipt does not match its original request")
    envelope = receipt.get("result")
    if (not isinstance(envelope, dict)
            or envelope.get("operation") != operations[stage]
            or envelope.get("request_fingerprint") != sha
            or not isinstance(envelope.get("authority_before"), dict)
            or envelope["authority_before"].get("repo_head") != expected_head
            or not isinstance(envelope.get("authority_after"), dict)):
        raise ModelReceiptBlocked("stage broker authority is not verified")
    actual = envelope.get("result")
    if not isinstance(actual, dict) or actual.get("returncode") != 0:
        raise ModelReceiptBlocked("stage result does not prove successful execution")
    after = envelope["authority_after"].get("repo_head")
    if not isinstance(after, str) or not _HEAD.fullmatch(after):
        raise ModelReceiptBlocked("stage has no verified resulting Git head")
    if stage in {"test", "publish"} and after != expected_head:
        raise ModelReceiptBlocked("non-committing canary stage changed Git HEAD")
    if stage == "test" and actual.get("timed_out") is not False:
        raise ModelReceiptBlocked("test stage timed out or lacks terminal timing")
    if stage == "commit":
        expected_paths = ["canary_live_" + nonce + ".py",
                          "tests/test_live_canary_" + nonce + ".py"]
        if (after == expected_head or actual.get("state") != "succeeded"
                or not isinstance(actual.get("authority"), dict)
                or actual["authority"].get("repo_head") != after
                or sorted(actual.get("paths", [])) != sorted(expected_paths)):
            raise ModelReceiptBlocked("commit does not prove exact two-file promotion")
    if stage == "publish":
        if (actual.get("state") != "succeeded"
                or actual.get("repository") != "Tran-Steven/do-again"
                or actual.get("head") != expected_head
                or type(actual.get("pull_request")) is not int
                or actual["pull_request"] < 1):
            raise ModelReceiptBlocked("publication lacks exact-head draft PR evidence")
    return {"request_id": rid, "after_head": after}


def _next_request(*, nonce: str, task: int, stage: str, prior_id: str,
                  expected_head: str, args: dict, issued_at: datetime | None) -> dict:
    operations = {"commit": "git_commit", "publish": "git_publish", "ci": "ci_observe"}
    now = issued_at or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ModelReceiptBlocked("follow-up issuance requires an aware timestamp")
    now = now.astimezone(timezone.utc)
    value = {
        "schema_version": 1,
        "request_id": "canary-" + nonce + "-" + str(task) + "-" + stage,
        "operation": operations[stage],
        "issued_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=20)).isoformat(),
        "args": args, "expected": {"repo_head": expected_head},
        "limits": {"timeout_seconds": 120},
        "continuation": {
            "acknowledged_receipts": [prior_id],
            "goal_state": "in_progress",
            "summary": "Exact preceding canary broker receipt verified; bounded next stage only.",
        },
    }
    return validate_request(value, max_ttl_seconds=3600)


def prepare_model_canary_commit(*, test_request: dict, test_receipt: dict,
                                nonce: str, task: int, expected_head: str,
                                issued_at: datetime | None = None) -> dict:
    proof = _require_stage_receipt(
        request=test_request, receipt=test_receipt, nonce=nonce, task=task,
        stage="test", expected_head=expected_head,
    )
    return _next_request(
        nonce=nonce, task=task, stage="commit", prior_id=proof["request_id"],
        expected_head=proof["after_head"],
        args={"paths": ["canary_live_" + nonce + ".py",
                        "tests/test_live_canary_" + nonce + ".py"],
              "message": "Validate synthetic Codex canary task " + str(task)},
        issued_at=issued_at,
    )


def prepare_model_canary_publish(*, commit_request: dict, commit_receipt: dict,
                                 nonce: str, task: int, expected_head: str,
                                 issued_at: datetime | None = None) -> dict:
    proof = _require_stage_receipt(
        request=commit_request, receipt=commit_receipt, nonce=nonce, task=task,
        stage="commit", expected_head=expected_head,
    )
    return _next_request(
        nonce=nonce, task=task, stage="publish",
        prior_id=proof["request_id"], expected_head=proof["after_head"],
        args={"title": "Do Again synthetic two-task Codex canary",
              "body": "Bounded, draft-only isolated validation. Production remains disabled."},
        issued_at=issued_at,
    )


def prepare_model_canary_ci(*, publish_request: dict, publish_receipt: dict,
                            nonce: str, task: int, expected_head: str,
                            issued_at: datetime | None = None) -> dict:
    proof = _require_stage_receipt(
        request=publish_request, receipt=publish_receipt, nonce=nonce, task=task,
        stage="publish", expected_head=expected_head,
    )
    return _next_request(
        nonce=nonce, task=task, stage="ci",
        prior_id=proof["request_id"], expected_head=proof["after_head"],
        args={"original_request_id": proof["request_id"]},
        issued_at=issued_at,
    )
