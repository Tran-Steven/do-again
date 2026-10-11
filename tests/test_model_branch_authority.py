"""Root (not legacy) epoch binding for separate Codex GitHub ref creation."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.model_branch import _check_operator_authority


@unittest.skipUnless(hasattr(os,"getuid"),"macOS native identity contract")
class CodexProtectedRefAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.installed={
            "source_sha":"a"*40,"production_ready":False,
            "operator_uid":502,"operator_home":"/Users/native-operator",
            "projects":[
                {"account":"_doagain_da","repo":"/Users/native-operator/do-again",
                 "github_repository":"Tran-Steven/do-again"},
                {"account":"_doagain_jp","repo":"/Users/native-operator/jobpipe",
                 "github_repository":"Tran-Steven/jobpipe"},
            ],
            "legacy_authority_path":"/not-a-valid-legacy-db",
        }
        self.grant={"parent_epoch":8}
        self.good={
            "source_sha":"a"*40,"operator_intent":"maintenance",
            "production_ready":False,"canary_authorized":False,
            "enforcement_verified":True,"enforcement_blocker":None,
            "unresolved_executions":[],"inflight_request_ids":[],
            "epoch":8,
        }

    def test_uses_two_protected_brokers_and_never_legacy_journal(self):
        checked=[]
        def rpc(repo,packet):
            checked.append((str(repo),packet))
            return self.good
        with patch("do_again.supervisor.model_branch.os.getuid",return_value=502):
            _check_operator_authority(self.installed,self.grant,rpc=rpc)
        self.assertEqual(checked,[
            ("/Users/native-operator/do-again",{"operation":"status"}),
            ("/Users/native-operator/jobpipe",{"operation":"status"}),
        ])

    def test_root_epoch_changed_even_if_legacy_epoch_would_match(self):
        changed={**self.good,"epoch":9}
        with patch("do_again.supervisor.model_branch.os.getuid",return_value=502):
            with self.assertRaisesRegex(ExecutionBlocked,"epoch changed"):
                _check_operator_authority(self.installed,self.grant,
                    rpc=lambda repo,packet:changed)

    def test_any_parent_native_blocker_prevents_first_ref_post(self):
        changes=(
            {"operator_intent":"active"},
            {"canary_authorized":True},
            {"enforcement_verified":False},
            {"enforcement_blocker":{"reason":"native escape"}},
            {"unresolved_executions":[{"request_id":"started"}]},
            {"inflight_request_ids":["test"]},
            {"source_sha":"b"*40},
        )
        with patch("do_again.supervisor.model_branch.os.getuid",return_value=502):
            for change in changes:
                with self.subTest(change=change),self.assertRaises(ExecutionBlocked):
                    _check_operator_authority(self.installed,self.grant,
                        rpc=lambda repo,packet:{**self.good,**change})

    def test_parent_project_scope_retarget_is_denied(self):
        self.installed["projects"][1]["repo"]="/Users/native-operator/other"
        with patch("do_again.supervisor.model_branch.os.getuid",return_value=502):
            with self.assertRaises(ExecutionBlocked):
                _check_operator_authority(self.installed,self.grant,
                    rpc=lambda repo,packet:self.good)


if __name__=="__main__":
    unittest.main()
