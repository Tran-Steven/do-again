import json,sqlite3,tempfile,unittest
from pathlib import Path
from contextlib import closing
from do_again.supervisor.macos_execution import ExecutionLedger,ExecutionBlocked
from do_again.supervisor.planning_probe_quarantine import append

class ProbeDispositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=ExecutionLedger(Path(self.tmp.name)/'journal.sqlite');self.project='project';self.rid='offline-'+'a'*20+'-4-probe'
        self.intent={'operation':'execute','source_sha':'b'*40,'request_fingerprint':'c'*64}
        self.ledger.reserve(self.project,self.rid,'d'*64,intent=self.intent)
    def disposition(self,**kwargs):
        with closing(sqlite3.connect(self.ledger.path)) as db:return append(db,self.project,self.rid,expected_intent=self.intent,approval_reference='explicit operator approval',**kwargs)
    def test_append_preserves_original_and_denies_replay_finish(self):
        before=self.ledger.evidence(self.project);original=self.ledger.observe_request(self.project,self.rid,'c'*64)
        proof=self.disposition();self.assertFalse(proof['terminal_success']);self.assertFalse(proof['tests_claimed_passed'])
        self.assertEqual(self.ledger.evidence(self.project),before);self.assertEqual(self.ledger.observe_request(self.project,self.rid,'c'*64),original)
        self.assertEqual(self.ledger.pending(self.project),[]);self.assertEqual(self.ledger.quarantined_planning_probes(self.project)[0]['state'],'quarantined_unresolved')
        for operation in (lambda:self.ledger.lookup(self.project,self.rid,'d'*64),lambda:self.ledger.reserve(self.project,self.rid,'d'*64,intent=self.intent),
                lambda:self.ledger.finish(self.project,self.rid,{'returncode':0,'source_sha':'b'*40}),self.disposition):
            with self.assertRaises(ExecutionBlocked):operation()
        self.ledger.reserve(self.project,'fresh-probe-identity','e'*64,intent={'operation':'execute'})
    def test_preview_never_hides_the_unknown_reservation(self):
        self.disposition(dry_run=True);self.assertEqual(len(self.ledger.pending(self.project)),1)
        self.assertEqual(self.ledger.quarantined_planning_probes(self.project),[])
    def test_deleted_finished_or_changed_original_always_blocks(self):
        for sql in ["DELETE FROM execution", "UPDATE execution SET state='terminal'", "UPDATE execution SET result='{}'",
                    "UPDATE execution SET fingerprint='changed'", "UPDATE capability_intent SET payload='{}'"]:
            with self.subTest(sql=sql),tempfile.TemporaryDirectory() as tmp:
                backup=Path(tmp)/'backup.sqlite'
                with closing(sqlite3.connect(self.ledger.path)) as db,closing(sqlite3.connect(backup)) as target:db.backup(target)
                self.disposition()
                with closing(sqlite3.connect(self.ledger.path)) as db:db.execute(sql);db.commit()
                with self.assertRaises(ExecutionBlocked):self.ledger.pending(self.project)
                self.ledger.path.unlink();backup.replace(self.ledger.path)
