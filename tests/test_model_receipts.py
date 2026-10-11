"""Offline receipt evidence regressions for non-browser Codex canary."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from do_again.model_canary import prepare_canary_edit
from do_again.model_receipts import ModelReceiptBlocked, verify_model_edit_receipt
from do_again.core.schema import request_fingerprint


class ModelReceiptTests(unittest.TestCase):
    nonce = "a" * 24
    head = "b" * 40

    def setUp(self):
        self.request = prepare_canary_edit(
            {"implementation": "def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
             "tests": "import unittest\nfrom canary_live_" + self.nonce +
                      " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                      "    def test_label(self):\n"
                      "        self.assertEqual(canonical_label('A B'), 'a-b')\n"},
            nonce=self.nonce, task=1, expected_head=self.head,
            issued_at=datetime.now(timezone.utc),
        )
        fingerprint = request_fingerprint(self.request)
        self.receipt = {
            "schema_version": 1,
            "request_id": self.request["request_id"],
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

    def test_exact_broker_result_yields_observation_but_not_ack(self):
        result = verify_model_edit_receipt(
            self.request, self.receipt, nonce=self.nonce, task=1,
            expected_head=self.head,
        )
        self.assertEqual(result["state"], "execution_verified")
        self.assertEqual(result["request_id"], self.request["request_id"])
        self.assertEqual(result["next_stage"], "test")
        self.assertTrue(result["execution_verified"])
        self.assertFalse(result["model_acknowledged"])
        self.assertFalse(result["browser_acknowledged"])
        self.assertEqual(len(result["receipt_sha256"]), 64)

    def test_missing_wrong_or_uncertain_evidence_never_progresses(self):
        variations = [
            ("request_id", "replayed-canary-request"),
            ("operation", "git_commit"),
            ("request_fingerprint", "0" * 64),
            ("state", "blocked_ambiguous_replay"),
            ("state", "failed"),
            ("result", None),
        ]
        for name, value in variations:
            with self.subTest(name=name, value=value):
                changed = copy.deepcopy(self.receipt)
                changed[name] = value
                with self.assertRaises(ModelReceiptBlocked):
                    verify_model_edit_receipt(self.request, changed,
                        nonce=self.nonce, task=1, expected_head=self.head)

    def test_nested_broker_result_must_have_source_identity_and_success(self):
        variations = [
            ("operation", "run_tests"),
            ("request_fingerprint", "0" * 64),
            ("authority_before", {"repo_head": "f" * 40}),
            ("authority_after", {"repo_head": "f" * 40}),
            ("result", {"returncode": 0, "timed_out": True}),
            ("result", {"returncode": 1, "timed_out": False}),
            ("result", {"returncode": 0}),
        ]
        for field, value in variations:
            with self.subTest(field=field):
                changed = copy.deepcopy(self.receipt)
                changed["result"][field] = value
                with self.assertRaises(ModelReceiptBlocked):
                    verify_model_edit_receipt(self.request, changed,
                        nonce=self.nonce, task=1, expected_head=self.head)

    def test_wrong_nonce_task_or_expected_head_never_proves_execution(self):
        variations = [
            {"nonce": "f" * 24, "task": 1, "expected_head": self.head},
            {"nonce": self.nonce, "task": 2, "expected_head": self.head},
            {"nonce": self.nonce, "task": 1, "expected_head": "0" * 40},
            {"nonce": "../escape", "task": 1, "expected_head": self.head},
        ]
        for scope in variations:
            with self.subTest(scope=scope), self.assertRaises(ModelReceiptBlocked):
                verify_model_edit_receipt(self.request, self.receipt, **scope)


if __name__ == "__main__":
    unittest.main()
