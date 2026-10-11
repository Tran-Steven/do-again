"""Pure offline jobpipe admission checks; never install, start, or submit."""
import copy
import tempfile
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
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.home = str(Path(temp.name).resolve())
        self.repo = Path(self.home) / "jobpipe"
        from do_again.supervisor.authority import project_identity
        self.key = project_identity(self.repo)
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


    def test_effect_authorization_and_resume_are_jobpipe_only(self):
        from contextlib import nullcontext
        from do_again.supervisor.live_canary import effect_authorized
        from do_again.supervisor.macos_server import ProjectBroker
        statuses={self.repo:dict(self.status),
                  Path(self.parent["repo"]):{"intent":"maintenance"},
                  Path(self.sonary["repo"]):{"intent":"maintenance"}}
        registry=SimpleNamespace(status=lambda repo:statuses[repo],
                                 set_intent=Mock(return_value=2))
        broker=SimpleNamespace(config=self.config,project=self.project,
            registry=registry,ledger=SimpleNamespace(pending=lambda key:[]),
            admission=nullcontext,_verified=lambda:True)
        now=datetime.now(timezone.utc).replace(microsecond=0)
        self.config["development_scope"]["issued_at_utc"]=(now-timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.config["development_scope"]["expires_at_utc"]=(now+timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(effect_authorized(broker))
        result=ProjectBroker.operator_intent(broker,"active")
        self.assertEqual(result,{"intent":"active","epoch":2})
        registry.set_intent.assert_called_once_with(self.repo,"active",goal_revision=self.goal)
        for sibling in (self.parent,self.sonary):
            registry.set_intent.reset_mock()
            sibling_broker=SimpleNamespace(**{**vars(broker),
                "project":SimpleNamespace(account=sibling["account"],
                    repo=Path(sibling["repo"]),key=sibling["key"])})
            with self.assertRaises(ExecutionBlocked):
                ProjectBroker.operator_intent(sibling_broker,"active")
            registry.set_intent.assert_not_called()
        statuses[Path(self.sonary["repo"])]={"intent":"active"}
        with self.assertRaises(ExecutionBlocked):
            ProjectBroker.operator_intent(broker,"active")

    def test_immutable_worker_accepts_only_the_sealed_jobpipe_grant(self):
        from do_again.core.schema import OperatorError
        from do_again.supervisor.immutable_worker import validate_worker_context, WORKER_OPERATIONS
        from do_again.supervisor.macos_execution import EXECUTION_ROOT
        cfg=copy.deepcopy(self.config)
        base=Path(self.home)/"sealed"
        cfg.update(schema_version=1,operator_uid=501,
                   python=str(base/"current/runtimes/python/bin/python3"))
        cfg["projects"][0].update(uid=401,gid=401)
        cfg["projects"][1].update(uid=402,gid=402)
        cfg["projects"][2].update(uid=403,gid=403)
        cfg["projects"][1]["worktree"]=str(EXECUTION_ROOT/self.key/"worktree")
        now=datetime.now(timezone.utc).replace(microsecond=0)
        cfg["development_scope"]["issued_at_utc"]=(now-timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cfg["development_scope"]["expires_at_utc"]=(now+timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        status={
            "source_sha":cfg["source_sha"],"operator_intent":"active",
            "production_ready":False,"enforcement_verified":True,
            "enforcement_blocker":None,"unresolved_executions":[],
            "inflight_request_ids":[],"goal_revision":self.goal,"epoch":2,
            "authority":{"repo_head":"d"*40},
            "worktree":cfg["projects"][1]["worktree"],"uid":402,
        }
        policy={"worker_policy_schema":1,"control_branch":"operator-control",
                "allowed_operations":list(WORKER_OPERATIONS)}
        kwargs=dict(installed_root=base,
                    module_path=base/"current/package/do_again/supervisor/immutable_worker.py",
                    interpreter=base/"current/runtimes/python/bin/python3",
                    uid=501,euid=501)
        self.assertEqual(validate_worker_context("jobpipe",cfg,status,policy,**kwargs)[0],self.repo)
        tampered=copy.deepcopy(cfg);tampered["development_scope"]["application_submissions"]=True
        with self.assertRaises(OperatorError):
            validate_worker_context("jobpipe",tampered,status,policy,**kwargs)
        with self.assertRaises(OperatorError):
            validate_worker_context("Sonary",cfg,status,policy,**kwargs)
        with self.assertRaises(OperatorError):
            validate_worker_context("do-again",cfg,status,policy,**kwargs)


if __name__=="__main__":
    unittest.main()
