"""No-effect Codex root follow-up publication tests for bounded task one."""
from __future__ import annotations

import base64
import copy
import hashlib
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import canonical_json,request_fingerprint
from do_again.model_canary import prepare_canary_edit
from do_again.model_grant import CodexCanaryScope
from do_again.model_stages import (
    prepare_model_canary_test,prepare_model_canary_commit,
    prepare_model_canary_publish,
)
from do_again.supervisor.control_history import blob_sha
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_native import CodexCanaryAuthority
from do_again.supervisor.model_followup_publish import _derive,publish_codex_followup


class FakeLedger:
    def __init__(self):
        self.reservation=None
        self.success=None
        self.known={}
    def lookup(self,project,rid,fp):
        if self.reservation is None:return None
        if self.reservation[1]!=fp:raise ExecutionBlocked("fingerprint conflict")
        if self.success is None:raise ExecutionBlocked("ambiguous started execution cannot replay")
        return self.success
    def reserve(self,project,rid,fp,*,intent):
        self.reservation=(rid,fp,intent)
    def finish(self,project,rid,result):
        self.success=result
    def intent(self,project,rid):
        return self.known.get(rid)


class FakeApi:
    repository="Tran-Steven/do-again"
    def __init__(self,branch,proposal):
        self.control_branch=branch
        self.first="a"*40;self.second="b"*40
        self.base_tree="c"*40;self.child_tree="d"*40
        self.head=self.first
        self.proposal=proposal
        self.sha=blob_sha(canonical_json(proposal)+b"\n")
        self.path="automation/do_again/requests/"+proposal["request_id"]+".json"
        self.writes=[]
        self.uncertain=False
    def snapshot(self):
        if self.head==self.first:
            return (self.first,{"sha":self.first,"tree":{"sha":self.base_tree},
                                "parents":[],"message":"old"},{})
        return (self.head,{"sha":self.head,"tree":{"sha":self.child_tree},
                            "message":"Do Again sealed Codex canary request "+self.proposal["request_id"],
                            "parents":[{"sha":self.first}]},{self.path:self.sha})
    def request(self,method,route,payload=None):
        if method!="GET":self.writes.append((method,route,payload))
        if method=="POST" and route=="git/blobs":
            assert blob_sha(base64.b64decode(payload["content"]))==self.sha
            return {"sha":self.sha}
        if method=="POST" and route=="git/trees":
            assert payload["base_tree"]==self.base_tree
            assert payload["tree"][0]["path"]==self.path
            return {"sha":self.child_tree}
        if method=="POST" and route=="git/commits":
            assert payload["parents"]==[self.first]
            return {"sha":self.second}
        if method=="GET" and route=="git/ref/heads/"+self.control_branch:
            return {"object":{"sha":self.head}}
        if method=="PATCH" and route=="git/refs/heads/"+self.control_branch:
            self.head=payload["sha"]
            if self.uncertain:raise ExecutionBlocked("lost ref response")
            return {"object":{"sha":self.head}}
        raise AssertionError((method,route))


class FollowupTests(unittest.TestCase):
    nonce="a"*24
    base="b"*40
    promoted="c"*40
    source="d"*40

    def setUp(self):
        now=datetime.now(timezone.utc)
        self.scope=CodexCanaryScope(nonce=self.nonce,baseline=self.base,
            parent_epoch=3,cli_sha256="e"*64,source_sha=self.source,
            expires_at_utc=now,control_branch="do-again/canary-"+self.nonce+"/control",
            max_model_calls=2)
        self.edit=prepare_canary_edit(
            {"implementation":"def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
             "tests":"import unittest\nfrom canary_live_"+self.nonce+
                     " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                     "    def test_ascii(self):\n"
                     "        self.assertEqual(canonical_label('A B'), 'a-b')\n"},
            nonce=self.nonce,task=1,expected_head=self.base,issued_at=now)
        self.edit_receipt=self.receipt(
            self.edit,self.base,self.base,{"returncode":0,"timed_out":False})
        self.test=prepare_model_canary_test(
            edit_request=self.edit,edit_receipt=self.edit_receipt,
            nonce=self.nonce,task=1,expected_head=self.base,issued_at=now)
        self.test_receipt=self.receipt(
            self.test,self.base,self.base,{"returncode":0,"timed_out":False})
        self.commit=prepare_model_canary_commit(
            test_request=self.test,test_receipt=self.test_receipt,
            nonce=self.nonce,task=1,expected_head=self.base,issued_at=now)
        self.commit_terminal={
            "returncode":0,"state":"succeeded",
            "authority":{"repo_head":self.promoted},
            "paths":["canary_live_"+self.nonce+".py",
                     "tests/test_live_canary_"+self.nonce+".py"],
        }
        self.commit_receipt=self.receipt(
            self.commit,self.base,self.promoted,self.commit_terminal)
        self.publish=prepare_model_canary_publish(
            commit_request=self.commit,commit_receipt=self.commit_receipt,
            nonce=self.nonce,task=1,expected_head=self.base,issued_at=now)
        self.publish_terminal={
            "returncode":0,"state":"succeeded","head":self.promoted,
            "repository":"Tran-Steven/do-again","pull_request":19,
        }
        self.publish_receipt=self.receipt(
            self.publish,self.promoted,self.promoted,self.publish_terminal)
        self.history={
            "test":(self.edit,self.edit_receipt,{"returncode":0}),
            "commit":(self.test,self.test_receipt,{"returncode":0}),
            "publish":(self.commit,self.commit_receipt,self.commit_terminal),
            "ci":(self.publish,self.publish_receipt,self.publish_terminal),
        }
        self.ledger=FakeLedger()
        authority=CodexCanaryAuthority.__new__(CodexCanaryAuthority)
        authority.scope=self.scope
        self.broker=SimpleNamespace(
            codex=authority,ledger=self.ledger,
            registry=SimpleNamespace(status=lambda repo:{"epoch":7}),
            project=SimpleNamespace(key="synthetic",repo="/synthetic"),
            config={"source_sha":self.source},
            lock=threading.Lock(),admission=nullcontext,control_cache=None,
        )

    @staticmethod
    def receipt(request,before,after,result):
        digest=request_fingerprint(request)
        return {
            "schema_version":1,"request_id":request["request_id"],
            "request_fingerprint":digest,"state":"succeeded",
            "operation":request["operation"],
            "result":{
                "operation":request["operation"],"request_fingerprint":digest,
                "authority_before":{"repo_head":before},
                "authority_after":{"repo_head":after},
                "result":result,
            },
        }

    def derive(self,stage,*,bad_receipt=None,bad_native=None):
        original,receipt,native=self.history[stage]
        if bad_receipt is not None:receipt=bad_receipt
        if bad_native is not None:native=bad_native
        rid=original["request_id"]
        reqpath="automation/do_again/requests/"+rid+".json"
        recpath="automation/do_again/receipts/"+rid+".json"
        reqsha=blob_sha(canonical_json(original)+b"\n")
        recsha=blob_sha(canonical_json(receipt)+b"\n")
        entries={reqpath:reqsha,recpath:recsha}
        self.ledger.known[rid]={"request_fingerprint":request_fingerprint(original)}
        objects={reqsha:original,recsha:receipt}
        with patch("do_again.supervisor.model_followup_publish.read_json_blob",
                   side_effect=lambda api,sha:objects[sha]),patch(
                   "do_again.supervisor.model_followup_publish._terminal",
                   return_value=native):
            return _derive(self.broker,self.scope,None,entries,stage)

    def test_all_fixed_continuations_derived_without_model_calls(self):
        expected={
            "test":("run_tests",self.base),
            "commit":("git_commit",self.base),
            "publish":("git_publish",self.promoted),
            "ci":("ci_observe",self.promoted),
        }
        for stage,(operation,head) in expected.items():
            with self.subTest(stage=stage):
                request=self.derive(stage)
                self.assertEqual(request["operation"],operation)
                self.assertEqual(request["expected"],{"repo_head":head})
                self.assertEqual(request["request_id"],
                                 "canary-"+self.nonce+"-1-"+stage)
                self.assertEqual(request["continuation"]["acknowledged_receipts"],
                                 [self.history[stage][0]["request_id"]])

    def test_forged_receipt_cannot_derive_any_followup(self):
        for stage in self.history:
            old,receipt,native=self.history[stage]
            bad=copy.deepcopy(receipt)
            bad["request_fingerprint"]="0"*64
            with self.subTest(stage=stage),self.assertRaises(ExecutionBlocked):
                self.derive(stage,bad_receipt=bad)

    def test_native_commit_and_pr_disagreements_fail_closed(self):
        for stage,change in (
            ("publish",{"authority":{"repo_head":"f"*40}}),
            ("ci",{"head":"f"*40}),
            ("ci",{"pull_request":20}),
        ):
            old,receipt,native=self.history[stage]
            bad={**native,**change}
            with self.subTest(stage=stage,change=change),self.assertRaises(ExecutionBlocked):
                self.derive(stage,bad_native=bad)

    def test_one_reservation_and_exact_git_readback_no_replay(self):
        proposal=self.derive("test")
        api=FakeApi(self.scope.control_branch,proposal)
        self.ledger.known[self.edit["request_id"]]={
            "request_fingerprint":request_fingerprint(self.edit)}
        self.packet={"operation":"codex_followup_publish",
                     "request_id":"codex-publish-"+self.nonce+"-1-test","epoch":7}
        checks=[
            patch("do_again.supervisor.model_followup_publish.sys.platform","darwin"),
            patch("do_again.supervisor.model_followup_publish.os.geteuid",
                  return_value=0,create=True),
            patch("do_again.supervisor.model_native.CodexCanaryAuthority.check"),
            patch("do_again.supervisor.macos_server.verify_installation"),
            patch("do_again.supervisor.model_followup_publish.snapshot",
                  side_effect=lambda remote:remote.snapshot()),
            patch("do_again.supervisor.model_followup_publish._derive",
                  return_value=proposal),
        ]
        for p in checks:p.start();self.addCleanup(p.stop)
        result=publish_codex_followup(self.broker,self.packet,api=api)
        self.assertEqual(result["state"],"succeeded")
        self.assertEqual(result["request_id"],proposal["request_id"])
        self.assertFalse(result["executed"])
        self.assertEqual(self.ledger.reservation[2]["stage"],"test")
        count=len(api.writes)
        self.assertEqual(publish_codex_followup(self.broker,self.packet,api=api),result)
        self.assertEqual(len(api.writes),count)

    def test_ambiguous_ref_response_never_dispatches_again(self):
        proposal=self.derive("test")
        api=FakeApi(self.scope.control_branch,proposal)
        api.uncertain=True
        packet={"operation":"codex_followup_publish",
                "request_id":"codex-publish-"+self.nonce+"-1-test","epoch":7}
        patches=[
            patch("do_again.supervisor.model_followup_publish.sys.platform","darwin"),
            patch("do_again.supervisor.model_followup_publish.os.geteuid",
                  return_value=0,create=True),
            patch("do_again.supervisor.model_native.CodexCanaryAuthority.check"),
            patch("do_again.supervisor.macos_server.verify_installation"),
            patch("do_again.supervisor.model_followup_publish.snapshot",
                  side_effect=lambda remote:remote.snapshot()),
            patch("do_again.supervisor.model_followup_publish._derive",
                  return_value=proposal),
        ]
        for p in patches:p.start();self.addCleanup(p.stop)
        with self.assertRaises(ExecutionBlocked):
            publish_codex_followup(self.broker,packet,api=api)
        count=len(api.writes)
        with self.assertRaisesRegex(ExecutionBlocked,"ambiguous started"):
            publish_codex_followup(self.broker,packet,api=api)
        self.assertEqual(len(api.writes),count)

    def test_payload_and_task_two_selector_rejected_before_model_or_github(self):
        api=FakeApi(self.scope.control_branch,self.test)
        patches=[
            patch("do_again.supervisor.model_followup_publish.sys.platform","darwin"),
            patch("do_again.supervisor.model_followup_publish.os.geteuid",
                  return_value=0,create=True),
        ]
        for p in patches:p.start();self.addCleanup(p.stop)
        for packet in (
            {"operation":"codex_followup_publish",
             "request_id":"codex-publish-"+self.nonce+"-2-test","epoch":7},
            {"operation":"codex_followup_publish",
             "request_id":"codex-publish-"+self.nonce+"-1-test",
             "epoch":7,"value":{"script":"unsafe"}},
        ):
            with self.subTest(packet=packet),self.assertRaises(ExecutionBlocked):
                publish_codex_followup(self.broker,packet,api=api)
        self.assertFalse(api.writes)


if __name__=="__main__":
    unittest.main()
