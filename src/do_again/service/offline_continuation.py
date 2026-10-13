"""Offline continuation state machine; never grants or performs effects.

An immutable root broker must supply sealed authority and verified GitHub/native
evidence. This module is deliberately not wired into the installed daemon.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ..core.schema import atomic_json, canonical_json, parse_utc, request_fingerprint
from ..supervisor.macos_execution import ExecutionBlocked


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


PERSISTENT_KIND = 'jobpipe-native-controller-v1'
MAX_PERSISTENT_CYCLES = 10000


def persistent_scope(scope):
    return isinstance(scope, dict) and scope.get('kind') == PERSISTENT_KIND


def controller_artifact(scope, name):
    # New authority gets new journals. Never reinterpret a canary journal.
    if persistent_scope(scope):
        return 'native-' + digest(scope)[:20] + '-' + name
    return name


def validate_scope(scope, *, binding, now: datetime):
    fields = {'kind', 'repository', 'branch', 'goal_sha256', 'source_sha',
              'epoch', 'issued_at_utc', 'expires_at_utc', 'max_requests',
              'max_cycles', 'max_outstanding', 'max_retries', 'planner',
              'integration', 'network_access', 'application_submissions',
              'browser_messages'}
    if not isinstance(scope, dict) or set(scope) != fields:
        raise ExecutionBlocked('controller scope absent or contains unknown authority')
    persistent = persistent_scope(scope)
    if (scope['kind'] not in {'jobpipe-offline-controller-v1', PERSISTENT_KIND}
            or scope['repository'] != 'Tran-Steven/jobpipe'
            or scope['branch'] != 'operator-control'
            or scope['planner'] not in {'sealed-offline', 'codex-cli', 'chatgpt-browser-cdp'}
            or scope['integration'] != 'proposal-only'
            or any(scope[k] is not False for k in
                   ('network_access', 'application_submissions', 'browser_messages'))
            or any(type(scope[k]) is not int or scope[k] != v for k, v in
                   [('max_outstanding', 1), ('max_retries', 0)])
            or type(scope['epoch']) is not int or scope['epoch'] < 1
            or not isinstance(scope['source_sha'], str)
            or not re.fullmatch('[0-9a-f]{40}', scope['source_sha'])
            or not isinstance(scope['goal_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', scope['goal_sha256'])
            or any(scope.get(k) != binding.get(k) for k in
                   ('repository', 'branch', 'goal_sha256', 'source_sha'))
            or (persistent and (type(binding.get('epoch')) is not int or binding['epoch'] < scope['epoch']))
            or (not persistent and scope['epoch'] != binding.get('epoch'))):
        raise ExecutionBlocked('controller binding or bounded authority differs')
    if (type(scope['max_cycles']) is not int or type(scope['max_requests']) is not int
            or scope['max_requests'] != scope['max_cycles']
            or not 1 <= scope['max_cycles'] <= (MAX_PERSISTENT_CYCLES if persistent else 3)
            or (not persistent and scope['max_cycles'] != 3)
            or (persistent and scope['planner'] != 'chatgpt-browser-cdp')):
        raise ExecutionBlocked('controller cycle budget or persistent provider differs')
    issued = parse_utc(scope['issued_at_utc'])
    if persistent:
        if scope['expires_at_utc'] is not None or now < issued:
            raise ExecutionBlocked('persistent scope requires explicit until-revoked authority')
        return
    expires = parse_utc(scope['expires_at_utc'])
    if not issued <= now < expires or not timedelta(0) < expires-issued <= timedelta(hours=2):
        raise ExecutionBlocked('controller expired, premature or exceeds two hours')


class OfflineContinuation:
    """Serialized broker-owned journal using existing durable JSON primitives.

    Caller holds the protected broker lock across every method and external
    publication. Root ownership and installation verification stay in broker.
    Planner and evidence inputs are untrusted until separately verified there.
    """
    def __init__(self, path: Path, scope: dict, binding: dict):
        self.path, self.scope, self.binding = path, scope, binding

    def _load(self, now):
        validate_scope(self.scope, binding=self.binding, now=now)
        if self.path.exists():
            state = json.loads(self.path.read_text())
            if state.get('scope_sha256') != digest(self.scope):
                raise ExecutionBlocked('controller journal belongs to another scope')
            return state
        return {'scope_sha256': digest(self.scope), 'state': 'stopped',
                'requests': [], 'acknowledged': [], 'outstanding': None}

    def _save(self, state):
        atomic_json(self.path, state)
        return state

    def start(self, now):
        state = self._load(now)
        if state['state'] == 'blocked':
            raise ExecutionBlocked('blocked controller requires explicit reconciliation')
        if state['state'] == 'stopped':
            state['state'] = 'waiting' if state['outstanding'] else 'planning'
        return self._save(state)

    def stop(self, now):
        state = self._load(now)
        state['state'] = 'stopped'
        return self._save(state)

    def status(self, now):
        return self._load(now)

    def block(self, reason, now):
        state = self._load(now)
        state.update(state='blocked', blocker=reason)
        return self._save(state)

    def reserve(self, proposal, *, pending_ids, now):
        state = self._load(now)
        if state['state'] != 'planning' or state['outstanding'] or pending_ids:
            raise ExecutionBlocked('controller not planning or a request remains outstanding')
        if len(state['requests']) >= self.scope['max_requests']:
            raise ExecutionBlocked('authorized request budget exhausted')
        required = {'request', 'gap', 'acceptance', 'evidence_sha256'}
        if (not isinstance(proposal, dict) or set(proposal) != required
                or any(not isinstance(proposal[k], str) or not proposal[k]
                       for k in ('gap', 'acceptance', 'evidence_sha256'))):
            raise ExecutionBlocked('proposal lacks falsifiable goal/test evidence')
        request = json.loads(json.dumps(proposal['request']))
        # Implementation + its tests must occupy one execution request. No
        # shell or hosted model capabilities are added by this state machine.
        if (request.get('operation') != 'scratch_script'
                or request.get('schema_version') != 1
                or not isinstance(request.get('args'), dict)
                or request['args'].get('language') != 'python'
                or not isinstance(request['args'].get('content'), str)
                or not 1 <= len(request['args']['content'].encode()) <= 65536
                or not isinstance(request.get('expected'), dict)
                or not re.fullmatch('[0-9a-f]{40}', str(request['expected'].get('repo_head', '')))
                or not isinstance(request.get('limits'), dict)
                or type(request['limits'].get('timeout_seconds')) is not int
                or not 1 <= request['limits']['timeout_seconds'] <= 600):
            raise ExecutionBlocked('initial validation accepts offline implementation requests only')
        issued = parse_utc(request.get('issued_at_utc', ''))
        expires = parse_utc(request.get('expires_at_utc', ''))
        if (not issued <= now < expires
                or not timedelta(0) < expires-issued <= timedelta(minutes=15)
                or (not persistent_scope(self.scope)
                    and expires > parse_utc(self.scope['expires_at_utc']))
                or not re.fullmatch('[0-9a-f]{64}', proposal['evidence_sha256'])):
            raise ExecutionBlocked('request lifetime or evidence binding differs')
        if state.get('validated_head') and request['expected']['repo_head'] != state['validated_head']:
            raise ExecutionBlocked('next development cycle must build on prior validated head')
        ordinal = len(state['requests']) + 1
        rid = 'offline-' + digest(self.scope)[:20] + '-' + str(ordinal)
        request['request_id'] = rid
        request['continuation'] = {'goal_id': self.scope['goal_sha256'],
            'goal_state': 'in_progress', 'acknowledged_receipts': list(state['acknowledged'][-8:] if persistent_scope(self.scope) else state['acknowledged']),
            'summary': proposal['gap'] + ': ' + proposal['acceptance']}
        # Reserve exact content before the broker initiates any remote effect.
        record = {'request': request, 'fingerprint': request_fingerprint(request),
                  'proposal_sha256': digest(proposal), 'phase': 'reserved',
                  'gap': proposal['gap'], 'acceptance': proposal['acceptance']}
        state['requests'].append(record)
        state['outstanding'] = rid
        state['state'] = 'waiting'
        self._save(state)
        return record

    def publication(self, remote_request, now):
        state = self._load(now)
        if not state['outstanding']:
            raise ExecutionBlocked('no original publication reservation')
        record = state['requests'][-1]
        if remote_request != record['request']:
            state.update(state='blocked', blocker='remote request differs from reservation')
            self._save(state)
            raise ExecutionBlocked(state['blocker'])
        record['phase'] = 'published'
        return self._save(state)

    def accept(self, receipt, *, evidence, now):
        state = self._load(now)
        if state['state'] == 'blocked':
            raise ExecutionBlocked('blocked controller cannot continue')
        rid = receipt.get('request_id')
        if rid in state['acknowledged']:
            return state  # Restart-safe duplicate observation; no new task.
        if rid != state['outstanding'] or not state['requests']:
            raise ExecutionBlocked('receipt is not the outstanding request')
        record = state['requests'][-1]
        # Evidence must be derived by the broker from durable remote blobs,
        # its native ledger and the exact-source test/artifact records. Never
        # accept worker stdout or model assertions as these attestations.
        checks = ('remote_durable', 'native_terminal', 'tests_passed',
                  'implementation_changed', 'authority_matches', 'no_submission')
        sha = evidence.get('implementation_commit')
        if (record['phase'] != 'published' or receipt.get('state') != 'succeeded'
                or receipt.get('request_fingerprint') != record['fingerprint']
                or any(evidence.get(k) is not True for k in checks)
                or evidence.get('request_fingerprint') != record['fingerprint']
                or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha)
                or evidence.get('test_commit') != sha
                or sha == record['request']['expected']['repo_head']):
            state.update(state='blocked', blocker='completion lacks verified implementation/test evidence')
            self._save(state)
            raise ExecutionBlocked(state['blocker'])
        state['acknowledged'].append(rid)
        state['outstanding'] = None
        record.update(phase='acknowledged', receipt_sha256=digest(receipt))
        state['validated_head'] = sha
        record.update(implementation_commit=sha, test_commit=sha,
                      branch=evidence.get('branch'))
        if state['state'] != 'stopped':
            state['state'] = 'completed' if len(state['acknowledged']) >= self.scope['max_cycles'] else 'planning'
        return self._save(state)
