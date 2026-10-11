"""Sealed offline worker entrypoint regressions; never start a real worker."""
from __future__ import annotations

import copy
import tempfile
import unittest
from argparse import Namespace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.core.broker_executor import BrokerExecutor
from do_again.core.schema import OperatorError, atomic_json
from do_again.service import daemon
from do_again.supervisor.admission import require_worker_active
from do_again.supervisor.authority import project_identity


class OfflineWorkerEntrypointTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.repo = self.home / "jobpipe"
        self.repo.mkdir()
        self.key = project_identity(self.repo)
        self.goal = "offline-worker-20261011"
        self.source = "c" * 40
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.config = {
            "operator_uid": 501, "operator_home": str(self.home),
            "source_sha": self.source, "production_ready": False,
            "projects": [
                {"account": "_doagain_da", "repo": str(self.home / "do-again"), "key": "a" * 64},
                {"account": "_doagain_jp", "repo": str(self.repo), "key": self.key,
                 "uid": 402, "worktree": "/sealed/worktree",
                 "github_repository": "Tran-Steven/jobpipe"},
                {"account": "_doagain_so", "repo": str(self.home / "Sonary"), "key": "b" * 64},
            ],
            "development_scope": {
                "kind": "jobpipe-offline-v1", "source_sha": self.source,
                "project_key": self.key, "goal_revision": self.goal,
                "issued_at_utc": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at_utc": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "network_access": False, "application_submissions": False,
            },
        }
        self.active = {
            "source_sha": self.source, "operator_intent": "active",
            "production_ready": False, "canary_authorized": False,
            "enforcement_verified": True, "enforcement_blocker": None,
            "goal_revision": self.goal, "worktree": "/sealed/worktree",
            "uid": 402, "authority": {"repo_head": "d" * 40},
            "unresolved_executions": [], "inflight_request_ids": [],
            "executables": ["/sealed/python3"], "epoch": 2,
        }
        self.states = {self.repo: self.active}
        for row in self.config["projects"]:
            if row["account"] != "_doagain_jp":
                self.states[Path(row["repo"])] = {
                    "operator_intent": "maintenance", "production_ready": False,
                    "source_sha": self.source, "unresolved_executions": [],
                    "inflight_request_ids": [],
                }
        self.calls = []

    def rpc(self, path, packet):
        self.calls.append((Path(path), dict(packet)))
        if packet.get("operation") == "status":
            return self.states[Path(path)]
        return {"broker_result": "fixture-only"}

    @contextmanager
    def sealed(self):
        with patch("do_again.supervisor.admission.sys.platform", "darwin"), \
             patch("do_again.supervisor.admission.os.getuid", return_value=501), \
             patch("do_again.supervisor.immutable_worker.read_worker_configuration",
                   side_effect=lambda: copy.deepcopy(self.config)):
            yield

    def test_valid_jobpipe_grant_only_with_maintenance_siblings(self):
        with self.sealed():
            self.assertIs(require_worker_active(self.repo, status=self.active, rpc=self.rpc),
                          self.active)
        self.assertEqual([p for p, _ in self.calls],
                         [self.home / "do-again", self.home / "Sonary"])
        self.assertTrue(all(packet == {"operation": "status"} for _, packet in self.calls))

    def test_offline_scope_cannot_activate_another_project_or_expired_grant(self):
        with self.sealed():
            with self.assertRaises(OperatorError):
                require_worker_active(self.home / "Sonary", status=self.active, rpc=self.rpc)
            self.config["development_scope"]["application_submissions"] = True
            with self.assertRaises(OperatorError):
                require_worker_active(self.repo, status=self.active, rpc=self.rpc)
            self.config["development_scope"]["application_submissions"] = False
            self.config["development_scope"]["expires_at_utc"] = "2020-01-01T00:00:00Z"
            with self.assertRaises(OperatorError):
                require_worker_active(self.repo, status=self.active, rpc=self.rpc)

    def test_active_sibling_or_unverified_broker_blocks_without_execution(self):
        with self.sealed():
            self.states[self.home / "Sonary"]["operator_intent"] = "active"
            with self.assertRaisesRegex(OperatorError, "sibling"):
                require_worker_active(self.repo, status=self.active, rpc=self.rpc)
            self.states[self.home / "Sonary"]["operator_intent"] = "maintenance"
            for change in ({"operator_intent": "maintenance"},
                           {"enforcement_verified": False},
                           {"production_ready": None},
                           {"source_sha": "b" * 40},
                           {"inflight_request_ids": ["unresolved"]}):
                with self.subTest(change=change), self.assertRaises(OperatorError):
                    require_worker_active(self.repo, status={**self.active, **change}, rpc=self.rpc)

    def test_offline_broker_executor_dispatches_only_after_sealed_validation(self):
        policy = self.home / "policy.json"
        atomic_json(policy, {"allowed_operations": ["scratch_script"]})
        executor = BrokerExecutor(repo=self.repo, policy_path=policy,
                                  state_dir=self.home / "state", rpc=self.rpc)
        request = {"operation": "scratch_script", "request_id": "offline-worker-0001",
                   "args": {"language": "python", "content": "print('fixture')"},
                   "expected": {"repo_head": "d" * 40}}
        with self.sealed(), patch("do_again.core.broker_executor.sys.platform", "darwin"):
            result = executor.execute(request)
        self.assertEqual(result["result"], {"broker_result": "fixture-only"})
        self.assertEqual([p["operation"] for _, p in self.calls].count("execute"), 1)
        self.calls.clear()
        self.active["enforcement_verified"] = False
        with self.sealed(), patch("do_again.core.broker_executor.sys.platform", "darwin"):
            with self.assertRaises(OperatorError):
                executor.execute(request)
        self.assertFalse(any(p["operation"] == "execute" for _, p in self.calls))

    def test_immutable_daemon_requires_full_worker_callback_set(self):
        args = Namespace(repo=str(self.repo), state_dir=str(self.home))
        admission = Mock()
        effect = Mock()
        control = object()
        with patch.object(daemon, "parse_args", return_value=args), \
             patch("do_again.supervisor.admission.require_worker_active",
                   return_value=self.active) as authority, \
             patch.object(daemon, "runtime_layout",
                          return_value=Namespace(browser_enabled=False)), \
             patch.object(daemon, "_run_admitted_worker", return_value=0) as launch:
            self.assertEqual(daemon.main([], admission_check=admission,
                                         control_transport=control,
                                         browser_effect=effect), 0)
        authority.assert_called_once_with(self.repo)
        admission.assert_called_once()
        launch.assert_called_once()

        # A caller cannot install a permissive callback without the sealed
        # transport and browser effect. That is a legacy production-only path.
        with patch.object(daemon, "parse_args", return_value=args), \
             patch("do_again.supervisor.admission.require_active",
                   side_effect=OperatorError("production-only")) as legacy, \
             patch.object(daemon, "_run_admitted_worker") as launch:
            with self.assertRaisesRegex(OperatorError, "production-only"):
                daemon.main([], admission_check=lambda: True)
            launch.assert_not_called()
            legacy.assert_called_once_with(self.repo)


if __name__ == "__main__":
    unittest.main()
