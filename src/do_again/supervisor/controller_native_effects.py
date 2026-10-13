"""Fixed production adapters over existing protected native capabilities."""
import hashlib
import json
import os
import uuid
from pathlib import Path
from ..core.schema import canonical_json, request_fingerprint
from .macos_execution import EXECUTION_ROOT, INSTALL_ROOT, ExecutionBlocked, capture, launch_spec
from .native_test_evidence import measure_committed_tree, observe_tests

class NativeControllerEffects:
    def __init__(self,broker,profile,admission,select_tests):
        self.broker,self.profile,self.admission=broker,profile,admission
        self.select_tests=select_tests

    def head(self):
        from .macos_server import worktree_authority
        return worktree_authority(self.broker.project.worktree)['repo_head']

    def capture(self,sid,argv,timeout):
        broker=self.broker;root=EXECUTION_ROOT/broker.project.key/'requests'/sid
        scratch,cache=root/'scratch',root/'cache'
        for p in (scratch,cache):
            p.mkdir(parents=True,exist_ok=True,mode=0o700);os.chown(p,broker.project.uid,broker.project.gid)
        for p in (root,root.parent):p.chmod(0o711)
        packet={'operation':'execute','request_id':sid,'argv':argv,'cwd':'.','timeout':timeout}
        result=capture(launch_spec(broker.project,packet,scratch,cache),timeout,start_guard=self.admission)
        # This flag is established by verified launch_spec confinement, not child stdout.
        result['network_denied']=True
        return result

    def prepare_manifest_metadata(self,sid,head):
        broker=self.broker
        # Git metadata belongs to the operator, not the sandboxed execution UID.
        # Export only allowlisted inert Git objects under root admission, using
        # the same sanitized copier as native Git transactions. Never expand
        # the worker's access to authoritative .git or host Git configuration.
        from .git_capabilities import copy_git_data
        if head!=self.head():
            raise ExecutionBlocked('manifest source head changed before protected Git export')
        root=EXECUTION_ROOT/broker.project.key/'requests'/sid
        root.mkdir(parents=True,exist_ok=True,mode=0o711)
        (root/'scratch').mkdir(mode=0o700,exist_ok=True)
        cache=root/'cache';cache.mkdir(mode=0o700,exist_ok=True)
        for path in (root/'scratch',cache):
            os.chown(path,broker.project.uid,broker.project.gid)
        for path in (root,root.parent):
            path.chmod(0o711)
        metadata=cache/('git-manifest-'+uuid.uuid4().hex)
        with self.admission():
            copy_git_data(broker.project.worktree/'.git',metadata)
            if head!=self.head():
                raise ExecutionBlocked('manifest source head changed during protected Git export')
            # Root-owned, world-readable inert Git snapshot. Worker UID cannot
            # chmod or write individual metadata files, while the parent cache
            # remains an untrusted per-request workspace.
            for directory,dirs,files in os.walk(metadata,followlinks=False):
                for name in files:
                    item=Path(directory)/name
                    if item.is_symlink() or item.stat().st_nlink!=1:
                        raise ExecutionBlocked('exported Git metadata contains an alias')
                    os.chown(item,0,0)
                    item.chmod(0o444)
                for name in dirs:
                    item=Path(directory)/name
                    if item.is_symlink():
                        raise ExecutionBlocked('exported Git metadata directory is aliased')
                    os.chown(item,0,0)
                    item.chmod(0o555)
            os.chown(metadata,0,0)
            metadata.chmod(0o555)
        return metadata

    def manifest(self,sid,head):
        broker=self.broker
        metadata=self.prepare_manifest_metadata(sid,head)
        code=('import sys;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
            'from do_again.supervisor.git_manifest import tree_manifest;import json;'
            'print(json.dumps(tree_manifest(sys.argv[1],sys.argv[2],sys.argv[3])))')
        result=self.capture(sid,[broker.config['python'],'-I','-S','-B','-c',code,
            broker.config['git'],str(metadata),head],30)
        if result['returncode'] or result['timed_out'] or result.get('stdout_truncated'):
            diagnostic={'exit_code':result['returncode'],'timed_out':result['timed_out'],
                'stdout_truncated':result.get('stdout_truncated',False),
                'stderr_sha256':hashlib.sha256(result.get('stderr','').encode()).hexdigest()}
            raise ExecutionBlocked('sealed Git manifest capture failed: '+json.dumps(diagnostic,sort_keys=True))
        manifest=json.loads(result['stdout'])
        for path,blob in self.profile['test_files'].items():
            if manifest.get(path,{}).get('blob_sha')!=blob:
                raise ExecutionBlocked('approved baseline tests changed')
        return manifest

    def measure(self,head,manifest):
        return measure_committed_tree(self.broker.project.worktree,commit=head,
            manifest=manifest,read_authority=self.head)

    def run_case(self,sid,case):
        # Load the trusted runner before adding mutable repository imports.
        # unittest's normal zero-tests exit(0) must never be accepted.
        if self.profile['test_runner']=='unittest':
            code=('import unittest,sys,os;sys.path.insert(0,os.getcwd());'
                'suite=unittest.defaultTestLoader.loadTestsFromName(sys.argv[1]);'
                'count=suite.countTestCases();'
                'result=unittest.TextTestRunner().run(suite);'
                'sys.exit(0 if count>0 and result.testsRun==count and result.wasSuccessful() and '
                'not result.skipped else 1)')
        else:
            code=('import sys,os;os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"]="1";'
                'sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/runtimes/test-packages'))+');'
                'import pytest;sys.path.insert(0,os.getcwd());'
                'counts={"passed":0,"skipped":0};'
                'observer=type("NativeObserver",(),{"pytest_runtest_logreport":lambda self,report: '
                'counts.__setitem__(report.outcome,counts.get(report.outcome,0)+1) '
                'if report.when=="call" or report.outcome=="skipped" else None})();'
                'status=pytest.main(["-q","-p","no:cacheprovider","--strict-markers",sys.argv[1]],plugins=[observer]);'
                'sys.exit(0 if status==0 and counts["passed"]>0 and counts["skipped"]==0 else 1)')
        return self.capture(sid,[self.broker.config['python'],'-I','-S','-B','-c',code,case],60)

    def collect(self,sid,fp):
        """Durable, independently captured failure probes for planning only."""
        broker=self.broker;project=broker.project.key;head=self.head()
        old=broker.ledger.observe_request(project,sid,fp)
        if old['state']=='terminal':
            if old['result']['evidence']['repo_head']!=head:raise ExecutionBlocked('planning evidence head differs')
            return old['result']['evidence']
        if old['state']!='not_started':raise ExecutionBlocked('planning probe uncertain; no replay')
        with self.admission():
            broker.ledger.reserve(project,sid,fp,intent={'operation':'execute',
                'source_sha':broker.config['source_sha'],'request_fingerprint':fp})
        manifest=self.manifest(sid,head);before=self.measure(head,manifest);cases=[]
        for case in self.profile['test_cases']:
            outcome=self.run_case(sid,case)
            if outcome['timed_out'] or outcome.get('stdout_truncated') or outcome.get('stderr_truncated'):
                raise ExecutionBlocked('planning probe incomplete')
            cases.append({'case':case,'returncode':outcome['returncode'],
                'stdout_sha256':hashlib.sha256(outcome['stdout'].encode()).hexdigest(),
                'stderr_sha256':hashlib.sha256(outcome['stderr'].encode()).hexdigest()})
        if before!=self.measure(head,manifest):raise ExecutionBlocked('planning probes changed source')
        sources={};budget=16384
        for path in self.profile['allowed_paths']:
            if path not in manifest:continue
            data=(broker.project.worktree/path).read_bytes();budget-=len(data)
            if budget<0:raise ExecutionBlocked('approved public source exposure exceeds 16 KiB')
            sources[path]=data.decode('utf-8')
        if before!=self.measure(head,manifest):raise ExecutionBlocked('source exposure measurement changed')
        evidence={'repo_head':head,'tree_sha256':before['tree_sha256'],'cases':cases,'public_sources':sources}
        result={'returncode':0,'timed_out':False,'source_sha':broker.config['source_sha'],'evidence':evidence}
        broker.ledger.finish(project,sid,result);return evidence

    def __call__(self,stage,sid,request,stages):
        broker=self.broker;fp=request_fingerprint(request);base=request['expected']['repo_head']
        epoch=broker.config['controller_scope']['epoch']+1
        if stage=='implementation':
            return broker._dispatch({'operation':'execute','request_id':sid,
                'argv':[broker.config['python'],'-B','-c',request['args']['content']],
                'cwd':'.','timeout':request['limits']['timeout_seconds'],
                'expected_head':base,'expected_authority':request['expected'],
                'request_fingerprint':fp},broker.config['operator_uid'])
        if stage=='commit':
            return broker._dispatch({'operation':'git_commit','request_id':sid,
                'expected_head':base,'expected_epoch':epoch,'paths':self.profile['allowed_paths'],
                'message':'Do Again offline development '+request['request_id'],
                'request_fingerprint':fp},broker.config['operator_uid'])
        head=stages['commit']['authority']['repo_head']
        if stage=='test':
            with self.admission():
                broker.ledger.reserve(broker.project.key,sid,fp,intent={'operation':'execute',
                    'source_sha':broker.config['source_sha'],'request_fingerprint':fp})
            manifest=self.manifest(sid,head)
            run=observe_tests(expected_head=head,cases=tuple(self.select_tests(request)),
                measure=lambda:self.measure(head,manifest),execute=lambda case:self.run_case(sid,case))
            result={'returncode':0,'timed_out':False,'source_sha':broker.config['source_sha'],'native_test_run':run}
            broker.ledger.finish(broker.project.key,sid,result);return result
        if stage=='publication':
            # Recheck current source after test and before existing native Git export.
            manifest=self.manifest(sid,head);self.measure(head,manifest)
            return broker._dispatch({'operation':'git_publish','request_id':sid,'expected_head':head,
                'expected_epoch':epoch,'title':'Do Again tested offline proposal '+request['request_id'],
                'body':'Offline development only; exact head '+head+'. Native test evidence recorded in the control receipt.',
                'request_fingerprint':fp},broker.config['operator_uid'])
        raise ExecutionBlocked('unapproved native development stage')
