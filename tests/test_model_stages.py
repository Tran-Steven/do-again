"""Offline model canary edit-to-test handoff regressions."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from do_again.model_canary import prepare_canary_edit
from do_again.model_stages import (prepare_model_canary_test, prepare_model_canary_commit,
                                   prepare_model_canary_publish, prepare_model_canary_ci)
from do_again.model_receipts import ModelReceiptBlocked
from do_again.core.schema import request_fingerprint, validate_request


class ModelStageTests(unittest.TestCase):
    nonce = "a" * 24
    head = "b" * 40

    def setUp(self):
        self.edit = prepare_canary_edit(
            {"implementation": "def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
             "tests": "import unittest\nfrom canary_live_" + self.nonce +
                      " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                      "    def test_label(self):\n"
                      "        self.assertEqual(canonical_label('A B'), 'a-b')\n"},
            nonce=self.nonce, task=1, expected_head=self.head,
            issued_at=datetime.now(timezone.utc),
        )
        fingerprint = request_fingerprint(self.edit)
        self.receipt = {
            "schema_version": 1,
            "request_id": self.edit["request_id"],
            "request_fingerprint": fingerprint,
            "operation": "scratch_script",
            "state": "succeeded",
            "result": {
                "operation": "scratch_script",
                "request_fingerprint": fingerprint,
                "authority_before": {"repo_head": self.head},
                "authority_after": {"repo_head": self.head},
                "result": {"returncode": 0, "timed_out": False},
            },
        }

    def run_builder(self, **changes):
        return prepare_model_canary_test(
            edit_request=changes.pop("edit_request", self.edit),
            edit_receipt=changes.pop("edit_receipt", self.receipt),
            nonce=changes.pop("nonce", self.nonce),
            task=changes.pop("task", 1),
            expected_head=changes.pop("expected_head", self.head),
            **changes,
        )

    def test_exact_receipt_builds_bounded_test_and_durable_acknowledgment(self):
        value = self.run_builder()
        self.assertEqual(value["operation"], "run_tests")
        self.assertEqual(value["request_id"], "canary-" + self.nonce + "-1-test")
        self.assertEqual(value["args"]["pattern"], "test_live_canary_" + self.nonce + ".py")
        self.assertEqual(value["expected"], {"repo_head": self.head})
        self.assertEqual(value["limits"], {"timeout_seconds": 120})
        self.assertEqual(value["continuation"]["acknowledged_receipts"], [self.edit["request_id"]])
        self.assertEqual(value["continuation"]["goal_state"], "in_progress")
        self.assertEqual(validate_request(value, max_ttl_seconds=3600), value)

    def test_failed_or_ambiguous_receipt_never_authorizes_a_test(self):
        for value in ("failed", "blocked_ambiguous_replay", "error", "cancelled"):
            changed = copy.deepcopy(self.receipt)
            changed["state"] = value
            with self.subTest(state=value), self.assertRaises(ModelReceiptBlocked):
                self.run_builder(edit_receipt=changed)

    def test_wrong_code_hash_and_authority_never_authorize_a_test(self):
        for field, value in [
            ("request_fingerprint", "0" * 64),
            ("request_id", "wrong-id"),
        ]:
            changed = copy.deepcopy(self.receipt)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ModelReceiptBlocked):
                self.run_builder(edit_receipt=changed)
        with self.assertRaises(ModelReceiptBlocked):
            self.run_builder(expected_head="f" * 40)

    def test_no_extra_model_call_or_execution(self):
        with self.assertRaises(ModelReceiptBlocked):
            self.run_builder(task=2)


class ModelStageSequenceTests(ModelStageTests):
    def setUp(self):
        super().setUp()
        self.test = self.run_builder()
        self.test_receipt = self._receipt(
            self.test, self.head, self.head,
            {"returncode": 0, "timed_out": False})
        self.commit = prepare_model_canary_commit(
            test_request=self.test, test_receipt=self.test_receipt,
            nonce=self.nonce, task=1, expected_head=self.head)
        self.after_commit = "c" * 40
        self.commit_receipt = self._receipt(
            self.commit, self.head, self.after_commit,
            {"returncode": 0, "state": "succeeded",
             "authority": {"repo_head": self.after_commit},
             "paths": ["canary_live_" + self.nonce + ".py",
                       "tests/test_live_canary_" + self.nonce + ".py"]})
        self.publish = prepare_model_canary_publish(
            commit_request=self.commit, commit_receipt=self.commit_receipt,
            nonce=self.nonce, task=1, expected_head=self.head)
        self.publish_receipt = self._receipt(
            self.publish, self.after_commit, self.after_commit,
            {"returncode": 0, "state": "succeeded",
             "repository": "Tran-Steven/do-again",
             "head": self.after_commit, "pull_request": 42})

    @staticmethod
    def _receipt(request, before, after, result):
        sha = request_fingerprint(request)
        return {"schema_version": 1, "request_id": request["request_id"],
                "request_fingerprint": sha, "operation": request["operation"],
                "state": "succeeded",
                "result": {"operation": request["operation"],
                           "request_fingerprint": sha,
                           "authority_before": {"repo_head": before},
                           "authority_after": {"repo_head": after},
                           "result": result}}

    def test_full_fixed_stage_chain_requires_no_further_model_calls(self):
        self.assertEqual(self.commit["operation"], "git_commit")
        self.assertEqual(self.commit["continuation"]["acknowledged_receipts"],
                         [self.test["request_id"]])
        self.assertEqual(self.commit["args"]["paths"],
                         ["canary_live_" + self.nonce + ".py",
                          "tests/test_live_canary_" + self.nonce + ".py"])
        self.assertEqual(self.publish["operation"], "git_publish")
        self.assertEqual(self.publish["expected"]["repo_head"], self.after_commit)
        self.assertEqual(self.publish["continuation"]["acknowledged_receipts"],
                         [self.commit["request_id"]])
        self.assertIn("synthetic", self.publish["args"]["title"])
        self.assertIn("disabled", self.publish["args"]["body"])
        ci = prepare_model_canary_ci(
            publish_request=self.publish, publish_receipt=self.publish_receipt,
            nonce=self.nonce, task=1, expected_head=self.after_commit)
        self.assertEqual(ci["operation"], "ci_observe")
        self.assertEqual(ci["request_id"], "canary-" + self.nonce + "-1-ci")
        self.assertEqual(ci["args"], {"original_request_id": self.publish["request_id"]})
        self.assertEqual(ci["continuation"]["acknowledged_receipts"],
                         [self.publish["request_id"]])
        self.assertEqual(validate_request(ci, max_ttl_seconds=3600), ci)

    def test_failed_or_ambiguous_test_never_authorizes_commit(self):
        for update in ({"state": "error"}, {"state": "blocked_ambiguous_replay"}):
            row = copy.deepcopy(self.test_receipt)
            row.update(update)
            with self.assertRaises(ModelReceiptBlocked):
                prepare_model_canary_commit(
                    test_request=self.test, test_receipt=row,
                    nonce=self.nonce, task=1, expected_head=self.head)

    def test_commit_requires_exact_files_and_head_change(self):
        for wrong in (
            {"result": {"returncode": 0, "state": "succeeded",
                        "authority": {"repo_head": self.after_commit},
                        "paths": [".github/workflows/change.yml"]}},
            {"result": {"returncode": 0, "state": "succeeded",
                        "authority": {"repo_head": self.head},
                        "paths": self.commit["args"]["paths"]}},
        ):
            receipt = copy.deepcopy(self.commit_receipt)
            receipt["result"].update(wrong)
            with self.assertRaises(ModelReceiptBlocked):
                prepare_model_canary_publish(
                    commit_request=self.commit, commit_receipt=receipt,
                    nonce=self.nonce, task=1, expected_head=self.head)

    def test_publication_requires_actual_draft_pull_request_evidence(self):
        for changed in (
            {"pull_request": None}, {"repository": "Tran-Steven/jobpipe"},
            {"head": self.head}, {"state": "post_dispatch_uncertain"},
        ):
            row = copy.deepcopy(self.publish_receipt)
            row["result"]["result"].update(changed)
            with self.assertRaises(ModelReceiptBlocked):
                prepare_model_canary_ci(
                    publish_request=self.publish, publish_receipt=row,
                    nonce=self.nonce, task=1, expected_head=self.after_commit)

if __name__ == "__main__":
    unittest.main()
