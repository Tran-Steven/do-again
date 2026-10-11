"""Offline strict root-installed Codex canary grant regressions."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from do_again.model_grant import sealed_codex_canary, bound_model_request
from do_again.supervisor.macos_execution import ExecutionBlocked


class CodexCanaryGrantTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc)
        self.config = {
            "schema_version": 1,
            "production_ready": False,
            "source_sha": "1" * 40,
            "operator_home": "/Users/isolated-operator",
            "projects": [
                {"account": "_doagain_da", "repo": "/Users/isolated-operator/do-again",
                 "github_repository": "Tran-Steven/do-again"},
                {"account": "_doagain_jp", "repo": "/Users/isolated-operator/jobpipe",
                 "github_repository": "Tran-Steven/jobpipe"},
            ],
            "codex_canary": {
                "nonce": "a" * 24, "baseline": "b" * 40,
                "parent_epoch": 3, "transport": "codex-cli",
                "cli_sha256": "c" * 64,
                "expires_at_utc": (self.now + timedelta(minutes=80)).isoformat(),
                "max_model_calls": 2,
            },
        }

    def check(self, *, changed=None, now=None):
        cfg=copy.deepcopy(self.config)
        if changed:
            cfg.update(changed)
        return sealed_codex_canary(cfg, now=now or self.now)

    def test_valid_grant_yields_immutable_no_browser_scope(self):
        scope=self.check()
        self.assertEqual(scope.nonce, "a"*24)
        self.assertEqual(scope.source_sha, "1"*40)
        self.assertEqual(scope.baseline, "b"*40)
        self.assertEqual(scope.cli_sha256, "c"*64)
        self.assertEqual(scope.max_model_calls, 2)
        self.assertEqual(scope.parent_epoch, 3)
        self.assertEqual(scope.control_branch, "do-again/canary-"+ "a"*24 + "/control")
        self.assertFalse(hasattr(scope, "chat_url"))
        self.assertFalse(hasattr(scope, "binding_identity"))

    def test_rejects_old_browser_grant_or_production(self):
        for change in ({"production_ready": True},
                       {"live_canary": {"nonce": "a"*24}},
                       {"codex_canary": None}):
            with self.subTest(change=change), self.assertRaises(ExecutionBlocked):
                self.check(changed=change)

    def test_rejects_broad_grants_or_mismatched_pinned_identity(self):
        mutations = [
            {"extra": True},
            {"transport": "browser"},
            {"transport": "api"},
            {"nonce": "../escape"},
            {"nonce": "A"*24},
            {"baseline": "main"},
            {"parent_epoch": True},
            {"parent_epoch": 0},
            {"cli_sha256": "0"*63},
            {"max_model_calls": 10},
            {"max_model_calls": 0},
            {"expires_at_utc": "never"},
            {"expires_at_utc": "2026-10-10T20:30:00"},
            {"expires_at_utc": (self.now + timedelta(hours=5)).isoformat()},
            {"expires_at_utc": (self.now - timedelta(seconds=1)).isoformat()},
        ]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(ExecutionBlocked):
                cfg=copy.deepcopy(self.config)
                cfg["codex_canary"].update(change)
                sealed_codex_canary(cfg,now=self.now)

    def test_rejects_changed_parent_or_sibling_scope(self):
        for variant in ("bad-home", "bad-account", "bad-repo", "bad-github", "bad-source"):
            cfg=copy.deepcopy(self.config)
            if variant=="bad-home":cfg["operator_home"]="/other"
            if variant=="bad-account":cfg["projects"][1]["account"]="_doagain_da"
            if variant=="bad-repo":cfg["projects"][0]["repo"]="/tmp/do-again"
            if variant=="bad-github":cfg["projects"][1]["github_repository"]="other/repo"
            if variant=="bad-source":cfg["source_sha"]="bad"
            with self.subTest(variant=variant),self.assertRaises(ExecutionBlocked):
                sealed_codex_canary(cfg,now=self.now)

    def test_request_is_fixed_to_two_tasks_and_typed_stage(self):
        scope=self.check()
        operations={"edit":"scratch_script","test":"run_tests",
                    "commit":"git_commit","publish":"git_publish","ci":"ci_observe"}
        for task in (1,2):
            for stage,operation in operations.items():
                request={"request_id":scope.request_prefix+str(task)+"-"+stage,
                         "operation":operation,
                         "expected":{"repo_head":scope.baseline}}
                self.assertTrue(bound_model_request(scope,task=task,stage=stage,request=request))
        bad={"request_id":scope.request_prefix+"3-edit",
             "operation":"scratch_script","expected":{"repo_head":scope.baseline}}
        with self.assertRaises(ExecutionBlocked):
            bound_model_request(scope,task=3,stage="edit",request=bad)
        with self.assertRaises(ExecutionBlocked):
            bound_model_request(scope,task=1,stage="edit",
                               request={**bad,"request_id":scope.request_prefix+"1-edit",
                                        "expected":{"repo_head":"f"*40}})
        with self.assertRaises(ExecutionBlocked):
            bound_model_request(scope,task=1,stage="edit",
                               request={**bad,"request_id":scope.request_prefix+"1-edit",
                                        "operation":"git_publish"})
        with self.assertRaises(ExecutionBlocked):
            bound_model_request(scope,task=1,stage="edit-repair",request=bad)


if __name__=="__main__":
    unittest.main()
