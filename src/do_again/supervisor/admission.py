"""Trusted production entrypoints fail closed before preparation or claiming."""
from __future__ import annotations

import sys
from pathlib import Path

from ..core.schema import OperatorError
from .macos_client import broker_request


def require_active(repo: Path) -> dict:
    if sys.platform != 'darwin':
        raise OperatorError('verified native project admission is unavailable; no fallback')
    status = broker_request(repo, {'operation': 'status'})
    if (status.get('operator_intent') != 'active'
            or status.get('production_ready') is not True
            or status.get('enforcement_verified') is not True):
        raise OperatorError('project admission is closed by operator intent or incomplete production migration')
    return status


def reject_legacy_runtime(repo: Path) -> None:
    # Legacy installers copy writable source and start an unrestricted operator
    # daemon. They must remain unavailable even after broker execution is ready.
    require_active(repo)
    raise OperatorError('legacy runtime preparation is disabled; use guarded immutable deployment')


def require_worker_active(repo: Path, *, status: dict | None = None, rpc=None) -> dict:
    """Admit production, sealed canary, or exact root-sealed offline jobpipe.

    This is only a *worker* gate. The legacy require_active/reject_legacy_runtime
    entrypoints remain production-only. Root broker still independently enforces
    all commands, publishes, epoch fencing, sandbox networking and job-app bans.
    """
    import os
    from types import SimpleNamespace
    if sys.platform != 'darwin':
        raise OperatorError('native worker authority is unavailable outside macOS')
    repo = Path(repo).resolve()
    request = rpc or broker_request
    observed = status if status is not None else request(repo, {'operation':'status'})
    if (not isinstance(observed, dict)
            or observed.get('operator_intent') != 'active'
            or observed.get('enforcement_verified') is not True
            or observed.get('enforcement_blocker') is not None):
        raise OperatorError('worker requires active, verified native broker authority')
    if (observed.get('production_ready') is True
            or observed.get('canary_authorized') is True):
        return observed
    if (observed.get('production_ready') is not False
            or observed.get('canary_authorized') is not False):
        raise OperatorError('unknown worker authority cannot be admitted')

    from .immutable_worker import read_worker_configuration
    from .authority import project_identity
    from .jobpipe_development import require_jobpipe_grant
    config = read_worker_configuration()
    projects = config.get('projects', [])
    if (config.get('production_ready') is not False
            or config.get('operator_uid') != os.getuid()
            or repo != Path(str(config.get('operator_home', ''))) / 'jobpipe'
            or observed.get('source_sha') != config.get('source_sha')
            or not isinstance(projects, list)):
        raise OperatorError('offline worker differs from installed operator and source')
    project = next((p for p in projects
                    if isinstance(p, dict) and p.get('account') == '_doagain_jp'), None)
    if (not isinstance(project, dict)
            or project.get('repo') != str(repo)
            or project.get('key') != project_identity(repo)
            or project.get('github_repository') != 'Tran-Steven/jobpipe'
            or observed.get('worktree') != project.get('worktree')
            or observed.get('uid') != project.get('uid')
            or observed.get('unresolved_executions') != []
            or observed.get('inflight_request_ids') != []):
        raise OperatorError('offline worker is not the sealed, drained jobpipe project')
    try:
        require_jobpipe_grant(config, SimpleNamespace(account='_doagain_jp',
            repo=repo, key=project['key']),
            {'goal_revision':observed.get('goal_revision')})
    except Exception as exc:
        from .macos_execution import ExecutionBlocked
        if not isinstance(exc, ExecutionBlocked):
            raise
        raise OperatorError(str(exc)) from exc
    for sibling in projects:
        if sibling['account'] == '_doagain_jp':
            continue
        peer = request(Path(sibling['repo']), {'operation':'status'})
        if (peer.get('operator_intent') != 'maintenance'
                or peer.get('production_ready') is not False
                or peer.get('source_sha') != config['source_sha']
                or peer.get('unresolved_executions') != []
                or peer.get('inflight_request_ids') != []):
            raise OperatorError('offline worker requires drained sibling maintenance')
    return observed
