import unittest
from do_again.service.planner_selection import select_controller_planner
from do_again.supervisor.macos_execution import ExecutionBlocked


class PlannerSelectionTests(unittest.TestCase):
    def test_sealed_provider_calls_only_its_factory(self):
        calls = []
        def factory(name):
            return lambda: calls.append(name) or name
        factories = dict(offline_factory=factory('offline'),
                         codex_factory=factory('codex'),
                         browser_factory=factory('browser'))
        for provider, expected in (
            ('sealed-offline','offline'), ('codex-cli','codex'),
            ('chatgpt-browser-cdp','browser')):
            calls.clear()
            self.assertEqual(select_controller_planner(provider, **factories), expected)
            self.assertEqual(calls, [expected])

    def test_browser_cannot_inherit_codex_implementation(self):
        called = []
        with self.assertRaisesRegex(ExecutionBlocked, 'qualified operator-side'):
            select_controller_planner('chatgpt-browser-cdp',
                offline_factory=lambda: called.append('offline'),
                codex_factory=lambda: called.append('codex'))
        self.assertEqual(called, [])

    def test_unknown_kind_never_falls_back(self):
        for provider in ('', 'unknown', None, 'codex', 'chatgpt'):
            with self.subTest(provider=provider):
                with self.assertRaises(ExecutionBlocked):
                    select_controller_planner(provider, offline_factory=lambda:None,
                                              codex_factory=lambda:None,
                                              browser_factory=lambda:None)
