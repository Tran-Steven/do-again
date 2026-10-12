from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.supervisor.dual_development import require_dual_offline_grant
from do_again.supervisor.macos_execution import ExecutionBlocked


class DualOfflineGrantTests(unittest.TestCase):
    def setUp(self):
        now=datetime(2026,10,12,2,0,tzinfo=timezone.utc)
        self.now=now
        home=str((Path(tempfile.gettempdir())/"dual-operator-fixture").resolve())
        self.projects=[]
        scopes=[]
        self.statuses={}
        for account,name in (("_doagain_da","do-again"),("_doagain_jp","jobpipe"),
                             ("_doagain_so","Sonary")):
            repo=Path(home)/name
            key=hashlib.sha256(os.fsencode(repo.resolve())).hexdigest()
            self.projects.append({"account":account,"repo":str(repo),
                                  "key":key,"source_sha":"c"*40})
            self.statuses[account]={"operator_intent":"maintenance","goal_revision":"review-v1",
                                    "production_ready":False,"source_sha":"b"*40,
                                    "enforcement_verified":True,"enforcement_blocker":None,
                                    "authority":{"repo_head":"c"*40},
                                    "unresolved_executions":[],"inflight_request_ids":[]}
            if account!="_doagain_so":
                scopes.append({"account":account,"project_key":key,
                               "goal_revision":"review-v1"})
        self.config={"operator_home":home,"source_sha":"b"*40,
                     "production_ready":False,"projects":self.projects,
                     "live_canary":None,"codex_canary":None,
                     "development_scope":{
                         "kind":"offline-dual-v1","source_sha":"b"*40,
                         "issued_at_utc":now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "expires_at_utc":(now+timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "projects":scopes,"max_parallel_workers":2,
                         "network_access":False,"application_submissions":False}}
        jp=self.projects[1]
        self.jp=SimpleNamespace(account=jp["account"],key=jp["key"],repo=Path(jp["repo"]))
        da=self.projects[0]
        self.da=SimpleNamespace(account=da["account"],key=da["key"],repo=Path(da["repo"]))

    def check(self, project=None):
        return require_dual_offline_grant(
            self.config,project or self.jp,self.statuses,now=self.now)

    def test_only_two_exact_scopes_are_admitted_with_sonary_held(self):
        self.assertEqual(self.check()["kind"],"offline-dual-v1")
        self.statuses["_doagain_da"]["operator_intent"]="active"
        self.assertEqual(self.check(self.da)["max_parallel_workers"],2)
        self.statuses["_doagain_jp"]["operator_intent"]="active"
        self.assertEqual(self.check()["kind"],"offline-dual-v1")

    def test_real_native_status_contract_not_synthetic_intent_alias(self):
        # macos_server._handle({'operation':'status'}) returns
        # operator_intent; registry.status() independently uses intent.
        self.assertEqual(self.statuses["_doagain_da"]["operator_intent"], "maintenance")
        self.assertEqual(self.check(self.da)["kind"], "offline-dual-v1")
        native=self.statuses["_doagain_da"]
        native["intent"]="active"
        self.assertEqual(self.check(self.da)["kind"], "offline-dual-v1")
        del native["operator_intent"]
        with self.assertRaises(ExecutionBlocked):
            self.check(self.da)

    def test_exact_dual_grant_admits_both_worker_entrypoints_with_real_broker_shape(self):
        from do_again.supervisor.admission import require_worker_active
        from do_again.core.schema import OperatorError
        # This exercise uses no real macOS socket. Both parent statuses match
        # the actual root broker's operator_intent/status response contract.
        now=datetime.now(timezone.utc).replace(microsecond=0)
        grant=self.config["development_scope"]
        grant["issued_at_utc"]=(now-timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        grant["expires_at_utc"]=(now+timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.config["operator_uid"]=501
        paths={}
        for p in self.projects:
            account=p["account"]
            p["uid"]={"_doagain_da":401,"_doagain_jp":402,"_doagain_so":403}[account]
            p["worktree"]="/root-sealed/"+account
            p["github_repository"]={"_doagain_da":"Tran-Steven/do-again",
                                    "_doagain_jp":"Tran-Steven/jobpipe",
                                    "_doagain_so":"Tran-Steven/Sonary"}[account]
            self.statuses[account].update(
                worktree=p["worktree"],uid=p["uid"],canary_authorized=False)
            paths[Path(p["repo"])]=account
        self.statuses["_doagain_da"]["operator_intent"]="active"
        self.statuses["_doagain_jp"]["operator_intent"]="active"
        seen=[]
        def rpc(repo,packet):
            self.assertEqual(packet,{"operation":"status"})
            seen.append(paths[Path(repo)])
            return self.statuses[paths[Path(repo)]]
        with patch("do_again.supervisor.admission.sys.platform","darwin"), \
             patch("do_again.supervisor.admission.os.getuid",return_value=501,create=True), \
             patch("do_again.supervisor.immutable_worker.read_worker_configuration",
                   return_value=self.config):
            self.assertIs(require_worker_active(self.da.repo,status=self.statuses["_doagain_da"],
                                                rpc=rpc),self.statuses["_doagain_da"])
            self.assertIs(require_worker_active(self.jp.repo,status=self.statuses["_doagain_jp"],
                                                rpc=rpc),self.statuses["_doagain_jp"])
            self.assertIn("_doagain_so",seen)
            self.statuses["_doagain_so"]["operator_intent"]="active"
            with self.assertRaises(OperatorError):
                require_worker_active(self.da.repo,status=self.statuses["_doagain_da"],rpc=rpc)
            self.statuses["_doagain_so"]["operator_intent"]="maintenance"
            self.statuses["_doagain_jp"]["unresolved_executions"]=[
                {"request_id":"browser-48b9fd1c09734faf98f77944a64581f2"}]
            with self.assertRaises(OperatorError):
                require_worker_active(self.da.repo,status=self.statuses["_doagain_da"],rpc=rpc)

    def test_old_jobpipe_only_grant_is_never_treated_as_dual(self):
        self.config["development_scope"]["kind"]="jobpipe-offline-v1"
        with self.assertRaises(ExecutionBlocked):
            self.check()

    def test_actual_unresolved_browser_tick_blocks_both_parents(self):
        self.statuses["_doagain_jp"]["unresolved_executions"]=[
            {"request_id":"browser-48b9fd1c09734faf98f77944a64581f2"}]
        with self.assertRaises(ExecutionBlocked):
            self.check(self.da)
        with self.assertRaises(ExecutionBlocked):
            self.check(self.jp)

    def test_production_network_or_application_effects_are_rejected(self):
        for key,bad in (("production_ready",True),("live_canary",{"nonce":"x"}),
                        ("codex_canary",{})):
            with self.subTest(key=key):
                original=self.config[key]
                self.config[key]=bad
                with self.assertRaises(ExecutionBlocked):self.check()
                self.config[key]=original
        scope=self.config["development_scope"]
        for key,bad in (("network_access",True),("application_submissions",True),
                        ("max_parallel_workers",3),("max_parallel_workers",True)):
            with self.subTest(key=key):
                original=scope[key]
                scope[key]=bad
                with self.assertRaises(ExecutionBlocked):self.check()
                scope[key]=original

    def test_retargeting_or_reordering_grant_fails(self):
        scope=self.config["development_scope"]
        scope["projects"].reverse()
        with self.assertRaises(ExecutionBlocked):self.check()
        scope["projects"].reverse()
        scope["projects"][0]["project_key"]="0"*64
        with self.assertRaises(ExecutionBlocked):self.check()

    def test_sonary_or_unapproved_caller_never_gets_authority(self):
        self.statuses["_doagain_so"]["operator_intent"]="active"
        with self.assertRaises(ExecutionBlocked):self.check()
        self.statuses["_doagain_so"]["operator_intent"]="maintenance"
        sonary=self.projects[-1]
        caller=SimpleNamespace(account=sonary["account"],repo=Path(sonary["repo"]),key=sonary["key"])
        with self.assertRaises(ExecutionBlocked):self.check(caller)

    def test_unverified_or_stale_native_broker_status_is_rejected(self):
        status=self.statuses["_doagain_da"]
        for field,bad in (("source_sha","f"*40),
                          ("enforcement_verified",False),
                          ("enforcement_blocker",{"reason":"test"}),
                          ("production_ready",True),
                          ("authority",{"repo_head":"0"*40})):
            with self.subTest(field=field):
                old=status[field]
                status[field]=bad
                with self.assertRaises(ExecutionBlocked):self.check()
                status[field]=old

    def test_lease_expiry_goal_revisions_and_pending_effects(self):
        scope=self.config["development_scope"]
        with self.assertRaises(ExecutionBlocked):
            require_dual_offline_grant(
                self.config,self.jp,self.statuses,now=self.now+timedelta(days=10))
        self.statuses["_doagain_da"]["goal_revision"]="changed"
        with self.assertRaises(ExecutionBlocked):self.check()
        self.statuses["_doagain_da"]["goal_revision"]="review-v1"
        self.statuses["_doagain_da"]["inflight_request_ids"]=["unresolved"]
        with self.assertRaises(ExecutionBlocked):self.check()

    def test_incomplete_or_ambiguous_sibling_observations_fail(self):
        self.statuses.pop("_doagain_so")
        with self.assertRaises(ExecutionBlocked):self.check()
        self.statuses["_doagain_so"]={"operator_intent":"maintenance",
                                    "unresolved_executions":[],"inflight_request_ids":[]}
        self.statuses["_doagain_jp"]["operator_intent"]="paused"
        with self.assertRaises(ExecutionBlocked):self.check()


if __name__=="__main__":
    unittest.main()
