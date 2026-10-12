from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .macos_execution import ExecutionBlocked


_ACCOUNTS = ("_doagain_da", "_doagain_jp")
_NAMES = {"_doagain_da": "do-again", "_doagain_jp": "jobpipe",
          "_doagain_so": "Sonary"}
_GRANT_FIELDS = frozenset({
    "kind", "source_sha", "issued_at_utc", "expires_at_utc",
    "projects", "max_parallel_workers", "network_access", "application_submissions",
})
_PROJECT_FIELDS = frozenset({"account", "project_key", "goal_revision"})


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ExecutionBlocked("dual development grant timestamp is malformed")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ExecutionBlocked("dual development grant timestamp is invalid") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ExecutionBlocked("dual development grant timestamp must be canonical")
    return parsed


def require_dual_offline_grant(
    config: dict[str, Any], project: Any,
    statuses: dict[str, dict[str, Any]], *, now: datetime | None = None,
) -> dict[str, Any]:
    grant = config.get("development_scope")
    if (not isinstance(grant, dict) or set(grant) != _GRANT_FIELDS
            or grant.get("kind") != "offline-dual-v1"
            or grant.get("network_access") is not False
            or grant.get("application_submissions") is not False
            or type(grant.get("max_parallel_workers")) is not int
            or grant["max_parallel_workers"] != 2
            or config.get("production_ready") is not False
            or config.get("live_canary") is not None
            or config.get("codex_canary") is not None
            or not isinstance(config.get("source_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", config["source_sha"])
            or grant.get("source_sha") != config["source_sha"]):
        raise ExecutionBlocked("unsealed or unsafe dual development authority")

    issued = _timestamp(grant["issued_at_utc"])
    expires = _timestamp(grant["expires_at_utc"])
    current = datetime.now(timezone.utc) if now is None else now
    if (not isinstance(current, datetime) or current.tzinfo is None
            or not issued <= current <= expires
            or not timedelta(0) < expires - issued <= timedelta(days=14)):
        raise ExecutionBlocked("dual development grant is expired or overlong")

    home = config.get("operator_home")
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise ExecutionBlocked("dual development operator directory is invalid")
    protected = config.get("projects")
    if not isinstance(protected, list) or len(protected) not in (2, 3):
        raise ExecutionBlocked("dual development project roster is invalid")
    accounts = [p.get("account") for p in protected if isinstance(p, dict)]
    if (len(accounts) != len(protected) or len(set(accounts)) != len(accounts)
            or set(accounts) not in (set(_ACCOUNTS), set(_NAMES))):
        raise ExecutionBlocked("dual development project identities differ")
    from .authority import project_identity

    for entry in protected:
        canonical = Path(home) / _NAMES[entry["account"]]
        if (entry.get("repo") != str(canonical)
                or entry.get("key") != project_identity(canonical)):
            raise ExecutionBlocked("dual development project path or key differs")

    scopes = grant.get("projects")
    if not isinstance(scopes, list) or len(scopes) != 2:
        raise ExecutionBlocked("dual development grant must authorize exactly two projects")
    if [x.get("account") for x in scopes if isinstance(x, dict)] != list(_ACCOUNTS):
        raise ExecutionBlocked("dual development must bind Do Again and jobpipe in canonical order")
    if len([x for x in scopes if isinstance(x, dict)]) != len(scopes):
        raise ExecutionBlocked("dual development scope must contain dictionaries")
    if not isinstance(statuses, dict) or set(statuses) != set(accounts):
        raise ExecutionBlocked("dual development requires fresh sibling status observations")
    for account, scope in zip(_ACCOUNTS, scopes):
        project_config = next(p for p in protected if p["account"] == account)
        status = statuses[account]
        if (set(scope) != _PROJECT_FIELDS
                or scope["project_key"] != project_config["key"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(scope["project_key"]))
                or not isinstance(scope["goal_revision"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", scope["goal_revision"])
                or not isinstance(status, dict)
                or status.get("goal_revision") != scope["goal_revision"]
                or status.get("intent") not in {"active", "maintenance"}
                or status.get("unresolved_executions") != []
                or status.get("inflight_request_ids") != []):
            raise ExecutionBlocked("dual development sibling goal or execution is not admitted")

    sonary = statuses.get("_doagain_so")
    if sonary is not None and (not isinstance(sonary, dict)
            or sonary.get("intent") != "maintenance"
            or sonary.get("unresolved_executions") != []
            or sonary.get("inflight_request_ids") != []):
        raise ExecutionBlocked("Sonary must remain in quiescent maintenance")

    matching = next((p for p in protected if p["key"] == getattr(project, "key", None)), None)
    if (matching is None or matching["account"] not in _ACCOUNTS
            or getattr(project, "account", None) != matching["account"]
            or getattr(project, "repo", None) != Path(matching["repo"])):
        raise ExecutionBlocked("dual development caller is not an approved project")
    return grant
