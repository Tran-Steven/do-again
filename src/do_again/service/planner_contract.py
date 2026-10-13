"""Provider-neutral planning contract; grants no network authority."""
from __future__ import annotations
from ..core.schema import canonical_json
from ..supervisor.macos_execution import ExecutionBlocked

ANSWER_LIMITS = {'gap': 1000, 'acceptance': 1000, 'script': 16000}
MAX_PROMPT_BYTES = 32768


def completed_changes(state):
    acknowledged = set(state.get('acknowledged', [])[-8:])
    return [{k:record.get(k) for k in ('receipt_sha256', 'implementation_commit',
                                      'test_commit', 'gap', 'acceptance')}
            | {'request_id': record['request']['request_id']}
            for record in state.get('requests', [])
            if record.get('phase') == 'acknowledged'
            and record['request']['request_id'] in acknowledged]


def planning_prompt(goal, evidence, state):
    """Only previously redacted public evidence may be passed by the caller."""
    if not isinstance(state, dict) or not isinstance(state.get('acknowledged'), list):
        raise ExecutionBlocked('planning state is not a verified continuation')
    prompt = canonical_json({
        'goal': goal, 'evidence': evidence,
        'completed_request_ids': state['acknowledged'][-8:],
        'completed_changes': completed_changes(state),
    }).decode()
    if len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise ExecutionBlocked('planning prompt exceeds authorized exposure budget')
    return prompt


def planning_schema():
    return {
        'type': 'object',
        'properties': {
            key: {'type': 'string', 'maxLength': limit}
            for key, limit in ANSWER_LIMITS.items()
        },
        'required': list(ANSWER_LIMITS),
        'additionalProperties': False,
    }


def validate_planning_answer(answer):
    if (not isinstance(answer, dict) or set(answer) != set(ANSWER_LIMITS)
            or any(not isinstance(answer[key], str) or not answer[key]
                   or len(answer[key]) > limit
                   for key, limit in ANSWER_LIMITS.items())):
        raise ExecutionBlocked('planner answer outside bounded schema')
    return answer


class AuthorizedTransportPlanner:
    """Shared bounded planner for separately authorized and reserved transports.

    The authorize hook must reject an unapproved or expired grant; the
    reserve_call hook must durably reserve before the transport executes.
    No browser session, CDP or network access is authorized by this class.
    """

    def __init__(self, authorize, reserve_call, transport, request_builder):
        self.authorize = authorize
        self.reserve_call = reserve_call
        self.transport = transport
        self.request_builder = request_builder

    def _validated_grant(self, now):
        raise ExecutionBlocked('planner provider requires explicit sealed adapter')

    def select(self, *, goal, evidence, state, now):
        grant = self._validated_grant(now)
        prompt = planning_prompt(goal, evidence, state)
        schema = planning_schema()
        persistent = grant.get('kind') == 'browser-planning-persistent-v1'
        completed = completed_changes(state)
        if persistent:
            schema['properties']['acknowledged_receipts'] = {'const': completed}
            schema['required'].append('acknowledged_receipts')
        cycle = len(state['requests']) + 1
        call = self.reserve_call(grant, cycle, prompt, schema)
        answer = self.transport(prompt, schema, call=call, grant=grant)
        if persistent:
            if (not isinstance(answer, dict)
                    or set(answer) != set(ANSWER_LIMITS) | {'acknowledged_receipts'}
                    or answer['acknowledged_receipts'] != completed):
                raise ExecutionBlocked('model did not acknowledge exact verified prior receipts')
            answer = {k:v for k,v in answer.items() if k != 'acknowledged_receipts'}
        return self.request_builder(validate_planning_answer(answer), evidence, now)
