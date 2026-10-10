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
