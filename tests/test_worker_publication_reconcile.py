from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.agent import Agent
from do_again.core.broker_executor import BrokerExecutor
from do_again.core.schema import OperatorError, atomic_json


class WorkerPublicationReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "project"
        self.repo.mkdir()
        self.policy = self.repo / "policy.json"
        atomic_json(self.policy, {"allowed_operations": ["git_publication_reconcile", "status"]})
        self.executor = BrokerExecutor(repo=self.repo, policy_path=self.policy, state_dir=self.repo / "state")
        self.agent = Agent(
            repo=self.repo,
            control_worktree=self.repo,
            branch="operator-control",
            policy_path=self.policy,
            state_dir=self.repo / "state",
        )
        self.status = {
            "operator_intent": "active",
            "production_ready": True,
            "enforcement_verified": True,
            "epoch": 4,
            "authority": {"repo_head": "a" * 40, "repo_branch": "do-again/task-123"},
        }
        self.request = {
            "request_id": "reconcile-20261008-01",
            "operation": "git_publication_reconcile",
            "args": {"original_request_id": "publication-20261008-01"},
            "expected": {"repo_head": "a" * 40},
        }

    def test_packet_selects_only_original_identity(self):
        packet = self.executor._packet(self.request, self.status)
        self.assertEqual(
            packet,
            {"operation": "git_publication_reconcile", "request_id": "publication-20261008-01"},
        )
        for extra in (
            {"repository": "untrusted/other"},
            {"ref": "main"},
            {"credential": "not-a-real-secret"},
            {"force": True},
            {"env": {"TEST": "1"}},
        ):
            with self.subTest(extra=extra), self.assertRaises(OperatorError):
                self.executor._packet(self.request | {"args": self.request["args"] | extra}, self.status)

    def test_invalid_original_identity_is_rejected(self):
        for original in ("", "../main", "short", None, 123, "x" * 161):
            with self.subTest(original=original), self.assertRaises(OperatorError):
                self.executor._packet(
                    self.request | {"args": {"original_request_id": original}}, self.status
                )

    def test_maintenance_and_incomplete_proofs_deny_before_reconciliation(self):
        for change in (
            {"operator_intent": "maintenance"},
            {"operator_intent": "paused"},
            {"operator_intent": "stopped"},
            {"production_ready": False},
            {"enforcement_verified": False},
        ):
            with self.subTest(change=change), patch(
                "do_again.core.broker_executor.sys.platform", "darwin"
            ), patch(
                "do_again.core.broker_executor.broker_request",
                return_value=self.status | change,
            ) as broker:
                with self.assertRaises(OperatorError):
                    self.executor.execute(self.request)
                broker.assert_called_once_with(self.repo.resolve(), {"operation": "status"})

    def test_uncertain_reconciliation_creates_failed_not_success_receipt(self):
        uncertain = {
            "state": "post_dispatch_uncertain",
            "replay": False,
            "request_id": "publication-20261008-01",
        }
        with patch(
            "do_again.core.broker_executor.sys.platform", "darwin"
        ), patch(
            "do_again.core.broker_executor.broker_request",
            side_effect=[self.status, uncertain, self.status],
        ) as broker:
            receipt = self.executor.execute(self.request)
        self.assertFalse(self.agent.result_succeeded(receipt))
        self.assertEqual(broker.call_args_list[1].args[1]["request_id"], "publication-20261008-01")
        self.assertEqual(broker.call_count, 3)

    def test_original_receipt_must_have_positive_reconciliation_evidence(self):
        results = [
            ({"state": "terminal", "replay": False}, False),
            ({"state": "post_dispatch_uncertain", "replay": False}, False),
            ({"state": "succeeded", "returncode": 0}, False),
            ({"state": "succeeded", "returncode": 1, "reconciled_read_only": True}, False),
            ({"state": "succeeded", "returncode": 0, "reconciled_read_only": False}, False),
            ({"state": "succeeded", "returncode": 0, "reconciled_read_only": True}, True),
        ]
        for value, expected in results:
            with self.subTest(value=value):
                self.assertEqual(
                    self.agent.result_succeeded({"operation": "git_publication_reconcile", "result": value}),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
