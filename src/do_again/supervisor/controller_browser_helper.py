"""Root-sealed browser planner helper; unavailable until native bridge qualification.

The helper does not start an operator browser process. It only calls an
already authenticated, operator-owned Unix socket after a durable controller
planner reservation. Missing qualification is fatal, not a Codex fallback.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from ..service.browser_bridge import BrokerBrowserBridge
from ..service.browser_planner import (
    BrowserResponseTransport, browser_grant_digest, validate_browser_planner_grant,
)
from .macos_execution import ExecutionBlocked, private_root_file


class SealedBrowserPlannerHelper:
    def __init__(self, config, state, admission):
        self.config, self.state, self.admission = config, state, admission
        grant = config.get('browser_planning_scope')
        self.validate(grant, datetime.now(timezone.utc))
        qualification = Path(state) / 'browser-planner-qualification.json'
        private_root_file(qualification)
        try:
            proof = json.loads(qualification.read_text())
        except (ValueError, OSError) as exc:
            raise ExecutionBlocked('browser bridge qualification unreadable') from exc
        scope = config['controller_scope']
        if (not isinstance(proof, dict)
                or set(proof) != {'state', 'source_sha', 'goal_sha256',
                                  'epoch', 'grant_sha256', 'operator_uid',
                                  'browser_calls', 'peer_uid'}
                or proof['state'] != 'qualified_operator_bridge'
                or proof['source_sha'] != config['source_sha']
                or proof['goal_sha256'] != scope['goal_sha256']
                or proof['epoch'] != scope['epoch']
                or proof['grant_sha256'] != browser_grant_digest(grant)
                or proof['operator_uid'] != 501
                or proof['peer_uid'] != 501
                or proof['browser_calls'] != 0):
            raise ExecutionBlocked('operator browser bridge lacks source-bound native qualification')
        self.socket_path = Path(config['operator_home']) / '.do_again/browser/planner.sock'

    def validate(self, grant, now):
        scope = self.config['controller_scope']
        return validate_browser_planner_grant(
            grant, source_sha=self.config['source_sha'],
            goal_sha256=scope['goal_sha256'], epoch=scope['epoch'], now=now)

    def authorize(self, now):
        with self.admission():
            return self.validate(self.config.get('browser_planning_scope'), now)

    def transport(self, prompt, schema, *, call, grant):
        with self.admission():
            self.validate(grant, datetime.now(timezone.utc))
        client = BrokerBrowserBridge(self.socket_path)
        return BrowserResponseTransport(client.send_once)(
            prompt, schema, call=call, grant=grant)
