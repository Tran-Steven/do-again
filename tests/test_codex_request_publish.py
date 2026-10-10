"""Offline brokered Codex request publication: replay and scope evidence."""
from __future__ import annotations

import base64
import copy
import hashlib
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import canonical_json
from do_again.model_canary import prepare_canary_edit
from do_again.model_grant import CodexCanaryScope
from do_again.supervisor.codex_request_publish import publish_codex_edit
from do_again.supervisor.control_history import blob_sha
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_native import CodexCanaryAuthority


class Ledger:
    def __init__(self):
        self.start={}
        self.results={}
    def lookup(self,project,request,fingerprint):
        if request not in self.start:return None
        if self.start[request]!=fingerprint:
            raise ExecutionBlocked("request fingerprint conflict")
        if request not in self.results:
            raise ExecutionBlocked("ambiguous started execution cannot replay")
        return self.results[request]
    def reserve(self,project,request,fingerprint,*,intent):
        if request in self.start:raise ExecutionBlocked("duplicate reservation")
        self.start[request]=fingerprint
        self.intent=intent
    def finish(self,project,request,result):
        self.results[request]=result


class FakeControlAPI:
    repository="Tran-Steven/do-again"
    def __init__(self,branch,nonce,request):
        self.control_branch=branch
        self.initial="a"*40
        self.head=self.initial
        self.tree="b"*40
        self.child_tree="c"*40
        self.created="d"*40
        self.writes=[]
        self.drop_patch=False
        self.path="automation/do_again/requests/"+request["request_id"]+".json"
        self.blob_sha=blob_sha(canonical_json(request)+b"\n")
        self.message="Do Again sealed Codex canary request "+request["request_id"]
    def snapshot(self):
        if self.head==self.initial:
            return self.head,{"sha":self.head,"tree":{"sha":self.tree},"message":"base",
                              "parents":[]},{}
        return self.head,{"sha":self.head,"tree":{"sha":self.child_tree},
                          "message":self.message,"parents":[{"sha":self.initial}]},{
                              self.path:self.blob_sha}
    def request(self,method,endpoint,payload=None):
        if method!="GET":
            self.writes.append((method,endpoint,copy.deepcopy(payload)))
        if method=="GET" and endpoint=="git/ref/heads/"+self.control_branch:
            return {"object":{"sha":self.head}}
        if method=="POST" and endpoint=="git/blobs":
            data=base64.b64decode(payload["content"])
            assert blob_sha(data)==self.blob_sha
            return {"sha":self.blob_sha}
        if method=="POST" and endpoint=="git/trees":
            assert payload["base_tree"]==self.tree
            assert payload["tree"][0]["path"]==self.path
            return {"sha":self.child_tree}
        if method=="POST" and endpoint=="git/commits":
            assert payload["parents"]==[self.initial]
            return {"sha":self.created}
        if method=="PATCH" and endpoint=="git/refs/heads/"+self.control_branch:
            assert payload=={"sha":self.created,"force":False}
            self.head=self.created
            if self.drop_patch:
                raise ExecutionBlocked("ambiguous network response")
            return {"object":{"sha":self.head}}
        raise AssertionError((method,endpoint))


class CodexRequestPublisherTests(unittest.TestCase):
    nonce="e"*24
    baseline="f"*40
    def setUp(self):
        self.request=prepare_canary_edit({
            "implementation":"def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
            "tests":"import unittest\nfrom canary_live_"+self.nonce+
                 " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                 "    def test_label(self):\n"
                 "        self.assertEqual(canonical_label('A B'),'a-b')\n",
        },nonce=self.nonce,task=1,expected_head=self.baseline,
          issued_at=datetime.now(timezone.utc))
        self.scope=CodexCanaryScope(nonce=self.nonce,baseline=self.baseline,
            parent_epoch=3,cli_sha256="0"*64,source_sha="1"*40,
            expires_at_utc=datetime.now(timezone.utc),
            control_branch="do-again/canary-"+self.nonce+"/control",
            max_model_calls=2)
        authority=CodexCanaryAuthority.__new__(CodexCanaryAuthority)
        authority.scope=self.scope
        self.broker=SimpleNamespace(
            codex=authority,lock=threading.Lock(),admission=nullcontext,
            registry=SimpleNamespace(status=lambda repo:{"epoch":4}),
            ledger=Ledger(),project=SimpleNamespace(key="test",repo="/synthetic"),
            config={"source_sha":"1"*40},control_cache=None)
        self.remote=FakeControlAPI(self.scope.control_branch,self.nonce,self.request)
        self.packet={"operation":"codex_request_publish",
            "request_id":"codex-publish-"+self.nonce+"-1-edit",
            "epoch":4,"value":self.request}
        patches=[
            patch("do_again.supervisor.codex_request_publish.sys.platform","darwin"),
            patch("do_again.supervisor.codex_request_publish.os.geteuid",return_value=0,create=True),
            patch("do_again.supervisor.model_native.CodexCanaryAuthority.check"),
            patch("do_again.supervisor.macos_server.verify_installation"),
            patch("do_again.supervisor.codex_request_publish.snapshot",
                  side_effect=lambda api:api.snapshot()),
        ]
        for item in patches:
            item.start();self.addCleanup(item.stop)

    def publish(self,packet=None):
        return publish_codex_edit(self.broker,packet or self.packet,api=self.remote)

    def test_one_published_request_uses_durable_journal_before_any_git_mutation(self):
        result=self.publish()
        self.assertEqual(result["state"],"succeeded")
        self.assertEqual(result["head"],self.remote.created)
        self.assertEqual(result["request_id"],self.request["request_id"])
        self.assertFalse(result["executed"])
        self.assertFalse(result["acknowledged"])
        self.assertEqual(self.broker.ledger.intent["path"],self.remote.path)
        self.assertEqual(self.broker.ledger.intent["control_branch"],self.scope.control_branch)
        self.assertEqual([x[1] for x in self.remote.writes],
            ["git/blobs","git/trees","git/commits",
             "git/refs/heads/"+self.scope.control_branch])
        count=len(self.remote.writes)
        self.assertEqual(self.publish(),result)
        self.assertEqual(len(self.remote.writes),count)

    def test_lost_ref_ack_does_not_replay_any_remote_effect(self):
        self.remote.drop_patch=True
        with self.assertRaises(ExecutionBlocked):
            self.publish()
        count=len(self.remote.writes)
        with self.assertRaisesRegex(ExecutionBlocked,"ambiguous started"):
            self.publish()
        self.assertEqual(len(self.remote.writes),count)
        self.assertEqual(self.remote.head,self.remote.created)

    def test_widened_control_packet_and_wrong_epoch_denied_before_mutation(self):
        packets=[
            {**self.packet,"epoch":9},
            {**self.packet,"request_id":"codex-publish-"+self.nonce+"-2-edit"},
            {**self.packet,"force":True},
            {**self.packet,"value":{**self.request,"operation":"git_publish"}},
            {**self.packet,"value":{**self.request,"args":{**self.request["args"],
                "content":self.request["args"]["content"]+"\nimport os"}}},
            {**self.packet,"value":{**self.request,"expected":{"repo_head":"a"*40}}},
        ]
        for packet in packets:
            with self.subTest(packet=packet.get("request_id")),self.assertRaises(ExecutionBlocked):
                self.publish(packet)
            self.assertEqual(self.remote.writes,[])

    def test_plain_parent_broker_has_no_codex_capability(self):
        delattr(self.broker,"codex")
        with self.assertRaisesRegex(ExecutionBlocked,"separately sealed"):
            self.publish()
        self.assertFalse(self.remote.writes)

    def test_existing_remote_identity_never_overwritten(self):
        before=self.remote.snapshot
        self.remote.snapshot=lambda:(
            "a"*40,{"sha":"a"*40,"tree":{"sha":"b"*40}},
            {self.remote.path:"9"*40},
        )
        with self.assertRaisesRegex(ExecutionBlocked,"already exists"):
            self.publish()
        self.assertEqual(self.remote.writes,[])


if __name__=="__main__":
    unittest.main()
