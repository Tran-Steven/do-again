"""Offline GitHub model-canary read-only receipt watcher tests."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from do_again.model_canary import prepare_canary_edit
from do_again.model_watch import observe_model_edit
from do_again.model_receipts import ModelReceiptBlocked
from do_again.core.schema import canonical_json, request_fingerprint
from do_again.supervisor.control_history import blob_sha


class ModelWatchTests(unittest.TestCase):
    nonce = "e" * 24
    head = "f" * 40

    def setUp(self):
        self.request = prepare_canary_edit(
            {"implementation": "def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
             "tests": "import unittest\nfrom canary_live_" + self.nonce
                      + " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                      "    def test_label(self):\n"
                      "        self.assertEqual(canonical_label('A B'), 'a-b')\n"},
            nonce=self.nonce, task=1, expected_head=self.head,
            issued_at=datetime.now(timezone.utc),
        )
        rid = self.request["request_id"]
        self.path = "automation/do_again/requests/" + rid + ".json"
        self.receipt_path = "automation/do_again/receipts/" + rid + ".json"
        self.req_sha = blob_sha(canonical_json(self.request) + b"\n")
        self.receipt = {
            "schema_version": 1, "request_id": rid,
            "operation": "scratch_script", "state": "succeeded",
            "request_fingerprint": request_fingerprint(self.request),
            "result": {
                "operation": "scratch_script",
                "request_fingerprint": request_fingerprint(self.request),
                "authority_before": {"repo_head": self.head},
                "authority_after": {"repo_head": self.head},
                "result": {"returncode": 0, "timed_out": False},
            },
        }
        self.api = SimpleNamespace(repository="Tran-Steven/do-again",
            control_branch="do-again/canary-" + self.nonce + "/control")

    def run_watch(self, entries, receipt=None):
        with patch("do_again.model_watch.snapshot", return_value=(
                "a" * 40, {"tree": {"sha": "b" * 40}}, entries
        )) as snapshot, patch("do_again.model_watch.read_json_blob",
                              return_value=receipt) as read:
            result = observe_model_edit(
                api=self.api, nonce=self.nonce, task=1, request=self.request)
        return result, snapshot, read

    def test_missing_original_request_is_not_evidence(self):
        result, _, read = self.run_watch({})
        self.assertEqual(result["state"], "not_published")
        self.assertFalse(result["replay"])
        read.assert_not_called()

    def test_absent_receipt_does_not_grant_next_stage(self):
        result, _, read = self.run_watch({self.path: self.req_sha})
        self.assertEqual(result["state"], "awaiting_broker_receipt")
        read.assert_not_called()

    def test_matching_original_request_and_receipt_only_verify_execution(self):
        result, _, read = self.run_watch({
            self.path: self.req_sha, self.receipt_path: "c" * 40,
        }, self.receipt)
        self.assertEqual(result["state"], "execution_verified")
        self.assertFalse(result["browser_acknowledged"])
        self.assertFalse(result["model_acknowledged"])
        read.assert_called_once()

    def test_conflicting_remote_request_fails_without_reading_receipt(self):
        with self.assertRaises(ModelReceiptBlocked):
            self.run_watch({
                self.path: "9" * 40, self.receipt_path: "c" * 40,
            }, self.receipt)

    def test_receipt_from_wrong_request_fails_closed(self):
        mismatched = copy.deepcopy(self.receipt)
        mismatched["request_fingerprint"] = "0" * 64
        with self.assertRaises(ModelReceiptBlocked):
            self.run_watch({
                self.path: self.req_sha, self.receipt_path: "c" * 40,
            }, mismatched)

    def test_outside_branch_and_repository_never_read(self):
        for attr, changed in [
            ("control_branch", "operator-control"),
            ("repository", "Tran-Steven/jobpipe"),
        ]:
            with self.subTest(attr=attr):
                previous = getattr(self.api, attr)
                setattr(self.api, attr, changed)
                try:
                    with patch("do_again.model_watch.snapshot") as snapshot:
                        with self.assertRaises(ModelReceiptBlocked):
                            observe_model_edit(api=self.api, nonce=self.nonce,
                                task=1, request=self.request)
                        snapshot.assert_not_called()
                finally:
                    setattr(self.api, attr, previous)


if __name__ == "__main__":
    unittest.main()
