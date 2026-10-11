"""Read-only Codex CI receipt and exact GitHub checkpoint tests."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import canonical_json, request_fingerprint
from do_again.model_receipts import ModelReceiptBlocked, verify_model_ci_receipt
from do_again.model_watch import observe_model_stage
from do_again.supervisor.control_history import blob_sha


class ModelCiWatchTests(unittest.TestCase):
    nonce = "a" * 24
    head = "b" * 40
    pull_request = 29

    def setUp(self):
        now = datetime.now(timezone.utc)
        self.rid = "canary-" + self.nonce + "-1-ci"
        self.pub = "canary-" + self.nonce + "-1-publish"
        self.request = {
            "schema_version": 1, "request_id": self.rid,
            "operation": "ci_observe",
            "issued_at_utc": now.isoformat(),
            "expires_at_utc": now.isoformat(),
            "args": {"original_request_id": self.pub},
            "expected": {"repo_head": self.head},
        }
        fp = request_fingerprint(self.request)
        self.result = {
            "state": "terminal", "status": "completed", "conclusion": "success",
            "returncode": 0, "replay": False, "repository": "Tran-Steven/do-again",
            "head_sha": self.head, "pull_request": self.pull_request,
            "run_id": 80033, "run_attempt": 1,
            "url": "https://github.com/Tran-Steven/do-again/actions/runs/80033",
        }
        self.receipt = {
            "schema_version": 1,
            "request_id": self.rid, "request_fingerprint": fp,
            "operation": "ci_observe", "state": "succeeded",
            "result": {
                "operation": "ci_observe", "request_fingerprint": fp,
                "authority_before": {"repo_head": self.head},
                "authority_after": {"repo_head": self.head},
                "result": self.result,
            },
        }
        self.api = SimpleNamespace(
            repository="Tran-Steven/do-again",
            control_branch="do-again/canary-" + self.nonce + "/control",
        )

    def verify(self, *, receipt=None, pull_request=None):
        return verify_model_ci_receipt(
            self.request, receipt if receipt is not None else self.receipt,
            nonce=self.nonce, task=1, expected_head=self.head,
            pull_request=self.pull_request if pull_request is None else pull_request,
        )

    def test_only_exact_terminal_ci_is_checkpoint_evidence_not_model_ack(self):
        result = self.verify()
        self.assertEqual(result["state"], "ci_verified")
        self.assertEqual(result["head_sha"], self.head)
        self.assertEqual(result["pull_request"], self.pull_request)
        self.assertEqual(result["run_id"], 80033)
        self.assertEqual(result["run_attempt"], 1)
        self.assertEqual(result["next_stage"], "checkpoint")
        self.assertFalse(result["model_acknowledged"])
        self.assertFalse(result["browser_acknowledged"])
        self.assertFalse(result["replay"])

    def test_waiting_successful_broker_receipt_never_unlocks_task_two(self):
        waiting = copy.deepcopy(self.receipt)
        waiting["result"]["result"].update(
            {"state": "waiting", "status": "in_progress",
             "conclusion": None, "returncode": 0})
        with self.assertRaises(ModelReceiptBlocked):
            self.verify(receipt=waiting)
        no_run = copy.deepcopy(self.receipt)
        no_run["result"]["result"].update(
            {"state": "waiting", "run_id": None, "status": "pending",
             "conclusion": None, "returncode": 0})
        with self.assertRaises(ModelReceiptBlocked):
            self.verify(receipt=no_run)

    def test_wrong_run_head_pr_attempt_status_replay_and_result_never_unlock(self):
        for change in (
            {"head_sha": "f"*40},
            {"pull_request": 999},
            {"run_id": 0},
            {"run_attempt": 0},
            {"run_attempt": True},
            {"status": "in_progress"},
            {"conclusion": "failure"},
            {"returncode": 1},
            {"replay": True},
            {"repository": "Tran-Steven/jobpipe"},
            {"url": "https://attacker.example/"},
            {"state": "post_dispatch_uncertain"},
        ):
            with self.subTest(change=change):
                row = copy.deepcopy(self.receipt)
                row["result"]["result"].update(change)
                with self.assertRaises(ModelReceiptBlocked):
                    self.verify(receipt=row)

    def test_mutated_outer_receipt_and_original_pub_identity_are_denied(self):
        for change in (
            {"state": "failed"},
            {"request_id": "canary-other-ci"},
            {"request_fingerprint": "0"*64},
        ):
            with self.subTest(change=change):
                row = copy.deepcopy(self.receipt)
                row.update(change)
                with self.assertRaises(ModelReceiptBlocked):
                    self.verify(receipt=row)
        self.request["args"]["original_request_id"] = "different-publication"
        with self.assertRaises(ModelReceiptBlocked):
            self.verify()

    def test_ci_read_only_remote_watcher_checks_original_request_and_receipt(self):
        req_path = "automation/do_again/requests/" + self.rid + ".json"
        rec_path = "automation/do_again/receipts/" + self.rid + ".json"
        entries = {
            req_path: blob_sha(canonical_json(self.request) + b"\n"),
            rec_path: "c"*40,
        }
        with patch("do_again.model_watch.snapshot", return_value=(
                "d"*40, {}, entries)) as snap, patch(
                "do_again.model_watch.read_json_blob",
                return_value=self.receipt) as read:
            outcome = observe_model_stage(
                api=self.api, nonce=self.nonce, task=1, stage="ci",
                request=self.request, pull_request=self.pull_request)
        snap.assert_called_once_with(self.api)
        read.assert_called_once_with(self.api, "c"*40)
        self.assertEqual(outcome["state"], "ci_verified")
        self.assertEqual(outcome["control_head"], "d"*40)
        self.assertEqual(outcome["receipt_blob_sha"], "c"*40)

    def test_ci_watcher_refuses_missing_publication_or_wrong_pull_request(self):
        with patch("do_again.model_watch.snapshot", return_value=(
                "d"*40, {}, {})), patch("do_again.model_watch.read_json_blob") as read:
            result = observe_model_stage(
                api=self.api, nonce=self.nonce, task=1, stage="ci",
                request=self.request, pull_request=self.pull_request)
            self.assertEqual(result["state"], "not_published")
            read.assert_not_called()
        req_path = "automation/do_again/requests/" + self.rid + ".json"
        rec_path = "automation/do_again/receipts/" + self.rid + ".json"
        entries = {
            req_path: blob_sha(canonical_json(self.request) + b"\n"),
            rec_path: "c"*40,
        }
        with patch("do_again.model_watch.snapshot", return_value=(
                "d"*40, {}, entries)), patch(
                "do_again.model_watch.read_json_blob", return_value=self.receipt):
            with self.assertRaises(ModelReceiptBlocked):
                observe_model_stage(
                    api=self.api, nonce=self.nonce, task=1, stage="ci",
                    request=self.request, pull_request=999)


if __name__ == "__main__":
    unittest.main()
