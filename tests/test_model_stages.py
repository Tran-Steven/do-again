"""Offline model canary edit-to-test handoff regressions."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from do_again.model_canary import prepare_canary_edit
from do_again.model_stages import prepare_model_canary_test
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

if __name__ == "__main__":
    unittest.main()
