"""Provider-neutral planning contract; grants no network authority."""
from __future__ import annotations
from ..core.schema import canonical_json
from ..supervisor.macos_execution import ExecutionBlocked

ANSWER_LIMITS = {'gap': 1000, 'acceptance': 1000, 'script': 16000}
MAX_PROMPT_BYTES = 32768


def planning_prompt(goal, evidence, state):
    """Only previously redacted public evidence may be passed by the caller."""
    if not isinstance(state, dict) or not isinstance(state.get('acknowledged'), list):
        raise ExecutionBlocked('planning state is not a verified continuation')
    prompt = canonical_json({
        'goal': goal, 'evidence': evidence,
        'completed_request_ids': state['acknowledged'],
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
        cycle = len(state['requests']) + 1
        call = self.reserve_call(grant, cycle, prompt, schema)
        answer = self.transport(prompt, schema, call=call, grant=grant)
        return self.request_builder(validate_planning_answer(answer), evidence, now)
