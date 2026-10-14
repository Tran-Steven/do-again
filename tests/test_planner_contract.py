"""The planner boundary is provider-neutral but never grants access itself."""
import unittest
from datetime import datetime, timezone

from do_again.service.planner_contract import (
    AuthorizedTransportPlanner, planning_prompt, planning_schema,
    validate_planning_answer,
)
from do_again.supervisor.macos_execution import ExecutionBlocked


class PlannerContractTests(unittest.TestCase):
    def test_fail_closed_without_explicit_provider(self):
        calls = []
        planner = AuthorizedTransportPlanner(
            authorize=lambda now: {'not': 'authority'},
            reserve_call=lambda *args: calls.append('reserved'),
            transport=lambda *args, **kwargs: calls.append('transport'),
            request_builder=lambda *args: calls.append('request'),
        )
        with self.assertRaisesRegex(ExecutionBlocked, 'explicit sealed adapter'):
            planner.select(goal={}, evidence={}, state={'requests': [], 'acknowledged': []},
                           now=datetime.now(timezone.utc))
        self.assertEqual(calls, [])

    def test_request_schema_is_bounded_and_provider_agnostic(self):
        schema = planning_schema()
        self.assertEqual(set(schema['required']), {'gap', 'acceptance', 'script'})
        self.assertFalse(schema['additionalProperties'])
        answer = {'gap': 'measured failure', 'acceptance': 'tests pass', 'script': 'print(1)'}
        self.assertEqual(validate_planning_answer(answer), answer)
        for malformed in (dict(answer, extra='x'), dict(answer, gap=''),
                          dict(answer, script='x'*16001), ['not an object']):
            with self.assertRaises(ExecutionBlocked):
                validate_planning_answer(malformed)

    def test_exposure_limit_and_continuation_binding(self):
        with self.assertRaisesRegex(ExecutionBlocked, 'verified continuation'):
            planning_prompt({}, {}, {'acknowledged': 'untrusted'})
        prompt = planning_prompt({'name': 'public'}, {'cases': []},
                                 {'acknowledged': ['receipt-1']})
        self.assertIn('receipt-1', prompt)
        with self.assertRaisesRegex(ExecutionBlocked, 'exposure budget'):
            planning_prompt({'value': 'x'*32768}, {}, {'acknowledged': []})

    def test_explicit_adapter_reserves_before_transport(self):
        events = []
        class TestAdapter(AuthorizedTransportPlanner):
            def _validated_grant(self, now):
                events.append('authorize')
                return {'scope': 'isolated-test'}

        def reserve(grant, cycle, prompt, schema):
            events.append('reserve')
            self.assertEqual(cycle, 1)
            return {'call_id': 'test-1'}

        def transport(prompt, schema, *, call, grant):
            events.append('transport')
            self.assertEqual(call, {'call_id': 'test-1'})
            return {'gap': 'g', 'acceptance': 'a', 'script': 'print(1)'}

        def build(answer, evidence, now):
            events.append('build')
            return answer

        planner = TestAdapter(None, reserve, transport, build)
        result = planner.select(goal={}, evidence={}, state={'requests': [], 'acknowledged': []},
                                now=datetime.now(timezone.utc))
        self.assertEqual(result['script'], 'print(1)')
        self.assertEqual(events, ['authorize', 'reserve', 'transport', 'build'])

    def test_bad_response_does_not_create_request(self):
        events = []
        class TestAdapter(AuthorizedTransportPlanner):
            def _validated_grant(self, now):
                return {'scope': 'isolated-test'}
        planner = TestAdapter(None, lambda *a: {'call_id': 'reserved'},
                              lambda *a, **kw: {'gap': 'g'},
                              lambda *a: events.append('request'))
        with self.assertRaisesRegex(ExecutionBlocked, 'bounded schema'):
            planner.select(goal={}, evidence={}, state={'requests': [], 'acknowledged': []},
                           now=datetime.now(timezone.utc))
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
