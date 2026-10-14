"""Provider-neutral planning contract; grants no network authority."""
from __future__ import annotations
from ..core.schema import canonical_json
from ..supervisor.macos_execution import ExecutionBlocked

ANSWER_LIMITS = {'gap': 1000, 'acceptance': 1000, 'script': 16000}
MAX_PROMPT_BYTES = 32768


def completed_changes(state):
    acknowledged = set(state.get('acknowledged', [])[-8:])
    return [{k:(record.get(k, '')[:256] if k in {'gap','acceptance'} and isinstance(record.get(k),str) else record.get(k))
             for k in ('receipt_sha256', 'implementation_commit', 'test_commit', 'gap', 'acceptance')}
            | {'request_id': record['request']['request_id']}
            for record in state.get('requests', [])
            if record.get('phase') == 'acknowledged'
            and record['request']['request_id'] in acknowledged]


def planning_prompt(goal, evidence, state, *, max_bytes=MAX_PROMPT_BYTES):
    """Only previously redacted public evidence may be passed by the caller."""
    if not isinstance(state, dict) or not isinstance(state.get('acknowledged'), list):
        raise ExecutionBlocked('planning state is not a verified continuation')
    prompt = canonical_json({
        'goal': goal, 'evidence': evidence,
        'completed_request_ids': state['acknowledged'][-8:],
        'completed_changes': completed_changes(state),
        'failed_changes': [{'request_id': r['request']['request_id'],
            'failure': {k:v for k,v in r['verified_failure'].items() if k!='test_cases'} | {
                'failed_test_cases': [{'case':c['case'],'exit_code':c['exit_code']}
                    for c in r['verified_failure'].get('test_cases',[]) if c['exit_code']!=0][:8]},
            'receipt_sha256': r['receipt_sha256']}
            for r in state.get('requests', [])[-3:] if r.get('phase') == 'failed'],
    }).decode()
    if max_bytes not in {32768,65536} or len(prompt.encode()) > max_bytes:
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
    try:
        compile(answer['script'], '<authorized-planner-script>', 'exec')
    except (SyntaxError, ValueError, TypeError) as exc:
        raise ExecutionBlocked('planner script is not valid Python before dispatch') from exc
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

    def _reject_original(self, call, error):
        journal = getattr(self.reserve_call, '__self__', None)
        if journal is None or not hasattr(journal, 'answered_invalid'):
            raise ExecutionBlocked('persistent planner lacks durable original reply journal')
        journal.answered_invalid(call, error)

    def select(self, *, goal, evidence, state, now):
        grant = self._validated_grant(now)
        prompt = planning_prompt(goal, evidence, state, max_bytes=grant.get("max_prompt_bytes",MAX_PROMPT_BYTES))
        schema = planning_schema()
        persistent = grant.get('kind') == 'browser-planning-persistent-v1'
        completed = completed_changes(state)
        if persistent:
            schema['properties']['acknowledged_receipts'] = {'const': completed}
            schema['required'].append('acknowledged_receipts')
        cycle = len(state['requests']) + 1
        call = self.reserve_call(grant, cycle, prompt, schema)
        try:
            answer = self.transport(prompt, schema, call=call, grant=grant)
        except ExecutionBlocked as exc:
            from .browser_planner import (BrowserPlanningNotDispatched, BrowserPlanningAnsweredInvalid,
                BrowserPlanningAwaitingReply, PlanningNotReady)
            if persistent and isinstance(exc, BrowserPlanningNotDispatched):
                journal = getattr(self.reserve_call, '__self__', None)
                if journal is None or not hasattr(journal, 'not_dispatched'):
                    raise ExecutionBlocked('persistent planner lacks durable refusal journal') from exc
                journal.not_dispatched(call, exc)
                raise PlanningNotReady(str(exc)) from exc
            if persistent and isinstance(exc, BrowserPlanningAwaitingReply):
                raise PlanningNotReady(str(exc)) from exc
            if persistent and isinstance(exc, BrowserPlanningAnsweredInvalid):
                self._reject_original(call, exc)
                raise PlanningNotReady(str(exc)) from exc
            raise
        if persistent:
            if (not isinstance(answer, dict)
                    or set(answer) != set(ANSWER_LIMITS) | {'acknowledged_receipts'}
                    or answer['acknowledged_receipts'] != completed):
                self._reject_original(call, 'model did not acknowledge exact verified prior receipts')
                from .browser_planner import PlanningNotReady
                raise PlanningNotReady('original reply rejected: receipt acknowledgment differs')
            answer = {k:v for k,v in answer.items() if k != 'acknowledged_receipts'}
        try:
            answer = validate_planning_answer(answer)
        except ExecutionBlocked as exc:
            if not persistent: raise
            self._reject_original(call, exc)
            from .browser_planner import PlanningNotReady
            raise PlanningNotReady('original reply rejected: bounded result schema differs') from exc
        return self.request_builder(answer, evidence, now)
