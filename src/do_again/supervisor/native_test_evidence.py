"""Protected-pipeline test observer; never accepts worker test summaries.

Callbacks must be installed by the sealed broker: measure verifies a clean Git
commit and hashes its tracked source; execute uses the existing offline capture
path. This module grants neither authority nor an execution entrypoint.
"""
import hashlib
import re
from .macos_execution import ExecutionBlocked


def observe_tests(*, expected_head, cases, measure, execute):
    if (not re.fullmatch('[0-9a-f]{40}', expected_head)
            or not isinstance(cases, tuple) or not cases or len(cases) > 64
            or len(set(cases)) != len(cases)
            or any(not isinstance(c, str) or not re.fullmatch(r'(?:[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*|tests/(?:[A-Za-z_][A-Za-z_0-9]*/)*test_[A-Za-z_0-9]+\.py(?:::[A-Za-z_][A-Za-z_0-9]*)*)', c) for c in cases)):
        raise ExecutionBlocked('sealed native test profile is invalid')
    before = measure()
    def valid(value):
        return (isinstance(value, dict) and value.get('commit') == expected_head
                and value.get('clean') is True
                and isinstance(value.get('tree_sha256'), str)
                and re.fullmatch('[0-9a-f]{64}', value['tree_sha256']))
    if not valid(before):
        raise ExecutionBlocked('test source is dirty or differs from committed implementation')
    outcomes = []
    for case in cases:
        result = execute(case)
        if (not isinstance(result, dict) or type(result.get('returncode')) is not int
                or result.get('timed_out') is not False
                or result.get('network_denied') is not True):
            raise ExecutionBlocked('native test capture/confinement evidence absent')
        outcome = {'case': case, 'exit_code': result['returncode'], 'timed_out': False}
        for stream in ('stdout', 'stderr'):
            value = result.get(stream)
            if not isinstance(value, str) or result.get(stream+'_truncated', False):
                raise ExecutionBlocked('native test output absent or truncated')
            outcome[stream+'_sha256'] = hashlib.sha256(value.encode()).hexdigest()
        outcomes.append(outcome)
        if result['returncode'] != 0:
            raise ExecutionBlocked('independently observed test process failed')
    after = measure()
    if not valid(after) or before != after:
        raise ExecutionBlocked('source changed during native tests')
    return {'commit_before': before['commit'], 'commit_after': after['commit'],
            'tree_before': before['tree_sha256'], 'tree_after': after['tree_sha256'],
            'cases': outcomes, 'network_denied': True}


def measure_committed_tree(root, *, commit, manifest, read_authority):
    """Hash source bytes against a sealed native Git tree manifest.

    The manifest is produced by the broker's sealed Git helper for this exact
    commit, never by the worker. Reject links, missing/extra source and mutation
    during reads. Git metadata remains governed by the existing broker fence.
    """
    import os
    import stat
    from pathlib import Path
    from ..core.schema import canonical_json
    if (not isinstance(manifest, dict) or not manifest or len(manifest) > 10000
            or not re.fullmatch('[0-9a-f]{40}', commit)):
        raise ExecutionBlocked('sealed source manifest missing or invalid')
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or read_authority() != commit:
        raise ExecutionBlocked('source root/authority differs')
    found = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            path = Path(directory)/name
            if path.is_symlink(): raise ExecutionBlocked('source directory link rejected')
            if path == root/'.git': directories.remove(name)
        for name in files:
            found.add((Path(directory)/name).relative_to(root).as_posix())
    if found != set(manifest): raise ExecutionBlocked('source has missing or untracked files')
    measured = []
    for name, entry in sorted(manifest.items()):
        path = Path(name)
        if (path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] == '.git'
                or not isinstance(entry, dict) or entry.get('mode') not in ('100644','100755')
                or not isinstance(entry.get('blob_sha'), str)
                or not re.fullmatch('[0-9a-f]{40}', entry['blob_sha'])):
            raise ExecutionBlocked('sealed source manifest entry invalid')
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in path.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd); directory_fd = next_fd
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        finally: os.close(directory_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 8*1024*1024:
                raise ExecutionBlocked('source file is aliased, nonregular or oversized')
            with os.fdopen(fd, 'rb', closefd=False) as stream: data = stream.read(8*1024*1024+1)
            after = os.fstat(fd)
        finally: os.close(fd)
        if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns) != (after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise ExecutionBlocked('source changed while being measured')
        blob = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        mode = '100755' if before.st_mode & 0o111 else '100644'
        if blob != entry['blob_sha'] or mode != entry['mode']:
            raise ExecutionBlocked('source bytes/mode differ from committed tree')
        measured.append({'path':name,'mode':mode,'sha256':hashlib.sha256(data).hexdigest()})
    if read_authority() != commit: raise ExecutionBlocked('commit changed during source measurement')
    return {'commit':commit,'clean':True,'tree_sha256':hashlib.sha256(canonical_json(measured)).hexdigest()}
