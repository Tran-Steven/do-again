"""One exact reviewed native amendment, with local-only commit and guarded recovery.

This uses the existing broker, UID capture, Git transaction and effect ledger.
It does not grant a worker permission to edit protected baseline tests.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from ..core.schema import atomic_json, canonical_json
from .macos_execution import ExecutionBlocked
from .reviewed_task_assets import ASSETS

BASE = ASSETS['lineage']['pinned_commit']
PATHS = tuple(ASSETS['files'])
REMOVED = ASSETS['acceptance_revision']['removed_requirement']
POLICY = {'publication': False, 'push': False, 'pull_request': False,
          'deployment': False, 'application_submissions': False,
          'automatic_merge': False, 'worker_network': False}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_scope(config, project=None):
    scope = config.get('reviewed_task_scope')
    if scope is None:
        return None
    fields = {'kind', 'source_sha', 'project_key', 'epoch', 'request_id', 'policy',
              'asset_sha256', 'original_request_id', 'original_request_fingerprint'}
    import re
    if (not isinstance(scope, dict) or set(scope) != fields
            or scope['kind'] != 'reviewed-jobpipe-ats-amendment-v1'
            or scope['source_sha'] != config.get('source_sha')
            or not re.fullmatch('[0-9a-f]{40}', str(scope['source_sha']))
            or not re.fullmatch('[0-9a-f]{64}', str(scope['project_key']))
            or not re.fullmatch('amended-[0-9a-f]{32}', str(scope['request_id']))
            or type(scope['epoch']) is not int or scope['epoch'] < 1
            or scope['policy'] != POLICY or any(type(v) is not bool for v in scope['policy'].values())
            or scope['asset_sha256'] != sha(canonical_json(ASSETS))
            or any(scope[k] != ASSETS['lineage'][k] for k in
                   ('original_request_id', 'original_request_fingerprint'))
            or config.get('production_ready') is not False):
        raise ExecutionBlocked('reviewed task scope or no-publication policy differs')
    if project is not None and (project.key != scope['project_key'] or project.account != '_doagain_jp'):
        raise ExecutionBlocked('reviewed task belongs only to the exact jobpipe identity')
    for name, item in ASSETS['files'].items():
        if sha(item['before'].encode()) != item['before_sha256'] or sha(item['after'].encode()) != item['after_sha256']:
            raise ExecutionBlocked('sealed reviewed bytes differ from approved hashes')
    tests = ASSETS['files']['tests/test_ats_question_triage.py']
    if not tests['after'].startswith(tests['before']):
        raise ExecutionBlocked('original baseline tests were not preserved byte-for-byte')
    return scope


def metadata_digest(root):
    """Measure every Git byte/mode/owner; no Git commands or index refresh."""
    records = []
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (path.is_file() and info.st_nlink != 1):
            raise ExecutionBlocked('Git recovery metadata contains an alias')
        if not (path.is_dir() or path.is_file()):
            raise ExecutionBlocked('Git recovery metadata is not regular')
        records.append([str(path.relative_to(root)), stat.S_IMODE(info.st_mode),
                        info.st_uid, info.st_gid, sha(path.read_bytes()) if path.is_file() else None])
    return sha(canonical_json(records))


def copy_git_metadata(source, destination):
    """Copy bytes, ownership and modes through unaliased directory descriptors."""
    def signature(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def copy_directory(source_fd, target):
        before = os.fstat(source_fd)
        target.mkdir(mode=0o700)
        target_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in sorted(os.listdir(source_fd)):
                info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=source_fd)
                    try:
                        if signature(os.fstat(child)) != signature(info):
                            raise ExecutionBlocked('Git directory changed before snapshot')
                        copy_directory(child, target / name)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
                    try:
                        if signature(os.fstat(child)) != signature(info):
                            raise ExecutionBlocked('Git file changed before snapshot')
                        output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600, dir_fd=target_fd)
                        with os.fdopen(output, 'wb') as stream:
                            while data := os.read(child, 1024 * 1024):
                                stream.write(data)
                            stream.flush()
                            if signature(os.fstat(child)) != signature(info):
                                raise ExecutionBlocked('Git file changed during snapshot')
                            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                            os.fsync(stream.fileno())
                    finally:
                        os.close(child)
                else:
                    raise ExecutionBlocked('Git snapshot contains an alias or special file')
            if signature(os.fstat(source_fd)) != signature(before):
                raise ExecutionBlocked('Git directory changed during snapshot')
            os.fchown(target_fd, before.st_uid, before.st_gid)
            os.fchmod(target_fd, stat.S_IMODE(before.st_mode))
            os.fsync(target_fd)
        finally:
            os.close(target_fd)

    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        copy_directory(source_fd, Path(destination))
    finally:
        os.close(source_fd)


class ReviewedTransaction:
    """Protected callbacks are supplied only by the installed native handler."""
    def __init__(self, root, journal, *, measure, test, commit, drain, swap_git, checkpoint=None):
        self.root, self.directory = Path(root), Path(journal)
        self.measure, self.test, self.commit = measure, test, commit
        self.drain, self.swap_git = drain, swap_git
        self.checkpoint = checkpoint or (lambda phase: None)
        self.path = self.directory / 'transaction.json'
        self.state = None

    def save(self):
        atomic_json(self.path, self.state)

    def write(self, name, expected, content, record):
        path = self.root / name
        info = path.lstat()
        if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or sha(path.read_bytes()) != expected
                or [stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid] != record):
            raise ExecutionBlocked('concurrent file modification; no overwrite')
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix='.reviewed-', dir=path.parent)
        try:
            os.fchmod(fd, record[0]); os.fchown(fd, record[1], record[2])
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            current = path.lstat()
            if (current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (info.st_ino, info.st_mtime_ns, info.st_ctime_ns):
                raise ExecutionBlocked('concurrent file replacement; no overwrite')
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def current_overlay(self):
        overlay = {}
        for name, item in ASSETS['files'].items():
            path = self.root / name
            info = path.lstat()
            data = path.read_bytes()
            if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or [stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid] != self.state['modes'][name]
                    or sha(data) not in {item['before_sha256'], item['after_sha256']}):
                raise ExecutionBlocked('unrecognized concurrent source; recovery refused')
            overlay[name] = data
        return overlay

    def rollback(self):
        self.drain()
        overlay = self.current_overlay()
        # Check the entire source and Git authority before changing either file.
        self.measure(overlay, self.state.get('commit', {}).get('authority', {}).get('repo_head', BASE))
        git_hash = metadata_digest(self.root / '.git')
        expected = self.state.get('committed_metadata', self.state['git_before'])
        if git_hash != expected:
            raise ExecutionBlocked('concurrent Git mutation; recovery refused')
        for name, data in overlay.items():
            self.measure(self.current_overlay(), self.state.get('commit', {}).get('authority', {}).get('repo_head', BASE))
            self.write(name, sha(data), ASSETS['files'][name]['before'].encode(), self.state['modes'][name])
        if git_hash != self.state['git_before']:
            if metadata_digest(self.root / '.git') != expected:
                raise ExecutionBlocked('Git changed before recovery exchange')
            self.swap_git(self.root / '.git', self.directory / 'git-before')
        if metadata_digest(self.root / '.git') != self.state['git_before']:
            raise ExecutionBlocked('Git recovery was not byte-perfect')
        self.measure({n: v['before'].encode() for n, v in ASSETS['files'].items()}, BASE)
        return {'status': 'restored', 'byte_perfect': True, 'git_unchanged': True,
                'file_hashes': {n: sha((self.root / n).read_bytes()) for n in PATHS}}

    def execute(self, identity):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state['identity'] != identity:
                raise ExecutionBlocked('recovery reservation identity differs')
            if self.state['phase'] == 'terminal':
                result = self.state['result']
                if result['returncode'] != 0 and result.get('recovery',{}).get('status') == 'not_needed':
                    return result  # historical pre-mutation refusal, never a successful source claim
                head = result.get('commit', {}).get('authority', {}).get('repo_head', BASE)
                expected = 'after' if result['returncode'] == 0 else 'before'
                measured = self.measure({n: v[expected].encode() for n, v in ASSETS['files'].items()}, head)
                if result['returncode'] == 0 and measured['tree_sha256'] != result['tested_tree_sha256']:
                    raise ExecutionBlocked('completed task source differs from tested content')
                if metadata_digest(self.root/'.git') != self.state.get('committed_metadata',self.state.get('git_before')):
                    raise ExecutionBlocked('completed task Git recovery authority changed')
                return self.state['result']
            if self.state['phase'] == 'blocked':
                raise ExecutionBlocked('blocked recovery cannot be replayed')
            # A process loss resumes recovery only, never edits/tests/commit again.
            if self.state['phase'] != 'prepared':
                recovery = self.rollback()
                result = {'returncode': 1, 'timed_out': False, 'recovery': recovery,
                          'before_file_hashes':self.state.get('verified_before_hashes',{}),
                          'after_file_hashes':self.state.get('observed_after_file_hashes',{}),
                          'publication': 'disabled', 'interrupted': True, 'tests': self.state.get('tests', [])}
                self.state.update(phase='terminal', result=result); self.save()
                return result
            self.measure({n: v['before'].encode() for n, v in ASSETS['files'].items()}, BASE)
            self.state['verified_before_hashes'] = {n:v['before_sha256'] for n,v in ASSETS['files'].items()}
            if 'git_before' in self.state and metadata_digest(self.root/'.git') != self.state['git_before']:
                raise ExecutionBlocked('Git changed during interrupted snapshot')
            result = {'returncode':1, 'timed_out':False, 'interrupted':True,
                      'recovery':{'status':'not_needed','source_mutated':False},
                      'publication':'disabled','tests':[]}
            self.state.update(phase='terminal',result=result);self.save()
            return result
        self.state = {'identity': identity, 'phase': 'prepared', 'modes': {}, 'tests': []}
        self.save()
        try:
            self.drain()
            self.measure({n: v['before'].encode() for n, v in ASSETS['files'].items()}, BASE)
            self.state['verified_before_hashes'] = {n:v['before_sha256'] for n,v in ASSETS['files'].items()}
            if (self.root / REMOVED).exists():
                raise ExecutionBlocked('removed nonexistent prerequisite was created')
            for name, item in ASSETS['files'].items():
                info = (self.root / name).lstat()
                self.state['modes'][name] = [stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid]
            self.state['git_before'] = metadata_digest(self.root / '.git')
            self.save()
            copy_git_metadata(self.root / '.git', self.directory / 'git-before')
            if (metadata_digest(self.directory / 'git-before') != self.state['git_before']
                    or metadata_digest(self.root / '.git') != self.state['git_before']):
                raise ExecutionBlocked('Git snapshot changed during copy')
            self.state['phase'] = 'snapshot'; self.save()
            self.checkpoint('snapshot')
            for name, item in ASSETS['files'].items():
                self.measure(self.current_overlay(), BASE)
                self.write(name, item['before_sha256'], item['after'].encode(), self.state['modes'][name])
                self.state['phase'] = 'writing'; self.save(); self.checkpoint(name)
            overlay = {n: v['after'].encode() for n, v in ASSETS['files'].items()}
            tested = self.measure(overlay, BASE)
            self.state['observed_after_file_hashes'] = {n: sha(data) for n,data in overlay.items()}
            # Independently execute the untouched baseline, all mandatory selectors,
            # and the complete after-file. Neither the proposal nor stdout can pass this.
            cases = [('original_baseline', ASSETS['expected_baseline_count']),
                     *[(c, None) for c in ASSETS['cases']],
                     ('complete_reviewed_file', ASSETS['expected_complete_count'])]
            self.state['phase'] = 'testing'; self.save()
            for case, expected_count in cases:
                outcome = self.test(case)
                self.state['tests'].append(outcome); self.save()
                observation = outcome.get('pytest_observation', {})
                if (outcome.get('returncode') != 0 or outcome.get('timed_out') is not False
                        or outcome.get('network_denied') is not True
                        or observation.get('pytest_exit_code') != 0
                        or type(observation.get('collected')) is not int
                        or observation['collected'] < 1
                        or observation.get('passed') != observation['collected']
                        or observation.get('failed') != 0 or observation.get('skipped') != 0
                        or (expected_count is not None and observation['collected'] != expected_count)):
                    raise ExecutionBlocked('independent native baseline/safety test failed')
                if self.measure(overlay, BASE) != tested:
                    raise ExecutionBlocked('tested content changed')
                self.checkpoint('test:' + case)
            self.state['tested'] = tested; self.state['phase'] = 'tested'; self.save()
            self.checkpoint('before_commit')
            self.measure(overlay, BASE)
            commit = self.commit(tested['tree_sha256'])
            self.state['commit'] = commit
            if (commit.get('returncode') != 0 or commit.get('base_sha') != BASE
                    or set(commit.get('paths', [])) != set(PATHS)
                    or commit.get('tested_tree_sha256') != tested['tree_sha256']):
                self.state.pop('commit', None)
                raise ExecutionBlocked('local commit did not bind exact tested content')
            head = commit['authority']['repo_head']
            self.measure(overlay, head)
            self.state['committed_metadata'] = metadata_digest(self.root / '.git')
            self.state['phase'] = 'committed'; self.save(); self.checkpoint('committed')
            result = {'returncode': 0, 'timed_out': False, 'commit': commit,
                      'tested_tree_sha256': tested['tree_sha256'], 'tests': self.state['tests'],
                      'before_file_hashes': {n: v['before_sha256'] for n, v in ASSETS['files'].items()},
                      'after_file_hashes': {n: v['after_sha256'] for n, v in ASSETS['files'].items()},
                      'recovery': {'status': 'not_required'}, 'publication': 'disabled'}
            import difflib
            diff = ''.join(''.join(difflib.unified_diff(
                item['before'].splitlines(keepends=True), item['after'].splitlines(keepends=True),
                fromfile='a/'+name, tofile='b/'+name)) for name, item in ASSETS['files'].items())
            if 'diff' in commit:
                if not isinstance(commit['diff'],str) or commit.get('diff_sha256')!=sha(commit['diff'].encode()):
                    raise ExecutionBlocked('protected commit diff evidence differs')
                diff=commit['diff']
            result.update(diff=diff, diff_sha256=sha(diff.encode()))
            self.state.update(phase='terminal', result=result); self.save()
            return result
        except BaseException as exc:
            if self.state['phase'] == 'prepared':
                # No source/Git mutation is possible before the snapshot checkpoint.
                recovery = {'status': 'not_needed', 'source_mutated': False}
            else:
                try:
                    recovery = self.rollback()
                except BaseException as failure:
                    self.state.update(phase='blocked', blocker=str(failure)); self.save()
                    raise ExecutionBlocked('recovery blocked; preserved concurrent/uncertain work') from failure
            result = {'returncode': 1, 'timed_out': False, 'error': type(exc).__name__,
                      'error_message': str(exc),
                      'before_file_hashes':self.state.get('verified_before_hashes',{}),
                      'expected_before_file_hashes':{n:v['before_sha256'] for n,v in ASSETS['files'].items()},
                      'after_file_hashes':self.state.get('observed_after_file_hashes',{}),
                      'tests': self.state['tests'], 'recovery': recovery, 'publication': 'disabled'}
            self.state.update(phase='terminal', result=result); self.save()
            return result
