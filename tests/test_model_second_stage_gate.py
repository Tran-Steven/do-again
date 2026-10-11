"""Offline second-task native stage admission after exact first-CI checkpoint."""
from __future__ import annotations

import importlib
import unittest
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import patch

from do_again.model_canary import prepare_canary_edit
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_stage_gate import admit_codex_task_packet


class SecondCodexNativeStageTests(unittest.TestCase):
    def setUp(self):
        fixture_class=importlib.import_module("test_model_stage_gate").CodexStageAdmissionTests
        first=fixture_class("test_all_first_task_native_stage_packets_are_exactly_admitted")
        first.setUp()
        self.fixture=first
        self.first_head="e"*40
        now=datetime.now(timezone.utc)
        nonce=first.nonce
        first.requests["edit"]=prepare_canary_edit({
            "implementation":"def canonical_label(text):\n    if not text.isascii():\n        raise ValueError('ASCII only')\n    return '-'.join(text.lower().split())\n",
            "tests":"import unittest\nfrom canary_live_"+nonce+
                    " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                    "    def test_ascii(self):\n"
                    "        self.assertEqual(canonical_label('A B'), 'a-b')\n",
        },nonce=nonce,task=2,expected_head=self.first_head,issued_at=now)
        prior="edit"
        for stage in ("test","commit","publish","ci"):
            original=dict(first.requests[stage])
            original["request_id"]="canary-"+nonce+"-2-"+stage
            original["expected"]={"repo_head":(
                first.promoted if stage in ("publish","ci") else self.first_head)}
            original["continuation"]={
                "acknowledged_receipts":["canary-"+nonce+"-2-"+prior],
                "goal_state":"in_progress","summary":"Bounded synthetic task two."
            }
            if stage=="commit":
                original["args"]={**original["args"],
                    "message":"Validate synthetic Codex canary task 2"}
            if stage=="ci":
                original["args"]={
                    "original_request_id":"canary-"+nonce+"-2-publish"}
            first.requests[stage]=original
            prior=stage
        first.ledger.requests={
            row["request_id"]:row for row in first.requests.values()}
        first.broker.codex=SimpleNamespace(
            second_ready=lambda:{"head_sha":self.first_head})

    def admit(self,stage,packet=None):
        f=self.fixture
        with patch("do_again.supervisor.model_stage_gate.read_sealed_request",
                   side_effect=lambda broker,scope,task,name:f.requests[name]):
            original=(
                {"operation":"ci_observe","request_id":"canary-"+f.nonce+"-2-publish"}
                if stage=="ci" else f.packet(stage)
            )
            return admit_codex_task_packet(
                f.broker,f.scope,original if packet is None else packet)

    def test_all_five_second_task_stages_require_first_CI_proof(self):
        for stage in ("edit","test","commit","publish","ci"):
            with self.subTest(stage=stage):
                result=self.admit(stage)
                self.assertEqual(result["stage"],stage)
                self.assertEqual(result["request_id"],
                                 "canary-"+self.fixture.nonce+"-2-"+stage)

    def test_missing_first_CI_disables_all_second_task_effects(self):
        f=self.fixture
        def unavailable():
            raise ExecutionBlocked("missing native first-task terminal CI")
        f.broker.codex.second_ready=unavailable
        for stage in ("edit","test","commit","publish","ci"):
            with self.subTest(stage=stage),self.assertRaises(ExecutionBlocked):
                self.admit(stage)

    def test_task_two_cannot_reuse_task_one_base_or_use_wrong_commit_message(self):
        f=self.fixture
        for stage,change in (
            ("edit",{"expected_head":f.base}),
            ("test",{"request_fingerprint":"f"*64}),
            ("commit",{"message":"wrong task"}),
            ("publish",{"expected_head":self.first_head}),
            ("ci",{"request_id":"canary-"+f.nonce+"-1-publish"}),
        ):
            with self.subTest(stage=stage):
                packet={**f.packet(stage),**change}
                with self.assertRaises(ExecutionBlocked):
                    self.admit(stage,packet)

    def test_unverified_task_two_commit_blocks_draft_publication(self):
        f=self.fixture
        original=f.ledger.started
        try:
            f.ledger.started.add(f.requests["commit"]["request_id"])
            with self.assertRaises(ExecutionBlocked):
                self.admit("publish")
        finally:
            f.ledger.started=original


if __name__=="__main__":
    unittest.main()
