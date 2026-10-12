"""Root-sealed, time-bounded OFFLINE development authority for jobpipe only.

This is NOT production or job-application permission. Model-selected commands
run under the existing network-denied macOS sandbox. The only trusted network
effects remain GitHub control/publication and ChatGPT receipt delivery.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .macos_execution import ExecutionBlocked

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_GOAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FIELDS = frozenset({
    "kind", "source_sha", "project_key", "goal_revision",
    "issued_at_utc", "expires_at_utc", "network_access", "application_submissions",
})


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise ExecutionBlocked("offline development timestamps must be exact UTC seconds")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ExecutionBlocked("invalid offline development UTC timestamp") from exc


def require_jobpipe_grant(config, project, status=None, *, now=None):
    """Pure admission check with no process, network, Git, or filesystem effects."""
    grant = config.get("development_scope")
    if not isinstance(grant, dict) or set(grant) != _FIELDS:
        raise ExecutionBlocked("missing or malformed root-sealed jobpipe development scope")
    if (config.get("production_ready") is not False
            or config.get("live_canary") is not None
            or config.get("codex_canary") is not None
            or grant["kind"] != "jobpipe-offline-v1"
            or grant["network_access"] is not False
            or grant["application_submissions"] is not False
            or not isinstance(config.get("source_sha"), str)
            or not _SHA.fullmatch(config["source_sha"])
            or grant["source_sha"] != config["source_sha"]):
        raise ExecutionBlocked("jobpipe development cannot grant production, network, or application authority")
    home = config.get("operator_home")
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise ExecutionBlocked("jobpipe development operator home is invalid")
    projects = config.get("projects")
    if not isinstance(projects, list) or len(projects) not in (2, 3):
        raise ExecutionBlocked("offline development requires the exact protected project roster")
    accounts = [p.get("account") for p in projects if isinstance(p, dict)]
    if (len(accounts) != len(projects) or len(set(accounts)) != len(accounts)
            or set(accounts) not in ({"_doagain_da", "_doagain_jp"},
                                     {"_doagain_da", "_doagain_jp", "_doagain_so"})):
        raise ExecutionBlocked("offline development has an invalid project roster")
    from .authority import project_identity
    repo = Path(home) / "jobpipe"
    key = project_identity(repo)
    if (getattr(project, "account", None) != "_doagain_jp"
            or getattr(project, "repo", None) != repo
            or getattr(project, "key", None) != key
            or not isinstance(grant["project_key"], str)
            or not _KEY.fullmatch(grant["project_key"])
            or grant["project_key"] != key
            or len([p for p in projects if p.get("account") == "_doagain_jp"
                    and p.get("repo") == str(repo) and p.get("key") == key]) != 1):
        raise ExecutionBlocked("offline development is not bound to the exact jobpipe identity")
    if (not isinstance(grant["goal_revision"], str)
            or not _GOAL.fullmatch(grant["goal_revision"])):
        raise ExecutionBlocked("offline development requires an exact accepted goal revision")
    issued = _timestamp(grant["issued_at_utc"])
    expires = _timestamp(grant["expires_at_utc"])
    current = now if now is not None else datetime.now(timezone.utc)
    if (not isinstance(current, datetime) or current.tzinfo is None
            or not issued <= current <= expires
            or not timedelta(0) < expires - issued <= timedelta(days=14)):
        raise ExecutionBlocked("offline development grant is expired, premature, or overlong")
    if status is not None and status.get("goal_revision") != grant["goal_revision"]:
        raise ExecutionBlocked("offline development goal revision differs from root grant")
    return grant


def require_broker_jobpipe_development(broker, *, now=None):
    """Fence every effect against the protected sibling projects."""
    status = broker.registry.status(broker.project.repo)
    require_jobpipe_grant(broker.config, broker.project, status, now=now)
    for sibling in broker.config["projects"]:
        if sibling["account"] == "_doagain_jp":
            continue
        peer = broker.registry.status(Path(sibling["repo"]))
        if (peer.get("intent") != "maintenance"
                or broker.ledger.pending(sibling["key"])):
            raise ExecutionBlocked("jobpipe development requires all sibling projects in drained maintenance")
    return True

def synthetic_qualification_config(config, **overrides):
    """Construct only the in-memory, root-gated synthetic probe broker scope.

    The actual sealed config must remain maintenance-only with its offline grant.
    Synthetic brokers simulate active execution on isolated fixture worktrees;
    inheriting that grant while setting production_ready=True would create an
    invalid mixed authority. The caller must bind this returned config exclusively
    to an inaccessible synthetic broker under the real maintenance admission fence.
    """
    if config.get("production_ready") is not False:
        raise ExecutionBlocked("synthetic qualification requires maintenance configuration")
    if config.get("development_scope") is not None:
        from types import SimpleNamespace
        project = next((p for p in config.get("projects", [])
                        if isinstance(p, dict) and p.get("account") == "_doagain_jp"), None)
        if project is None:
            raise ExecutionBlocked("synthetic qualification lacks jobpipe scope")
        require_jobpipe_grant(config, SimpleNamespace(
            account=project["account"], repo=Path(project["repo"]), key=project["key"]))
    controller_fields={"controller_scope","controller_goal","controller_goal_sha256","controller_profile","hosted_planning_scope"}
    if "production_ready" in overrides or "development_scope" in overrides or controller_fields&set(overrides):
        raise ExecutionBlocked("synthetic qualification authority fields cannot be overridden")
    value=dict(config,production_ready=True,development_scope=None,**overrides)
    for field in controller_fields:value.pop(field,None)
    return value
