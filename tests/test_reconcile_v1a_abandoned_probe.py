"""The original V1A probe can only become audited, permanently unresolved history."""
import importlib.util
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "tools/reconcile_v1a_abandoned_probe.py"
spec=importlib.util.spec_from_file_location("reconcile_v1a_abandoned_probe",MODULE)
recovery=importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class ReconcileV1AProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=sqlite3.connect(Path(self.temp.name)/"authority.sqlite",isolation_level=None)
        self.addCleanup(self.db.close)
        self.project="fixture-project-key"
        self.expected={"operation":"execute","source_sha":recovery.OLD_SOURCE,
                       "request_fingerprint":recovery.FINGERPRINT}
        self.db.execute("CREATE TABLE execution (project TEXT,request TEXT,fingerprint TEXT,state TEXT,result TEXT,PRIMARY KEY(project,request))")
        self.db.execute("CREATE TABLE capability_intent (project TEXT,request TEXT,payload TEXT,PRIMARY KEY(project,request))")
        self.db.execute("INSERT INTO execution VALUES(?,?,?,'started',NULL)",
            (self.project,recovery.REQUEST,recovery.FINGERPRINT))
        self.db.execute("INSERT INTO capability_intent VALUES(?,?,?)",
            (self.project,recovery.REQUEST,json.dumps(self.expected)))

    def observed(self):
        row=self.db.execute("SELECT state,result FROM execution WHERE project=? AND request=?",
            (self.project,recovery.REQUEST)).fetchone()
        return row

    def test_readonly_preview_does_not_create_tables_or_change_started_request(self):
        before=self.db.total_changes
        result=recovery.classify(self.db,self.project,self.expected,dry_run=True)
        self.assertEqual(result["operation"],"would_quarantine")
        self.assertEqual(self.db.total_changes,before)
        self.assertEqual(self.observed(),("started",None))
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='v1a_abandoned_probe_audit'").fetchone())

    def test_audit_quarantine_preserves_original_intent_and_denies_replay(self):
        result=recovery.classify(self.db,self.project,self.expected,dry_run=False)
        self.assertEqual(result["state"],"abandoned")
        self.assertFalse(result["terminal_success"])
        self.assertFalse(result["replay_permitted"])
        self.assertEqual(self.observed(),("abandoned",None))
        self.assertEqual(json.loads(self.db.execute("SELECT payload FROM capability_intent").fetchone()[0]),self.expected)
        row=self.db.execute("SELECT prior_state,disposition FROM v1a_abandoned_probe_audit").fetchone()
        self.assertEqual(row,("started","quarantined_unresolved_not_replayable"))
        with self.assertRaisesRegex(RuntimeError,"already altered"):
            recovery.classify(self.db,self.project,self.expected,dry_run=False)

    def test_mismatch_or_terminal_outcome_refuses_any_quarantine(self):
        self.db.execute("UPDATE execution SET fingerprint='f' || substr(fingerprint,2)")
        with self.assertRaisesRegex(RuntimeError,"already altered"):
            recovery.classify(self.db,self.project,self.expected,dry_run=False)
        self.assertEqual(self.observed(),("started",None))
        self.db.execute("UPDATE execution SET fingerprint=?",(recovery.FINGERPRINT,))
        self.db.execute("UPDATE execution SET state='terminal',result='{}'")
        with self.assertRaisesRegex(RuntimeError,"already altered"):
            recovery.classify(self.db,self.project,self.expected,dry_run=False)

    def test_rejects_incorrect_authority_or_controller_progress(self):
        config={"source_sha":recovery.OLD_SOURCE,"production_ready":False,
                "projects":[{"account":"_doagain_jp","source_sha":recovery.JOBPIPE_HEAD}]}
        status={"intent":"maintenance","epoch":15}
        observed={"state":"started","result":None,"fingerprint":recovery.FINGERPRINT,
                  "intent":self.expected}
        state={"state":"blocked","requests":[],"outstanding":None,"acknowledged":[]}
        self.assertEqual(recovery.validate(config,status,observed,no_workers=True,
                         worktree_head=recovery.JOBPIPE_HEAD,state=state,planning_files=False)["account"],"_doagain_jp")
        for wrong_status in ({"intent":"active","epoch":14},{"intent":"maintenance","epoch":16}):
            with self.assertRaisesRegex(RuntimeError,"epoch 15"):
                recovery.validate(config,wrong_status,observed,no_workers=True,
                                  worktree_head=recovery.JOBPIPE_HEAD,state=state,planning_files=False)
        with self.assertRaisesRegex(RuntimeError,"progress"):
            recovery.validate(config,status,observed,no_workers=True,
                              worktree_head=recovery.JOBPIPE_HEAD,
                              state={**state,"requests":[{"request":"x"}]},planning_files=False)
        with self.assertRaisesRegex(RuntimeError,"journal"):
            recovery.validate(config,status,observed,no_workers=True,
                              worktree_head=recovery.JOBPIPE_HEAD,state=state,planning_files=True)
        with self.assertRaisesRegex(RuntimeError,"process"):
            recovery.validate(config,status,observed,no_workers=False,
                              worktree_head=recovery.JOBPIPE_HEAD,state=state,planning_files=False)


if __name__=="__main__":
    unittest.main()
