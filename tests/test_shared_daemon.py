import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from do_again.service.daemon import _run_admitted_worker


class SharedSealedDaemonTests(unittest.TestCase):
    def test_qualification_uses_shared_engine_and_stops_at_terminal_receipt(self):
        observed = {}
        executor = object()
        transport = object()

        class AgentStub:
            def __init__(self, **kwargs):
                observed.update(kwargs)
                self.stop_requested = False

            def run(self, *, once):
                self.once = once
                observed['receipt_callback']({'request_id': 'synthetic', 'state': 'succeeded'})
                observed['stopped'] = self.stop_requested
                return 0

        args = Namespace(control_worktree='/tmp/control', branch='operator-control', remote='origin',
                         policy='/tmp/policy.json', once=False)
        with patch('do_again.service.daemon.Agent', AgentStub), patch('do_again.service.daemon.signal.signal'):
            result = _run_admitted_worker(args, Path('/tmp/repo'), Path('/tmp/state'), False,
                                          admission_check=lambda: None, executor=executor,
                                          control_transport=transport,
                                          receipt_observer=lambda row: row['state'] == 'succeeded')
        self.assertEqual(result, 0)
        self.assertIs(observed['executor'], executor)
        self.assertIs(observed['control_transport'], transport)
        self.assertTrue(observed['stopped'])


if __name__ == '__main__':
    unittest.main()
