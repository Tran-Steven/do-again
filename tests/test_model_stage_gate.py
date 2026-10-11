"""Offline first-task Codex native stage admission, no real GitHub or effects."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import canonical_json,request_fingerprint
from do_again.model_canary import prepare_canary_edit
from do_again.model_grant import CodexCanaryScope
from do_again.supervisor.control_history import blob_sha
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_stage_gate import (
    admit_first_task_packet,read_sealed_request,
)


class BrokerLedger:
    def __init__(self,requests,source_sha,head):
        self.requests=requests
        self.source=source_sha
        self.head=head
        self.failed=set()
        self.started=set()
    def intent(self,project,rid):
        if rid not in self.requests:return None
        stage=rid.rsplit("-",1)[1]
        return {"operation":{"edit":"execute","test":"execute",
                             "commit":"git_commit","publish":"git_publish"}[stage],
                "source_sha":self.source,
                "request_fingerprint":request_fingerprint(self.requests[rid])}
    def observe_request(self,project,rid,fingerprint):
        stage=rid.rsplit("-",1)[1]
        if rid in self.started:return {"state":"post_dispatch_uncertain","replay":False}
        result={"source_sha":self.source,"returncode":0,"timed_out":False}
        if stage=="commit":
            result.update({"state":"succeeded","authority":{"repo_head":self.head}})
        if stage=="publish":
            result.update({"state":"succeeded","head":self.head,
                           "pull_request":19,"repository":"Tran-Steven/do-again"})
        if rid in self.failed:result["returncode"]=1
        return {"state":"terminal","replay":False,"source_sha":self.source,
                "request_fingerprint":fingerprint,"result":result}


class CodexStageAdmissionTests(unittest.TestCase):
    nonce="a"*24
    base="b"*40
    promoted="c"*40
    source="d"*40
    def setUp(self):
        now=datetime.now(timezone.utc)
        self.scope=CodexCanaryScope(
            nonce=self.nonce,baseline=self.base,parent_epoch=4,
            cli_sha256="e"*64,source_sha=self.source,
            expires_at_utc=now+timedelta(hours=1),
            control_branch="do-again/canary-"+self.nonce+"/control",
            max_model_calls=2,
        )
        self.paths=["canary_live_"+self.nonce+".py",
                    "tests/test_live_canary_"+self.nonce+".py"]
        self.edit=prepare_canary_edit(
            {"implementation":"def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
             "tests":"import unittest\nfrom canary_live_"+self.nonce+
                 " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                 "    def test_name(self):\n"
                 "        self.assertEqual(canonical_label('A B'),'a-b')\n"},
            nonce=self.nonce,task=1,expected_head=self.base,issued_at=now)
        self.requests={"edit":self.edit}
        ops={"test":"run_tests","commit":"git_commit","publish":"git_publish","ci":"ci_observe"}
        args={
            "test":{"discover":True,"start_directory":"tests",
                    "pattern":"test_live_canary_"+self.nonce+".py","cwd":"."},
            "commit":{"paths":self.paths,"message":"Validate synthetic Codex canary task 1"},
            "publish":{"title":"Do Again synthetic two-task Codex canary",
                       "body":"Bounded, draft-only isolated validation. Production remains disabled."},
            "ci":{"original_request_id":"canary-"+self.nonce+"-1-publish"},
        }
        prior="edit"
        for stage in ("test","commit","publish","ci"):
            head=self.promoted if stage in ("publish","ci") else self.base
            self.requests[stage]={
                "schema_version":1,"request_id":"canary-"+self.nonce+"-1-"+stage,
                "operation":ops[stage],"issued_at_utc":now.isoformat(),
                "expires_at_utc":(now+timedelta(minutes=20)).isoformat(),
                "args":args[stage],"expected":{"repo_head":head},
                "limits":{"timeout_seconds":120},
                "continuation":{"acknowledged_receipts":
                    ["canary-"+self.nonce+"-1-"+prior],
                    "goal_state":"in_progress"},
            }
            prior=stage
        self.by_id={r["request_id"]:r for r in self.requests.values()}
        self.ledger=BrokerLedger(self.by_id,self.source,self.promoted)
        self.broker=SimpleNamespace(
            config={"python":"/sealed/python","source_sha":self.source},
            project=SimpleNamespace(key="child",repo="/sealed/codex-child"),
            ledger=self.ledger,registry=SimpleNamespace(
                status=lambda repo:{"epoch":9}),
        )

    def packet(self,stage):
        request=self.requests[stage]
        rid=request["request_id"]
        sha=request_fingerprint(request)
        if stage=="ci":
            return {"operation":"ci_observe","request_id":"canary-"+self.nonce+"-1-publish"}
        if stage in ("edit","test"):
            argv=(["/sealed/python","-c",request["args"]["content"]]
                  if stage=="edit" else
                  ["/sealed/python","-m","unittest","discover",
                   "-s","tests","-p","test_live_canary_"+self.nonce+".py"])
            return {"operation":"execute","request_id":rid,"argv":argv,
                    "cwd":".","timeout":120,
                    "request_fingerprint":sha,"expected_head":request["expected"]["repo_head"],
                    "expected_authority":request["expected"]}
        return {"operation":"git_"+stage,"request_id":rid,
                **({ "paths":self.paths,"message":request["args"]["message"]}
                    if stage=="commit" else request["args"]),
                "expected_head":request["expected"]["repo_head"],
                "expected_epoch":9,"request_fingerprint":sha}

    def authorize(self,stage,packet=None):
        with patch("do_again.supervisor.model_stage_gate.read_sealed_request",
                   side_effect=lambda broker,scope,task,stage:self.requests[stage]):
            return admit_first_task_packet(
                self.broker,self.scope,self.packet(stage) if packet is None else packet)

    def test_all_first_task_native_stage_packets_are_exactly_admitted(self):
        for stage in ("edit","test","commit","publish","ci"):
            with self.subTest(stage=stage):
                proof=self.authorize(stage)
                self.assertEqual(proof["stage"],stage)
                self.assertEqual(proof["request_id"],self.requests[stage]["request_id"])

    def test_forged_script_epoch_fingerprint_and_arguments_are_denied(self):
        edits=[
            ("edit",{"argv":["/sealed/python","-c","print(1)"]}),
            ("edit",{"timeout":240}),
            ("edit",{"expected_head":"f"*40}),
            ("test",{"request_fingerprint":"0"*64}),
            ("test",{"argv":["/sealed/python","-m","unittest","discover","-s","."]}),
            ("commit",{"paths":["README.md"]}),
            ("commit",{"expected_epoch":8}),
            ("publish",{"title":"Different title"}),
            ("publish",{"expected_head":self.base}),
            ("ci",{"request_id":"canary-"+self.nonce+"-2-publish"}),
        ]
        for stage,changed in edits:
            with self.subTest(stage=stage,change=changed):
                packet={**self.packet(stage),**changed}
                with self.assertRaises(ExecutionBlocked):
                    self.authorize(stage,packet)

    def test_started_or_failed_predecessor_never_opens_next_native_stage(self):
        for prior,next_stage in (
            ("edit","test"),("test","commit"),("commit","publish"),("publish","ci"),
        ):
            rid=self.requests[prior]["request_id"]
            self.ledger.started.add(rid)
            with self.subTest(prior=prior,next=next_stage),self.assertRaises(ExecutionBlocked):
                self.authorize(next_stage)
            self.ledger.started.remove(rid)
            self.ledger.failed.add(rid)
            with self.subTest(prior=prior,failed=True),self.assertRaises(ExecutionBlocked):
                self.authorize(next_stage)
            self.ledger.failed.remove(rid)

    def test_missing_broker_intent_refuses_worker_claimed_success(self):
        original=self.ledger.requests
        try:
            self.ledger.requests={}
            with self.assertRaisesRegex(ExecutionBlocked,"original root broker intent"):
                self.authorize("test")
        finally:
            self.ledger.requests=original

    def test_remote_request_fingerprint_cannot_be_changed_after_publication(self):
        candidate=copy.deepcopy(self.requests["commit"])
        candidate["args"]["paths"]=["other.py"]
        with patch("do_again.supervisor.model_stage_gate.read_sealed_request",
                   return_value=candidate):
            with self.assertRaises(ExecutionBlocked):
                admit_first_task_packet(self.broker,self.scope,self.packet("commit"))

    def test_remote_request_retrieval_rejects_noncanonical_and_wrong_branch(self):
        request=self.edit
        rid=request["request_id"]
        path="automation/do_again/requests/"+rid+".json"
        sha=blob_sha(canonical_json(request)+b"\n")
        api=SimpleNamespace(repository="Tran-Steven/do-again",
            control_branch=self.scope.control_branch)
        with patch("do_again.supervisor.model_stage_gate.api_for",return_value=api),patch(
                "do_again.supervisor.model_stage_gate.snapshot",
                return_value=("a"*40,{}, {path:sha})),patch(
                "do_again.supervisor.model_stage_gate.read_json_blob",return_value=request):
            self.assertEqual(read_sealed_request(self.broker,self.scope,1,"edit"),request)
        api.control_branch="operator-control"
        with patch("do_again.supervisor.model_stage_gate.api_for",return_value=api),patch(
                "do_again.supervisor.model_stage_gate.snapshot") as remote:
            with self.assertRaises(ExecutionBlocked):
                read_sealed_request(self.broker,self.scope,1,"edit")
            remote.assert_not_called()


if __name__=="__main__":
    unittest.main()
