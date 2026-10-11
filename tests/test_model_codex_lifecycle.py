"""No-root/no-network Codex activation and final two-task certificate tests."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

from do_again.model_grant import CodexCanaryScope
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_native import activate_codex
from do_again.supervisor.model_completion import finalize_codex_canary


class CodexTwoTaskLifecycleTests(unittest.TestCase):
    nonce="a"*24
    source="b"*40
    head1="c"*40
    head2="d"*40

    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.scope=CodexCanaryScope(
            nonce=self.nonce,baseline="e"*40,parent_epoch=3,
            cli_sha256="f"*64,source_sha=self.source,
            expires_at_utc=datetime.now(timezone.utc)+timedelta(hours=1),
            control_branch="do-again/canary-"+self.nonce+"/control",
            max_model_calls=2,
        )
        authority=SimpleNamespace(scope=self.scope,
            check=Mock(),parent_check=Mock(),second_ready=Mock())
        status={"intent":"maintenance","epoch":5,
                "goal_revision":"codex-canary-"+self.nonce}
        self.status=status
        self.registry=SimpleNamespace(
            status=lambda repo:dict(self.status),
            set_intent=Mock(return_value=6))
        self.broker=SimpleNamespace(
            codex=authority,lock=threading.Lock(),admission=nullcontext,
            config={"source_sha":self.source,"production_ready":False},
            registry=self.registry,project=SimpleNamespace(
                key="child",repo=self.root/"child"),state=self.root,
            _verified=lambda:True,_inflight=lambda:[],
            ledger=SimpleNamespace(pending=lambda key:[]),
        )

    def test_activation_requires_staged_worker_and_can_run_once(self):
        staged={"phase":"staged","epoch":6,
                "source_sha":self.source,
                "label":"io.github.tran-steven.do-again.worker.fake"}
        spec=("io.github.tran-steven.do-again.worker.fake",
              {"ProgramArguments":["/sealed/python","--codex-canary"]})
        with patch("do_again.supervisor.model_native.sys.platform","darwin"),patch(
                "do_again.supervisor.model_native.os.geteuid",
                return_value=0,create=True),patch(
                "do_again.supervisor.macos_server.verify_installation"),patch(
                "do_again.supervisor.worker_service.service_spec",return_value=spec):
            with self.assertRaisesRegex(ExecutionBlocked,"has not been staged"):
                activate_codex(self.broker)
            (self.root/"worker-deployment.json").write_text(json.dumps(staged))
            result=activate_codex(self.broker)
            self.assertEqual(result["epoch"],6)
            self.assertFalse(result["production_ready"])
            self.assertFalse(result["started"])
            self.assertTrue((self.root/"codex-canary-activation.json").exists())
            self.registry.set_intent.assert_called_once_with(
                self.broker.project.repo,"active",goal_revision=self.status["goal_revision"])
            with self.assertRaisesRegex(ExecutionBlocked,"already exists"):
                activate_codex(self.broker)

    def test_activation_rejects_quota_irrelevant_production_mode(self):
        self.broker.config["production_ready"]=True
        with patch("do_again.supervisor.model_native.sys.platform","darwin"),patch(
                "do_again.supervisor.model_native.os.geteuid",return_value=0,create=True):
            with self.assertRaises(ExecutionBlocked):
                activate_codex(self.broker)
        self.registry.set_intent.assert_not_called()

    def valid_first_checkpoint(self):
        return {"state":"terminal","conclusion":"success",
                "ci_sha256":"0"*64,"head_sha":self.head1,
                "pull_request":43,"source_sha":self.source}

    def valid_second_ci(self):
        return {
            "state":"terminal","status":"completed","conclusion":"success",
            "returncode":0,"replay":False,
            "repository":"Tran-Steven/do-again","head_sha":self.head2,
            "pull_request":43,"run_id":9876,"run_attempt":1,
        }

    def test_root_completion_requires_two_distinct_heads_and_sets_maintenance(self):
        published={"returncode":0,"state":"succeeded",
                   "repository":"Tran-Steven/do-again",
                   "head":self.head2,"pull_request":43}
        with patch("do_again.supervisor.model_completion.sys.platform","darwin"),patch(
                "do_again.supervisor.model_completion.os.geteuid",
                return_value=0,create=True),patch(
                "do_again.supervisor.macos_server.verify_installation"),patch(
                "do_again.supervisor.model_completion.observe_checkpoint",
                return_value=self.valid_first_checkpoint()),patch(
                "do_again.supervisor.model_stage_gate._terminal",return_value=published),patch(
                "do_again.supervisor.ci_observation.observe_ci",
                return_value=self.valid_second_ci()):
            result=finalize_codex_canary(
                self.broker,{"operation":"codex_canary_complete"})
        self.assertEqual(result["first_head"],self.head1)
        self.assertEqual(result["second_head"],self.head2)
        self.assertEqual(result["second_result"],"terminal_success")
        self.assertFalse(result["production_ready"])
        self.registry.set_intent.assert_called_once_with(
            self.broker.project.repo,"maintenance",goal_revision=self.status["goal_revision"])
        self.assertTrue((self.root/"codex-two-task-complete.json").is_file())

    def test_incomplete_ci_and_changed_pr_cannot_claim_acceptance(self):
        for change in ({"state":"waiting","status":"in_progress",
                        "conclusion":None,"returncode":0},
                       {"state":"terminal","status":"completed",
                        "conclusion":"failure","returncode":1},
                       {"pull_request":42},
                       {"head_sha":self.head1}):
            with self.subTest(change=change),patch(
                    "do_again.supervisor.model_completion.sys.platform","darwin"),patch(
                    "do_again.supervisor.model_completion.os.geteuid",
                    return_value=0,create=True),patch(
                    "do_again.supervisor.macos_server.verify_installation"),patch(
                    "do_again.supervisor.model_completion.observe_checkpoint",
                    return_value=self.valid_first_checkpoint()),patch(
                    "do_again.supervisor.model_stage_gate._terminal",return_value={
                        "returncode":0,"state":"succeeded",
                        "repository":"Tran-Steven/do-again",
                        "head":self.head2,"pull_request":43}),patch(
                    "do_again.supervisor.ci_observation.observe_ci",
                    return_value={**self.valid_second_ci(),**change}):
                with self.assertRaises(ExecutionBlocked):
                    finalize_codex_canary(
                        self.broker,{"operation":"codex_canary_complete"})
        self.registry.set_intent.assert_not_called()
        self.assertFalse((self.root/"codex-two-task-complete.json").exists())


if __name__=="__main__":
    unittest.main()
