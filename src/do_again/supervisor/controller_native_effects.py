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

    def capture(self,sid,argv,timeout,*,read_only_source=False,extra_read_paths=()):
        broker=self.broker;root=EXECUTION_ROOT/broker.project.key/'requests'/sid
        scratch,cache=root/'scratch',root/'cache'
        for p in (scratch,cache):
            p.mkdir(parents=True,exist_ok=True,mode=0o700);os.chown(p,broker.project.uid,broker.project.gid)
        for p in (root,root.parent):p.chmod(0o711)
        packet={'operation':'execute','request_id':sid,'argv':argv,'cwd':'.','timeout':timeout}
        from .source_transaction import LOCAL_KIND
        source_writes=() if read_only_source or self.profile.get('kind') == LOCAL_KIND else None
        spec=launch_spec(broker.project,packet,scratch,cache,source_write_paths=source_writes)
        if read_only_source:
            spec['args'][2]+='(deny file-write* (subpath '+json.dumps(str(broker.project.worktree))+'))'
        for path in extra_read_paths:
            spec['args'][2]+='(allow file-read* (subpath '+json.dumps(str(path))+'))'
        result=capture(spec,timeout,start_guard=self.admission)
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
            if self.profile.get('kind') == 'native-development-local-v2' and path in self.profile['source_contract']: continue
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
            code=('import sys,os,json;os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"]="1";'
                'sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/runtimes/test-packages'))+');'
                'import pytest;sys.path.insert(0,os.getcwd());'
                'counts={"passed":0,"failed":0,"skipped":0,"collected":0};'
                'observer=type("NativeObserver",(),{"pytest_collection_finish":lambda self,session: counts.__setitem__("collected",len(session.items)),"pytest_runtest_logreport":lambda self,report: '
                'counts.__setitem__(report.outcome,counts.get(report.outcome,0)+1) '
                'if report.when=="call" or report.outcome=="skipped" else None})();'
                'status=pytest.main(["-q","-p","no:cacheprovider","--strict-markers",sys.argv[1]],plugins=[observer]);'
                'print("DO_AGAIN_NATIVE_TEST_COUNTS "+json.dumps(dict(counts,pytest_exit_code=int(status)),sort_keys=True));'
                'sys.exit(0 if status==0 and counts["passed"]>0 and counts["skipped"]==0 else 1)')
        result=self.capture(sid,[self.broker.config['python'],'-I','-S','-B','-c',code,case],60,
            **({'read_only_source':True} if self.profile.get('kind')=='native-development-autonomous-v1' else {}))
        if self.profile['test_runner']=='pytest':
            lines=result.get('stdout','').splitlines()
            if lines and lines[-1].startswith('DO_AGAIN_NATIVE_TEST_COUNTS '):
                counts=json.loads(lines[-1].removeprefix('DO_AGAIN_NATIVE_TEST_COUNTS '))
                if (set(counts)!={'passed','failed','skipped','collected','pytest_exit_code'}
                        or any(type(v) is not int or v<0 for v in counts.values())):
                    raise ExecutionBlocked('sealed pytest capture counts malformed')
                result['native_test_counts']=counts
            elif self.profile.get('kind') in {'native-development-local-v2','native-development-autonomous-v1'}:
                raise ExecutionBlocked('sealed pytest capture counts absent')
        return result

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
                'file_present': (broker.project.worktree / case.split('::', 1)[0]).is_file(),
                'pytest_exit_category': {0:'passed',1:'failed',2:'interrupted',3:'internal_error',
                    4:'collection_or_usage_error',5:'no_tests_collected'}.get(outcome.get('native_test_counts',{}).get('pytest_exit_code',outcome['returncode']),'other'),
                'native_test_counts':outcome.get('native_test_counts'),
                'stdout_sha256':hashlib.sha256(outcome['stdout'].encode()).hexdigest(),
                'stderr_sha256':hashlib.sha256(outcome['stderr'].encode()).hexdigest()})
        if before!=self.measure(head,manifest):raise ExecutionBlocked('planning probes changed source')
        autonomous=self.profile.get('kind')=='native-development-autonomous-v1'
        sources={};inventory={};budget=24576 if autonomous else 16384;contents={}
        for path in dict.fromkeys([*self.profile['allowed_paths'], *self.profile['test_files']]):
            if path not in manifest:continue
            data=(broker.project.worktree/path).read_bytes()
            if autonomous:contents[path]=data
            else:
                budget-=len(data)
                if budget<0:raise ExecutionBlocked('approved public source exposure exceeds 16 KiB')
                sources[path]=data.decode('utf-8')
        if autonomous:
            from .planning_source_evidence import summarize_public_sources
            sources,inventory=summarize_public_sources(contents,budget=budget,
                implementation_paths=self.profile['implementation_paths'])
        if before!=self.measure(head,manifest):raise ExecutionBlocked('source exposure measurement changed')
        evidence={'repo_head':head,'tree_sha256':before['tree_sha256'],'cases':cases,'public_sources':sources}
        if autonomous:evidence['public_source_inventory']=inventory
        result={'returncode':0,'timed_out':False,'source_sha':broker.config['source_sha'],'evidence':evidence}
        broker.ledger.finish(project,sid,result);return evidence

    def source_transaction(self, rid, request):
        from .source_transaction import SourceTransaction
        from .macos_execution import private_root_file
        directory=self.broker.state / (rid+'-rollback')
        if not directory.exists(): directory.mkdir(mode=0o700)
        snapshot=directory/'source-rollback.json'
        if snapshot.exists() or snapshot.is_symlink():
            private_root_file(snapshot)
            manifest=json.loads(snapshot.read_text())['manifest']
        else:
            manifest=self.manifest(rid+'-baseline',request['expected']['repo_head'])
        return SourceTransaction(self.broker.project.worktree,directory,self.profile,
            head=request['expected']['repo_head'],manifest=manifest,read_authority=self.head)

    def candidate_transaction(self,rid,request):
        from .candidate_transaction import CandidateTransaction
        directory=self.broker.state/(rid+'-candidate')
        path=directory/'candidate.json'
        manifest=json.loads(path.read_text())['original_manifest'] if path.exists() else self.manifest(rid+'-baseline',request['expected']['repo_head'])
        return CandidateTransaction(self.broker.project.worktree,directory,self.profile,
            head=request['expected']['repo_head'],manifest=manifest,read_authority=self.head)

    def __call__(self,stage,sid,request,stages):
        broker=self.broker;fp=request_fingerprint(request);base=request['expected']['repo_head']
        from ..service.offline_continuation import persistent_scope
        epoch=broker.config['controller_scope']['epoch']+1
        if persistent_scope(broker.config['controller_scope']):
            epoch=broker.registry.status(broker.project.repo)['epoch']
        from .candidate_transaction import KIND as AUTONOMOUS_KIND
        if self.profile.get('kind')==AUTONOMOUS_KIND:
            rid=request['request_id'];txn=self.candidate_transaction(rid,request)
            if stage=='snapshot':
                with self.admission():
                    broker.ledger.reserve(broker.project.key,sid,fp,intent={'operation':'execute',
                        'source_sha':broker.config['source_sha'],'request_fingerprint':fp,'aggregate':True})
                result={'returncode':0,'timed_out':False,'source_sha':broker.config['source_sha'],'baseline':txn.prepare()}
                broker.ledger.finish(broker.project.key,sid,result);return result
            if stage=='test':
                txn.freeze()
                with self.admission():
                    broker.ledger.reserve(broker.project.key,sid,fp,intent={'operation':'execute',
                        'source_sha':broker.config['source_sha'],'request_fingerprint':fp})
                run=observe_tests(expected_head=base,cases=tuple(self.select_tests(request)),
                    measure=txn.measure,execute=lambda case:self.run_case(sid,case),allow_failures=True)
                result={'returncode':int(any(c['exit_code']!=0 for c in run['cases'])),
                    'timed_out':False,'source_sha':broker.config['source_sha'],'native_test_run':run}
                broker.ledger.finish(broker.project.key,sid,result);return result
            if stage=='commit':
                tested=txn.measure()
                result=broker._dispatch({'operation':'git_commit','request_id':sid,
                    'expected_head':base,'expected_epoch':epoch,'paths':self.profile['allowed_paths'],
                    'message':'Do Again verified development '+rid,'tested_tree_sha256':tested['tree_sha256'],
                    'request_fingerprint':fp},broker.config['operator_uid'])
                if result.get('returncode')==0:
                    head=result['authority']['repo_head'];txn.committed(head,self.manifest(rid+'-committed',head))
                return result
            if stage=='failure_observation':
                from .macos_execution import MacOSProcesses
                with broker.admission():
                    MacOSProcesses().drain(broker.project.uid)
                    return txn.rollback()
        if self.profile.get('kind') == 'native-development-local-v2':
            rid = sid.rsplit('-',1)[0] if stage in {'regression_baseline','implementation','test','commit'} else sid
            if stage=='source_snapshot':
                # Original native failure identity is read-only provenance. No
                # past stage, reservation, or browser call is reopened.
                original=self.profile['original_task']
                observed=broker.ledger.observe_request(broker.project.key,
                    original['request_id']+'-implementation',original['request_fingerprint'])
                if (observed.get('state')!='terminal' or observed.get('result',{}).get('returncode')==0
                        or observed.get('result',{}).get('timed_out') is not False):
                    raise ExecutionBlocked('original task failure is not positively terminal')
                return self.source_transaction(rid,request).prepare()
            if stage=='regression_baseline':
                # Apply only already reviewed test bytes through the SAME
                # confined executor; implementation bytes stay pinned.
                code='from pathlib import Path\n'+''.join('Path('+repr(name)+').write_bytes('+repr(v['after_content'].encode())+')\n'
                    for name,v in self.profile['source_contract'].items() if v['preserve_prefix'])
                prepared=broker._dispatch({'operation':'execute','request_id':sid+'-edit',
                    'argv':[broker.config['python'],'-B','-c',code], 'cwd':'.','timeout':60,
                    'expected_head':base,'request_fingerprint':fp},broker.config['operator_uid'])
                if prepared.get('returncode')!=0 or prepared.get('timed_out',False):
                    raise ExecutionBlocked('native regression baseline preparation failed')
                with self.admission():
                    broker.ledger.reserve(broker.project.key,sid,fp,intent={'operation':'execute',
                        'source_sha':broker.config['source_sha'],'request_fingerprint':fp})
                txn=self.source_transaction(rid,request)
                try:
                    run=observe_tests(expected_head=base,cases=tuple(self.select_tests(request)),
                        measure=txn.regression_baseline,execute=lambda case:self.run_case(sid,case),allow_failures=True)
                    failed=[c for c in run['cases'] if c['exit_code']!=0]
                    actual_assertion_failure=bool(failed) and all(
                        c.get('native_test_counts',{}).get('pytest_exit_code')==1
                        and c.get('native_test_counts',{}).get('failed',0)>0 for c in failed)
                    result={'returncode':0 if actual_assertion_failure else 1,'timed_out':False,
                        'source_sha':broker.config['source_sha'],'native_regression_baseline':run}
                except Exception as exc:
                    from .macos_execution import MacOSProcesses
                    with broker.admission(): MacOSProcesses().drain(broker.project.uid)
                    result={'returncode':1,'timed_out':True,'source_sha':broker.config['source_sha'],
                        'error':'regression baseline capture failed: '+str(exc)}
                broker.ledger.finish(broker.project.key,sid,result);return result
            if stage=='source_rollback':
                from .macos_execution import MacOSProcesses
                with broker.admission():
                    MacOSProcesses().drain(broker.project.uid)
                    return self.source_transaction(rid,request).rollback()
            if stage=='source_committed':
                head=stages['commit']['authority']['repo_head']
                return dict(self.source_transaction(rid,request).committed(head,self.manifest(rid+'-final',head)),
                    epoch=broker.registry.status(broker.project.repo)['epoch'])
            if stage=='test':
                with self.admission():
                    broker.ledger.reserve(broker.project.key,sid,fp,intent={'operation':'execute',
                        'source_sha':broker.config['source_sha'],'request_fingerprint':fp})
                txn=self.source_transaction(rid,request)
                try:
                    run=observe_tests(expected_head=base,cases=tuple(self.select_tests(request)),
                        measure=txn.candidate,execute=lambda case:self.run_case(sid,case),allow_failures=True)
                    result={'returncode':int(any(c['exit_code']!=0 for c in run['cases'])),
                        'timed_out':False,'source_sha':broker.config['source_sha'],'native_test_run':run}
                except Exception as exc:
                    from .macos_execution import MacOSProcesses
                    with broker.admission(): MacOSProcesses().drain(broker.project.uid)
                    result={'returncode':1,'timed_out':True,'source_sha':broker.config['source_sha'],
                        'error':'native test capture or measurement failed: '+str(exc)}
                broker.ledger.finish(broker.project.key,sid,result);return result
            if stage=='commit':
                self.source_transaction(rid,request).candidate()
            if stage=='publication': raise ExecutionBlocked('sealed local-only contract prohibits publication')
        if stage=='failure_observation':
            # No mutation: prove that a known failed local stage left an exact
            # committed tree, including the unchanged immutable baseline tests.
            current=self.head();manifest=self.manifest(sid+'-failure-inspection',current)
            measured=self.measure(current,manifest)
            return {'clean_source_verified': True, 'current_commit':current,
                    'tree_sha256':measured['tree_sha256']}
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
                measure=lambda:self.measure(head,manifest),execute=lambda case:self.run_case(sid,case),
                allow_failures=persistent_scope(broker.config['controller_scope']))
            result={'returncode':int(any(case['exit_code']!=0 for case in run['cases'])),
                    'timed_out':False,'source_sha':broker.config['source_sha'],'native_test_run':run}
            broker.ledger.finish(broker.project.key,sid,result);return result
        if stage=='publication':
            # Recheck current source after test and before existing native Git export.
            manifest=self.manifest(sid+'-inspection',head);self.measure(head,manifest)
            return broker._dispatch({'operation':'git_publish','request_id':sid,'expected_head':head,
                'expected_epoch':epoch,'title':'Do Again tested offline proposal '+request['request_id'],
                'body':'Offline development only; exact head '+head+'. Native test evidence recorded in the control receipt.',
                'request_fingerprint':fp},broker.config['operator_uid'])
        raise ExecutionBlocked('unapproved native development stage')
