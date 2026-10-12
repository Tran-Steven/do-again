"""Continuation driver reusing the protected request/receipt transport."""
from __future__ import annotations

from typing import Protocol
from ..supervisor.macos_execution import ExecutionBlocked


class Planner(Protocol):
    def select(self, *, goal: dict, evidence: dict, state: dict, now) -> dict: ...


class DeterministicPlanner:
    """Offline qualification only; selectors use actual fixture test evidence."""
    def __init__(self, selector): self.selector = selector

    def select(self, *, goal, evidence, state, now):
        return self.selector(goal, evidence, state, now)


class ControllerLoop:
    def __init__(self, controller, history, planner: Planner, goal, collect_evidence, native_verify):
        self.controller, self.history, self.planner = controller, history, planner
        self.goal, self.collect_evidence, self.native_verify = goal, collect_evidence, native_verify

    def tick(self, now):
        state = self.controller.status(now)
        if state['state'] in {'stopped', 'blocked', 'completed'}: return state
        if state['outstanding']:
            self.history.publish_reserved(now)  # Reconcile same identity after restart.
            self.history.completion(now, self.native_verify)
            return self.controller.status(now)
        if state['state'] != 'planning': raise ExecutionBlocked('unknown controller lifecycle')
        pending = self.history.pending()
        if pending: raise ExecutionBlocked('remote request outstanding before planning')
        try:
            proposal = self.planner.select(goal=self.goal, evidence=self.collect_evidence(),
                                           state=state, now=now)
        except Exception as exc:
            self.controller.block('planner failed or uncertain: '+type(exc).__name__, now)
            raise
        self.controller.reserve(proposal, pending_ids=pending, now=now)
        self.history.publish_reserved(now)
        return self.controller.status(now)
