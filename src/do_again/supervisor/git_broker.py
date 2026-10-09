"""Project-bound local commits. Git never executes with supervisor authority."""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
import time
from pathlib import Path

from ..core.schema import canonical_json
from .git_capabilities import GitTransaction, SHA
from .macos_execution import (EXECUTION_ROOT, INSTALL_ROOT, REQUEST_ID, ExecutionBlocked,
                              MacOSProcesses, capture, launch_spec)


def atomic_swap(first: Path, second: Path) -> None:
    """Darwin's atomic directory exchange; no rename-pair fallback."""
    if sys.platform != 'darwin':
        raise ExecutionBlocked('atomic Git promotion is unsupported on this platform')
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    rename = library.renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # Values from Darwin sys/fcntl.h and sys/stdio.h: AT_FDCWD, RENAME_SWAP.
    if rename(-2, os.fsencode(first), -2, os.fsencode(second), 2):
        raise OSError(ctypes.get_errno(), 'atomic Git metadata exchange failed')


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def seal_metadata(path: Path) -> None:
    for item in (path, *path.rglob('*')):
        os.chown(item, 0, 0)
        item.chmod(0o755 if item.is_dir() else 0o644)
        if item.is_file():
            with item.open('rb') as stream:
                os.fsync(stream.fileno())
    # Children before their parents, so new directory entries are durable.
    for directory in sorted((p for p in path.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        sync_directory(directory)
    sync_directory(path)
    sync_directory(path.parent)


def validate_packet(packet: dict) -> None:
    required = {'operation', 'request_id', 'expected_head', 'expected_epoch', 'paths', 'message'}
    if set(packet) - required - {'request_fingerprint'} or not required <= set(packet) or packet['operation'] != 'git_commit':
        raise ExecutionBlocked('unknown Git capability or administrative fields')
    if not isinstance(packet['request_id'], str) or not REQUEST_ID.fullmatch(packet['request_id']):
        raise ExecutionBlocked('invalid Git request identity')
    if not isinstance(packet['expected_head'], str) or not SHA.fullmatch(packet['expected_head']):
        raise ExecutionBlocked('Git requires an exact starting commit')
    epoch = packet['expected_epoch']
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ExecutionBlocked('Git requires an exact authority epoch')
    if 'request_fingerprint' in packet and (not isinstance(packet['request_fingerprint'], str)
            or not re.fullmatch('[0-9a-f]{64}', packet['request_fingerprint'])):
        raise ExecutionBlocked('invalid original Git request fingerprint')


def commit_via_broker(broker, packet: dict) -> dict:
    """Caller is the authenticated project listener, never an execution script.

    Admission and pause share the existing project fence. One execution-ledger
    reservation covers the entire transaction, including its promotion boundary.
    """
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('local Git capability requires the immutable root macOS broker')
    validate_packet(packet)
    from .macos_server import verify_installation, worktree_authority
    with broker.lock:
        def gate(*, head=True):
            status = broker.registry.status(broker.project.repo)
            if (status['intent'] != 'active' or status['epoch'] != packet['expected_epoch']
                    or broker.config.get('production_ready') is not True or not broker._verified()):
                raise ExecutionBlocked('operator authority or native qualification blocks Git')
            if head and worktree_authority(broker.project.worktree)['repo_head'] != packet['expected_head']:
                raise ExecutionBlocked('assigned Git head changed')
            return status

        status = gate(head=False)
        revision = status.get('goal_revision')
        if not isinstance(revision, str) or not revision:
            raise ExecutionBlocked('Git requires an accepted project goal revision')
        verify_installation(broker.config)
        git = INSTALL_ROOT / 'current/runtimes/git/bin/git'
        if broker.config.get('git') != str(git) or git not in broker.project.executables:
            raise ExecutionBlocked('sealed native Git is not admitted')
        branch = 'do-again/task-' + hashlib.sha256((broker.project.key + ':' + revision).encode()).hexdigest()[:20]
        root = EXECUTION_ROOT / broker.project.key / 'requests' / packet['request_id']
        scratch, cache = root / 'scratch', root / 'cache'
        transaction = GitTransaction(git, broker.project.worktree, scratch / 'git-candidate', packet['expected_head'], branch)
        commands = transaction.commit_commands(packet['paths'], packet['message'])
        # A directory is not an exact file binding. Deleted files remain eligible;
        # their names must also appear in the final verified diff.
        for relative in packet['paths']:
            if (broker.project.worktree / relative).is_dir():
                raise ExecutionBlocked('Git commits require file paths, not directory scopes')
        fingerprint = hashlib.sha256(canonical_json(packet)).hexdigest()
        processes = MacOSProcesses()
        with broker.admission():
            gate(head=False)
            recovered = broker.ledger.lookup(broker.project.key, packet['request_id'], fingerprint)
            if recovered is not None:
                return recovered
            gate()
            if processes.owned(broker.project.uid):
                raise ExecutionBlocked('dedicated identity has unresolved processes')
            broker.ledger.reserve(broker.project.key, packet['request_id'], fingerprint)
        # No authoritative effect occurs in the candidate phase.
        try:
            for path in (scratch, cache):
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chown(path, broker.project.uid, broker.project.gid)
            for ancestor in (root, root.parent):
                ancestor.chmod(0o711)
            transaction.prepare(broker.project.worktree / '.git')
            for path in (transaction.metadata, *transaction.metadata.rglob('*')):
                os.chown(path, broker.project.uid, broker.project.gid)
            deadline = time.monotonic() + 120
            def run(argv):
                remaining = min(30, deadline - time.monotonic())
                if remaining <= 0:
                    raise ExecutionBlocked('Git transaction exceeded its bounded execution window')
                execution = {'operation':'execute','request_id':packet['request_id'],
                             'argv':argv,'cwd':'.','timeout':remaining}
                spec = launch_spec(broker.project, execution, scratch, cache)
                # capture rechecks process ownership inside this same launch fence.
                from contextlib import contextmanager
                @contextmanager
                def admission():
                    with broker.admission():
                        gate()
                        yield
                outcome = capture(spec, remaining, start_guard=admission)
                if outcome['returncode'] or outcome['timed_out'] or outcome.get('stdout_truncated') or outcome.get('stderr_truncated'):
                    raise ExecutionBlocked('candidate Git command failed; metadata was not promoted')
                return outcome['stdout']
            head = ''
            for command in commands:
                head = run(command).strip()
            sealed = scratch / 'git-sealed'
            transaction.validate_candidate(head, sealed)
            parents = run(transaction._command('rev-list','--parents','-n','1',head)).strip().split()
            changed = run(transaction._command('diff-tree','--no-commit-id','--name-only','-z','-r',head)).rstrip('\x00').split('\x00')
            if parents != [head, packet['expected_head']] or not all(path in packet['paths'] for path in changed) or changed == ['']:
                raise ExecutionBlocked('candidate parent or exact file bindings failed verification')
            seal_metadata(sealed)
        except Exception as exc:
            result = {'state':'failed_pre_promotion','returncode':1,'error':str(exc),'source_sha':broker.config['source_sha']}
            broker.ledger.finish(broker.project.key, packet['request_id'], result)
            return result
        # Keep the started record if anything at/after exchange becomes uncertain,
        # including lost receipt publication. Never swap back or replay that request.
        with broker.admission():
            try:
                gate()
                if processes.owned(broker.project.uid):
                    raise ExecutionBlocked('dedicated descendants remain before promotion')
            except Exception as exc:
                result = {'state':'failed_pre_promotion','returncode':1,'error':str(exc),'source_sha':broker.config['source_sha']}
                broker.ledger.finish(broker.project.key, packet['request_id'], result)
                return result
            atomic_swap(broker.project.worktree / '.git', sealed)
            sync_directory(broker.project.worktree)
            sync_directory(scratch)
            actual = worktree_authority(broker.project.worktree)
            if actual != {'repo_head':head,'repo_branch':branch}:
                raise ExecutionBlocked('Git promotion is uncertain; reconciliation required')
            result = {'state':'succeeded','returncode':0,'base_sha':packet['expected_head'],'authority':actual,
                      'paths':changed,'source_sha':broker.config['source_sha'],'execution_uid':broker.project.uid}
            broker.ledger.finish(broker.project.key, packet['request_id'], result)
            return result
