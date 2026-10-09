from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from do_again.supervisor.git_capabilities import CONFIG, GitTransaction, copy_git_data
from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class GitCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git = Path(shutil.which('git')).resolve()
        self.env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(self.root),
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_TERMINAL_PROMPT': '0', 'SYSTEMROOT': os.environ.get('SYSTEMROOT', '')}
        self.run_git('init', '-q', str(self.repo))
        self.run_git('-C', str(self.repo), 'config', 'user.name', 'Fixture')
        self.run_git('-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'one.txt').write_text('before\n')
        (self.repo / 'two.txt').write_text('before\n')
        self.run_git('-C', str(self.repo), 'add', '.')
        self.run_git('-C', str(self.repo), 'commit', '-qm', 'baseline')
        self.head = self.run_git('-C', str(self.repo), 'rev-parse', 'HEAD').strip()

    def run_git(self, *args):
        return subprocess.run([str(self.git), *args], env=self.env, capture_output=True,
                              text=True, check=True).stdout

    def transaction(self, branch='do-again/scoped-capability'):
        return GitTransaction(self.git, self.repo, self.root / 'candidate', self.head, branch)

    def execute_fixture(self, commands):
        # Tests only: production must use the native runner, never this subprocess.
        result = ''
        for command in commands:
            result = subprocess.run(command, env=self.env, capture_output=True,
                                    text=True, check=True).stdout
        return result.strip()

    def test_exact_path_commit_ignores_staged_edits_and_preserves_authoritative_git(self):
        (self.repo / 'one.txt').write_text('after\n')
        (self.repo / 'two.txt').write_text('unselected\n')
        self.run_git('-C', str(self.repo), 'add', 'two.txt')
        tx = self.transaction()
        tx.prepare(self.repo / '.git')
        head = self.execute_fixture(tx.commit_commands(['one.txt'], 'selected change'))
        tx.validate_candidate(head, self.root / 'sealed')
        changed = self.execute_fixture([tx._command('diff-tree', '--no-commit-id', '--name-only', '-r', head)])
        self.assertEqual(changed, 'one.txt')
        self.assertEqual(self.run_git('-C', str(self.repo), 'rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual((self.root / 'sealed/config').read_bytes(), CONFIG)

    def test_hostile_config_hooks_filters_and_alternates_are_not_imported(self):
        sentinel = self.root / 'sentinel'
        config = self.repo / '.git/config'
        with config.open('a') as stream:
            stream.write('\n[include]\n path = /nonexistent/host-config\n'
                         '[filter "attack"]\n clean = touch ' + sentinel.as_posix() + '\n')
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\ntouch "' + sentinel.as_posix() + '"\n')
        hook.chmod(0o755)
        (self.repo / '.git/objects/info/alternates').write_text('/nonexistent/host-objects\n')
        (self.repo / '.gitattributes').write_text('*.txt filter=attack\n')
        (self.repo / 'one.txt').write_text('after\n')
        tx = self.transaction()
        tx.prepare(self.repo / '.git')
        head = self.execute_fixture(tx.commit_commands(['one.txt'], 'safe change'))
        tx.validate_candidate(head, self.root / 'sealed')
        self.assertFalse(sentinel.exists())
        self.assertFalse((tx.metadata / 'hooks').exists())
        self.assertFalse((tx.metadata / 'objects/info/alternates').exists())

    def test_rejects_wrong_head_and_reserved_or_forged_branches(self):
        for branch in ('main', 'operator-control', 'do-again/../main', '-bad', 'do-again/x/y'):
            with self.assertRaises(ExecutionBlocked):
                self.transaction(branch)
        tx = self.transaction()
        tx.prepare(self.repo / '.git')
        with self.assertRaises(ExecutionBlocked):
            tx.check_head('0' * 40)

    def test_rejects_pathspec_escape_and_option_injection(self):
        tx = self.transaction()
        for path in ('../other', '/tmp/other', '.git/config', 'x/.git/config',
                     'x/../y', 'x\\y', '-A', './one.txt', 'x//y', ''):
            with self.assertRaises(ExecutionBlocked, msg=path):
                tx.commit_commands([path], 'change')
        self.assertIn('--literal-pathspecs', tx.commit_commands([':(glob)*'], 'change')[0])

    def test_rejects_aliases_even_in_discarded_metadata(self):
        link = self.repo / '.git/hooks/alias'
        try:
            link.symlink_to(self.repo / 'one.txt')
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(ExecutionBlocked):
            copy_git_data(self.repo / '.git', self.root / 'candidate')

    def test_hardlink_and_budget_fail_closed(self):
        os.link(self.repo / 'one.txt', self.repo / '.git/linked')
        with self.assertRaises(ExecutionBlocked):
            copy_git_data(self.repo / '.git', self.root / 'candidate')
        (self.repo / '.git/linked').unlink()
        with self.assertRaises(ExecutionBlocked):
            copy_git_data(self.repo / '.git', self.root / 'budget', byte_budget=1)

    def test_packed_replace_references_are_removed_without_losing_head(self):
        self.run_git('-C', str(self.repo), 'pack-refs', '--all', '--prune')
        packed = self.repo / '.git/packed-refs'
        with packed.open('a') as stream:
            stream.write(f'{self.head} refs/replace/{self.head}\n')
        tx = self.transaction()
        tx.prepare(self.repo / '.git')
        self.assertNotIn('refs/replace/', (tx.metadata / 'packed-refs').read_text())
        tx.check_head(self.head)

    @unittest.skipUnless(os.name=='posix','native qualification uses POSIX ownership')
    def test_escape_evidence_does_not_contaminate_allowed_git_proof(self):
        from do_again.supervisor.macos_probe import git_proof_directories
        from do_again.supervisor.macos_execution import profile, ProjectExecution
        old = self.root / 'escape-scratch';old.mkdir()
        sentinel = self.root / 'protected';sentinel.write_text('unchanged')
        (old / 'symlink-proof').symlink_to(sentinel)
        scratch, cache = git_proof_directories(self.root, os.getuid(), os.getgid())
        project = ProjectExecution(self.repo, 401, 401, '_doagain_da', self.repo, (self.git,))
        with self.assertRaises(ExecutionBlocked):
            profile(project, old, cache)
        plan = profile(project, scratch, cache)
        self.assertIn(str(scratch), plan)
        self.assertTrue((old / 'symlink-proof').is_symlink())
        self.assertEqual(sentinel.read_text(), 'unchanged')

    def test_native_probe_missing_git_fails_closed_without_launch(self):
        from do_again.supervisor.macos_probe import verify_native_git
        from unittest.mock import Mock, patch
        with patch('do_again.supervisor.macos_probe.capture') as capture:
            with self.assertRaises(ExecutionBlocked):
                verify_native_git({}, Mock(executables=()), self.root, self.root, 'fixture', start_guard=Mock())
            capture.assert_not_called()

    def test_candidate_configuration_and_binding_drift_block_sealing(self):
        tx = self.transaction()
        tx.prepare(self.repo / '.git')
        (self.repo / 'one.txt').write_text('after\n')
        head = self.execute_fixture(tx.commit_commands(['one.txt'], 'change'))
        (tx.metadata / 'config').write_text('[core]\n hooksPath = hooks\n')
        with self.assertRaises(ExecutionBlocked):
            tx.validate_candidate(head, self.root / 'sealed')
        self.assertFalse((self.root / 'sealed').exists())


class NativeBrokerProofAdmissionTests(unittest.TestCase):
    def test_unprotected_or_production_fixture_cannot_enter_broker(self):
        from do_again.supervisor.macos_probe import verify_native_git_broker
        from unittest.mock import Mock, patch
        project = Mock(key='fixed-project-key')
        with patch('do_again.supervisor.git_broker.commit_via_broker') as commit:
            for config in ({}, {'production_ready':True}, {'production_ready':False}):
                with self.subTest(config=config), self.assertRaises(ExecutionBlocked):
                    verify_native_git_broker(config, project, Path('/untrusted/fixture'),
                                             Path('/untrusted'), 'a'*40, 'nonce', start_guard=Mock())
            commit.assert_not_called()
