"""Read-only GitHub receipt observation for Codex canary messages.

All execution remains in the existing confined worker. This cannot publish
requests or impersonate the legacy ChatGPT acknowledgment.
"""
from __future__ import annotations

from .core.schema import canonical_json
from .model_receipts import ModelReceiptBlocked, verify_model_edit_receipt
from .supervisor.control_history import blob_sha, read_json_blob, snapshot


def observe_model_edit(*, api, nonce: str, task: int, request: dict) -> dict:
    from .model_canary import CodexCanaryProposalRejected
    from .model_receipts import _NONCE, _HEAD
    if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(task) is not int or task not in (1, 2)
            or not isinstance(request, dict)
            or not isinstance(request.get("expected"), dict)
            or not isinstance(request["expected"].get("repo_head"), str)
            or not _HEAD.fullmatch(request["expected"]["repo_head"])):
        raise ModelReceiptBlocked("invalid original Codex canary observation scope")
    branch = "do-again/canary-" + nonce + "/control"
    if (getattr(api, "repository", None) != "Tran-Steven/do-again"
            or getattr(api, "control_branch", None) != branch):
        raise ModelReceiptBlocked("GitHub observation is outside the sealed canary control branch")
    rid = "canary-" + nonce + "-" + str(task) + "-edit"
    if request.get("request_id") != rid:
        raise ModelReceiptBlocked("observed request ID differs")
    before = "automation/do_again/requests/" + rid + ".json"
    after = "automation/do_again/receipts/" + rid + ".json"
    _, _, entries = snapshot(api)
    expected = blob_sha(canonical_json(request) + b"\n")
    request_sha = entries.get(before)
    if request_sha is None:
        return {"state": "not_published", "request_id": rid, "replay": False}
    if request_sha != expected:
        raise ModelReceiptBlocked("remote model request payload differs from the original identity")
    receipt_sha = entries.get(after)
    if receipt_sha is None:
        return {"state": "awaiting_broker_receipt", "request_id": rid, "replay": False}
    receipt = read_json_blob(api, receipt_sha)
    return verify_model_edit_receipt(
        request, receipt, nonce=nonce, task=task,
        expected_head=request["expected"]["repo_head"],
    )
