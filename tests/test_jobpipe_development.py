"""Pure offline jobpipe admission checks; never install, start, or submit."""
import copy
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.supervisor.jobpipe_development import (
    require_jobpipe_grant, require_broker_jobpipe_development,
)
from do_again.supervisor.macos_execution import ExecutionBlocked


class OfflineJobpipeDevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 11, 16, 0, tzinfo=timezone.utc)
        self.home = "/Users/test-operator"
        self.repo = Path(self.home) / "jobpipe"
        self.key = hashlib.sha256(str(self.repo).encode()).hexdigest()
        self.goal = "jobpipe-development-20261011"
        self.jobpipe = {"account": "_doagain_jp", "repo": str(self.repo), "key": self.key}
        self.parent = {"account": "_doagain_da", "repo": self.home+"/do-again", "key": "a"*64}
        self.sonary = {"account": "_doagain_so", "repo": self.home+"/Sonary", "key": "b"*64}
        self.config = {"production_ready": False, "operator_home": self.home,
                       "source_sha": "c"*40, "projects": [self.parent,self.jobpipe,self.sonary],
                       "development_scope": {
                           "kind": "jobpipe-offline-v1", "source_sha": "c"*40,
                           "project_key": self.key, "goal_revision": self.goal,
                           "issued_at_utc": "2026-10-11T15:00:00Z",
                           "expires_at_utc": "2026-10-12T15:00:00Z",
                           "network_access": False, "application_submissions": False}}
        self.project = SimpleNamespace(account="_doagain_jp",repo=self.repo,key=self.key)
        self.status = {"intent":"maintenance","goal_revision":self.goal}

    def allow(self, config=None, project=None, status=None, at=None):
        return require_jobpipe_grant(config if config is not None else self.config,
                                     project if project is not None else self.project,
                                     status if status is not None else self.status,
                                     now=at if at is not None else self.now)

    def test_only_exact_jobpipe_offline_scope_is_admitted(self):
        self.assertEqual(self.allow()["kind"],"jobpipe-offline-v1")
        config=copy.deepcopy(self.config)
        config["projects"].pop()
        self.assertEqual(self.allow(config)["project_key"],self.key)
        config["projects"].append(self.sonary)
        self.assertEqual(self.allow(config)["project_key"],self.key)

    def test_wrong_projects_and_sibling_impersonation_rejected(self):
        for account,repo,key in [
            ("_doagain_da",Path(self.home)/"do-again","a"*64),
            ("_doagain_so",Path(self.home)/"Sonary","b"*64),
            ("_doagain_jp",Path(self.home)/"Sonary",self.key),
            ("_doagain_jp",self.repo,"d"*64)]:
            with self.subTest(account=account,repo=str(repo)):
                with self.assertRaises(ExecutionBlocked):
                    self.allow(project=SimpleNamespace(account=account,repo=repo,key=key))
        config=copy.deepcopy(self.config)
        config["projects"][1]["repo"]=self.home+"/different"
        with self.assertRaises(ExecutionBlocked):self.allow(config)
        config=copy.deepcopy(self.config)
        config["projects"].append(dict(self.jobpipe))
        with self.assertRaises(ExecutionBlocked):self.allow(config)

    def test_no_production_network_application_submission_or_canary_overlap(self):
        for key,value in [
            ("production_ready",True),("live_canary",{"nonce":"fake"}),
            ("codex_canary",{"nonce":"fake"}),("source_sha","d"*40)]:
            config=copy.deepcopy(self.config);config[key]=value
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):self.allow(config)
        for key,value in [
            ("network_access",True),("application_submissions",True),
            ("kind","jobpipe-online-v1"),("source_sha","d"*40),
            ("project_key","d"*64),("goal_revision","production")]:
            config=copy.deepcopy(self.config);config["development_scope"][key]=value
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):self.allow(config)

    def test_rejects_unknown_fields_missing_fields_and_bad_types(self):
        for key in self.config["development_scope"]:
            cfg=copy.deepcopy(self.config);cfg["development_scope"].pop(key)
            with self.subTest(missing=key),self.assertRaises(ExecutionBlocked):self.allow(cfg)
        cfg=copy.deepcopy(self.config);cfg["development_scope"]["allow_all"]=True
        with self.assertRaises(ExecutionBlocked):self.allow(cfg)
        for key,value in [("network_access",0),("application_submissions",None)]:
            cfg=copy.deepcopy(self.config);cfg["development_scope"][key]=value
            with self.assertRaises(ExecutionBlocked):self.allow(cfg)

    def test_time_bounds_are_enforced_without_implicit_renewal(self):
        for at in [self.now-timedelta(days=1),self.now+timedelta(days=30)]:
            with self.subTest(at=at),self.assertRaises(ExecutionBlocked):
                self.allow(at=at)
        for field,bad in [("issued_at_utc","2026-10-11T15:00:00+00:00"),
                           ("expires_at_utc","2026-11-11T15:00:00Z"),
                           ("expires_at_utc","2026-10-11T14:00:00Z"),
                           ("expires_at_utc","bad")]:
            cfg=copy.deepcopy(self.config);cfg["development_scope"][field]=bad
            with self.subTest(field=field,bad=bad),self.assertRaises(ExecutionBlocked):
                self.allow(cfg)

    def test_goal_revision_binding_fails_closed(self):
        with self.assertRaises(ExecutionBlocked):
            self.allow(status={"goal_revision":"previous-jobpipe-goal"})

    def test_sibling_maintenance_and_pending_execution_fences(self):
        statuses={self.repo:self.status,
                  Path(self.parent["repo"]):{"intent":"maintenance"},
                  Path(self.sonary["repo"]):{"intent":"maintenance"}}
        pending={}
        broker=SimpleNamespace(config=self.config,project=self.project,
            registry=SimpleNamespace(status=lambda r:statuses[r]),
            ledger=SimpleNamespace(pending=lambda key:pending.get(key,[])))
        self.assertTrue(require_broker_jobpipe_development(broker,now=self.now))
        statuses[Path(self.sonary["repo"])]={"intent":"active"}
        with self.assertRaises(ExecutionBlocked):require_broker_jobpipe_development(broker,now=self.now)
        statuses[Path(self.sonary["repo"])]={"intent":"maintenance"}
        pending[self.parent["key"]]=[{"request_id":"uncertain"}]
        with self.assertRaises(ExecutionBlocked):require_broker_jobpipe_development(broker,now=self.now)


if __name__=="__main__":
    unittest.main()
