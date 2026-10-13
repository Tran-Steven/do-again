"""Scoped amendment recovery; OS/root installation is not substituted as proof."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from do_again.core.schema import canonical_json
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.reviewed_task import (
    ASSETS, BASE, PATHS, POLICY, ReviewedTransaction, copy_git_metadata, metadata_digest, sha, validate_scope,
)


class OriginalBaselineTraversalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.root = self.home / 'request'

    def test_restrictive_daemon_umask_preserves_traversal_and_immutable_bytes(self):
        from do_again.supervisor.reviewed_task_native import prepare_original_baseline
        previous = os.umask(0o077)
        try:
            baseline, directory = prepare_original_baseline(self.root, b'approved public tests\n', owner_uid=os.getuid())
        finally:
            os.umask(previous)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o711)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        self.assertEqual(baseline.stat().st_mode & 0o777, 0o444)
        self.assertEqual(baseline.read_bytes(), b'approved public tests\n')

    def test_existing_altered_baseline_is_refused_without_rewrite(self):
        from do_again.supervisor.reviewed_task_native import prepare_original_baseline
        baseline, _ = prepare_original_baseline(self.root, b'original\n', owner_uid=os.getuid())
        baseline.chmod(0o644); baseline.write_bytes(b'external change\n')
        with self.assertRaisesRegex(ExecutionBlocked, 'original independent baseline changed'):
            prepare_original_baseline(self.root, b'original\n', owner_uid=os.getuid())
        self.assertEqual(baseline.read_bytes(), b'external change\n')

    def test_aliased_request_directory_is_refused_before_creation(self):
        from do_again.supervisor.reviewed_task_native import prepare_original_baseline
        external = self.home / 'external'; external.mkdir()
        self.root.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(ExecutionBlocked, 'ownership or alias'):
            prepare_original_baseline(self.root, b'original\n', owner_uid=os.getuid())
        self.assertEqual(list(external.iterdir()), [])


class GitMetadataSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.source = self.home / 'original'
        self.source.mkdir(mode=0o750); (self.source / 'objects').mkdir(mode=0o700)
        (self.source / 'HEAD').write_bytes(b'original HEAD\n')
        (self.source / 'objects' / 'inert').write_bytes(b'original object\n')
        (self.source / 'objects' / 'inert').chmod(0o444)
        self.destination = self.home / 'saved'

    def test_explicitly_preserves_every_file_and_directory_owner_and_mode(self):
        expected = sorted((p.stat().st_uid, p.stat().st_gid) for p in
                          [self.source, *self.source.rglob('*')])
        before = metadata_digest(self.source)
        with patch('do_again.supervisor.reviewed_task.os.fchown', wraps=os.fchown) as ownership:
            copy_git_metadata(self.source, self.destination)
        self.assertEqual(sorted(call.args[1:] for call in ownership.call_args_list), expected)
        self.assertEqual(metadata_digest(self.destination), before)
        self.assertEqual(metadata_digest(self.source), before)

    def test_rejects_symlink_without_following_it(self):
        (self.source / 'alias').symlink_to(self.source / 'HEAD')
        with self.assertRaisesRegex(ExecutionBlocked, 'alias'):
            copy_git_metadata(self.source, self.destination)
        self.assertEqual((self.source / 'HEAD').read_bytes(), b'original HEAD\n')

    def test_rejects_hardlink(self):
        os.link(self.source / 'HEAD', self.source / 'alias')
        with self.assertRaisesRegex(ExecutionBlocked, 'alias'):
            copy_git_metadata(self.source, self.destination)

    def test_refuses_replaced_file_between_inspection_and_open(self):
        original_open = os.open
        def concurrent_open(name, *args, **kwargs):
            if name == 'HEAD' and kwargs.get('dir_fd') is not None:
                (self.source / 'HEAD').rename(self.source / 'old-head')
                (self.source / 'HEAD').write_bytes(b'concurrent bytes\n')
            return original_open(name, *args, **kwargs)
        with patch('do_again.supervisor.reviewed_task.os.open', side_effect=concurrent_open):
            with self.assertRaisesRegex(ExecutionBlocked, 'changed before snapshot'):
                copy_git_metadata(self.source, self.destination)
        self.assertEqual((self.source / 'HEAD').read_bytes(), b'concurrent bytes\n')

    def test_fifo_replacement_cannot_block_snapshot_open(self):
        original_open = os.open
        def concurrent_open(name, *args, **kwargs):
            if name == 'HEAD' and kwargs.get('dir_fd') is not None:
                self.assertTrue(args[0] & os.O_NONBLOCK)
                (self.source / 'HEAD').unlink()
                os.mkfifo(self.source / 'HEAD')
            return original_open(name, *args, **kwargs)
        with patch('do_again.supervisor.reviewed_task.os.open', side_effect=concurrent_open):
            with self.assertRaisesRegex(ExecutionBlocked, 'changed before snapshot'):
                copy_git_metadata(self.source, self.destination)


class ReviewedTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name); self.root = self.home / 'source'; self.root.mkdir()
        for name, value in ASSETS['files'].items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value['before'])
        (self.root/'unrelated.txt').write_bytes(b'preserved unrelated source\n')
        (self.root/'.git').mkdir(); (self.root/'.git'/'HEAD').write_text(BASE+'\n')
        self.head = BASE; self.events = []
        self.before_git = metadata_digest(self.root/'.git')
        self.before = {n: (self.root/n).read_bytes() for n in PATHS}
        self.identity = {'request_id':'amended-'+'1'*32,'request_fingerprint':'e'*64}

    def measure(self, overlay, head):
        self.assertEqual(head, self.head)
        for name, data in overlay.items():
            if (self.root/name).read_bytes() != data:
                raise ExecutionBlocked('source bytes differ')
        if (self.root/'unrelated.txt').read_bytes() != b'preserved unrelated source\n':
            raise ExecutionBlocked('unrelated concurrent modification')
        records = sorted((str(p.relative_to(self.root)), sha(p.read_bytes()))
                         for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts)
        return {'commit':head, 'tree_sha256':sha(canonical_json(records))}

    def observe_case(self, case):
        self.events.append(('test',case))
        count = ASSETS['expected_baseline_count'] if case=='original_baseline' else (
            ASSETS['expected_complete_count'] if case=='complete_reviewed_file' else 1)
        return {'case':case,'returncode':0,'timed_out':False,'network_denied':True,
                'pytest_observation':{'pytest_exit_code':0,'collected':count,'passed':count,'failed':0,'skipped':0}}

    def commit(self, tree):
        self.events.append(('commit',tree)); self.head = 'b'*40
        (self.root/'.git'/'HEAD').write_text(self.head+'\n')
        return {'returncode':0,'base_sha':BASE,'paths':list(PATHS),
                'authority':{'repo_head':self.head},'tested_tree_sha256':tree}

    def swap(self, first, second):
        # Portable fixture substitution for Darwin's atomic directory exchange.
        temporary = self.home/'swap'; first.rename(temporary); second.rename(first); temporary.rename(second)
        self.head = (first/'HEAD').read_text().strip()

    def transaction(self, *, test=None, commit=None, checkpoint=None):
        return ReviewedTransaction(self.root, self.home/'journal', measure=self.measure,
            test=test or self.observe_case, commit=commit or self.commit, drain=lambda:None,
            swap_git=self.swap, checkpoint=checkpoint)

    def assert_restored(self, result):
        self.assertEqual(result['returncode'],1)
        self.assertEqual(result['recovery']['status'],'restored')
        self.assertEqual(result['publication'],'disabled')
        self.assertEqual(metadata_digest(self.root/'.git'),self.before_git)
        self.assertEqual({n:(self.root/n).read_bytes() for n in PATHS},self.before)
        self.assertEqual((self.root/'unrelated.txt').read_bytes(),b'preserved unrelated source\n')

    def test_exact_success_tests_before_commit_and_zero_publication(self):
        result = self.transaction().execute(self.identity)
        self.assertEqual(result['returncode'],0)
        self.assertEqual(len(result['tests']),10)
        self.assertEqual(self.events[-1][0],'commit')
        self.assertTrue(all(e[0]=='test' for e in self.events[:-1]))
        self.assertEqual(result['publication'],'disabled')
        self.assertTrue((self.root/'tests/test_ats_question_triage.py').read_text().startswith(
            ASSETS['files']['tests/test_ats_question_triage.py']['before']))
        self.assertFalse((self.root/'tests/test_ats_question_triage_regression.py').exists())
        prior=list(self.events)
        self.assertEqual(self.transaction().execute(self.identity),result)
        self.assertEqual(self.events,prior)

    def test_partial_write_failure_restores_both_files_and_git(self):
        def fail(phase):
            if phase==PATHS[0]:raise OSError('injected failure after first write')
        self.assert_restored(self.transaction(checkpoint=fail).execute(self.identity))
        self.assertEqual(self.events,[])

    def test_failed_test_restores_without_commit(self):
        def fail(case):
            result=self.observe_case(case);result['returncode']=1
            result['pytest_observation'].update(pytest_exit_code=1,passed=0,failed=1)
            return result
        self.assert_restored(self.transaction(test=fail).execute(self.identity))
        self.assertFalse(any(e[0]=='commit' for e in self.events))

    def test_fake_success_print_without_observed_tests_cannot_commit(self):
        self.assert_restored(self.transaction(test=lambda case:{
            'returncode':0,'timed_out':False,'network_denied':True,'stdout':'SUCCESS tests passed'
        }).execute(self.identity))
        self.assertEqual(self.events,[])

    def test_timeout_restores_without_commit(self):
        def timeout(case):
            result=self.observe_case(case);result['timed_out']=True;return result
        self.assert_restored(self.transaction(test=timeout).execute(self.identity))

    def test_killed_worker_restores_without_commit(self):
        def killed(case):raise KeyboardInterrupt('injected interrupted child')
        self.assert_restored(self.transaction(test=killed).execute(self.identity))

    def test_pre_promotion_commit_failure_restores(self):
        self.assert_restored(self.transaction(commit=lambda tree:{'returncode':1}).execute(self.identity))
        self.assertEqual(self.head,BASE)

    def test_post_commit_failure_restores_original_git_bytes(self):
        def fail(phase):
            if phase=='committed':raise OSError('failure after commit before receipt')
        self.assert_restored(self.transaction(checkpoint=fail).execute(self.identity))
        self.assertEqual(self.head,BASE)

    def test_concurrent_source_is_preserved_and_recovery_blocks(self):
        def change(phase):
            if phase==PATHS[0]:
                (self.root/'unrelated.txt').write_bytes(b'external change')
                raise OSError('concurrent writer')
        with self.assertRaises(ExecutionBlocked):self.transaction(checkpoint=change).execute(self.identity)
        self.assertEqual((self.root/'unrelated.txt').read_bytes(),b'external change')
        self.assertEqual(metadata_digest(self.root/'.git'),self.before_git)
        with self.assertRaises(ExecutionBlocked):self.transaction().execute(self.identity)

    def test_changed_approved_file_is_not_overwritten(self):
        def change(phase):
            if phase==PATHS[0]:
                (self.root/PATHS[0]).write_bytes(b'external replacement')
                raise OSError('concurrent writer')
        with self.assertRaises(ExecutionBlocked):self.transaction(checkpoint=change).execute(self.identity)
        self.assertEqual((self.root/PATHS[0]).read_bytes(),b'external replacement')

    def test_wrong_before_hash_blocks_before_mutation(self):
        (self.root/PATHS[0]).write_bytes(b'different original source')
        result=self.transaction().execute(self.identity)
        self.assertEqual(result['returncode'],1)
        self.assertEqual(result['recovery']['source_mutated'],False)
        self.assertEqual((self.root/PATHS[0]).read_bytes(),b'different original source')
        self.assertEqual(metadata_digest(self.root/'.git'),self.before_git)

    def test_policy_has_no_hidden_publication_and_preserves_lineage(self):
        scope={'kind':'reviewed-jobpipe-ats-amendment-v1','source_sha':'a'*40,
               'project_key':'b'*64,'epoch':23,'request_id':'amended-'+'1'*32,
               'policy':dict(POLICY),'asset_sha256':sha(canonical_json(ASSETS)),
               'original_request_id':ASSETS['lineage']['original_request_id'],
               'original_request_fingerprint':ASSETS['lineage']['original_request_fingerprint']}
        config={'source_sha':'a'*40,'production_ready':False,'reviewed_task_scope':scope}
        self.assertEqual(validate_scope(config),scope)
        for name in POLICY:
            altered=json.loads(json.dumps(config));altered['reviewed_task_scope']['policy'][name]=True
            with self.assertRaises(ExecutionBlocked):validate_scope(altered)
        scope['request_id']=scope['original_request_id']
        with self.assertRaises(ExecutionBlocked):validate_scope(config)

    @unittest.skipUnless(hasattr(os, 'fork'), 'POSIX process-loss fixture')
    def test_supervisor_process_loss_before_commit_recovers_without_reexecution(self):
        child = os.fork()
        if child == 0:
            def crash(phase):
                if phase == 'before_commit':os._exit(91)
            self.transaction(checkpoint=crash).execute(self.identity)
            os._exit(92)
        _, status = os.waitpid(child, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 91)
        self.assertTrue(all((self.root/n).read_text() == ASSETS['files'][n]['after'] for n in PATHS))
        result = self.transaction().execute(self.identity)
        self.assert_restored(result)
        self.assertTrue(result['interrupted'])
        self.assertEqual(self.events, [])

    def test_first_class_scope_blocks_all_publication_and_general_execution_ports(self):
        from do_again.supervisor.macos_server import ProjectBroker
        broker=ProjectBroker.__new__(ProjectBroker)
        broker.config={'operator_uid':os.getuid(),'reviewed_task_scope':{'fixture':True}}
        for operation in ('git_publish','git_commit','control_publish','control_reconcile',
                          'git_publication_reconcile','controller_start','controller_tick',
                          'development_execute','execute','dependency_install'):
            with self.assertRaises(ExecutionBlocked):broker._dispatch({'operation':operation},os.getuid())


class ReviewedLedgerTests(unittest.TestCase):
    def test_only_exact_reviewed_parent_can_reserve_one_local_commit_child(self):
        from do_again.supervisor.macos_execution import ExecutionLedger
        with tempfile.TemporaryDirectory() as directory:
            ledger=ExecutionLedger(Path(directory)/'native.sqlite')
            parent='amended-'+'1'*32;fp='e'*64;source='a'*40
            ledger.reserve('p',parent,fp,intent={'operation':'reviewed_task','publication':'disabled',
                'source_sha':source,'request_fingerprint':fp})
            intent={'operation':'git_commit','source_sha':source,'request_fingerprint':fp}
            with self.assertRaises(ExecutionBlocked):ledger.reserve('p','other-effect','b'*64,intent=intent)
            with self.assertRaises(ExecutionBlocked):ledger.reserve('p',parent+'-wrong','b'*64,intent=intent,parent_request=parent)
            with self.assertRaises(ExecutionBlocked):ledger.reserve('p',parent+'-commit','b'*64,
                intent={**intent,'request_fingerprint':'c'*64},parent_request=parent)
            ledger.reserve('p',parent+'-commit','b'*64,intent=intent,parent_request=parent)
            self.assertEqual(len(ledger.pending('p')),2)
            ledger.finish('p',parent+'-commit',{'returncode':0,'timed_out':False,'source_sha':source})
            ledger.finish('p',parent,{'returncode':0,'timed_out':False,'source_sha':source,'publication':'disabled'})
            self.assertEqual(ledger.observe_request('p',parent,fp)['state'],'terminal')
            with self.assertRaises(ExecutionBlocked):ledger.reserve('p',parent+'-commit2','d'*64,intent=intent,parent_request=parent)
