import copy
import unittest
import os
from do_again.supervisor.native_checkpoint import verify_claim
from do_again.supervisor.macos_execution import ExecutionBlocked

class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.claim={'request_id':'amended-original-123','request_fingerprint':'a'*64,'source_sha':'b'*40,'commit':'c'*40,'tree_sha256':'d'*64}
        self.authority={'repo_head':'c'*40,'repo_branch':'do-again/task-original'}
        self.measurement={'commit':'c'*40,'clean':True,'tree_sha256':'d'*64}
        self.original={'state':'terminal','source_sha':'b'*40,'request_fingerprint':'a'*64,'result':{
            'returncode':0,'timed_out':False,'source_sha':'b'*40,'execution_uid':402,
            'tested_tree_sha256':'d'*64,'tests':[{'returncode':0,'timed_out':False}],
            'commit':{'authority':self.authority,'returncode':0,'tested_tree_sha256':'d'*64}}}
    def test_known_success_retains_exact_tested_head(self):
        self.assertTrue(verify_claim(self.claim,self.original,self.authority,self.measurement))
    def test_unknown_failed_dirty_or_other_tree_rejected(self):
        for variant in ('uncertain','failed','dirty','tree','head','tests'):
            original=copy.deepcopy(self.original);authority=dict(self.authority);measure=dict(self.measurement)
            if variant=='uncertain':original['state']='post_dispatch_uncertain'
            if variant=='failed':original['result']['returncode']=1
            if variant=='dirty':measure['clean']=False
            if variant=='tree':measure['tree_sha256']='e'*64
            if variant=='head':authority['repo_head']='e'*40
            if variant=='tests':original['result']['tests'][0]['returncode']=1
            with self.subTest(variant=variant),self.assertRaises(ExecutionBlocked):
                verify_claim(self.claim,original,authority,measure)

    @unittest.skipUnless(os.name=="posix", "native POSIX fixture; Windows native parity is not claimed")
    def test_checkpoint_metadata_preserves_approved_operator_ownership_and_rejects_aliases(self):
        import os,tempfile
        from pathlib import Path
        from do_again.supervisor.native_checkpoint import require_checkpoint_metadata
        with tempfile.TemporaryDirectory() as root:
            metadata=Path(root)/'git';metadata.mkdir(mode=0o700)
            head=metadata/'HEAD';head.write_text('c'*40);head.chmod(0o600)
            require_checkpoint_metadata(metadata,operator_uid=os.getuid())
            head.chmod(0o666)
            with self.assertRaises(ExecutionBlocked):require_checkpoint_metadata(metadata,operator_uid=os.getuid())
            head.chmod(0o600);head.unlink();head.symlink_to(Path(root)/'other')
            with self.assertRaises((ExecutionBlocked,FileNotFoundError)):
                require_checkpoint_metadata(metadata,operator_uid=os.getuid())
