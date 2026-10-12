"""Existing Codex transport behind a separate sealed hosted-planning gate.

The broker must supply an authenticated operator-identity transport helper.
No helper is installed or inferred from the offline execution grant here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from ..core.schema import canonical_json, parse_utc, atomic_json
from ..supervisor.macos_execution import ExecutionBlocked


class CodexPlanner:
    def __init__(self, authorize, reserve_call, transport, request_builder):
        self.authorize, self.reserve_call = authorize, reserve_call
        self.transport, self.request_builder = transport, request_builder

    def select(self, *, goal, evidence, state, now):
        grant = self.authorize(now)  # Root-sealed gate, independent of worker scope.
        if (not isinstance(grant, dict) or grant.get('provider') != 'openai'
                or grant.get('transport') != 'codex-cli'
                or type(grant.get('execution_uid')) is not int
                or grant.get('execution_uid') != 501
                or grant.get('max_calls_per_cycle') != 1
                or grant.get('max_total_calls') != 3
                or grant.get('worker_network_access') is not False
                or grant.get('browser_messages') is not False
                or grant.get('application_submissions') is not False
                or not now < parse_utc(grant.get('expires_at_utc', ''))):
            raise ExecutionBlocked('separate hosted planning authority absent or differs')
        # Only pre-redacted repository evidence/goal enters this object. The
        # collector must exclude private candidate data and operational secrets.
        prompt = canonical_json({'goal': goal, 'evidence': evidence,
                                  'completed_request_ids': state['acknowledged']}).decode()
        if len(prompt.encode()) > 32768:
            raise ExecutionBlocked('planning prompt exceeds authorized exposure budget')
        fields = {'gap': 1000, 'acceptance': 1000, 'script': 16000}
        schema = {'type': 'object', 'properties': {
            key: {'type': 'string', 'maxLength': length} for key, length in fields.items()},
            'required': list(fields), 'additionalProperties': False}
        cycle = len(state['requests']) + 1
        # Must durably reserve a unique scope/cycle call identity before any
        # hosted effect. A lost response remains uncertain; never auto-retry.
        call = self.reserve_call(grant, cycle, prompt, schema)
        answer = self.transport(prompt, schema, call=call, grant=grant)
        if (not isinstance(answer, dict) or set(answer) != set(fields)
                or any(not isinstance(answer[k], str) or not answer[k]
                       or len(answer[k]) > fields[k] for k in fields)):
            raise ExecutionBlocked('planner answer outside bounded schema')
        return self.request_builder(answer, evidence, now)


class PlanningCallJournal:
    """Broker-lock-protected, root-owned call budget; uncertainty is no-retry."""
    def __init__(self, path: Path): self.path = path

    def reserve(self, grant, cycle, prompt, schema):
        if type(cycle) is not int or not 1 <= cycle <= 3:
            raise ExecutionBlocked('hosted planner cycle outside three-call qualification')
        scope_hash = hashlib.sha256(canonical_json(grant)).hexdigest()
        value = json.loads(self.path.read_text()) if self.path.exists() else {
            'scope_sha256': scope_hash, 'calls': {}}
        if value['scope_sha256'] != scope_hash:
            raise ExecutionBlocked('hosted planning journal belongs to another grant')
        key = str(cycle)
        if key in value['calls'] or len(value['calls']) >= 3:
            raise ExecutionBlocked('hosted call already reserved or call budget exhausted')
        record = {'call_id': 'planner-'+scope_hash[:20]+'-'+key, 'state': 'reserved',
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
            'schema_sha256': hashlib.sha256(canonical_json(schema)).hexdigest()}
        value['calls'][key] = record
        atomic_json(self.path, value)
        return record
