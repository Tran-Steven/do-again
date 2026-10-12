"""Fail-closed controller planner selection.

Provider choice comes solely from the sealed controller scope. A newly added
provider cannot inherit a legacy transport or root broker capability.
"""
from __future__ import annotations

from ..supervisor.macos_execution import ExecutionBlocked


def select_controller_planner(kind, *, offline_factory, codex_factory,
                              browser_factory=None):
    if kind == 'sealed-offline':
        return offline_factory()
    if kind == 'codex-cli':
        return codex_factory()
    if kind == 'chatgpt-browser-cdp':
        if browser_factory is None:
            raise ExecutionBlocked(
                'browser planner lacks qualified operator-side broker transport')
        return browser_factory()
    raise ExecutionBlocked('controller planner provider is not explicitly authorized')
