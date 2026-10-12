"""Full read-only Codex stage-chain receipt binding tests."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import canonical_json, request_fingerprint
from do_again.model_canary import prepare_canary_edit
from do_again.model_receipts import ModelReceiptBlocked
from do_again.model_stages import (
    prepare_model_canary_test, prepare_model_canary_commit,
    prepare_model_canary_publish,
)
from do_again.model_watch import observe_model_stage
from do_again.supervisor.control_history import blob_sha


class ModelChainWatchTests(unittest.TestCase):
    nonce = "a" * 24
    baseline = "b" * 40
    committed = "c" * 40

    def setUp(self):
        self.api = SimpleNamespace(repository="Tran-Steven/do-again",
            control_branch="do-again/canary-" + self.nonce + "/control")
        now = datetime.now(timezone.utc)
        self.edit = prepare_canary_edit(
            {"implementation": "def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
             "tests": "import unittest\nfrom canary_live_" + self.nonce +
                      " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                      "    def test_label(self):\n"
                      "        self.assertEqual(canonical_label('A B'), 'a-b')\n"},
            nonce=self.nonce, task=1, expected_head=self.baseline, issued_at=now)
        self.edit_receipt = self.receipt(self.edit, self.baseline, self.baseline,
                                        {"returncode": 0, "timed_out": False})
        self.test = prepare_model_canary_test(
            edit_request=self.edit, edit_receipt=self.edit_receipt,
            nonce=self.nonce, task=1, expected_head=self.baseline, issued_at=now)
        self.test_receipt = self.receipt(self.test, self.baseline, self.baseline,
                                        {"returncode": 0, "timed_out": False})
        self.commit = prepare_model_canary_commit(
            test_request=self.test, test_receipt=self.test_receipt,
            nonce=self.nonce, task=1, expected_head=self.baseline, issued_at=now)
        self.commit_receipt = self.receipt(
            self.commit, self.baseline, self.committed,
            {"state": "succeeded", "returncode": 0,
             "authority": {"repo_head": self.committed},
             "paths": ["canary_live_" + self.nonce + ".py",
                       "tests/test_live_canary_" + self.nonce + ".py"]})
        self.publish = prepare_model_canary_publish(
            commit_request=self.commit, commit_receipt=self.commit_receipt,
            nonce=self.nonce, task=1, expected_head=self.baseline, issued_at=now)
        self.publish_receipt = self.receipt(
            self.publish, self.committed, self.committed,
            {"state": "succeeded", "returncode": 0, "repository": "Tran-Steven/do-again",
             "head": self.committed, "pull_request": 99})

    @staticmethod
    def receipt(request, old, new, result):
        fingerprint = request_fingerprint(request)
        return {
            "schema_version": 1, "request_id": request["request_id"],
            "request_fingerprint": fingerprint, "state": "succeeded",
            "operation": request["operation"],
            "result": {
                "operation": request["operation"],
                "request_fingerprint": fingerprint,
                "authority_before": {"repo_head": old},
                "authority_after": {"repo_head": new},
                "result": result,
            },
        }

    def observe(self, stage, request, receipt, *, request_sha=None, include_receipt=True):
        rid = request["request_id"]
        req_path = "automation/do_again/requests/" + rid + ".json"
        rec_path = "automation/do_again/receipts/" + rid + ".json"
        sha = blob_sha(canonical_json(request) + b"\n") if request_sha is None else request_sha
        entries = {req_path: sha}
        if include_receipt:
            entries[rec_path] = "d" * 40
        with patch("do_again.model_watch.snapshot", return_value=(
                "e"*40, {"tree":{"sha":"f"*40}}, entries)) as snapshot, patch(
                "do_again.model_watch.read_json_blob", return_value=receipt) as read:
            result = observe_model_stage(
                api=self.api, nonce=self.nonce, task=1,
                stage=stage, request=request)
        snapshot.assert_called_once_with(self.api)
        if include_receipt:
            read.assert_called_once_with(self.api, "d"*40)
        else:
            read.assert_not_called()
        return result

    def test_every_brokered_stage_requires_exact_git_blobs(self):
        chain = [
            ("edit", self.edit, self.edit_receipt, "test"),
            ("test", self.test, self.test_receipt, "commit"),
            ("commit", self.commit, self.commit_receipt, "publish"),
            ("publish", self.publish, self.publish_receipt, "ci"),
        ]
        for stage, request, receipt, expected_next in chain:
            with self.subTest(stage=stage):
                actual = self.observe(stage, request, receipt)
                self.assertEqual(actual["state"], "execution_verified")
                self.assertEqual(actual["request_id"], request["request_id"])
                self.assertEqual(actual["next_stage"], expected_next)
                self.assertEqual(actual["control_head"], "e"*40)
                self.assertEqual(actual["receipt_blob_sha"], "d"*40)
                self.assertFalse(actual["replay"])
                self.assertFalse(actual["model_acknowledged"])
                self.assertFalse(actual["browser_acknowledged"])

    def test_remote_absent_receipt_never_promotes_any_stage(self):
        for stage, request in [
            ("edit", self.edit), ("test", self.test),
            ("commit", self.commit), ("publish", self.publish),
        ]:
            with self.subTest(stage=stage):
                state = self.observe(stage, request, {}, include_receipt=False)
                self.assertEqual(state["state"], "awaiting_broker_receipt")
                self.assertFalse(state["replay"])

    def test_conflicting_remote_request_blocks_stage_before_receipt_read(self):
        with self.assertRaises(ModelReceiptBlocked):
            self.observe("commit", self.commit, self.commit_receipt,
                         request_sha="0"*40)

    def test_fake_and_changed_broker_results_are_rejected(self):
        for stage, request, receipt in [
            ("edit", self.edit, self.edit_receipt),
            ("test", self.test, self.test_receipt),
            ("commit", self.commit, self.commit_receipt),
            ("publish", self.publish, self.publish_receipt),
        ]:
            for field, changed in [
                ("state", "blocked_ambiguous_replay"),
                ("request_fingerprint", "0"*64),
                ("operation", "status"),
            ]:
                bad = copy.deepcopy(receipt)
                bad[field] = changed
                with self.subTest(stage=stage, field=field), self.assertRaises(ModelReceiptBlocked):
                    self.observe(stage, request, bad)

    def test_api_cannot_watch_other_repo_other_grant_or_ci_as_execution(self):
        with patch("do_again.model_watch.snapshot") as snapshot:
            for values in (
                {"stage":"ci"},
                {"stage":"edit","nonce":"f"*24},
                {"stage":"publish"},
            ):
                with self.subTest(values=values),self.assertRaises(ModelReceiptBlocked):
                    observe_model_stage(api=self.api, nonce=values.get("nonce",self.nonce),
                        task=1, stage=values["stage"], request=self.edit)
            snapshot.assert_not_called()
        self.api.repository="Tran-Steven/jobpipe"
        with self.assertRaises(ModelReceiptBlocked):
            observe_model_stage(api=self.api,nonce=self.nonce,task=1,
                                stage="edit",request=self.edit)


if __name__ == "__main__":
    unittest.main()
