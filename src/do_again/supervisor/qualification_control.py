"""In-memory Git-data fixture for installed qualification. No network effects.

Only the immutable maintenance qualifier creates this object; broker packets
cannot choose this transport or create authoritative production requests.
"""
import base64
import copy
import hashlib
from ..core.schema import canonical_json
from .control_history import blob_sha
from .macos_execution import ExecutionBlocked

class FixtureControlRepository:
    def __init__(self):
        self.head='a'*40;self.tree='b'*40;self.entries={};self.blobs={};self.writes=[];self.lose_patch=False
        self.commits={self.head:{'sha':self.head,'tree':{'sha':self.tree},'parents':[],'message':'baseline'}}
        self.trees={self.tree:{}}
    def request(self,method,endpoint,payload=None):
        if method!='GET':self.writes.append((method,endpoint,copy.deepcopy(payload)))
        if method=='GET':
            if endpoint=='git/ref/heads/operator-control':return {'object':{'sha':self.head}}
            kind,sha=endpoint.split('/')[1:3];sha=sha.split('?')[0]
            if kind=='commits':return self.commits[sha]
            if kind=='trees':return {'sha':sha,'truncated':False,'tree':[{'path':p,'sha':s,'mode':'100644','type':'blob'} for p,s in self.trees[sha].items()]}
            if kind=='blobs':
                data=self.blobs[sha];return {'encoding':'base64','size':len(data),'content':base64.b64encode(data).decode()}
        if endpoint=='git/blobs':
            data=base64.b64decode(payload['content']);sha=blob_sha(data);self.blobs[sha]=data;return {'sha':sha}
        if endpoint=='git/trees':
            entries=dict(self.trees[payload['base_tree']]);entries.update({e['path']:e['sha'] for e in payload['tree']})
            sha=hashlib.sha1(canonical_json(entries)).hexdigest();self.trees[sha]=entries;return {'sha':sha}
        if endpoint=='git/commits':
            sha=hashlib.sha1(canonical_json(payload)).hexdigest()
            self.commits[sha]=dict(payload,sha=sha,tree={'sha':payload['tree']},parents=[{'sha':p} for p in payload['parents']]);return {'sha':sha}
        if endpoint=='git/refs/heads/operator-control':
            assert payload['force'] is False;self.head=payload['sha']
            if self.lose_patch:
                self.lose_patch=False
                raise ExecutionBlocked('synthetic lost reference response')
            return {'object':{'sha':self.head}}
        raise AssertionError(endpoint)

