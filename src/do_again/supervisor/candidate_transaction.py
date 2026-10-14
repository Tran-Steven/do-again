"""Scoped model edits: preserve the baseline, freeze tests, verify or restore."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
from ..core.schema import atomic_json, canonical_json
from .macos_execution import ExecutionBlocked
from .native_test_evidence import measure_committed_tree

KIND='native-development-autonomous-v1'


class CandidateTransaction:
    def __init__(self,root,directory,profile,*,head,manifest,read_authority,owner_uid=None):
        self.root=Path(root);self.directory=Path(directory);self.profile=profile
        self.head=head;self.manifest=manifest;self.read_authority=read_authority
        self.owner_uid=os.geteuid() if owner_uid is None else owner_uid
        self.path=self.directory/'candidate.json'
        self.binding=hashlib.sha256(canonical_json({'profile':profile,'head':head,'manifest':manifest})).hexdigest()

    def _parent(self,name):
        parts=Path(name).parts
        fd=os.open(self.root,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                next_fd=os.open(part,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,dir_fd=fd)
                os.close(fd);fd=next_fd
            return fd,parts[-1]
        except BaseException:os.close(fd);raise

    def _read(self,name):
        parent,leaf=self._parent(name)
        try:fd=os.open(leaf,os.O_RDONLY | os.O_NOFOLLOW,dir_fd=parent)
        finally:os.close(parent)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>65536:
                raise ExecutionBlocked('scoped candidate file is aliased or oversized')
            with os.fdopen(fd,'rb',closefd=False) as stream:data=stream.read(65537)
            after=os.fstat(fd)
            if (info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ExecutionBlocked('candidate changed during measurement')
            return data,info
        finally:os.close(fd)

    def _state(self):
        info=self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=self.owner_uid or info.st_nlink!=1
                or info.st_mode & 0o077 or info.st_size>16*1024*1024):
            raise ExecutionBlocked('candidate journal privacy differs')
        value=json.loads(self.path.read_text())
        if value.get('binding')!=self.binding:raise ExecutionBlocked('candidate journal binding differs')
        for name,entry in value['files'].items():
            if name not in self.profile['allowed_paths']:raise ExecutionBlocked('candidate snapshot path differs')
            if entry is not None and hashlib.sha256(base64.b64decode(entry['bytes'],validate=True)).hexdigest()!=entry['sha256']:
                raise ExecutionBlocked('original candidate snapshot changed')
        if set(value['files'])!=set(self.profile['allowed_paths']):raise ExecutionBlocked('candidate snapshot incomplete')
        return value

    def prepare(self):
        if self.path.exists() or self.path.is_symlink():return self._state()['before']
        self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        info=self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=self.owner_uid or info.st_mode & 0o077:
            raise ExecutionBlocked('candidate journal directory privacy differs')
        before=measure_committed_tree(self.root,commit=self.head,manifest=self.manifest,read_authority=self.read_authority)
        saved={}
        for name in self.profile['allowed_paths']:
            try:data,info=self._read(name)
            except FileNotFoundError:
                if name in self.manifest:raise ExecutionBlocked('tracked candidate source missing')
                saved[name]=None;continue
            saved[name]={'bytes':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest(),
                'mode':stat.S_IMODE(info.st_mode),'uid':info.st_uid,'gid':info.st_gid}
        if before!=measure_committed_tree(self.root,commit=self.head,manifest=self.manifest,read_authority=self.read_authority):
            raise ExecutionBlocked('baseline changed before snapshot')
        atomic_json(self.path,{'binding':self.binding,'files':saved,'before':before,'original_manifest':self.manifest,'state':'prepared'})
        return before

    def freeze(self):
        state=self._state()
        if 'candidate_manifest' in state:return self.measure()
        manifest={k:dict(v) for k,v in self.manifest.items()};changed=[]
        for name,original in state['files'].items():
            data,info=self._read(name)
            if original is not None and (info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode))!=(original['uid'],original['gid'],original['mode']):
                raise ExecutionBlocked('candidate source ownership or mode changed')
            blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            mode='100755' if info.st_mode & 0o111 else '100644'
            manifest[name]={'blob_sha':blob,'mode':mode}
            if manifest[name]!=self.manifest.get(name):changed.append(name)
        if not set(changed)&set(self.profile['implementation_paths']):
            raise ExecutionBlocked('candidate has no independently measured implementation change')
        measured=measure_committed_tree(self.root,commit=self.head,manifest=manifest,read_authority=self.read_authority)
        state.update(candidate_manifest=manifest,candidate_measurement=measured,changed_paths=changed,state='frozen')
        atomic_json(self.path,state);return measured

    def measure(self):
        state=self._state()
        measured=measure_committed_tree(self.root,commit=self.head,manifest=state['candidate_manifest'],read_authority=self.read_authority)
        if measured!=state['candidate_measurement']:raise ExecutionBlocked('tested candidate bytes changed')
        return measured

    def committed(self,head,manifest):
        state=self._state()
        if manifest!=state['candidate_manifest']:raise ExecutionBlocked('committed tree differs from tested candidate')
        measured=measure_committed_tree(self.root,commit=head,manifest=manifest,read_authority=self.read_authority)
        if measured['tree_sha256']!=state['candidate_measurement']['tree_sha256']:
            raise ExecutionBlocked('committed source hash differs from tests')
        state.update(state='committed',commit=head);atomic_json(self.path,state)
        return measured

    def rollback(self):
        state=self._state()
        if self.read_authority()!=self.head:raise ExecutionBlocked('candidate Git authority changed; no automatic rollback')
        for name,entry in state['files'].items():
            try:parent,leaf=self._parent(name)
            except FileNotFoundError:
                if entry is None:continue
                raise
            try:
                if entry is None:
                    try:
                        fd=os.open(leaf,os.O_RDONLY | os.O_NOFOLLOW,dir_fd=parent)
                    except FileNotFoundError:continue
                    try:
                        info=os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                            raise ExecutionBlocked('new candidate file is aliased; preserve for review')
                    finally:os.close(fd)
                    os.unlink(leaf,dir_fd=parent)
                else:
                    fd=os.open(leaf,os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,0o600,dir_fd=parent)
                    try:
                        info=os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                            raise ExecutionBlocked('candidate rollback target is aliased')
                        data=base64.b64decode(entry['bytes'],validate=True)
                        os.ftruncate(fd,0)
                        with os.fdopen(fd,'wb',closefd=False) as stream:stream.write(data);stream.flush()
                        if self.owner_uid==0:os.fchown(fd,entry['uid'],entry['gid'])
                        os.fchmod(fd,entry['mode']);os.fsync(fd)
                    finally:os.close(fd)
                os.fsync(parent)
            finally:os.close(parent)
        measured=measure_committed_tree(self.root,commit=self.head,manifest=self.manifest,read_authority=self.read_authority)
        if measured!=state['before']:raise ExecutionBlocked('candidate rollback source differs from original baseline')
        state.update(state='rolled_back',rollback=measured);atomic_json(self.path,state)
        return {'clean_source_verified':True,'byte_perfect':True,'current_commit':self.head,'tree_sha256':measured['tree_sha256']}
