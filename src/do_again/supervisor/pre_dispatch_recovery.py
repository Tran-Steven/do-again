from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .macos_execution import ExecutionBlocked

ORIGINAL_REQUEST = "browser-48b9fd1c09734faf98f77944a64581f2"
ORIGINAL_SOURCE = "279350b2a3a280904b72068f96785343945f97b9"
ORIGINAL_EPOCH = 4
MAINTENANCE_EPOCH = 5


def _protected_file(path: Path, uid: int):
    if path.is_symlink() or not path.is_file():
        raise ExecutionBlocked("original browser evidence is missing or aliased")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
            or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 262144):
        raise ExecutionBlocked("original browser evidence is not protected")
    return path.read_bytes()


def verify_pre_dispatch(config, project, request_id, intent, status):
    home = Path(config["operator_home"])
    uid = config["operator_uid"]
    if (request_id != ORIGINAL_REQUEST
            or project.get("account") != "_doagain_jp"
            or project.get("repo") != str(home / "jobpipe")
            or status.get("intent") != "maintenance"
            or status.get("epoch") != MAINTENANCE_EPOCH
            or not isinstance(intent, dict)
            or intent.get("operation") != "browser_tick"
            or intent.get("source_sha") != ORIGINAL_SOURCE
            or intent.get("repo") != project["repo"]
            or intent.get("epoch") != ORIGINAL_EPOCH):
        raise ExecutionBlocked("original browser request does not match the one-time maintenance recovery")
    base = home / ".do_again" / "projects" / project["key"][:12] / "state"
    if base.is_symlink() or not base.is_dir():
        raise ExecutionBlocked("original browser state directory is not protected")
    current = base / "browser_submission_uncertain.json"
    archive = base / ("browser_submission_presend_" + ORIGINAL_REQUEST + ".json")
    if current.exists() and archive.exists():
        raise ExecutionBlocked("conflicting original browser journals")
    selected = current if current.exists() else archive
    payload = _protected_file(selected, uid)
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ExecutionBlocked("original browser journal cannot be verified") from exc
    ids = value.get("request_ids") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("state") != "preparing"
            or value.get("payload_sha256") is not None
            or value.get("acknowledgment_token") is not None
            or not isinstance(ids, list) or not 1 <= len(ids) <= 20
            or len(ids) != len(set(str(x) for x in ids))
            or any(not isinstance(x, str) or not x or "/" in x or "\\" in x for x in ids)):
        raise ExecutionBlocked("original browser Send is not proven pre-dispatch")
    outbox = base / "browser_outbox"
    if outbox.is_symlink() or not outbox.is_dir():
        raise ExecutionBlocked("original browser outbox is unavailable")
    for request in ids:
        _protected_file(outbox / (request + ".json"), uid)
    receipt = base / "browser_tick_receipts" / (ORIGINAL_REQUEST + ".json")
    if receipt.exists() or receipt.is_symlink():
        raise ExecutionBlocked("original browser already has terminal evidence")
    return {"current": current, "archive": archive, "sha256": hashlib.sha256(payload).hexdigest(), "count": len(ids)}


def reconcile_verified_pre_dispatch(broker, request_id):
    from .macos_execution import MacOSProcesses
    from .worker_service import service_present, service_spec
    from ..core.schema import atomic_json

    if broker.config.get("production_ready") is not False:
        raise ExecutionBlocked("pre-dispatch recovery requires disabled production")
    project = next((p for p in broker.config["projects"]
                    if p.get("key") == broker.project.key), None)
    if project is None:
        raise ExecutionBlocked("original protected project is unavailable")
    pending = broker.ledger.pending(broker.project.key)
    if len(pending) != 1 or pending[0]["request_id"] != request_id:
        raise ExecutionBlocked("original browser request is not the only pending effect")
    status = broker.registry.status(broker.project.repo)
    original = broker.ledger.intent(broker.project.key, request_id)
    proof = verify_pre_dispatch(broker.config, project, request_id, original, status)
    if MacOSProcesses().owned(project["uid"]):
        raise ExecutionBlocked("dedicated execution remains active")
    label, _ = service_spec(broker, status["epoch"])
    if service_present(broker, label):
        raise ExecutionBlocked("original worker service remains loaded")
    result = {"state": "completed", "delivered": 0,
              "liveness": "reconciled_before_send", "delivery_acknowledged": False,
              "outbox_preserved": True}
    audit = Path(broker.config["authority_path"]).parent / (
        "pre_dispatch_recovery_" + request_id + ".json")
    expected_audit = {"schema_version": 1, "request_id": request_id,
                      "source_sha": ORIGINAL_SOURCE, "epoch": ORIGINAL_EPOCH,
                      "preparing_sha256": proof["sha256"], "outbox_count": proof["count"],
                      "result": result}
    # Crash recovery is retryable only when the preexisting protected audit
    # exactly matches the same original journal. Never replace or amend it.
    if audit.exists() or audit.is_symlink():
        data = _protected_file(audit, os.geteuid())
        try:
            previous = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ExecutionBlocked("original browser recovery audit is invalid") from exc
        if type(previous) is not dict or previous != expected_audit:
            raise ExecutionBlocked("original browser recovery audit differs; operator review required")
    else:
        atomic_json(audit, expected_audit)
    # Reverify the original evidence after creating/reading the root audit.
    # A crash between audit creation, archival and ledger finish may retry,
    # but an altered journal or outbox cannot be used to complete recovery.
    rechecked = verify_pre_dispatch(broker.config, project, request_id, original, status)
    if (rechecked["sha256"] != proof["sha256"]
            or rechecked["count"] != proof["count"]):
        raise ExecutionBlocked("original browser pre-dispatch evidence changed")
    if rechecked["current"].exists():
        rechecked["current"].rename(rechecked["archive"])
        descriptor = os.open(str(rechecked["archive"].parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    broker.ledger.finish(broker.project.key, request_id, result)
    return dict(result, reconciled_pre_dispatch=True)
