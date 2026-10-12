import base64
import hashlib
import json
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import test_offline_continuation as fixtures
from test_control_history import Remote
from do_again.core.schema import canonical_json
from do_again.service.controller_loop import ControllerLoop, DeterministicPlanner
from do_again.service.controller_planner import CodexPlanner, PlanningCallJournal
from do_again.supervisor.controller_transport import ControllerHistory
from do_again.supervisor.controller_service import controller_service
from do_again.supervisor.macos_execution import ExecutionBlocked


class LoopTests(unittest.TestCase):
    def setUp(self):
        fixtures.ContinuationTests.setUp(self)
        self.remote = Remote(); self.remote.repository = 'Tran-Steven/jobpipe'
        self.history = ControllerHistory(self.c, self.remote, nullcontext)
        self.product = Path(self.temp.name)/'fixture_product.py'
        self.product.write_text('')
        self.executions = []; self.native = {}

    def proposal(self): return fixtures.ContinuationTests.proposal(self)

    def finish(self, record): return fixtures.ContinuationTests.finish(self, record)

    def evidence(self):
        ns = {}; exec(self.product.read_text(), ns)
        failures = []
        for name in ('recover_checkpoint', 'deduplicate', 'resume_handoff'):
            if name not in ns: failures.append(name)
        return {'failures': failures, 'source_sha256': hashlib.sha256(self.product.read_bytes()).hexdigest()}

    def select(self, goal, evidence, state, now):
        # Choose from currently failing fixture acceptance probes, not cycle index.
        name = evidence['failures'][0]
        changes = {
            'recover_checkpoint': 'def recover_checkpoint(saved):\n    return dict(saved)\nassert recover_checkpoint({"step": 2}) == {"step": 2}\n',
            'deduplicate': 'def deduplicate(ids):\n    return list(dict.fromkeys(ids))\nassert deduplicate(["a", "b", "a"]) == ["a", "b"]\n',
            'resume_handoff': 'def resume_handoff(completed, session_matches):\n    return completed is True and session_matches is True\nassert not resume_handoff(True, False)\nassert resume_handoff(True, True)\n'}
        proposal = self.proposal()
        proposal['request']['args']['script'] = changes[name]
        proposal.update(gap=name, acceptance='fixture acceptance probes pass for '+name,
                        evidence_sha256=hashlib.sha256(canonical_json(evidence)).hexdigest())
        return proposal

    def loop(self):
        return ControllerLoop(self.c, self.history, DeterministicPlanner(self.select),
                              {'priorities': ['recovery', 'duplicates', 'handoff']},
                              self.evidence, lambda record, receipt: self.native[receipt['request_id']])

    def publish_fixture_receipt(self, receipt):
        api = self.remote
        data = canonical_json(receipt)+b'\n'
        blob = api.request('POST', 'git/blobs', {'encoding': 'base64', 'content': base64.b64encode(data).decode()})
        tree = api.request('POST', 'git/trees', {'base_tree': api.commits[api.head]['tree']['sha'],
            'tree': [{'path': 'automation/do_again/receipts/'+receipt['request_id']+'.json',
                      'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
        commit = api.request('POST', 'git/commits', {'tree': tree['sha'], 'parents': [api.head], 'message': 'fixture receipt'})
        api.request('PATCH', 'git/refs/heads/operator-control', {'sha': commit['sha'], 'force': False})

    def worker(self):
        state = self.c.status(self.now)
        record = state['requests'][-1]; rid = record['request']['request_id']
        if rid in self.executions: return
        before = self.product.read_text()
        after = before+record['request']['args']['script']
        exec(compile(after, str(self.product), 'exec'), {})  # Pure offline fixture regression tests.
        self.product.write_text(after)
        self.executions.append(rid)
        receipt, evidence = self.finish(record)
        evidence['implementation_changed'] = before != after
        self.native[rid] = evidence
        self.publish_fixture_receipt(receipt)

    def test_three_cycles_real_fixture_edits_and_restart(self):
        self.c.start(self.now)
        loop = self.loop()
        for cycle in range(3):
            loop.tick(self.now)
            self.worker()
            # New runtime objects after durable receipt, before acknowledgment.
            self.c = type(self.c)(self.path, self.scope, self.binding)
            self.history = ControllerHistory(self.c, self.remote, nullcontext)
            loop = self.loop(); loop.tick(self.now)
            self.assertEqual(len(self.c.status(self.now)['acknowledged']), cycle+1)
        self.assertEqual(self.c.status(self.now)['state'], 'completed')
        self.assertEqual(self.evidence()['failures'], [])
        self.assertEqual(len(set(self.executions)), 3)
        loop.tick(self.now)
        self.assertEqual(len(self.executions), 3)

    def test_lost_ref_response_recovers_without_another_remote_write(self):
        self.c.start(self.now); self.remote.lose_patch = True
        with self.assertRaises(ExecutionBlocked): self.loop().tick(self.now)
        writes = len(self.remote.writes); self.remote.lose_patch = False
        self.loop().tick(self.now)
        self.assertEqual(len(self.remote.writes), writes)
        self.worker(); self.loop().tick(self.now)
        self.assertEqual(len(self.executions), 1)

    def test_uncertain_absent_publication_never_replayed(self):
        self.c.start(self.now)
        record = self.c.reserve(self.proposal(), pending_ids=[], now=self.now)
        state = self.c.status(self.now); state['requests'][-1]['publication_started'] = True
        self.c._save(state)
        with self.assertRaises(ExecutionBlocked): self.history.publish_reserved(self.now)
        self.assertEqual(self.remote.writes, [])

    def test_wrong_repository_cannot_publish(self):
        self.c.start(self.now); self.remote.repository = 'Tran-Steven/do-again'
        with self.assertRaises(ExecutionBlocked): self.loop().tick(self.now)
        self.assertEqual(self.remote.writes, [])

    def test_hosted_absent_grant_never_calls_transport(self):
        calls = []
        planner = CodexPlanner(lambda now: None, lambda *a: calls.append('reserve'),
                               lambda *a, **k: calls.append('network'), lambda *a: None)
        with self.assertRaises(ExecutionBlocked):
            planner.select(goal={}, evidence={}, state={'acknowledged': [], 'requests': []}, now=self.now)
        self.assertEqual(calls, [])

    def test_broker_rejects_caller_supplied_scope_before_effects(self):
        with self.assertRaises(ExecutionBlocked):
            controller_service(None, {'operation': 'controller_start', 'scope': self.scope})

    def test_hosted_call_reservation_survives_restart_without_replay(self):
        path = Path(self.temp.name)/'calls.json'
        grant = {'max_total_calls': 3}
        journal = PlanningCallJournal(path)
        journal.reserve(grant, 1, 'redacted prompt', {})
        with self.assertRaises(ExecutionBlocked):
            PlanningCallJournal(path).reserve(grant, 1, 'redacted prompt', {})
        journal.reserve(grant, 2, 'redacted prompt', {})
        journal.reserve(grant, 3, 'redacted prompt', {})
        with self.assertRaises(ExecutionBlocked): journal.reserve(grant, 4, 'prompt', {})
