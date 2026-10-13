"""Existing Codex transport behind a separate sealed hosted-planning gate.

The broker must supply an authenticated operator-identity transport helper.
No helper is installed or inferred from the offline execution grant here.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from ..core.schema import canonical_json, parse_utc, atomic_json, OperatorError
from ..supervisor.macos_execution import ExecutionBlocked
from .planner_contract import AuthorizedTransportPlanner


class CodexPlanner(AuthorizedTransportPlanner):
    """Existing Codex implementation of the shared planner contract."""

    def _validated_grant(self, now):
        grant = self.authorize(now)  # Root-sealed gate, independent of worker scope.
        if (not isinstance(grant, dict) or grant.get('provider') != 'openai'
                or grant.get('transport') != 'codex-cli'
                or grant.get('model') != 'gpt-6.1-sol'
                or not isinstance(grant.get('backend_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', grant['backend_sha256'])
                or grant.get('max_call_seconds') != 120
                or grant.get('max_total_seconds') != 360
                or grant.get('max_prompt_bytes') != 32768
                or grant.get('max_answer_bytes') != 65536
                or type(grant.get('execution_uid')) is not int
                or grant.get('execution_uid') != 501
                or grant.get('max_calls_per_cycle') != 1
                or grant.get('max_total_calls') != 3
                or grant.get('worker_network_access') is not False
                or grant.get('browser_messages') is not False
                or grant.get('application_submissions') is not False):
            raise ExecutionBlocked('separate hosted planning authority absent or differs')
        try:
            issued = parse_utc(grant.get('issued_at_utc', ''))
            expires = parse_utc(grant.get('expires_at_utc', ''))
        except (ValueError, TypeError, OperatorError):
            raise ExecutionBlocked('hosted planning authorization timestamps invalid') from None
        if not issued <= now < expires or (expires-issued).total_seconds() > 7200:
            raise ExecutionBlocked('separate hosted planning authority absent or differs')
        return grant

class PlanningCallJournal:
    """Broker-lock-protected, root-owned call budget; uncertainty is no-retry."""
    def __init__(self, path: Path): self.path = path

    def reserve(self, grant, cycle, prompt, schema):
        persistent = grant.get('kind') == 'browser-planning-persistent-v1'
        limit = grant.get('max_calls', 0) if persistent else 3
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ExecutionBlocked('invalid persistent call budget')
        if type(cycle) is not int or not 1 <= cycle <= limit:
            raise ExecutionBlocked('hosted planner cycle outside approved call budget')
        scope_hash = hashlib.sha256(canonical_json(grant)).hexdigest()
        value = json.loads(self.path.read_text()) if self.path.exists() else {
            'scope_sha256': scope_hash, 'calls': {}}
        if value['scope_sha256'] != scope_hash:
            raise ExecutionBlocked('hosted planning journal belongs to another grant')
        key = str(cycle)
        if key in value['calls'] or len(value['calls']) >= limit:
            raise ExecutionBlocked('hosted call already reserved or call budget exhausted')
        record = {'call_id': 'planner-'+scope_hash[:20]+'-'+key, 'state': 'reserved',
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'schema_sha256': hashlib.sha256(canonical_json(schema)).hexdigest()}
        value['calls'][key] = record
        atomic_json(self.path, value)
        return record
