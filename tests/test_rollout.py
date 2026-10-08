from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.service import rollout


class StagedRuntimeRolloutTests(unittest.TestCase):
    def setUp(self):
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

    def test_pending_request_blocks_upgrade(self):
        req = self.layout.control_worktree / "automation/do_again/requests/r1.json"
        req.write_text("{}")
        value = self.assess()
        self.assertFalse(value["safe"])
        self.assertIn("pending_requests", {b["kind"] for b in value["blockers"]})

    def test_started_ledger_blocks_upgrade(self):
        req = self.layout.control_worktree / "automation/do_again/requests/r1.json"
        req.write_text("{}")
        ledger = self.layout.state_dir / "requests/r1.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({"state":"started"}))
        value = self.assess()
        kinds={b["kind"] for b in value["blockers"]}
        self.assertIn("pending_requests", kinds)
        self.assertIn("started_requests", kinds)

    def test_daemon_child_process_blocks_upgrade(self):
        rows=[
            {"pid":100,"ppid":1,"command":"daemon"},
            {"pid":101,"ppid":100,"command":"python tests.py"},
            {"pid":102,"ppid":101,"command":"node browser.js"},
        ]
        value=self.assess(rows)
        block=next(b for b in value["blockers"] if b["kind"]=="executor_children")
        self.assertEqual([p["pid"] for p in block["processes"]],[101,102])

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


if __name__=="__main__":
    unittest.main()
