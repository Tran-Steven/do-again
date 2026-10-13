"""Root-owned byte snapshots for explicitly sealed local development contracts.

No process, network, Git or caller-selected capability is exposed here. The
native pipeline holds its writer fence and drains the dedicated UID before
restoration. Original journals and snapshots are never deleted or replaced.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..core.schema import atomic_json, canonical_json
from .macos_execution import ExecutionBlocked
from .native_test_evidence import measure_committed_tree

LOCAL_KIND = 'native-development-local-v2'


def validate_source_contract(profile):
    files = profile.get('source_contract')
    origin = profile.get('original_task')
    if (not isinstance(files, dict) or set(files) != set(profile['allowed_paths'])
            or not 1 <= len(files) <= 16 or not isinstance(origin, dict)
            or set(origin) != {'request_id', 'request_fingerprint', 'planner_call_id', 'base_commit'}
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{7,159}', str(origin['request_id']))
            or not re.fullmatch(r'[0-9a-f]{64}', str(origin['request_fingerprint']))
            or not re.fullmatch(r'planner-[A-Za-z0-9._-]+', str(origin['planner_call_id']))
            or not re.fullmatch(r'[0-9a-f]{40}', str(origin['base_commit']))):
        raise ExecutionBlocked('local source contract lacks exact paths and original identity')
    for path, value in files.items():
        if (not isinstance(value, dict) or set(value) != {'before_sha256', 'after_sha256',
                'before_blob', 'after_blob', 'mode', 'preserve_prefix', 'after_content'}
                or value['mode'] not in {'100644', '100755'}
                or type(value['preserve_prefix']) is not bool
                or any(not re.fullmatch(r'[0-9a-f]{64}', str(value[k]))
                       for k in ('before_sha256', 'after_sha256'))
                or any(not re.fullmatch(r'[0-9a-f]{40}', str(value[k]))
                       for k in ('before_blob', 'after_blob'))
                or value['before_sha256'] == value['after_sha256']):
            raise ExecutionBlocked('local source hashes or preservation rule are invalid')
        content=value['after_content']
        if (not isinstance(content,str) or len(content.encode())>256*1024
                or hashlib.sha256(content.encode()).hexdigest()!=value['after_sha256']
                or hashlib.sha1(b'blob '+str(len(content.encode())).encode()+b'\0'+content.encode()).hexdigest()!=value['after_blob']):
            raise ExecutionBlocked('approved output content and hashes differ')
        if path.endswith('.py'): compile(content,path,'exec')
        if path in profile['test_files'] and (not value['preserve_prefix']
                or profile['test_files'][path] != value['before_blob']):
            raise ExecutionBlocked('editable baseline tests must preserve original bytes')
    return files


class SourceTransaction:
    def __init__(self, root, directory, profile, *, head, manifest, read_authority,
                 owner_uid=0):
        self.root, self.directory = Path(root), Path(directory)
        self.profile, self.head, self.manifest = profile, head, manifest
        self.read_authority, self.owner_uid = read_authority, owner_uid
        self.files = validate_source_contract(profile)
        self.path = self.directory / 'source-rollback.json'
        if self.head != profile['original_task']['base_commit']:
            raise ExecutionBlocked('amended task differs from original pinned commit')
        if os.geteuid() != owner_uid:
            raise ExecutionBlocked('only protected snapshot owner may transact source')
        for path in (self.directory, *self.directory.parents):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or (path == self.directory and
                    (info.st_uid != owner_uid or info.st_mode & 0o077)):
                raise ExecutionBlocked('rollback snapshot directory is not private and unaliased')

    def _open(self, name, *, writable=False, create=False):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] == '.git':
            raise ExecutionBlocked('rollback path escapes assigned source')
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in path.parts[:-1]:
                nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = nxt
            flags = os.O_RDWR if writable else os.O_RDONLY
            if create: flags |= os.O_CREAT
            result = os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        finally:
            os.close(fd)
        info = os.fstat(result)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024:
            os.close(result)
            raise ExecutionBlocked('task source is special, aliased, or exceeds snapshot bound')
        return result

    def _read(self, name):
        fd = self._open(name)
        try:
            before = os.fstat(fd)
            with os.fdopen(fd, 'rb', closefd=False) as stream: data = stream.read(256 * 1024 + 1)
            after = os.fstat(fd)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ExecutionBlocked('task source changed during snapshot')
            return data, before
        finally: os.close(fd)

    def _state(self):
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != self.owner_uid or info.st_mode & 0o077
                or info.st_size > 8 * 1024 * 1024):
            raise ExecutionBlocked('rollback journal is not protected')
        state = json.loads(self.path.read_text())
        if (state['head'] != self.head or state['contract_sha256'] != self.digest()
                or state['manifest'] != self.manifest):
            raise ExecutionBlocked('original rollback journal binding differs')
        for name, entry in state['files'].items():
            data = base64.b64decode(entry['bytes'], validate=True)
            if hashlib.sha256(data).hexdigest() != self.files[name]['before_sha256']:
                raise ExecutionBlocked('retained original snapshot hash differs')
        if set(state['files']) != set(self.files):
            raise ExecutionBlocked('retained original snapshot paths differ')
        return state

    def digest(self):
        return hashlib.sha256(canonical_json(self.profile)).hexdigest()

    def prepare(self):
        if self.path.exists() or self.path.is_symlink():
            return self._state()
        measured = measure_committed_tree(self.root, commit=self.head,
            manifest=self.manifest, read_authority=self.read_authority)
        saved = {}
        for name, contract in self.files.items():
            if self.manifest.get(name) != {'blob_sha':contract['before_blob'], 'mode':contract['mode']}:
                raise ExecutionBlocked('original source contract differs from sealed Git tree')
            data, info = self._read(name)
            if hashlib.sha256(data).hexdigest() != contract['before_sha256']:
                raise ExecutionBlocked('original task source hash differs')
            saved[name] = {'bytes':base64.b64encode(data).decode(),
                          'mode':stat.S_IMODE(info.st_mode), 'uid':info.st_uid, 'gid':info.st_gid}
        if measured != measure_committed_tree(self.root, commit=self.head,
                manifest=self.manifest, read_authority=self.read_authority):
            raise ExecutionBlocked('source changed before durable snapshot')
        state = {'head':self.head, 'contract_sha256':self.digest(), 'manifest':self.manifest,
                 'files':saved, 'before_measurement':measured, 'state':'prepared'}
        atomic_json(self.path, state)
        return state

    def regression_baseline(self):
        state = self._state()
        manifest = {k:dict(v) for k,v in self.manifest.items()}
        for name, contract in self.files.items():
            data, _ = self._read(name)
            expected = contract['after_sha256'] if contract['preserve_prefix'] else contract['before_sha256']
            if hashlib.sha256(data).hexdigest() != expected:
                raise ExecutionBlocked('regression baseline is not approved tests plus pinned implementation')
            if contract['preserve_prefix']:
                if not data.startswith(base64.b64decode(state['files'][name]['bytes'])):
                    raise ExecutionBlocked('regression baseline changed original tests')
                manifest[name] = {'blob_sha':contract['after_blob'], 'mode':contract['mode']}
        return measure_committed_tree(self.root, commit=self.head,
            manifest=manifest, read_authority=self.read_authority)

    def candidate(self):
        state = self._state()
        manifest = {k:dict(v) for k,v in self.manifest.items()}
        for name, contract in self.files.items():
            data, info = self._read(name)
            original_info=state['files'][name]
            if (stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)!=(original_info['mode'],original_info['uid'],original_info['gid']):
                raise ExecutionBlocked('approved source permissions or ownership changed')
            if hashlib.sha256(data).hexdigest() != contract['after_sha256']:
                raise ExecutionBlocked('candidate does not equal approved source hash')
            if contract['preserve_prefix'] and not data.startswith(base64.b64decode(state['files'][name]['bytes'])):
                raise ExecutionBlocked('candidate changed original baseline test bytes')
            manifest[name] = {'blob_sha':contract['after_blob'], 'mode':contract['mode']}
        return measure_committed_tree(self.root, commit=self.head,
            manifest=manifest, read_authority=self.read_authority)

    def committed(self, head, manifest):
        state = self._state()
        expected = {k:dict(v) for k,v in self.manifest.items()}
        for name, contract in self.files.items():
            expected[name] = {'blob_sha':contract['after_blob'], 'mode':contract['mode']}
        if manifest != expected or head == self.head:
            raise ExecutionBlocked('committed manifest differs from approved tested candidate')
        measured = measure_committed_tree(self.root, commit=head,
            manifest=manifest, read_authority=self.read_authority)
        import difflib
        diff = ''
        for name, contract in self.files.items():
            data, info = self._read(name)
            original_info=state['files'][name]
            if (stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid)!=(original_info['mode'],original_info['uid'],original_info['gid']):
                raise ExecutionBlocked('approved source permissions or ownership changed')
            if hashlib.sha256(data).hexdigest() != contract['after_sha256']:
                raise ExecutionBlocked('committed source hash differs')
            original = base64.b64decode(state['files'][name]['bytes'])
            diff += ''.join(difflib.unified_diff(original.decode().splitlines(keepends=True),
                data.decode().splitlines(keepends=True), fromfile='a/'+name, tofile='b/'+name))
        return dict(measured, diff=diff)

    def rollback(self):
        state = self._state()
        if self.read_authority() != self.head:
            raise ExecutionBlocked('Git authority changed; rollback requires original effect reconciliation')
        # Called only after native UID drain under the original project writer fence.
        for name, entry in state['files'].items():
            data = base64.b64decode(entry['bytes'], validate=True)
            fd = self._open(name, writable=True, create=True)
            try:
                os.ftruncate(fd, 0)
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(data); stream.flush()
                if self.owner_uid == 0: os.fchown(fd, entry['uid'], entry['gid'])
                os.fchmod(fd, entry['mode']); os.fsync(fd)
            finally: os.close(fd)
            parent = os.open(self.root / Path(name).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(parent)
            finally: os.close(parent)
        measured = measure_committed_tree(self.root, commit=self.head,
            manifest=self.manifest, read_authority=self.read_authority)
        if measured != state['before_measurement']:
            raise ExecutionBlocked('byte-perfect source rollback verification failed')
        state['state'] = 'rolled_back'; state['rollback_measurement'] = measured
        atomic_json(self.path, state)
        return {'byte_perfect':True, 'measurement':measured,
                'file_hashes':{k:v['before_sha256'] for k,v in self.files.items()}}
