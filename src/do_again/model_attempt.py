"""Durable one-shot Codex synthetic model proposal, never GitHub publication.

Only a separately sealed root-installed codex_canary can invoke this entry
point. A model call is reserved on disk before inference; a started attempt is
never retried, even after a process crash or a lost response. This module has
no broker publishing permission and does not activate any worker.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from .core.schema import atomic_json, canonical_json
from .model_canary import (
    canary_model_schema, canary_task_prompt, prepare_canary_edit,
    validate_prepared_edit,
)
from .model_grant import sealed_codex_canary
from .model_quota import require_model_capacity
from .model_transport import (
    CodexTransportBlocked, generate_structured, login_ready,
)
from .supervisor.macos_execution import ExecutionBlocked


def _validate_pinned_binary(scope, home: Path) -> Path:
    root = (home / ".do_again" / "codex-tools").resolve()
    binary = root / "node_modules" / ".bin" / "codex"
    if not binary.exists():
        raise ExecutionBlocked("sealed isolated Codex binary is unavailable")
    target = binary.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ExecutionBlocked("sealed Codex binary escaped its isolated installation")
    info = target.stat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o022
            or info.st_size <= 0 or info.st_size > 16 * 1024 * 1024):
        raise ExecutionBlocked("Codex binary ownership, permissions, or size differ")
    if hashlib.sha256(target.read_bytes()).hexdigest() != scope.cli_sha256:
        raise ExecutionBlocked("Codex binary differs from the sealed digest")
    return binary


def _attempt_path(home: Path, nonce: str, task: int) -> Path:
    base = home / ".do_again" / "codex-canary-attempts" / nonce
    for folder in (base, base.parent, base.parent.parent):
        if folder.is_symlink():
            raise ExecutionBlocked("Codex attempt state contains aliased directories")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.stat().st_uid != os.getuid() or base.stat().st_mode & 0o077:
        raise ExecutionBlocked("Codex attempt journal is not private to the operator")
    return base / ("task-" + str(task) + ".json")


def _reserve(path: Path, record: dict) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                         getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        folder = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    except Exception:
        # Never delete an ambiguous intent. Read-only reconciliation only.
        raise


def run_first_codex_canary_proposal(config: dict, *, allow_model_call: bool = False) -> dict:
    """Make at most one bounded task-one model attempt; never publish or execute."""
    if not allow_model_call:
        raise CodexTransportBlocked("one-shot Codex proposal requires explicit model authorization")
    scope = sealed_codex_canary(config)
    home = Path(config["operator_home"])
    if (os.name != "posix" or not hasattr(os, "geteuid")
            or os.getuid() == 0 or os.geteuid() != os.getuid()
            or config.get("operator_uid") != os.getuid()
            or home.resolve() != Path.home().resolve()):
        raise ExecutionBlocked("Codex proposal must run as the pinned unprivileged operator")
    binary = _validate_pinned_binary(scope, home)
    prompt = canary_task_prompt(nonce=scope.nonce, task=1)
    schema = canary_model_schema()
    # All non-effecting preconditions precede the durable reservation.
    if not login_ready(binary, home=home):
        raise CodexTransportBlocked("Codex ChatGPT sign-in is no longer valid")
    require_model_capacity(binary, home)
    path = _attempt_path(home, scope.nonce, 1)
    original = {
        "schema_version": 1, "state": "model_started",
        "nonce": scope.nonce, "task": 1, "transport": "codex-cli",
        "source_sha": scope.source_sha, "baseline": scope.baseline,
        "cli_sha256": scope.cli_sha256,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(canonical_json(schema)).hexdigest(),
        "model_calls_reserved": 1, "published": False, "executed": False,
    }
    try:
        _reserve(path, original)
    except FileExistsError:
        raise ExecutionBlocked("original Codex model attempt already exists; never retry") from None
    # Any exception after the reservation leaves 'model_started' durable.
    # An inconclusive model response cannot be treated as a fresh request.
    response = generate_structured(
        prompt, schema, allow_model_call=True,
        binary=binary, home=home, timeout_seconds=180,
    )
    request = prepare_canary_edit(
        response, nonce=scope.nonce, task=1, expected_head=scope.baseline,
    )
    validate_prepared_edit(request, nonce=scope.nonce, task=1,
                           expected_head=scope.baseline)
    complete = dict(original, state="candidate_ready",
                    request_sha256=hashlib.sha256(canonical_json(request)).hexdigest(),
                    request=request)
    atomic_json(path, complete)
    return {"state": "candidate_ready", "request": request,
            "published": False, "executed": False,
            "model_calls_reserved": 1}


def observe_original_codex_attempt(config: dict) -> dict:
    """Read only the original task-one journal; no inference or retry."""
    scope = sealed_codex_canary(config)
    home = Path(config["operator_home"])
    path = home / ".do_again" / "codex-canary-attempts" / scope.nonce / "task-1.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        return {"state": "not_reserved", "replay": False}
    record = json.loads(path.read_text())
    if (record.get("nonce") != scope.nonce or record.get("task") != 1
            or record.get("source_sha") != scope.source_sha
            or record.get("baseline") != scope.baseline
            or record.get("cli_sha256") != scope.cli_sha256):
        raise ExecutionBlocked("Codex original model journal identity differs")
    if record.get("state") != "candidate_ready":
        return {"state": "model_started_uncertain", "replay": False}
    candidate = validate_prepared_edit(
        record["request"], nonce=scope.nonce, task=1,
        expected_head=scope.baseline)
    if hashlib.sha256(canonical_json(candidate)).hexdigest() != record.get("request_sha256"):
        raise ExecutionBlocked("Codex prepared request journal hash differs")
    return {"state": "candidate_ready", "request": candidate,
            "published": False, "executed": False, "replay": False}
