"""Native ChatGPT browser canary grant is bound to root, never stale legacy DB."""
import os
import tempfile
import unittest
from pathlib import Path

from do_again.supervisor.canary_bootstrap import _protected_parent_state
from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(os.name == "posix", "protected canary root identity uses POSIX paths")
class RootChatGPTBootstrapGateTests(unittest.TestCase):
    def setUp(self):
        self.home="/Users/isolated-operator"
        self.config={
            "operator_home":self.home,"source_sha":"a"*40,"production_ready":False,
            "legacy_authority_path":"/nonexistent/stale-legacy.db",
            "projects":[
                {"account":"_doagain_da","repo":self.home+"/do-again",
                 "github_repository":"Tran-Steven/do-again"},
                {"account":"_doagain_jp","repo":self.home+"/jobpipe",
                 "github_repository":"Tran-Steven/jobpipe"},
            ]}
        self.native={
            "source_sha":"a"*40,"operator_intent":"maintenance","epoch":18,
            "production_ready":False,"canary_authorized":False,
            "enforcement_verified":True,"enforcement_blocker":None,
            "unresolved_executions":[],"inflight_request_ids":[],
        }

    def test_binds_two_native_parents_without_local_sqlite(self):
        history=[]
        def root(repo,packet):
            history.append((repo,packet))
            return dict(self.native)
        state=_protected_parent_state(self.config,rpc=root)
        self.assertEqual(state["epoch"],18)
        self.assertEqual(history,[
            (Path(self.home+"/do-again"),{"operation":"status"}),
            (Path(self.home+"/jobpipe"),{"operation":"status"}),
        ])

    def test_rejects_incomplete_confinement_or_replay(self):
        changes=[
            {"operator_intent":"active"},{"canary_authorized":True},
            {"enforcement_verified":False},{"enforcement_blocker":{"reason":"escape"}},
            {"unresolved_executions":[{"request_id":"old-effect"}]},
            {"inflight_request_ids":["old-effect"]},{"source_sha":"b"*40},
        ]
        for row in changes:
            with self.subTest(change=row),self.assertRaises(ExecutionBlocked):
                _protected_parent_state(self.config,rpc=lambda repo,packet:{**self.native,**row})

    def test_rejects_retargeted_or_missing_jobpipe(self):
        self.config["projects"][1]["repo"]="/somewhere/else"
        with self.assertRaisesRegex(ExecutionBlocked,"project path changed"):
            _protected_parent_state(self.config,rpc=lambda repo,packet:self.native)
        self.config["projects"].pop()
        with self.assertRaisesRegex(ExecutionBlocked,"both protected"):
            _protected_parent_state(self.config,rpc=lambda repo,packet:self.native)


if __name__=="__main__":unittest.main()
