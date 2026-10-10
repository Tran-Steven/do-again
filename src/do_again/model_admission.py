"""Read-only native maintenance admission for a separate Codex canary.

No model invocation, GitHub publication, browser action, or state mutation is
authorized by this status check. The protected root broker supplies status;
caller-provided project state is never accepted as equivalent evidence.
"""
from __future__ import annotations

from pathlib import Path

from .model_grant import CodexCanaryScope
from .supervisor.macos_execution import ExecutionBlocked


def require_codex_parent_maintenance(config: dict, scope: CodexCanaryScope, *, rpc=None) -> dict:
    from .supervisor.macos_client import broker_request

    if not isinstance(scope, CodexCanaryScope) or not isinstance(config, dict):
        raise ExecutionBlocked("Codex maintenance gate requires a sealed scope")
    projects = config.get("projects", [])
    if (not isinstance(projects, list) or len(projects) != 2
            or scope.source_sha != config.get("source_sha")
            or config.get("production_ready") is not False):
        raise ExecutionBlocked("Codex model preparation is outside maintenance configuration")
    by_account = {project.get("account"): project for project in projects
                  if isinstance(project, dict)}
    if set(by_account) != {"_doagain_da", "_doagain_jp"}:
        raise ExecutionBlocked("Codex parent project scope differs")
    reader = rpc if rpc is not None else broker_request
    observed = {}
    for account in ("_doagain_da", "_doagain_jp"):
        project = by_account[account]
        repo = project.get("repo")
        if not isinstance(repo, str) or not Path(repo).is_absolute():
            raise ExecutionBlocked("Codex project path was not sealed")
        result = reader(Path(repo), {"operation": "status"})
        if not isinstance(result, dict):
            raise ExecutionBlocked("Codex protected maintenance status was unavailable")
        if (result.get("source_sha") != scope.source_sha
                or result.get("production_ready") is not False
                or result.get("operator_intent") != "maintenance"
                or result.get("canary_authorized") is not False
                or result.get("enforcement_verified") is not True
                or result.get("enforcement_blocker") is not None
                or result.get("unresolved_executions") != []
                or result.get("inflight_request_ids") != []
                or type(result.get("epoch")) is not int
                or result["epoch"] < 1):
            raise ExecutionBlocked("protected parent maintenance authority is not quiescent")
        if account == "_doagain_da" and result["epoch"] != scope.parent_epoch:
            raise ExecutionBlocked("sealed Codex parent epoch is stale or changed")
        observed[account] = {
            "epoch": result["epoch"], "maintenance": True,
            "enforcement_verified": True, "no_unresolved_effects": True,
        }
    return observed
