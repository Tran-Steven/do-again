"""Explicit authorization boundary for a future ChatGPT browser planner.

This module cannot launch CDP, choose a browser profile, read cookies or send
a chat message. A separately qualified installed broker must supply transport.
Never reinterpret a Codex grant as browser authority.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta

from ..core.schema import canonical_json, parse_utc
from ..supervisor.macos_execution import ExecutionBlocked
from .planner_contract import AuthorizedTransportPlanner
from .offline_continuation import MAX_PERSISTENT_CYCLES


class BrowserPlanningNotDispatched(ExecutionBlocked):
    """Authenticated helper positively refused before its Send boundary."""


class PlanningNotReady(ExecutionBlocked):
    """A proven pre-dispatch wait, never uncertainty or task completion."""


def browser_grant_digest(grant):
    return hashlib.sha256(canonical_json(grant)).hexdigest()


def validate_browser_planner_grant(grant, *, source_sha, goal_sha256, epoch, now):
    """Verify a tightly scoped, separate browser grant, without a browser effect."""
    required = {
        'kind', 'transport', 'repository', 'branch', 'source_sha',
        'goal_sha256', 'epoch', 'issued_at_utc', 'expires_at_utc',
        'conversation_id', 'profile_sha256', 'max_calls',
        'max_prompt_bytes', 'max_answer_bytes', 'application_submissions',
        'worker_network_access', 'production_deployment', 'automatic_merge',
    }
    if not isinstance(grant, dict) or set(grant) != required:
        raise ExecutionBlocked('separate browser planner grant absent or contains unknown authority')
    persistent = grant['kind'] == 'browser-planning-persistent-v1'
    if (grant['kind'] not in {'browser-planning-v1', 'browser-planning-persistent-v1'}
            or grant['transport'] != 'chatgpt-browser-cdp'
            or grant['repository'] != 'Tran-Steven/jobpipe'
            or grant['branch'] != 'operator-control'
            or grant['source_sha'] != source_sha
            or grant['goal_sha256'] != goal_sha256
            or type(grant['epoch']) is not int
            or grant['epoch'] != epoch):
        raise ExecutionBlocked('browser planner grant identity differs')
    if (not isinstance(grant['conversation_id'], str)
            or not re.fullmatch(r'[0-9a-f-]{36}', grant['conversation_id'])
            or not isinstance(grant['profile_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', grant['profile_sha256'])):
        raise ExecutionBlocked('browser planner conversation or profile identity differs')
    if (type(grant['max_calls']) is not int or not (1 <= grant['max_calls'] <= MAX_PERSISTENT_CYCLES if persistent else grant['max_calls'] == 3)
            or type(grant['max_prompt_bytes']) is not int
            or grant['max_prompt_bytes'] != 32768
            or type(grant['max_answer_bytes']) is not int
            or grant['max_answer_bytes'] != 65536
            or any(grant[key] is not False for key in
                   ('application_submissions', 'worker_network_access',
                    'production_deployment', 'automatic_merge'))):
        raise ExecutionBlocked('browser planner privileges or budgets differ')
    try:
        issued = parse_utc(grant['issued_at_utc'])
        expires = None if persistent else parse_utc(grant['expires_at_utc'])
    except (TypeError, ValueError, KeyError) as exc:
        raise ExecutionBlocked('browser planner grant timestamps invalid') from exc
    if persistent:
        if grant['expires_at_utc'] is not None or now < issued:
            raise ExecutionBlocked('persistent browser authority must be explicitly until revoked')
        return grant
    if not issued <= now < expires or not timedelta(0) < expires-issued <= timedelta(hours=2):
        raise ExecutionBlocked('browser planner grant expired, premature, or overlong')
    return grant


class BrowserPlanner(AuthorizedTransportPlanner):
    """Adapter contract: only a trusted installed caller can supply a transport.

    A call still needs an independent, broker-side browser capability and
    durable no-retry reservation before first dispatch.
    """

    def __init__(self, authorize, reserve_call, transport, request_builder,
                 *, source_sha, goal_sha256, epoch):
        super().__init__(authorize, reserve_call, transport, request_builder)
        self.source_sha = source_sha
        self.goal_sha256 = goal_sha256
        self.epoch = epoch

    def _validated_grant(self, now):
        grant = self.authorize(now)
        return validate_browser_planner_grant(
            grant, source_sha=self.source_sha, goal_sha256=self.goal_sha256,
            epoch=self.epoch, now=now)


def validate_reserved_browser_call(call_id, grant):
    match = re.fullmatch(r'planner-([a-f0-9]{20})-([1-9][0-9]{0,4})', str(call_id))
    if (match is None or match[1] != browser_grant_digest(grant)[:20]
            or int(match[2]) > grant['max_calls']):
        raise ExecutionBlocked('browser call identity outside exact approved grant budget')


class BrowserResponseTransport:
    """No-CDP adapter for an injected, separately authorized single-send driver.

    The driver must enforce the bound CDP profile/session and no-retry rules.
    This layer accepts only an exact call-bound, JSON-only assistant response.
    """

    def __init__(self, send_once):
        self.send_once = send_once

    def __call__(self, prompt, schema, *, call, grant):
        import json
        pattern = r'planner-[a-f0-9]{20}-[1-9][0-9]{0,4}' if grant.get('kind') == 'browser-planning-persistent-v1' else r'planner-[a-f0-9]{20}-[123]'
        if (not isinstance(call, dict) or not isinstance(call.get('call_id'), str)
                or not re.fullmatch(pattern, call['call_id'])):
            raise ExecutionBlocked('browser transport requires a durably reserved call identity')
        if grant.get('kind') == 'browser-planning-persistent-v1':
            validate_reserved_browser_call(call['call_id'], grant)
        if not isinstance(prompt, str) or len(prompt.encode()) > grant['max_prompt_bytes']:
            raise ExecutionBlocked('browser planning prompt exceeds authorized length')
        envelope = {
            'call_id': call['call_id'], 'task': 'Return one JSON object and nothing else',
            'response_schema': schema, 'planning_input': json.loads(prompt),
        }
        message = json.dumps(envelope, sort_keys=True, separators=(',', ':'))
        if len(message.encode()) > grant['max_prompt_bytes'] + 2048:
            raise ExecutionBlocked('browser envelope exceeds bounded transmission budget')
        # The broker must have reserved the call before this single dispatch.
        received = self.send_once(message, conversation_id=grant['conversation_id'],
                                  call_id=call['call_id'],
                                  profile_sha256=grant['profile_sha256'],
                                  grant_sha256=browser_grant_digest(grant))
        if (not isinstance(received, dict) or set(received) != {'conversation_id', 'response'}
                or received['conversation_id'] != grant['conversation_id']
                or not isinstance(received['response'], str)
                or len(received['response'].encode()) > grant['max_answer_bytes']):
            raise ExecutionBlocked('browser planning reply does not match bound session')
        try:
            answer = json.loads(received['response'])
        except (ValueError, TypeError) as exc:
            raise ExecutionBlocked('browser planning reply must be a single JSON object') from exc
        if (not isinstance(answer, dict) or set(answer) != {'call_id', 'result'}
                or answer['call_id'] != call['call_id']):
            raise ExecutionBlocked('browser planning reply does not match reserved call')
        return answer['result']
