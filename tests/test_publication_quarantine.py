import hashlib,json,tempfile,unittest
from contextlib import closing
from pathlib import Path
from do_again.supervisor.macos_execution import ExecutionBlocked,ExecutionLedger
from do_again.supervisor.publication_quarantine import append_quarantine

class PublicationQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=ExecutionLedger(Path(self.temp.name)/'ledger.sqlite')
        self.project='project';self.rid='original-publish-1';self.fp='a'*64
        self.intent={'operation':'git_publish','source_sha':'b'*40,'request_fingerprint':'c'*64}
        self.ledger.reserve(self.project,self.rid,self.fp,intent=self.intent)
    def append(self,**kwargs):
        import sqlite3
        with closing(sqlite3.connect(self.ledger.path)) as db:
            return append_quarantine(db,self.project,self.rid,expected_intent=self.intent,
                approval_reference='explicit operator recovery approval',**kwargs)
    def test_append_preserves_original_rows_and_refuses_all_replays(self):
        before=self.ledger.evidence(self.project)
        original=self.ledger.observe_request(self.project,self.rid,'c'*64)
        proof=self.append()
        self.assertFalse(proof['terminal_success']);self.assertFalse(proof['replay_permitted'])
        self.assertEqual(self.ledger.evidence(self.project),before)
        self.assertEqual(self.ledger.observe_request(self.project,self.rid,'c'*64),original)
        self.assertEqual(self.ledger.pending(self.project),[])
        self.assertEqual(self.ledger.quarantined_publications(self.project)[0]['state'],'quarantined_unresolved')
        for action in (lambda:self.ledger.lookup(self.project,self.rid,self.fp),
                       lambda:self.ledger.reserve(self.project,self.rid,self.fp,intent=self.intent),
                       lambda:self.ledger.finish(self.project,self.rid,{'returncode':0,'source_sha':'b'*40}),self.append):
            with self.assertRaises(ExecutionBlocked):action()
        self.ledger.reserve(self.project,'fresh-native-request-2','d'*64,intent={'operation':'execute'})
        self.assertEqual([r['request_id'] for r in self.ledger.pending(self.project)],['fresh-native-request-2'])
    def test_preview_does_not_hide_pending_or_create_audit(self):
        self.append(dry_run=True)
        self.assertEqual(len(self.ledger.pending(self.project)),1)
        with self.assertRaises(ExecutionBlocked):self.ledger.reserve(self.project,'fresh-native-request-2','d'*64)
    def test_changed_original_intent_or_audit_fails_closed(self):
        import sqlite3
        self.append()
        with closing(sqlite3.connect(self.ledger.path)) as db:
            db.execute("UPDATE publication_quarantine SET source_sha='wrong'");db.commit()
        with self.assertRaises(ExecutionBlocked):self.ledger.pending(self.project)
        with self.assertRaises(ExecutionBlocked):self.ledger.reserve(self.project,'fresh-native-request-2','d'*64)
    def test_no_implicit_approval_and_nonpublication_cannot_be_quarantined(self):
        import sqlite3
        with closing(sqlite3.connect(self.ledger.path)) as db:
            with self.assertRaises(ExecutionBlocked):append_quarantine(db,self.project,self.rid,
                expected_intent=self.intent,approval_reference='')
        self.intent['operation']='execute'
        with self.assertRaises(ExecutionBlocked):self.append()

    def test_changed_state_deleted_original_or_changed_result_blocks_every_admission(self):
        import sqlite3
        for mutation in ["UPDATE execution SET state='terminal',result='{}'",
                         "DELETE FROM execution",
                         "UPDATE execution SET result='{}'",
                         "UPDATE execution SET fingerprint='wrong'",
                         "DELETE FROM capability_intent"]:
            fixture=PublicationQuarantineTests();fixture.setUp()
            try:
                fixture.append()
                with closing(sqlite3.connect(fixture.ledger.path)) as db,db:db.execute(mutation)
                actions=[lambda:fixture.ledger.pending(fixture.project),
                    lambda:fixture.ledger.quarantined_publications(fixture.project),
                    lambda:fixture.ledger.lookup(fixture.project,fixture.rid,fixture.fp),
                    lambda:fixture.ledger.observe_request(fixture.project,fixture.rid,'c'*64),
                    lambda:fixture.ledger.reserve(fixture.project,fixture.rid,fixture.fp),
                    lambda:fixture.ledger.reserve(fixture.project,'fresh-native-request-2','d'*64)]
                for index,action in enumerate(actions):
                    with self.subTest(mutation=mutation,action=index),self.assertRaises(ExecutionBlocked):action()
            finally:fixture.doCleanups()
