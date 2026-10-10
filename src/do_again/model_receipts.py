"""Read-only evidence gate for Codex-authored, broker-executed canary requests.

A model draft or GitHub publication alone is never execution evidence.
This parser requires the original request hash, stage, and broker result.
It never grants the browser-specific ChatGPT acknowledgment.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .core.schema import canonical_json, request_fingerprint

_NONCE = re.compile(r"[0-9a-f]{24}\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")


class ModelReceiptBlocked(ValueError):
    """The original model-canary execution is not proven."""


def verify_model_edit_receipt(
    request: dict[str, Any],
    receipt: dict[str, Any],
    *,
    nonce: str,
    task: int,
    expected_head: str,
) -> dict[str, Any]:
    if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(task) is not int or task not in (1, 2)
            or not isinstance(expected_head, str) or not _HEAD.fullmatch(expected_head)):
        raise ModelReceiptBlocked("invalid synthetic canary scope")
    rid = "canary-" + nonce + "-" + str(task) + "-edit"
    if (not isinstance(request, dict) or not isinstance(receipt, dict)
            or request.get("request_id") != rid
            or request.get("operation") != "scratch_script"
            or request.get("expected") != {"repo_head": expected_head}
            or receipt.get("schema_version") != 1
            or receipt.get("request_id") != rid
            or receipt.get("operation") != "scratch_script"
            or receipt.get("request_fingerprint") != request_fingerprint(request)):
        raise ModelReceiptBlocked("receipt does not bind the exact original canary request")
    if receipt.get("state") != "succeeded":
        raise ModelReceiptBlocked("canary execution was not successful")
    details = receipt.get("result")
    if (not isinstance(details, dict) or details.get("operation") != "scratch_script"
            or details.get("request_fingerprint") != request_fingerprint(request)):
        raise ModelReceiptBlocked("broker execution evidence is missing or mismatched")
    before = details.get("authority_before")
    after = details.get("authority_after")
    if (not isinstance(before, dict) or before.get("repo_head") != expected_head
            or not isinstance(after, dict) or after.get("repo_head") != expected_head):
        raise ModelReceiptBlocked("broker authority changed during the synthetic edit")
    result = details.get("result")
    if (not isinstance(result, dict) or result.get("returncode") != 0
            or result.get("timed_out") is not False):
        raise ModelReceiptBlocked("synthetic edit lacks a successful confined execution")
    digest = hashlib.sha256(canonical_json(receipt)).hexdigest()
    return {
        "state": "execution_verified",
        "request_id": rid,
        "request_fingerprint": request_fingerprint(request),
        "receipt_sha256": digest,
        "source_head": expected_head,
        "next_stage": "test",
        "execution_verified": True,
        "model_acknowledged": False,
        "browser_acknowledged": False,
    }


def verify_model_ci_receipt(
    request: dict[str, Any],
    receipt: dict[str, Any],
    *,
    nonce: str,
    task: int,
    expected_head: str,
    pull_request: int,
) -> dict[str, Any]:
    """Only terminal, exact-head successful CI is a synthetic canary checkpoint.

    A waiting CI observation may have returncode=0 and a 'succeeded' worker
    receipt. It MUST NOT be mistaken for a completed, passing CI run.
    """
    if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(task) is not int or task not in (1, 2)
            or not isinstance(expected_head, str) or not _HEAD.fullmatch(expected_head)
            or type(pull_request) is not int or pull_request < 1
            or not isinstance(request, dict) or not isinstance(receipt, dict)):
        raise ModelReceiptBlocked("invalid original CI observation scope")
    rid = "canary-" + nonce + "-" + str(task) + "-ci"
    published = "canary-" + nonce + "-" + str(task) + "-publish"
    fp = request_fingerprint(request)
    if (request.get("request_id") != rid
            or request.get("operation") != "ci_observe"
            or request.get("expected") != {"repo_head": expected_head}
            or request.get("args") != {"original_request_id": published}
            or receipt.get("schema_version") != 1
            or receipt.get("request_id") != rid
            or receipt.get("operation") != "ci_observe"
            or receipt.get("request_fingerprint") != fp
            or receipt.get("state") != "succeeded"):
        raise ModelReceiptBlocked("CI receipt differs from the original publication identity")
    envelope = receipt.get("result")
    if (not isinstance(envelope, dict)
            or envelope.get("operation") != "ci_observe"
            or envelope.get("request_fingerprint") != fp
            or not isinstance(envelope.get("authority_before"), dict)
            or envelope["authority_before"].get("repo_head") != expected_head
            or not isinstance(envelope.get("authority_after"), dict)
            or envelope["authority_after"].get("repo_head") != expected_head):
        raise ModelReceiptBlocked("CI broker authority changed during observation")
    actual = envelope.get("result")
    if (not isinstance(actual, dict) or actual.get("state") != "terminal"
            or actual.get("status") != "completed"
            or actual.get("conclusion") != "success"
            or actual.get("returncode") != 0
            or actual.get("replay") is not False
            or actual.get("repository") != "Tran-Steven/do-again"
            or actual.get("head_sha") != expected_head
            or actual.get("pull_request") != pull_request
            or type(actual.get("run_id")) is not int or actual["run_id"] < 1
            or type(actual.get("run_attempt")) is not int or actual["run_attempt"] < 1
            or actual.get("url") != "https://github.com/Tran-Steven/do-again/actions/runs/"
                                 + str(actual.get("run_id"))):
        raise ModelReceiptBlocked("CI has not provided matching terminal passing evidence")
    return {
        "state": "ci_verified", "request_id": rid,
        "receipt_sha256": hashlib.sha256(canonical_json(receipt)).hexdigest(),
        "head_sha": expected_head, "run_id": actual["run_id"],
        "run_attempt": actual["run_attempt"], "pull_request": pull_request,
        "publication_request_id": published, "next_stage": "checkpoint",
        "ci_success": True, "model_acknowledged": False,
        "browser_acknowledged": False, "replay": False,
    }
