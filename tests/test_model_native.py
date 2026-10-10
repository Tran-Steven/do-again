"""Sealed Codex root child rejects sibling, browser and direct execution scope."""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace

from do_again.model_canary import prepare_canary_edit
from do_again.supervisor.model_native import codex_project,CodexCanaryAuthority
from do_again.supervisor.control_history import codex_entries
from do_again.supervisor.macos_execution import ExecutionBlocked


class CodexRootChildTests(unittest.TestCase):
    nonce="a"*24
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home=Path(self.temp.name).resolve()
        self.config={
            "source_sha":"1"*40,"operator_home":str(home),"production_ready":False,
            "projects":[
                {"account":"_doagain_da","repo":str(home/"do-again"),
                 "github_repository":"Tran-Steven/do-again","key":"oldkey","uid":410,"gid":410,
                 "worktree":"/private/var/do-again-execution/oldkey/worktree",
                 "executables":["/usr/bin/python3"]},
                {"account":"_doagain_jp","repo":str(home/"jobpipe"),
                 "github_repository":"Tran-Steven/jobpipe"},
            ],
            "codex_canary":{
                "nonce":self.nonce,"baseline":"2"*40,
                "parent_epoch":3,"transport":"codex-cli",
                "cli_sha256":"3"*64,"max_model_calls":2,
                "expires_at_utc":(datetime.now(timezone.utc)+timedelta(minutes=70)).isoformat(),
            },
        }
        self.scope,self.parent_project,self.project=codex_project(self.config)
        self.parent=SimpleNamespace(
            config=self.config,registry=SimpleNamespace(status=lambda repo:
                {"intent":"maintenance","epoch":3}),
            project=SimpleNamespace(repo=Path(self.parent_project["repo"]),key="parent"),
            ledger=SimpleNamespace(pending=lambda key:[]),
            _verified=lambda:True,_inflight=lambda:[],
        )
        self.broker=SimpleNamespace(
            config=self.config,
            registry=SimpleNamespace(status=lambda repo:{"intent":"active","epoch":4,
                "goal_revision":"codex-canary-"+self.nonce}),
            project=SimpleNamespace(repo=Path(self.project["repo"]),key=self.project["key"]),
            _verified=lambda:True,
            state=home/"state",
        )
        self.authority=CodexCanaryAuthority(self.broker,self.parent,self.scope)
        self.request=prepare_canary_edit({
            "implementation":"def canonical_label(text):\n    return '-'.join(text.strip().lower().split())\n",
            "tests":"import unittest\nfrom canary_live_"+self.nonce+
                    " import canonical_label\nclass TestLabel(unittest.TestCase):\n"
                    "    def test_value(self):\n"
                    "        self.assertEqual(canonical_label('A'), 'a')\n",
        },nonce=self.nonce,task=1,expected_head="2"*40)
        self.reqpath="automation/do_again/requests/"+self.request["request_id"]+".json"

    def test_child_isolated_from_parent_and_old_control(self):
        self.assertNotEqual(self.project["repo"],self.parent_project["repo"])
        self.assertNotEqual(self.project["key"],self.parent_project["key"])
        self.assertEqual(self.project["control_branch"],self.scope.control_branch)
        self.assertEqual(self.project["github_repository"],"Tran-Steven/do-again")
        self.assertIn(self.nonce,self.project["repo"])

    def test_only_first_canonical_edit_request_is_eligible(self):
        self.authority.request(self.request,self.reqpath)
        self.authority.packet({"operation":"codex_request_publish",
            "request_id":"codex-publish-"+self.nonce+"-1-edit","value":self.request})
        with self.assertRaises(ExecutionBlocked):
            self.authority.request(self.request,self.reqpath.replace("-1-edit","-2-edit"))
        with self.assertRaises(ExecutionBlocked):
            self.authority.request({**self.request,"operation":"git_publish"},self.reqpath)
        with self.assertRaises(ExecutionBlocked):
            self.authority.request(self.request,
                "automation/do_again/requests/other-"+self.nonce+".json")

    def test_direct_script_or_task_two_packets_do_not_bypass_broker(self):
        for packet in (
            {"operation":"execute","request_id":self.request["request_id"],
             "argv":["/usr/bin/python3","-c","print(123)"],"timeout":120},
            {"operation":"codex_request_publish",
             "request_id":"codex-publish-"+self.nonce+"-2-edit","value":self.request},
            {"operation":"control_publish","path":self.reqpath},
            {"operation":"control_publish","path":"automation/do_again/requests/new.json"},
            {"operation":"control_publish","path":"automation/do_again/claims/canary-"
             +self.nonce+"-2-test.json"},
        ):
            with self.subTest(operation=packet.get("operation"),
                              request_id=packet.get("request_id")),self.assertRaises(ExecutionBlocked):
                self.authority.packet(packet)

    def test_sibling_started_native_execution_blocks_codex_child_authority(self):
        from do_again.supervisor.authority import project_identity
        sibling_key=project_identity(Path(self.config["projects"][1]["repo"]))
        self.parent.ledger.pending=lambda key: (
            [{"request_id":"started-uncertain"}] if key==sibling_key else [])
        with self.assertRaisesRegex(ExecutionBlocked,"sibling"):
            self.authority.parent_check()

    def test_task_one_remote_control_filter_disallows_other_canary_and_task_two(self):
        base="automation/do_again/requests/"
        entries={
            base+"canary-"+self.nonce+"-1-edit.json":"a"*40,
            base+"canary-"+self.nonce+"-1-test.json":"a"*40,
            base+"canary-"+self.nonce+"-2-edit.json":"a"*40,
            base+"canary-"+self.nonce+"-1-edit-repair.json":"a"*40,
            base+"canary-"+("b"*24)+"-1-edit.json":"a"*40,
            "automation/do_again/receipts/canary-"+self.nonce+"-1-edit.json":"a"*40,
        }
        actual=codex_entries(SimpleNamespace(codex=self.authority),entries)
        self.assertEqual(set(actual),{
            base+"canary-"+self.nonce+"-1-edit.json",
            base+"canary-"+self.nonce+"-1-test.json",
            "automation/do_again/receipts/canary-"+self.nonce+"-1-edit.json",
        })

    def test_reused_old_browser_grant_cannot_create_codex_child(self):
        self.config["live_canary"]={"nonce":"b"*24}
        with self.assertRaises(ExecutionBlocked):
            codex_project(self.config)


if __name__=="__main__":
    unittest.main()
