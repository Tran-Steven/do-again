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

class StagedCheckpointTests(unittest.TestCase):
    def setUp(self):
        from do_again.supervisor.native_checkpoint import STAGED_KIND
        self.claim={'kind':STAGED_KIND,'request_id':'offline-staged-task-1','request_fingerprint':'a'*64,
            'source_sha':'b'*40,'commit':'c'*40,'tree_sha256':'d'*64}
        self.authority={'repo_head':'c'*40,'repo_branch':'do-again/task-original'}
        self.measurement={'commit':'c'*40,'clean':True,'tree_sha256':'d'*64}
        self.profile={'kind':'native-development-autonomous-v1','allowed_paths':['code.py','tests/test_new.py'],
            'implementation_paths':['code.py'],'test_cases':['tests/test_original.py','tests/test_new.py'],'test_runner':'pytest'}
        cases=[{'case':name,'exit_code':0,'timed_out':False,'native_test_counts':
            {'collected':2,'passed':2,'failed':0,'skipped':0,'pytest_exit_code':0}} for name in self.profile['test_cases']]
        values={'snapshot':{'baseline':{'clean':True,'commit':'e'*40,'tree_sha256':'f'*64}},
            'implementation':{'execution_uid':402,'network_denied':True,'source_write_paths':self.profile['allowed_paths']},
            'test':{'native_test_run':{'cases':cases,'commit_before':'e'*40,'commit_after':'e'*40,
                'tree_before':'d'*64,'tree_after':'d'*64,'network_denied':True}},
            'commit':{'authority':self.authority,'base_sha':'e'*40,'paths':self.profile['allowed_paths'],
                'execution_uid':402,'tested_tree_sha256':'d'*64}}
        self.stages={name:{'state':'terminal','source_sha':'b'*40,'request_fingerprint':'a'*64,
            'result':dict(value,returncode=0,timed_out=False,source_sha='b'*40)} for name,value in values.items()}
        self.intents={name:{'operation':'git_commit' if name=='commit' else 'execute'} for name in values}

    def verify(self,stages=None,measurement=None):
        from do_again.supervisor.native_checkpoint import verify_staged_claim
        return verify_staged_claim(self.claim,stages or self.stages,self.intents,self.authority,
            measurement or self.measurement,profile=self.profile,execution_uid=402)

    def test_tested_stages_retain_commit_without_publication_or_aggregate_success(self):
        before=copy.deepcopy(self.stages)
        self.assertTrue(self.verify());self.assertEqual(self.stages,before)
        self.assertNotIn('publication',self.stages)

    def test_failed_missing_unsafe_or_changed_native_stage_is_rejected(self):
        for change in ('missing','source','fingerprint','uncertain','failed','uid','network','cases','counts','tree','paths','base'):
            stages=copy.deepcopy(self.stages)
            if change=='missing':stages.pop('test')
            if change=='source':stages['test']['source_sha']='f'*40
            if change=='fingerprint':stages['test']['request_fingerprint']='f'*64
            if change=='uncertain':stages['test']['state']='post_dispatch_uncertain'
            if change=='failed':stages['test']['result']['returncode']=1
            if change=='uid':stages['implementation']['result']['execution_uid']=501
            if change=='network':stages['implementation']['result']['network_denied']=False
            if change=='cases':stages['test']['result']['native_test_run']['cases'].pop()
            if change=='counts':stages['test']['result']['native_test_run']['cases'][0]['native_test_counts']['passed']=0
            if change=='tree':stages['commit']['result']['tested_tree_sha256']='f'*64
            if change=='paths':stages['commit']['result']['paths']=['outside.py']
            if change=='base':stages['test']['result']['native_test_run']['commit_after']='c'*40
            with self.subTest(change=change),self.assertRaises(ExecutionBlocked):self.verify(stages)
        with self.assertRaises(ExecutionBlocked):self.verify(measurement=dict(self.measurement,clean=False))
