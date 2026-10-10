"""Offline non-browser Codex canary worker transitions; no model or network calls."""
from __future__ import annotations

import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace

from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_canary_worker import (
    _receipt_driver,_poll_terminal_ci,_admission,
)


class CodexCanaryWorkerTests(unittest.TestCase):
    nonce="a"*24

    def test_each_published_native_receipt_selects_exact_next_stage(self):
        calls=[]
        status={}
        receiver=_receipt_driver(self.nonce,1,Path("/sealed/codex"),7,
            rpc=lambda repo,pkt:(calls.append(pkt) or {"state":"succeeded"}),
            status=status)
        for stage,next_stage in (("edit","test"),("test","commit"),
                                 ("commit","publish"),("publish","ci")):
            result=receiver({
                "request_id":"canary-"+self.nonce+"-1-"+stage,
                "state":"succeeded",
            })
            self.assertFalse(result)
            self.assertEqual(calls[-1],{
                "operation":"codex_followup_publish",
                "request_id":"codex-publish-"+self.nonce+"-1-"+next_stage,
                "epoch":7,
            })
        self.assertEqual(len(calls),4)
        self.assertTrue(receiver({
            "request_id":"canary-"+self.nonce+"-1-ci","state":"succeeded"}))
        self.assertTrue(status["ci_receipt"])
        self.assertIsNone(status.get("failure"))

    def test_failed_or_unrecognized_stage_halts_without_git_publication(self):
        calls=[]
        status={}
        receiver=_receipt_driver(self.nonce,1,Path("/sealed/codex"),7,
            rpc=lambda repo,pkt:(calls.append(pkt) or {"state":"succeeded"}),
            status=status)
        for record in (
            {"request_id":"canary-"+self.nonce+"-1-edit","state":"failed"},
            {"request_id":"canary-"+self.nonce+"-3-edit","state":"succeeded"},
            {"request_id":"canary-"+self.nonce+"-1-edit-repair","state":"succeeded"},
        ):
            status.clear()
            self.assertTrue(receiver(record))
            self.assertIsInstance(status["failure"],str)
        self.assertEqual(calls,[])

    def test_ambiguous_native_followup_does_not_generate_retry_packet(self):
        calls=[]
        receiver=_receipt_driver(self.nonce,2,Path("/sealed/codex"),8,
            rpc=lambda repo,pkt:(calls.append(pkt) or (_ for _ in ()).throw(
                RuntimeError("GitHub branch PATCH response uncertain"))),
            status:= {})
        self.assertTrue(receiver({
            "request_id":"canary-"+self.nonce+"-2-edit","state":"succeeded"}))
        self.assertEqual(len(calls),1)
        self.assertIn("uncertain",status["failure"])

    def test_root_qualified_ci_waits_readonly_until_terminal_success(self):
        count=[]
        scope=SimpleNamespace(expires_at_utc=datetime.now(timezone.utc)+timedelta(minutes=40))
        def rpc(repo,pkt):
            count.append(pkt)
            if len(count)==1:raise ExecutionBlocked("Codex first CI has not completed successfully")
            return {"state":"terminal","conclusion":"success"}
        sleeps=[]
        outcome=_poll_terminal_ci(Path("/sealed"),1,scope,rpc=rpc,
            check=lambda:None,sleep=lambda seconds:sleeps.append(seconds))
        self.assertEqual(outcome["state"],"terminal")
        self.assertEqual(len(count),2)
        self.assertEqual(sleeps,[20])
        self.assertEqual(count[0],{"operation":"codex_ci_checkpoint"})

    def test_unrelated_root_ci_error_does_not_get_retried(self):
        calls=[]
        scope=SimpleNamespace(expires_at_utc=datetime.now(timezone.utc)+timedelta(minutes=40))
        def rpc(repo,pkt):
            calls.append(pkt)
            raise ExecutionBlocked("installed native confinement lost")
        with self.assertRaises(ExecutionBlocked):
            _poll_terminal_ci(Path("/sealed"),2,scope,rpc=rpc,
                              check=lambda:None,sleep=lambda seconds:None)
        self.assertEqual(len(calls),1)

    def test_after_second_terminal_completion_does_not_claim_production(self):
        scope=SimpleNamespace(expires_at_utc=datetime.now(timezone.utc)+timedelta(minutes=40))
        proof={"kind":"codex_two_task_native_acceptance",
               "production_ready":False,
               "first_result":"terminal_success","second_result":"terminal_success"}
        result=_poll_terminal_ci(Path("/sealed"),2,scope,
            rpc=lambda repo,pkt:proof,check=lambda:None)
        self.assertIs(result,proof)

    def test_original_native_operator_identity_and_quiescence_required(self):
        project={"repo":"/sealed/codex","worktree":"/sandbox/worktree",
                 "uid":505,"goal_revision":"codex-canary-"+self.nonce}
        config={"source_sha":"a"*40}
        args=SimpleNamespace(expected_epoch=9)
        verified={
            "source_sha":"a"*40,"epoch":9,
            "production_ready":False,"canary_authorized":True,
            "operator_intent":"active","enforcement_verified":True,
            "enforcement_blocker":None,"worktree":"/sandbox/worktree",
            "uid":505,"goal_revision":project["goal_revision"],
            "unresolved_executions":[],"inflight_request_ids":[],
        }
        self.assertEqual(_admission(config,args,project,rpc=lambda repo,pkt:verified),verified)
        for field,bad in (
            ("production_ready",True),("canary_authorized",False),
            ("epoch",10),("enforcement_verified",False),
            ("unresolved_executions",[{"request_id":"started"}]),
            ("inflight_request_ids",["inflight"]),
        ):
            with self.subTest(field=field),self.assertRaises(ExecutionBlocked):
                _admission(config,args,project,rpc=lambda repo,pkt:{**verified,field:bad})


if __name__=="__main__":
    unittest.main()
