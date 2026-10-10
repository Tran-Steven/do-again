"""Read-only, exact-blob GitHub evidence for Codex canary stages.

This is an observer, never a publisher, executor, or authority grant. The
request must match the original immutable control-branch blob, and the
receipt must come from that same branch's verified Git tree.
"""
from __future__ import annotations

from .core.schema import canonical_json
from .model_receipts import ModelReceiptBlocked, verify_model_edit_receipt
from .supervisor.control_history import blob_sha, read_json_blob, snapshot


def observe_model_stage(*, api, nonce: str, task: int, stage: str, request: dict) -> dict:
    from .model_receipts import _NONCE, _HEAD
    from .model_stages import _require_stage_receipt
    operations = {"edit": "scratch_script", "test": "run_tests",
                  "commit": "git_commit", "publish": "git_publish"}
    if (not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(task) is not int or task not in (1, 2)
            or stage not in operations or not isinstance(request, dict)
            or request.get("schema_version") != 1
            or request.get("operation") != operations[stage]
            or not isinstance(request.get("expected"), dict)
            or not isinstance(request["expected"].get("repo_head"), str)
            or not _HEAD.fullmatch(request["expected"]["repo_head"])):
        raise ModelReceiptBlocked("invalid original Codex canary observation scope")
    branch = "do-again/canary-" + nonce + "/control"
    if (getattr(api, "repository", None) != "Tran-Steven/do-again"
            or getattr(api, "control_branch", None) != branch):
        raise ModelReceiptBlocked("GitHub observation is outside the sealed canary control branch")
    rid = "canary-" + nonce + "-" + str(task) + "-" + stage
    if request.get("request_id") != rid:
        raise ModelReceiptBlocked("observed stage request ID differs")
    request_path = "automation/do_again/requests/" + rid + ".json"
    receipt_path = "automation/do_again/receipts/" + rid + ".json"
    head, _, entries = snapshot(api)
    expected = blob_sha(canonical_json(request) + b"\n")
    observed_request = entries.get(request_path)
    if observed_request is None:
        return {"state": "not_published", "request_id": rid, "stage": stage, "replay": False}
    if observed_request != expected:
        raise ModelReceiptBlocked("remote model request payload differs from original identity")
    receipt_sha = entries.get(receipt_path)
    if receipt_sha is None:
        return {"state": "awaiting_broker_receipt", "request_id": rid,
                "stage": stage, "replay": False}
    receipt = read_json_blob(api, receipt_sha)
    expected_head = request["expected"]["repo_head"]
    if stage == "edit":
        evidence = verify_model_edit_receipt(
            request, receipt, nonce=nonce, task=task, expected_head=expected_head)
    else:
        proof = _require_stage_receipt(
            request=request, receipt=receipt, nonce=nonce, task=task,
            stage=stage, expected_head=expected_head)
        evidence = {"state": "execution_verified", "request_id": rid,
                    "after_head": proof["after_head"], "next_stage": {
                        "test": "commit", "commit": "publish", "publish": "ci",
                    }[stage], "execution_verified": True,
                    "model_acknowledged": False, "browser_acknowledged": False}
    return dict(evidence, stage=stage, control_head=head,
                receipt_blob_sha=receipt_sha, replay=False)


def observe_model_edit(*, api, nonce: str, task: int, request: dict) -> dict:
    """Backward-compatible exact remote edit observation."""
    return observe_model_stage(api=api, nonce=nonce, task=task,
                               stage="edit", request=request)
