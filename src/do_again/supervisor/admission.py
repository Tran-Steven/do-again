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
