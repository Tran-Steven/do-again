"""Offline, zero-model Codex operator grant and GitHub ref bootstrap tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(os.name=="posix", "isolated operator grants require POSIX file ownership")
class CodexBootstrapTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home=Path(temp.name).resolve()
        root=self.home/".do_again/codex-tools/node_modules/.bin"
        root.mkdir(parents=True)
        self.binary=root/"codex"
        self.binary.write_text("sealed synthetic Codex executable fixture")
        self.binary.chmod(0o700)
        self.config={
            "operator_home":str(self.home),"operator_uid":os.getuid(),
            "source_sha":"a"*40,"production_ready":False,
            "projects":[
                {"account":"_doagain_da","repo":str(self.home/"do-again"),
                 "github_repository":"Tran-Steven/do-again"},
                {"account":"_doagain_jp","repo":str(self.home/"jobpipe"),
                 "github_repository":"Tran-Steven/jobpipe"},
            ],
        }
        self.status={
            "source_sha":"a"*40,"operator_intent":"maintenance",
            "production_ready":False,"canary_authorized":False,
            "enforcement_verified":True,"enforcement_blocker":None,
            "unresolved_executions":[],"inflight_request_ids":[],
            "epoch":7,
        }

    def test_exclusive_grant_binds_native_maintenance_and_cli_without_model_calls(self):
        from do_again.supervisor.model_bootstrap import bootstrap_codex_canary
        output=self.home/"fresh.json"
        rpc=Mock(return_value=self.status)
        with patch("do_again.supervisor.model_bootstrap.Path.home",return_value=self.home):
            result=bootstrap_codex_canary(self.config,baseline="b"*40,
                                          output=output,nonce="c"*24,rpc=rpc)
        self.assertEqual(result["transport"],"codex-cli")
        self.assertEqual(result["parent_epoch"],7)
        self.assertEqual(result["model_calls"],0)
        self.assertEqual(result["browser_calls"],0)
        self.assertEqual(rpc.call_count,2)
        value=json.loads(output.read_text())
        self.assertEqual(set(value),{"nonce","baseline","parent_epoch",
            "transport","cli_sha256","expires_at_utc","max_model_calls"})
        self.assertEqual(value["max_model_calls"],2)
        self.assertEqual(output.stat().st_mode & 0o077,0)
        with patch("do_again.supervisor.model_bootstrap.Path.home",return_value=self.home):
            with self.assertRaisesRegex(ExecutionBlocked,"overwrite"):
                bootstrap_codex_canary(self.config,baseline="b"*40,
                    output=output,nonce="c"*24,rpc=rpc)

    def test_started_native_effect_refuses_grant_before_writing(self):
        from do_again.supervisor.model_bootstrap import bootstrap_codex_canary
        output=self.home/"new.json"
        dirty={**self.status,"unresolved_executions":[{"request_id":"old"}]}
        with patch("do_again.supervisor.model_bootstrap.Path.home",return_value=self.home):
            with self.assertRaisesRegex(ExecutionBlocked,"not native-quiescent"):
                bootstrap_codex_canary(self.config,baseline="b"*40,
                    output=output,nonce="c"*24,rpc=lambda repo,pkt:dirty)
        self.assertFalse(output.exists())

    def test_mutable_cli_and_invalid_baseline_fail_before_grant(self):
        from do_again.supervisor.model_bootstrap import bootstrap_codex_canary
        output=self.home/"nope.json"
        with patch("do_again.supervisor.model_bootstrap.Path.home",return_value=self.home):
            with self.assertRaises(ExecutionBlocked):
                bootstrap_codex_canary(self.config,baseline="main",
                    output=output,nonce="c"*24,rpc=lambda repo,pkt:self.status)
            self.binary.chmod(0o722)
            with self.assertRaises(ExecutionBlocked):
                bootstrap_codex_canary(self.config,baseline="b"*40,
                    output=output,nonce="c"*24,rpc=lambda repo,pkt:self.status)
        self.assertFalse(output.exists())


@unittest.skipUnless(os.name=="posix", "original GitHub effect journals use POSIX ownership")
class CodexBranchTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve()
        self.grant=self.root/"grant.json"
        self.nonce="a"*24
        self.baseline="b"*40
        self.grant.write_text(json.dumps({
            "nonce":self.nonce,"baseline":self.baseline,
            "parent_epoch":5,"transport":"codex-cli",
            "cli_sha256":"c"*64,"max_model_calls":2,
            "expires_at_utc":"2026-10-10T22:00:00+00:00",
        }))
        self.grant.chmod(0o600)
        self.name="refs/heads/do-again/canary-"+self.nonce+"/control"

    def test_single_post_and_remote_readback_with_original_reservation(self):
        from do_again.supervisor.model_branch import provision_codex_control_branch
        calls=[]
        def api(method,endpoint,body=None):
            calls.append((method,endpoint))
            if len(calls)==1:return 404,{}
            if len(calls)==2:return 201,{"ref":self.name,
                "object":{"sha":self.baseline}}
            return 200,{"ref":self.name,"object":{"sha":self.baseline}}
        journal=self.root/"journals"
        with patch("do_again.supervisor.model_branch._check_operator_authority"),patch(
                "do_again.supervisor.model_branch._api",side_effect=api):
            outcome=provision_codex_control_branch(
                grant=self.grant,installed={},journal_root=journal)
        self.assertFalse(outcome["reconciled"])
        self.assertEqual([x[0] for x in calls],["GET","POST","GET"])
        record=json.loads((journal/(self.nonce+".json")).read_text())
        self.assertEqual(record["phase"],"verified")

    def test_ambiguous_post_never_reissues_and_recovery_is_get_only(self):
        from do_again.supervisor.model_branch import provision_codex_control_branch
        journal=self.root/"journals"
        def first(method,endpoint,body=None):
            if method=="GET":return 404,{}
            raise ExecutionBlocked("GitHub response uncertain")
        with patch("do_again.supervisor.model_branch._check_operator_authority"),patch(
                "do_again.supervisor.model_branch._api",side_effect=first):
            with self.assertRaises(ExecutionBlocked):
                provision_codex_control_branch(
                    grant=self.grant,installed={},journal_root=journal)
        ticket=journal/(self.nonce+".json")
        self.assertEqual(json.loads(ticket.read_text())["phase"],"publication_reserved")
        with patch("do_again.supervisor.model_branch._check_operator_authority"),patch(
                "do_again.supervisor.model_branch._api",return_value=(404,{})) as api:
            with self.assertRaises(ExecutionBlocked):
                provision_codex_control_branch(
                    grant=self.grant,installed={},journal_root=journal)
            self.assertEqual(api.call_count,1)
            self.assertEqual(api.call_args.args[0],"GET")

    def test_unreserved_reconciliation_never_writes_github(self):
        from do_again.supervisor.model_branch import provision_codex_control_branch
        with patch("do_again.supervisor.model_branch._check_operator_authority"),patch(
                "do_again.supervisor.model_branch._api") as api:
            with self.assertRaisesRegex(ExecutionBlocked,"no original reserved"):
                provision_codex_control_branch(grant=self.grant,installed={},
                    journal_root=self.root/"empty",reconcile_only=True)
        api.assert_not_called()


if __name__=="__main__":
    unittest.main()
