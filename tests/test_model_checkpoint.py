"""Offline Codex two-task root checkpoint regression coverage."""
from __future__ import annotations

import copy
import hashlib
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import canonical_json, atomic_json as real_atomic_json
from do_again.model_grant import CodexCanaryScope
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_checkpoint import (
    certify_first_task_ci,validate_checkpoint,observe_checkpoint,
)


class CodexCIProofTests(unittest.TestCase):
    nonce="a"*24
    head="b"*40
    source="c"*40

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scope=CodexCanaryScope(
            nonce=self.nonce,baseline="d"*40,parent_epoch=4,
            cli_sha256="e"*64,source_sha=self.source,
            expires_at_utc=datetime.now(timezone.utc)+timedelta(hours=1),
            control_branch="do-again/canary-"+self.nonce+"/control",
            max_model_calls=2,
        )
        self.ci={
            "state":"terminal","status":"completed","conclusion":"success",
            "returncode":0,"replay":False,
            "repository":"Tran-Steven/do-again",
            "head_sha":self.head,"pull_request":33,
            "run_id":123456,"run_attempt":1,
            "url":"https://github.com/Tran-Steven/do-again/actions/runs/123456",
        }
        self.record={
            "schema_version":1,"kind":"codex_native_ci_checkpoint",
            "nonce":self.nonce,"source_sha":self.source,
            "baseline":self.scope.baseline,"parent_epoch":4,
            "task":1,"repository":"Tran-Steven/do-again",
            "operation":"ci_observe",
            **self.ci,"publication_request_id":"canary-"+self.nonce+"-1-publish",
            "model_acknowledged":False,"browser_acknowledged":False,
        }
        self.record["ci_sha256"]=hashlib.sha256(canonical_json(self.record)).hexdigest()
        self.ledger=SimpleNamespace(
            intent=lambda key,rid: {
                "operation":"git_publish","source_sha":self.source,
                "request_fingerprint":"f"*64,
            },
            observe_request=lambda key,rid,fp: {
                "state":"terminal","replay":False,
                "result":{"state":"succeeded","returncode":0,
                          "repository":"Tran-Steven/do-again",
                          "head":self.head,"pull_request":33},
            },
        )
        authority=SimpleNamespace(scope=self.scope,check=lambda:True)
        self.broker=SimpleNamespace(
            codex=authority,ledger=self.ledger,config={"source_sha":self.source},
            project=SimpleNamespace(key="synthetic",repo=Path(self.temp.name)),
            state=Path(self.temp.name),lock=threading.Lock(),admission=nullcontext,
        )

    def test_exact_native_checkpoint_validates_and_cannot_claim_model_ack(self):
        self.assertEqual(validate_checkpoint(self.broker,self.scope,self.record),self.record)
        bad=copy.deepcopy(self.record)
        bad["model_acknowledged"]=True
        bad["ci_sha256"]=hashlib.sha256(canonical_json({
            k:v for k,v in bad.items() if k!="ci_sha256"})).hexdigest()
        with self.assertRaises(ExecutionBlocked):
            validate_checkpoint(self.broker,self.scope,bad)

    def test_waiting_failed_changed_head_run_pr_and_digest_are_denied(self):
        changes=[
            {"state":"waiting"},{"status":"in_progress"},{"conclusion":None},
            {"conclusion":"failure"},{"returncode":1},{"replay":True},
            {"head_sha":self.scope.baseline},
            {"pull_request":0},{"run_id":0},{"run_attempt":True},
            {"url":"https://other.invalid/"},{"parent_epoch":999},
            {"nonce":"f"*24},{"ci_sha256":"0"*64},
        ]
        for change in changes:
            with self.subTest(change=change):
                candidate={**self.record,**change}
                if "ci_sha256" not in change:
                    candidate["ci_sha256"]=hashlib.sha256(canonical_json({
                        k:v for k,v in candidate.items() if k!="ci_sha256"})).hexdigest()
                with self.assertRaises(ExecutionBlocked):
                    validate_checkpoint(self.broker,self.scope,candidate)

    def test_root_certifies_exact_terminal_result_without_model_call(self):
        patches=[
            patch("do_again.supervisor.model_checkpoint.sys.platform","darwin"),
            patch("do_again.supervisor.model_checkpoint.os.geteuid",return_value=0,create=True),
            patch("do_again.supervisor.macos_server.verify_installation"),
            patch("do_again.supervisor.model_checkpoint.observe_checkpoint",
                  return_value={"state":"not_verified","task":1,"replay":False}),
            patch("do_again.supervisor.ci_observation.observe_ci",return_value=self.ci),
            patch("do_again.supervisor.model_checkpoint.atomic_json",wraps=real_atomic_json),
        ]
        started=[]
        for p in patches:started.append(p.start());self.addCleanup(p.stop)
        verified=certify_first_task_ci(self.broker,{"operation":"codex_ci_checkpoint"})
        self.assertEqual(verified,self.record)
        started[-1].assert_called_once()
        self.assertTrue(str(started[-1].call_args.args[0]).endswith("codex-ci-task-one.json"))

    def test_ci_poll_waiting_or_failure_cannot_create_checkpoint(self):
        with patch("do_again.supervisor.model_checkpoint.sys.platform","darwin"),patch(
                "do_again.supervisor.model_checkpoint.os.geteuid",return_value=0,create=True),patch(
                "do_again.supervisor.macos_server.verify_installation"),patch(
                "do_again.supervisor.model_checkpoint.observe_checkpoint",
                return_value={"state":"not_verified"}),patch(
                "do_again.supervisor.model_checkpoint.atomic_json") as write:
            for update in ({"state":"waiting","status":"in_progress",
                            "conclusion":None,"returncode":0},
                           {"state":"terminal","status":"completed",
                            "conclusion":"failure","returncode":1}):
                with self.subTest(update=update),patch(
                        "do_again.supervisor.ci_observation.observe_ci",
                        return_value={**self.ci,**update}):
                    with self.assertRaises(ExecutionBlocked):
                        certify_first_task_ci(self.broker,{"operation":"codex_ci_checkpoint"})
            write.assert_not_called()

    def test_read_only_root_checkpoint_missing_does_not_infer_success(self):
        result=observe_checkpoint(self.broker,self.scope)
        self.assertEqual(result["state"],"not_verified")
        self.assertFalse(result["replay"])

    def test_root_checkpoint_payload_cannot_be_supplied_by_operator(self):
        with patch("do_again.supervisor.model_checkpoint.sys.platform","darwin"),patch(
                "do_again.supervisor.model_checkpoint.os.geteuid",return_value=0,create=True):
            with self.assertRaises(ExecutionBlocked):
                certify_first_task_ci(self.broker,{
                    "operation":"codex_ci_checkpoint","result":self.ci})


if __name__=="__main__":
    unittest.main()
