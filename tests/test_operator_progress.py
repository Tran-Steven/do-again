from __future__ import annotations

from do_again.core.executor import LocalExecutor

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.agent import Agent
from do_again.core.schema import OperatorError, request_fingerprint, validate_request


class OperatorProgressSchemaTests(unittest.TestCase):
    def base_request(self) -> dict:
        return {
            "schema_version": 1,
            "request_id": "next-step",
            "operation": "scratch_script",
            "issued_at_utc": "2099-01-01T00:00:00+00:00",
            "expires_at_utc": "2099-01-01T00:10:00+00:00",
            "args": {},
            "expected": {},
            "limits": {},
        }

    @patch("do_again.core.schema.utc_now")
    def test_continuation_is_normalized_and_fingerprinted(self, now_mock) -> None:
        from datetime import datetime, timezone

        now_mock.return_value = datetime(2099, 1, 1, tzinfo=timezone.utc)
        request = self.base_request()
        request["continuation"] = {
            "acknowledged_receipts": ["prior-001", "prior-002"],
            "goal_state": "in_progress",
            "goal_id": "goal-alpha",
            "summary": " verified first slice ",
        }
        normalized = validate_request(
            request,
            max_ttl_seconds=3600,
            max_future_skew_seconds=300,
        )
        self.assertEqual(
            normalized["continuation"],
            {
                "acknowledged_receipts": ["prior-001", "prior-002"],
                "goal_state": "in_progress",
                "goal_id": "goal-alpha",
                "summary": "verified first slice",
            },
        )
        without = dict(normalized)
        without.pop("continuation")
        self.assertNotEqual(request_fingerprint(normalized), request_fingerprint(without))

    @patch("do_again.core.schema.utc_now")
    def test_duplicate_acknowledgements_are_rejected(self, now_mock) -> None:
        from datetime import datetime, timezone

        now_mock.return_value = datetime(2099, 1, 1, tzinfo=timezone.utc)
        request = self.base_request()
        request["continuation"] = {
            "acknowledged_receipts": ["prior-001", "prior-001"],
        }
        with self.assertRaises(OperatorError):
            validate_request(request, max_ttl_seconds=3600)


class OperatorProgressAgentTests(unittest.TestCase):
    def make_agent(self, root: Path) -> Agent:
        repo = root / "repo"
        control = root / "control"
        state = root / "state"
        repo.mkdir()
        control.mkdir()
        policy = root / "policy.json"
        policy.write_text(json.dumps({"poll_seconds": 3}))
        return Agent(
            repo=repo,
            control_worktree=control,
            branch="operator-control",
            policy_path=policy,
            state_dir=state,
            executor=LocalExecutor(repo=repo, policy_path=policy, state_dir=state),
        )

    def test_progress_requires_durable_acknowledged_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(Path(tmp))
            request = {
                "continuation": {
                    "acknowledged_receipts": ["missing-receipt"],
                    "goal_state": "in_progress",
                }
            }
            with self.assertRaises(OperatorError):
                agent.record_operator_progress(
                    request,
                    request_id="next-step",
                    fingerprint="abc",
                )

    def test_progress_is_durable_and_receipt_echoes_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = self.make_agent(Path(tmp))
            prior = agent.control_worktree / agent.receipt_relative("prior-001")
            prior.parent.mkdir(parents=True, exist_ok=True)
            prior.write_text(json.dumps({"request_id": "prior-001", "state": "succeeded"}))
            request = {
                "schema_version": 1,
                "request_id": "next-step",
                "operation": "scratch_script",
                "issued_at_utc": "2099-01-01T00:00:00+00:00",
                "expires_at_utc": "2099-01-01T00:10:00+00:00",
                "args": {},
                "expected": {},
                "limits": {},
                "continuation": {
                    "acknowledged_receipts": ["prior-001"],
                    "goal_state": "completed",
                    "goal_id": "goal-alpha",
                    "summary": "done",
                },
            }
            fingerprint = request_fingerprint(request)
            agent.record_operator_progress(
                request,
                request_id="next-step",
                fingerprint=fingerprint,
            )
            progress = json.loads((agent.state_dir / "operator_progress.json").read_text())
            self.assertEqual(progress["acknowledged_receipts"], ["prior-001"])
            self.assertEqual(progress["goal_state"], "completed")
            self.assertEqual(progress["goal_id"], "goal-alpha")
            receipt = agent.make_receipt(
                request=request,
                state="succeeded",
                started_at="2099-01-01T00:00:01+00:00",
                result={"ok": True},
            )
            self.assertEqual(receipt["operator_progress"], request["continuation"])


if __name__ == "__main__":
    unittest.main()
