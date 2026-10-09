from __future__ import annotations

from do_again.core.executor import LocalExecutor

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.service import rollout
from do_again.core.agent import Agent


class StagedRuntimeRolloutTests(unittest.TestCase):
    def setUp(self):
        # Exercise legacy cutover mechanics only in isolated temporary fixtures.
        from unittest.mock import patch
        admission = patch("do_again.supervisor.admission.reject_legacy_runtime")
        admission.start()
        self.addCleanup(admission.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        os.environ["DO_AGAIN_HOME"] = str(self.home)
        self.addCleanup(os.environ.pop, "DO_AGAIN_HOME", None)
        (self.repo / "do-again.toml").write_text("[do_again]\nbrowser = false\n")
        self.layout = rollout.runtime_layout(self.repo)
        self.layout.control_worktree.mkdir(parents=True)
        (self.layout.control_worktree / "automation/do_again/requests").mkdir(parents=True)
        (self.layout.control_worktree / "automation/do_again/receipts").mkdir(parents=True)
        self.layout.state_dir.mkdir(parents=True)
        live = self.layout.runtime_source / "do_again"
        (live / "service").mkdir(parents=True)
        (live / "service" / "daemon.py").write_text("OLD=1\n")
        self.status = {"installed": True, "running": True, "pid": 100}

    def assess(self, rows=None):
        with patch.object(rollout, "service_status", return_value=self.status):
            return rollout.assess_upgrade(self.repo, process_rows=rows or [])

    def write_agent_ledger(self, request_id="r1", state="started"):
        policy = self.root / "policy.json"
        policy.write_text('{"allowed_operations": []}')
        agent = Agent(repo=self.repo, control_worktree=self.layout.control_worktree,
                      branch="operator-control", policy_path=policy,
                      state_dir=self.layout.state_dir, executor=LocalExecutor(repo=self.repo, policy_path=policy, state_dir=self.layout.state_dir), )
        agent.write_ledger(request_id, {"state": state})

    def test_offline_upgrade_stages_stopped_runtime_preserving_pending_request(self):
        self.status = {"installed": True, "running": False, "pid": None}
        request = self.layout.control_worktree / "automation/do_again/requests/r1.json"
        request.write_text('{"request_id":"r1"}')
        marker = self.layout.state_dir / "keep.json"
        marker.write_text('{"user_state":"preserve"}')
        stop, restart = Mock(), Mock()
        with patch.object(rollout, "find_repo", return_value=self.repo), patch.object(
            rollout, "service_status", return_value=self.status
        ):
            report=rollout.staged_upgrade(self.repo,apply=True,offline=True,
                                          process_rows=[],stop=stop,restart=restart)
        self.assertTrue(report["applied"])
        self.assertFalse(report["deferred"])
        self.assertTrue(report["verification"]["hash_matches"])
        self.assertFalse(report["verification"]["service"]["running"])
        self.assertTrue(request.exists())
        self.assertTrue(marker.exists())
        tx=json.loads((self.layout.state_dir/"runtime_upgrade.json").read_text())
        self.assertTrue(tx["offline"])
        self.assertEqual(tx["queued_requests_preserved"], ["r1"])
        stop.assert_not_called()
        restart.assert_not_called()

    def test_offline_upgrade_refuses_running_service(self):
        stop, restart=Mock(),Mock()
        with patch.object(rollout,"find_repo",return_value=self.repo),patch.object(
            rollout,"service_status",return_value=self.status
        ):
            report=rollout.staged_upgrade(self.repo,apply=True,offline=True,
                                          process_rows=[],stop=stop,restart=restart)
        self.assertFalse(report["applied"])
        self.assertEqual(report["effective_blockers"][0]["kind"],"offline_requires_stopped_service")
        stop.assert_not_called()
        restart.assert_not_called()

    def test_offline_upgrade_refuses_started_ledger(self):
        self.status={"installed":True,"running":False,"pid":None}
        q=self.layout.control_worktree/"automation/do_again/requests/r1.json"
        q.write_text('{}')
        self.write_agent_ledger()
        with patch.object(rollout,"find_repo",return_value=self.repo),patch.object(
            rollout,"service_status",return_value=self.status
        ):
            report=rollout.staged_upgrade(self.repo,apply=True,offline=True,process_rows=[])
        self.assertFalse(report["applied"])
        self.assertIn("started_requests",{x["kind"] for x in report["effective_blockers"]})

    def test_offline_upgrade_refuses_outbox_unacknowledged(self):
        self.status={"installed":True,"running":False,"pid":None}
        outbox=self.layout.state_dir/"browser_outbox"
        outbox.mkdir()
        (outbox/"r1.json").write_text('{}')
        with patch.object(rollout,"find_repo",return_value=self.repo),patch.object(
            rollout,"service_status",return_value=self.status
        ):
            report=rollout.staged_upgrade(self.repo,apply=True,offline=True,process_rows=[])
        self.assertFalse(report["applied"])
        self.assertIn("browser_delivery_pending",{x["kind"] for x in report["effective_blockers"]})

    def test_offline_upgrade_defers_when_execution_claim_appears_during_staging(self):
        self.status={"installed":True,"running":False,"pid":None}
        q=self.layout.control_worktree/"automation/do_again/requests/r1.json"
        q.write_text('{}')
        original=rollout._stage_source
        def stage_then_claim(layout,tx):
            result=original(layout,tx)
            self.write_agent_ledger()
            return result
        stop,restart=Mock(),Mock()
        with patch.object(rollout,"find_repo",return_value=self.repo),patch.object(
            rollout,"service_status",return_value=self.status
        ),patch.object(rollout,"_stage_source",side_effect=stage_then_claim):
            report=rollout.staged_upgrade(self.repo,apply=True,offline=True,process_rows=[],
                                          stop=stop,restart=restart)
        self.assertFalse(report["applied"])
        self.assertTrue(report["deferred"])
        self.assertIn("started_requests",{x["kind"] for x in report["preflight"]["blockers"]})
        self.assertEqual((self.layout.runtime_source/"do_again/service/daemon.py").read_text(),"OLD=1\n")
        stop.assert_not_called()
        restart.assert_not_called()

    def test_pending_request_blocks_upgrade(self):
        req = self.layout.control_worktree / "automation/do_again/requests/r1.json"
        req.write_text("{}")
        value = self.assess()
        self.assertFalse(value["safe"])
        self.assertIn("pending_requests", {b["kind"] for b in value["blockers"]})

    def test_started_ledger_blocks_upgrade(self):
        req = self.layout.control_worktree / "automation/do_again/requests/r1.json"
        req.write_text("{}")
        self.write_agent_ledger()
        value = self.assess()
        kinds={b["kind"] for b in value["blockers"]}
        self.assertIn("pending_requests", kinds)
        self.assertIn("started_requests", kinds)

    def test_orphan_started_ledger_blocks_without_request_file(self):
        self.write_agent_ledger("orphan")
        self.assertEqual(self.assess()["started_requests"], ["orphan"])

    def test_started_ledger_blocks_even_if_receipt_exists(self):
        self.write_agent_ledger()
        receipt = self.layout.control_worktree / "automation/do_again/receipts/r1.json"
        receipt.write_text('{"state":"succeeded"}')
        self.assertFalse(self.assess()["safe"])

    def test_corrupt_execution_record_blocks_offline_upgrade(self):
        self.write_agent_ledger()
        (self.layout.state_dir / "ledger/r1.json").write_text("{")
        self.status = {"installed": True, "running": False, "pid": None}
        value = self.assess()
        kinds = {b["kind"] for b in rollout._effective_upgrade_blockers(value, offline=True)}
        self.assertIn("invalid_execution_records", kinds)

    def test_terminal_agent_ledger_does_not_block(self):
        self.write_agent_ledger(state="terminal")
        self.assertTrue(self.assess()["safe"])

    def test_uncertain_intent_blocks_even_with_empty_outbox(self):
        (self.layout.state_dir / "browser_submission_uncertain.json").write_text('{}')
        self.assertIn("browser_submission_uncertain", {b["kind"] for b in self.assess()["blockers"]})

    def test_daemon_child_process_blocks_upgrade(self):
        rows=[
            {"pid":100,"ppid":1,"command":"daemon"},
            {"pid":101,"ppid":100,"command":"python tests.py"},
            {"pid":102,"ppid":101,"command":"node browser.js"},
        ]
        value=self.assess(rows)
        block=next(b for b in value["blockers"] if b["kind"]=="executor_children")
        self.assertEqual([p["pid"] for p in block["processes"]],[101,102])

    def _managed_browser_fixture(self, *, profile_ok=True, other_lease=True):
        browser=self.home/"browser"
        browser.mkdir(parents=True,exist_ok=True)
        profile=(browser/"profile").resolve()
        (browser/"state.json").write_text(json.dumps({
            "pid":110,"port":9224,
            "profile_dir":str(profile if profile_ok else browser/"user-profile"),
        }))
        leases=browser/"leases"
        leases.mkdir()
        if other_lease:
            (leases/"other.json").write_text(json.dumps({
                "active":True,"repo":str(self.root/"jobpipe"),
                "daemon_pid":999
            }))
        return [
            {"pid":100,"ppid":1,"command":"python -m do_again.service.daemon"},
            {"pid":110,"ppid":100,"command":f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9224 --user-data-dir={profile}"},
            {"pid":111,"ppid":110,"command":"Google Chrome Helper (Renderer)"},
            {"pid":112,"ppid":111,"command":"Google Chrome Helper (Renderer)"},
        ]

    def test_shared_managed_browser_does_not_block_restart(self):
        rows=self._managed_browser_fixture()
        with patch.object(rollout,"pid_alive",return_value=True):
            value=self.assess(rows)
        self.assertTrue(value["safe"])
        self.assertEqual(value["ignored_shared_browser_children"],[110,111,112])

    def test_real_executor_alongside_managed_browser_still_blocks_upgrade(self):
        rows=self._managed_browser_fixture()
        rows.extend([
            {"pid":120,"ppid":100,"command":"python run_proof.py"},
            {"pid":121,"ppid":120,"command":"node playwright"},
        ])
        with patch.object(rollout,"pid_alive",return_value=True):
            value=self.assess(rows)
        self.assertFalse(value["safe"])
        blocked=next(b for b in value["blockers"] if b["kind"]=="executor_children")
        self.assertEqual([x["pid"] for x in blocked["processes"]],[120,121])
        self.assertEqual(value["ignored_shared_browser_children"],[110,111,112])

    def test_user_browser_with_different_profile_still_blocks(self):
        rows=self._managed_browser_fixture(profile_ok=False)
        with patch.object(rollout,"pid_alive",return_value=True):
            value=self.assess(rows)
        self.assertFalse(value["safe"])
        self.assertIn("executor_children",{b["kind"] for b in value["blockers"]})

    def test_browser_without_other_live_lease_remains_blocker(self):
        rows=self._managed_browser_fixture(other_lease=False)
        with patch.object(rollout,"pid_alive",return_value=True):
            value=self.assess(rows)
        self.assertFalse(value["safe"])
        self.assertEqual(value["ignored_shared_browser_children"],[])

    def test_stale_managed_browser_pid_is_not_whitelisted(self):
        rows=self._managed_browser_fixture()
        with patch.object(rollout,"pid_alive",return_value=False):
            value=self.assess(rows)
        self.assertFalse(value["safe"])

    def test_outbox_blocks_upgrade(self):
        outbox=self.layout.state_dir/"browser_outbox"
        outbox.mkdir()
        (outbox/"r1.json").write_text("{}")
        value=self.assess()
        self.assertIn("browser_delivery_pending",{b["kind"] for b in value["blockers"]})

    def test_dry_run_never_stops_service(self):
        with patch.object(rollout,"find_repo",return_value=self.repo), \
             patch.object(rollout,"assess_upgrade",return_value={"safe":True}), \
             patch.object(rollout,"stop_service") as stop:
            value=rollout.staged_upgrade(self.repo,apply=False)
        self.assertFalse(value["applied"])
        stop.assert_not_called()

    def test_recheck_defers_if_claim_appears_after_staging(self):
        safe={"safe":True,"service":{"running":True},"blockers":[]}
        unsafe={"safe":False,"service":{"running":True},"blockers":[{"kind":"pending_requests"}]}
        stage=self.layout.root/"runtime-staging/t/do_again"
        (stage/"service").mkdir(parents=True)
        for name in ("daemon.py","liveness.py","rollout.py"):
            (stage/"service"/name).write_text(name)
        with patch.object(rollout,"find_repo",return_value=self.repo), \
             patch.object(rollout,"assess_upgrade",side_effect=[safe,unsafe]), \
             patch.object(rollout,"_stage_source",return_value=(stage,"abc")), \
             patch.object(rollout,"stop_service") as stop:
            value=rollout.staged_upgrade(self.repo,apply=True)
        self.assertTrue(value["deferred"])
        stop.assert_not_called()

    def test_successful_cutover_preserves_state_control_and_verifies_hash(self):
        old_control=self.layout.control_worktree
        marker=self.layout.state_dir/"keep.json"
        marker.write_text('{"keep":true}')
        safe={"safe":True,"service":{"running":True,"installed":True,"pid":100},"blockers":[]}
        staged=self.layout.root/"runtime-staging/t/do_again"
        (staged/"service").mkdir(parents=True)
        for name in ("daemon.py","liveness.py","rollout.py"):
            (staged/"service"/name).write_text("NEW="+repr(name)+"\n")
        expected=rollout._tree_hash(staged)
        stop=Mock(return_value={"running":False})
        restart=Mock(return_value={"running":True})
        with patch.object(rollout,"find_repo",return_value=self.repo), \
             patch.object(rollout,"assess_upgrade",side_effect=[safe,safe,{**safe,"service":{"running":False,"installed":True,"pid":None}}]), \
             patch.object(rollout,"_stage_source",return_value=(staged,expected)), \
             patch.object(rollout,"service_status",return_value={"installed":True,"running":True,"pid":200}):
            value=rollout.staged_upgrade(self.repo,apply=True,stop=stop,restart=restart)
        self.assertTrue(value["applied"])
        self.assertTrue(marker.is_file())
        self.assertTrue(old_control.is_dir())
        self.assertEqual(rollout._tree_hash(self.layout.runtime_source/"do_again"),expected)
        stop.assert_called_once()
        restart.assert_called_once()

    def test_restart_failure_rolls_back_old_runtime_and_writes_incident(self):
        old_hash=rollout._tree_hash(self.layout.runtime_source/"do_again")
        safe={"safe":True,"service":{"running":True,"installed":True,"pid":100},"blockers":[]}
        staged=self.layout.root/"runtime-staging/t/do_again"
        (staged/"service").mkdir(parents=True)
        for name in ("daemon.py","liveness.py","rollout.py"):
            (staged/"service"/name).write_text("NEW=1\n")
        expected=rollout._tree_hash(staged)
        restart=Mock(side_effect=[RuntimeError("boom"),{"running":True}])
        with patch.object(rollout,"find_repo",return_value=self.repo), \
             patch.object(rollout,"assess_upgrade",side_effect=[safe,safe,{**safe,"service":{"running":False,"installed":True,"pid":None}}]), \
             patch.object(rollout,"_stage_source",return_value=(staged,expected)), \
             self.assertRaises(rollout.ServiceError):
            rollout.staged_upgrade(self.repo,apply=True,stop=Mock(),restart=restart)
        self.assertEqual(rollout._tree_hash(self.layout.runtime_source/"do_again"),old_hash)
        tx=json.loads((self.layout.state_dir/"runtime_upgrade.json").read_text())
        self.assertEqual(tx["state"],"rolled_back")
        incidents=list((self.layout.state_dir/"incidents").glob("runtime-upgrade-*.json"))
        self.assertEqual(len(incidents),1)

    def test_rollback_failure_is_actionable(self):
        safe={"safe":True,"service":{"running":True,"installed":True,"pid":100},"blockers":[]}
        staged=self.layout.root/"runtime-staging/t/do_again"
        (staged/"service").mkdir(parents=True)
        for name in ("daemon.py","liveness.py","rollout.py"):
            (staged/"service"/name).write_text("NEW=1\n")
        expected=rollout._tree_hash(staged)
        restart=Mock(side_effect=RuntimeError("restart dead"))
        with patch.object(rollout,"find_repo",return_value=self.repo), \
             patch.object(rollout,"assess_upgrade",side_effect=[safe,safe,{**safe,"service":{"running":False,"installed":True,"pid":None}}]), \
             patch.object(rollout,"_stage_source",return_value=(staged,expected)), \
             self.assertRaisesRegex(rollout.ServiceError,"ROLLBACK FAILED"):
            rollout.staged_upgrade(self.repo,apply=True,stop=Mock(),restart=restart)
        tx=json.loads((self.layout.state_dir/"runtime_upgrade.json").read_text())
        self.assertEqual(tx["state"],"rollback_failed")

    def test_zombie_daemon_descendant_does_not_block_upgrade(self):
        rows = [
            {"pid": 100, "ppid": 1, "state": "S", "command": "do-again daemon"},
            {"pid": 101, "ppid": 100, "state": "Z", "command": "(git)"},
        ]
        with patch.object(rollout, "service_status", return_value={
            "installed": True, "running": True, "pid": 100,
        }), patch.object(rollout, "_shared_browser_child_pids", return_value=set()):
            result = rollout.assess_upgrade(self.repo, process_rows=rows)
        self.assertFalse(any(b["kind"] == "executor_children" for b in result["blockers"]))

    def test_live_daemon_descendant_still_blocks_upgrade(self):
        rows = [
            {"pid": 100, "ppid": 1, "state": "S", "command": "do-again daemon"},
            {"pid": 101, "ppid": 100, "state": "S", "command": "git fetch origin operator-control"},
        ]
        with patch.object(rollout, "service_status", return_value={
            "installed": True, "running": True, "pid": 100,
        }), patch.object(rollout, "_shared_browser_child_pids", return_value=set()):
            result = rollout.assess_upgrade(self.repo, process_rows=rows)
        blockers = [b for b in result["blockers"] if b["kind"] == "executor_children"]
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0]["processes"][0]["pid"], 101)

    def test_child_with_unclassified_state_still_blocks(self):
        result = self.assess([
            {"pid": 100, "ppid": 1, "command": "daemon"},
            {"pid": 101, "ppid": 100, "command": "unknown child"},
        ])
        self.assertTrue(any(b["kind"] == "executor_children" for b in result["blockers"]))



if __name__=="__main__":
    unittest.main()
