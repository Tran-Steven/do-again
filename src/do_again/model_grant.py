"""Strict Codex-only sealed canary identity. No browser or runtime activation.

This contract is read from a future root-owned installation configuration, not
from CLI arguments or generated model text. It has no effect on old installs,
which contain only the browser-specific live_canary grant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .supervisor.macos_execution import ExecutionBlocked


_HEX24 = re.compile(r"[0-9a-f]{24}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_GRANTED_FIELDS = frozenset({
    "nonce", "baseline", "parent_epoch", "transport", "cli_sha256",
    "expires_at_utc", "max_model_calls",
})


@dataclass(frozen=True)
class CodexCanaryScope:
    nonce: str
    baseline: str
    parent_epoch: int
    cli_sha256: str
    source_sha: str
    expires_at_utc: datetime
    control_branch: str
    max_model_calls: int

    @property
    def request_prefix(self) -> str:
        return "canary-" + self.nonce + "-"


def sealed_codex_canary(config: dict[str, Any], *, now: datetime | None = None) -> CodexCanaryScope:
    """Pure admission validation, deliberately distinct from browser canaries."""
    if not isinstance(config, dict) or config.get("production_ready") is not False:
        raise ExecutionBlocked("Codex canary requires production-disabled installed configuration")
    if config.get("live_canary") is not None:
        raise ExecutionBlocked("Codex canary cannot reuse or coexist with a ChatGPT browser grant")
    grant = config.get("codex_canary")
    if not isinstance(grant, dict) or set(grant) != _GRANTED_FIELDS:
        raise ExecutionBlocked("sealed Codex canary grant is absent or has unknown capabilities")
    if (grant.get("transport") != "codex-cli"
            or not isinstance(grant.get("nonce"), str)
            or not _HEX24.fullmatch(grant["nonce"])
            or not isinstance(grant.get("baseline"), str)
            or not _HEX40.fullmatch(grant["baseline"])
            or type(grant.get("parent_epoch")) is not int
            or grant["parent_epoch"] < 1
            or not isinstance(grant.get("cli_sha256"), str)
            or not _HEX64.fullmatch(grant["cli_sha256"])
            or type(grant.get("max_model_calls")) is not int
            or grant["max_model_calls"] != 2):
        raise ExecutionBlocked("Codex canary nonce, model identity, or two-call budget is invalid")
    source_sha = config.get("source_sha")
    if not isinstance(source_sha, str) or not _HEX40.fullmatch(source_sha):
        raise ExecutionBlocked("sealed Codex canary source manifest is invalid")
    home = config.get("operator_home")
    projects = config.get("projects")
    if (not isinstance(home, str) or not Path(home).is_absolute()
            or not isinstance(projects, list) or len(projects) != 2
            or any(not isinstance(p, dict) for p in projects)
            or {p.get("account") for p in projects} != {"_doagain_da", "_doagain_jp"}):
        raise ExecutionBlocked("Codex canary parent project identities are invalid")
    parent = next(p for p in projects if p.get("account") == "_doagain_da")
    sibling = next(p for p in projects if p.get("account") == "_doagain_jp")
    if (parent.get("repo") != str(Path(home) / "do-again")
            or parent.get("github_repository") != "Tran-Steven/do-again"
            or sibling.get("repo") != str(Path(home) / "jobpipe")
            or sibling.get("github_repository") != "Tran-Steven/jobpipe"):
        raise ExecutionBlocked("Codex canary is not scoped to the installed Do Again parent")
    when = grant.get("expires_at_utc")
    if not isinstance(when, str) or len(when) > 40 or not when.endswith("+00:00"):
        raise ExecutionBlocked("Codex canary requires an explicit UTC expiry")
    try:
        expiry = datetime.fromisoformat(when)
    except ValueError:
        raise ExecutionBlocked("Codex canary expiry is invalid") from None
    if expiry.tzinfo != timezone.utc:
        raise ExecutionBlocked("Codex canary expiry is not UTC")
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None:
        raise ExecutionBlocked("Codex canary clock has no trusted timezone")
    instant = instant.astimezone(timezone.utc)
    if not instant < expiry <= instant + timedelta(hours=2):
        raise ExecutionBlocked("Codex canary grant has expired or exceeds its bounded horizon")
    # The separate activation journal must enforce a tighter runtime 2h limit.
    return CodexCanaryScope(
        nonce=grant["nonce"], baseline=grant["baseline"],
        parent_epoch=grant["parent_epoch"], cli_sha256=grant["cli_sha256"],
        source_sha=source_sha, expires_at_utc=expiry,
        control_branch="do-again/canary-" + grant["nonce"] + "/control",
        max_model_calls=2,
    )


def bound_model_request(scope: CodexCanaryScope, *, task: int, stage: str, request: dict) -> bool:
    """Reject anything outside the two-task synthetic request namespace."""
    operations = {
        "edit": "scratch_script",
        "test": "run_tests",
        "commit": "git_commit",
        "publish": "git_publish",
        "ci": "ci_observe",
    }
    if (not isinstance(scope, CodexCanaryScope)
            or type(task) is not int or task not in (1, 2)
            or stage not in operations or not isinstance(request, dict)
            or request.get("request_id") != scope.request_prefix + str(task) + "-" + stage
            or request.get("operation") != operations[stage]):
        raise ExecutionBlocked("Codex canary request is outside the fixed two-task plan")
    if stage == "edit" and request.get("expected") != {"repo_head": scope.baseline} and task == 1:
        raise ExecutionBlocked("initial synthetic edit does not bind the sealed baseline")
    return True
