"""Trusted worker compatibility mirror; all remote history effects use the broker."""
from __future__ import annotations
import hashlib
from pathlib import Path
from ..supervisor.macos_client import broker_request
from ..supervisor.control_history import valid_path
from .schema import OperatorError, atomic_json, canonical_json, request_fingerprint


class BrokerControlHistory:
    def __init__(self,repo: Path,control: Path,epoch: int,*,rpc=None):
        self.repo=repo;self.control=control;self.epoch=epoch;self.known={};self.rpc=rpc or broker_request

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
