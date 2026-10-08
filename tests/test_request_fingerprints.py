from __future__ import annotations

import itertools
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.core.agent import Agent
from do_again.core.schema import OperatorError, atomic_json, request_fingerprint, utc_now, validate_request


class RequestFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.control = self.root / "control"
        self.state = self.root / "state"
        self.policy = self.root / "policy.json"
        atomic_json(self.policy, {"schema_version": 1, "max_request_ttl_seconds": 3600})
        self.executions = []
        self.agents = []

    def agent(self):
        agent = Agent(repo=self.repo, control_worktree=self.control, branch="operator-control", policy_path=self.policy, state_dir=self.state)
        agent.publish_json = lambda relative, value, message: atomic_json(self.control / relative, value)
        # This fixture models the control store with local JSON rather than Git.
        # Explicitly mock synchronization when exercising conflict publishing.
        agent.sync = lambda: None
        agent.acquire_remote_claim = lambda request: atomic_json(self.control / agent.claim_relative(request["request_id"]), {"request_fingerprint": request_fingerprint(request)}) or True

        def execute(request):
            self.executions.append(request)
            ledger = agent.local_ledger(request["request_id"])
            self.assertEqual(ledger["request_fingerprint"], request_fingerprint(request))
            return {"request_fingerprint": request_fingerprint(request), "result": {"returncode": 0}}

        agent.executor.execute = Mock(side_effect=execute)
        self.agents.append(agent)
        return agent

    def request(self, request_id="fingerprint-request-0001"):
        now = utc_now()
        return {"schema_version": 1, "request_id": request_id, "operation": "status", "issued_at_utc": now.isoformat(), "expires_at_utc": (now + timedelta(minutes=10)).isoformat()}

    def write_request(self, request):
        path = self.control / "automation/do_again/requests" / (request["request_id"] + ".json")
        atomic_json(path, request)
        return path

    def test_all_optional_field_combinations_execute_once_across_restart(self):
        for index, included in enumerate(itertools.product((False, True), repeat=3)):
            with self.subTest(included=included):
                request = self.request("fingerprint-fields-" + str(index))
                for field, present in zip(("args", "expected", "limits"), included):
                    if present:
                        request[field] = {}
                path = self.write_request(request)
                agent = self.agent()
                self.assertTrue(agent.process_path(path))
                receipt = agent.receipt_payload(request["request_id"])
                fingerprint = request_fingerprint(request)
                self.assertEqual(receipt["state"], "succeeded")
                self.assertEqual(receipt["request_fingerprint"], fingerprint)
                self.assertEqual(receipt["result"]["request_fingerprint"], fingerprint)
                self.assertEqual(agent.claim_payload(request["request_id"])["request_fingerprint"], fingerprint)
                self.assertEqual(agent.local_ledger(request["request_id"])["request_fingerprint"], fingerprint)
                self.assertFalse(agent.process_path(path))
                restarted = self.agent()
                self.assertFalse(restarted.process_path(path))
                restarted.executor.execute.assert_not_called()
        self.assertEqual(len(self.executions), 8)
        self.assertFalse(self.control.joinpath("automation/do_again/conflicts").exists())


    def test_preclaim_cancellation_publishes_cancelled_receipt_without_execution(self):
        request_id = "cancel-before-execution"
        request = self.request(request_id)
        agent = self.agent()
        request_path = agent.requests_dir / f"{request_id}.json"
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(request), encoding="utf-8")
        cancellation = {
            "schema_version": 1,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint(request),
            "state": "cancelled_before_execution",
            "reason": "test cancellation",
            "cancelled_at_utc": request["issued_at_utc"],
        }
        cancel_path = agent.cancellations_dir / f"{request_id}.json"
        cancel_path.parent.mkdir(parents=True, exist_ok=True)
        cancel_path.write_text(json.dumps(cancellation), encoding="utf-8")
        with patch.object(agent, "acquire_remote_claim") as claim, patch.object(
            agent.executor, "execute"
        ) as execute, patch.object(agent, "publish_receipt"), patch.object(
            agent, "notify_receipt"
        ):
            changed = agent.process_path(request_path)
        self.assertTrue(changed)
        claim.assert_not_called()
        execute.assert_not_called()
        ledger = agent.local_ledger(request_id)
        self.assertEqual(ledger["receipt"]["state"], "cancelled")

    def test_legacy_normalized_receipt_is_not_reexecuted_after_expiry(self):
        request = self.request()
        path = self.write_request(request)
        agent = self.agent()
        normalized = validate_request(request, max_ttl_seconds=3600)
        receipt = agent.make_receipt(request=normalized, state="succeeded", started_at=utc_now().isoformat())
        agent.publish_receipt(receipt)
        with patch("do_again.core.schema.utc_now", return_value=utc_now() + timedelta(hours=2)):
            self.assertFalse(agent.process_path(path))
        agent.executor.execute.assert_not_called()
        self.assertFalse(self.control.joinpath("automation/do_again/conflicts").exists())

    def test_changed_request_with_legacy_receipt_is_rejected(self):
        request = self.request()
        path = self.write_request(request)
        agent = self.agent()
        normalized = validate_request(request, max_ttl_seconds=3600)
        receipt = agent.make_receipt(request=normalized, state="succeeded", started_at=utc_now().isoformat())
        agent.publish_receipt(receipt)
        request["args"] = {"changed": True}
        atomic_json(path, request)
        self.assertTrue(agent.process_path(path))
        agent.executor.execute.assert_not_called()
        self.assertEqual(agent.receipt_payload(request["request_id"]), receipt)
        self.assertEqual(len(list(agent.conflicts_dir.rglob("*.json"))), 1)

    def test_legacy_started_ledger_is_blocked_without_execution(self):
        request = self.request()
        path = self.write_request(request)
        agent = self.agent()
        normalized = validate_request(request, max_ttl_seconds=3600)
        agent.write_ledger(request["request_id"], {"state": "started", "request_fingerprint": request_fingerprint(normalized), "started_at_utc": utc_now().isoformat()})
        self.assertTrue(agent.process_path(path))
        agent.executor.execute.assert_not_called()
        receipt = agent.receipt_payload(request["request_id"])
        self.assertEqual(receipt["state"], "blocked_ambiguous_replay")
        self.assertEqual(receipt["request_fingerprint"], request_fingerprint(request))
        self.assertFalse(agent.process_path(path))

    def test_legacy_terminal_ledger_republishes_receipt_without_execution(self):
        request = self.request()
        path = self.write_request(request)
        agent = self.agent()
        normalized = validate_request(request, max_ttl_seconds=3600)
        receipt = agent.make_receipt(request=normalized, state="succeeded", started_at=utc_now().isoformat())
        agent.write_ledger(request["request_id"], {"state": "terminal", "request_fingerprint": request_fingerprint(request), "receipt": receipt})
        self.assertTrue(agent.process_path(path))
        agent.executor.execute.assert_not_called()
        self.assertEqual(agent.receipt_payload(request["request_id"]), receipt)
        self.assertFalse(agent.process_path(path))

    def test_executor_uses_omitted_optional_defaults(self):
        from do_again.core.executor import LocalExecutor
        atomic_json(self.policy, {"schema_version": 1, "allowed_operations": ["status"]})
        executor = LocalExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.state)
        request = self.request()
        with patch.object(executor, "authority_snapshot", return_value={}), patch.object(executor, "_status", return_value={"ready": True}):
            result = executor.execute(request)
        self.assertEqual(result["request_fingerprint"], request_fingerprint(request))
        self.assertEqual(result["result"], {"ready": True})

    def test_continuation_waiting_for_ci_is_normalized_and_fingerprinted(self):
        request = self.request()
        request["continuation"] = {
            "acknowledged_receipts": [],
            "goal_state": "waiting_for_ci",
            "goal_id": "rollout-pr11",
            "ci": {
                "repository": "Tran-Steven/do-again",
                "run_id": 37716899421,
                "head_sha": "1D92D174D49E058E0DFE46D55B5FE7BCEBA70FE5",
            },
        }
        normalized = validate_request(request, max_ttl_seconds=3600)
        self.assertEqual(normalized["continuation"]["goal_state"], "waiting_for_ci")
        self.assertEqual(
            normalized["continuation"]["ci"]["head_sha"],
            "1d92d174d49e058e0dfe46d55b5fe7bceba70fe5",
        )
        base = request_fingerprint(request)
        request["continuation"]["ci"]["run_id"] += 1
        self.assertNotEqual(base, request_fingerprint(request))

    def test_waiting_for_ci_requires_explicit_ci_metadata(self):
        request = self.request()
        request["continuation"] = {
            "acknowledged_receipts": [],
            "goal_state": "waiting_for_ci",
        }
        with self.assertRaisesRegex(OperatorError, "requires continuation.ci"):
            validate_request(request, max_ttl_seconds=3600)

