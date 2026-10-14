"""Real local Git/file fixtures; these are not native V1 acceptance."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from do_again.supervisor.candidate_transaction import CandidateTransaction,KIND
from do_again.supervisor.git_manifest import tree_manifest
from do_again.supervisor.macos_execution import ExecutionBlocked

@unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'descriptor-verified POSIX fixture')
class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name);self.repo=base/'repo';self.repo.mkdir();self.directory=base/'journal'
        (self.repo/'code.py').write_bytes(b'def answer():\r\n    return 0\r\n')
        (self.repo/'baseline.py').write_text('assert True\n')
        self.git('init','-q');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        self.git('add','.');self.git('commit','-qm','Baseline')
        self.head=self.git('rev-parse','HEAD');self.manifest=tree_manifest(Path('/usr/bin/git'),self.repo/'.git',self.head)
        self.profile={'kind':KIND,'allowed_paths':['code.py','new_test.py'],'implementation_paths':['code.py']}
        self.txn=CandidateTransaction(self.repo,self.directory,self.profile,head=self.head,manifest=self.manifest,
            read_authority=lambda:self.git('rev-parse','HEAD'))
        self.txn.prepare()
    def git(self,*args):return subprocess.check_output(['git','-C',str(self.repo),*args],stderr=subprocess.DEVNULL,text=True).strip()
    def edit(self):
        (self.repo/'code.py').write_text('def answer():\n    return 1\n')
        (self.repo/'new_test.py').write_text('from code import answer\nassert answer()==1\n')
    def test_freeze_then_mutation_is_rejected(self):
        self.edit();before=self.txn.freeze();self.assertEqual(before,self.txn.measure())
        (self.repo/'code.py').write_text('def answer(): return 2\n')
        with self.assertRaises(ExecutionBlocked):self.txn.measure()
    def test_rollback_restores_bytes_mode_and_removes_only_new_approved_file(self):
        mode=(self.repo/'code.py').stat().st_mode&0o777;self.edit();self.txn.freeze()
        result=self.txn.rollback();self.assertTrue(result['byte_perfect'])
        self.assertEqual((self.repo/'code.py').read_bytes(),b'def answer():\r\n    return 0\r\n')
        self.assertEqual((self.repo/'code.py').stat().st_mode&0o777,mode)
        self.assertFalse((self.repo/'new_test.py').exists());self.assertEqual(self.git('status','--porcelain'),'')
        self.assertTrue((self.directory/'candidate.json').exists())
    def test_tests_only_or_print_noop_does_not_qualify(self):
        (self.repo/'new_test.py').write_text('assert True\n')
        with self.assertRaisesRegex(ExecutionBlocked,'implementation change'):self.txn.freeze()
    def test_outside_scope_edit_is_preserved_and_blocks_recovery(self):
        self.edit();(self.repo/'baseline.py').write_text('changed outside scope\n')
        with self.assertRaises(ExecutionBlocked):self.txn.freeze()
        with self.assertRaises(ExecutionBlocked):self.txn.rollback()
        self.assertEqual((self.repo/'baseline.py').read_text(),'changed outside scope\n')
    def test_changed_git_authority_never_rolls_back(self):
        self.edit();self.txn.freeze();self.git('add','.');self.git('commit','-qm','Candidate')
        with self.assertRaisesRegex(ExecutionBlocked,'Git authority changed'):self.txn.rollback()
    def test_later_approved_edit_blocks_recovery_without_touching_any_file(self):
        self.edit();self.txn.freeze()
        (self.repo/'code.py').write_text('independent later edit\n')
        before={p.name:p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
        with self.assertRaises(ExecutionBlocked):self.txn.rollback()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.repo.iterdir() if p.is_file()})
    def test_later_outside_edit_blocks_recovery_before_any_restoration(self):
        self.edit();self.txn.freeze()
        (self.repo/'baseline.py').write_text('independent outside edit\n')
        before={p.name:p.read_bytes() for p in self.repo.iterdir() if p.is_file()}
        with self.assertRaises(ExecutionBlocked):self.txn.rollback()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.repo.iterdir() if p.is_file()})
    def test_later_permission_change_blocks_recovery(self):
        self.edit();self.txn.freeze();(self.repo/'code.py').chmod(0o600)
        with self.assertRaisesRegex(ExecutionBlocked,'target changed'):self.txn.rollback()
        self.assertTrue((self.repo/'new_test.py').exists())
    def test_promoted_commit_must_match_tested_bytes(self):
        self.edit();tested=self.txn.freeze();self.git('add','.');self.git('commit','-qm','Tested candidate')
        head=self.git('rev-parse','HEAD');manifest=tree_manifest(Path('/usr/bin/git'),self.repo/'.git',head)
        self.assertEqual(self.txn.committed(head,manifest)['tree_sha256'],tested['tree_sha256'])
    def test_hardlink_candidate_never_receives_root_restoration(self):
        self.edit();os.link(self.repo/'code.py',self.repo/'extra.py')
        with self.assertRaises(ExecutionBlocked):self.txn.freeze()
        with self.assertRaises(ExecutionBlocked):self.txn.rollback()
    def test_restart_uses_original_snapshot(self):
        self.edit();second=CandidateTransaction(self.repo,self.directory,self.profile,head=self.head,
            manifest=self.manifest,read_authority=lambda:self.git('rev-parse','HEAD'))
        second.prepare();second.freeze();second.rollback()
        self.assertEqual((self.repo/'code.py').read_bytes(),b'def answer():\r\n    return 0\r\n')
