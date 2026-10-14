"""Trusted worker compatibility mirror; all remote history effects use the broker."""
from __future__ import annotations
import hashlib
import json
import os
import stat
from pathlib import Path
from ..supervisor.macos_client import broker_request
from ..supervisor.control_history import valid_path
from .schema import OperatorError, atomic_json, canonical_json, request_fingerprint


class BrokerControlHistory:
    def __init__(self,repo: Path,control: Path,epoch: int,*,rpc=None):
        self.repo=repo;self.control=control;self.epoch=epoch;self.known={};self.rpc=rpc or broker_request
        self._warm_known()

    def _warm_known(self):
        # These hashes are hints, never authority: the broker compares each
        # against the current remote tree and refreshes every mismatch. Only
        # exact canonical JSON bytes can avoid rehydration after restart.
        if not hasattr(os,'getuid'): return
        root=self.control/'automation/do_again'
        if not root.exists(): return
        from ..supervisor.control_history import blob_sha, PATH
        for file in root.rglob('*.json'):
            relative=file.relative_to(self.control).as_posix()
            if not PATH.fullmatch(relative): continue
            self.target(relative)
            fd=os.open(file,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
            try:
                info=os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                        or info.st_nlink!=1 or info.st_mode & 0o022 or info.st_size>1024*1024):
                    raise OperatorError('control cache record privacy or size differs')
                try:
                    with os.fdopen(fd,closefd=False) as stream:value=json.load(stream)
                except (ValueError,UnicodeError): continue
                if not isinstance(value,dict): continue
                self.known[relative]=blob_sha(canonical_json(value)+b'\n')
                if len(self.known)>5000: raise OperatorError('control cache exceeds budget')
            finally:os.close(fd)

    def target(self,path):
        valid_path(path)
        target=self.control/path
        for ancestor in (target,*target.parents):
            if ancestor.is_symlink():raise OperatorError('control mirror contains an alias')
            if ancestor==self.control:break
        if target.exists() and (not target.is_file() or target.stat().st_nlink!=1):
            raise OperatorError('control mirror contains a special file or hardlink')
        return target

    def sync(self):
        for _ in range(320):
            response=self.rpc(self.repo,{'operation':'control_sync','epoch':self.epoch,'known':self.known})
            for path in response['removed']:
                self.target(path).unlink(missing_ok=True);self.known.pop(path,None)
            for path,record in response['files'].items():
                atomic_json(self.target(path),record['value']);self.known[path]=record['sha']
            if response['complete']:return
        raise OperatorError('control synchronization exceeded its bounded batch count')

    def publish(self,path: Path,value: dict):
        relative=path.as_posix();valid_path(relative,write=True)
        digest=hashlib.sha256(canonical_json({'path':relative,'value':value,'epoch':self.epoch})).hexdigest()
        packet={'operation':'control_publish','request_id':'control-'+digest,'epoch':self.epoch,
                'path':relative,'value':value}
        result=self.rpc(self.repo,packet)
        if result.get('state')!='succeeded' or result.get('returncode')!=0:
            raise OperatorError('control publication lacks positive evidence')
        atomic_json(self.target(relative),value);self.known[relative]=result['blob']

    def claim(self,agent,request):
        self.sync()
        request_id=request['request_id'];fingerprint=request_fingerprint(request)
        existing=agent.claim_payload(request_id)
        if existing is not None:
            if existing.get('request_fingerprint')!=fingerprint:
                agent.publish_conflict(request=request,reason='request_id conflicts with a durable remote claim',
                                       existing_fingerprint=existing.get('request_fingerprint'))
                return False
            return existing.get('agent_instance_id')==agent.instance_id
        from .schema import utc_now
        import socket,os
        value={'schema_version':1,'request_id':request_id,'request_fingerprint':fingerprint,
               'agent_instance_id':agent.instance_id,'host':socket.gethostname(),'pid':os.getpid(),
               'claimed_at_utc':utc_now().isoformat()}
        self.publish(agent.claim_relative(request_id),value)
        return True
