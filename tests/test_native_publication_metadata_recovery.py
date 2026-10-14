import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from do_again.supervisor import native_publication_metadata_recovery as r
from do_again.supervisor.macos_execution import ExecutionBlocked

class MetadataRecoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.pr={'number':28,'state':'open','draft':True,'html_url':'https://github.com/Tran-Steven/jobpipe/pull/28',
                 'head':{'sha':r.HEAD,'ref':r.BRANCH,'repo':{'full_name':'Tran-Steven/jobpipe'}},
                 'base':{'ref':'main','repo':{'full_name':'Tran-Steven/jobpipe'}},'title':r.OLD_TITLE,'body':r.OLD_BODY}
        self.intent={'operation':'git_publish','repository':'Tran-Steven/jobpipe','head':r.HEAD,'branch':r.BRANCH,
            'source_sha':r.SOURCE,'title':r.TITLE,'body':r.BODY,'request_fingerprint':r.FP,
            'classification':'operator_approved_tested_checkpoint'}
    def test_only_exact_old_or_exact_authorized_metadata_accepted(self):
        self.assertEqual(r.validate_pr(self.pr,allow_original=True),'original')
        with self.assertRaises(ExecutionBlocked):r.validate_pr(self.pr,allow_original=False)
        self.pr.update(title=r.TITLE,body=r.BODY)
        self.assertEqual(r.validate_pr(self.pr,allow_original=False),'complete')
    def test_identity_and_independent_payload_changes_block(self):
        for key,value in [('number',29),('state','closed'),('draft',False),('body','independent change'),('html_url','https://other.invalid')]:
            pr=copy.deepcopy(self.pr);pr[key]=value
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):r.validate_pr(pr,allow_original=True)
        for section,key,value in [('head','sha',r.BASE),('head','ref','main'),('base','ref','other')]:
            pr=copy.deepcopy(self.pr);pr[section][key]=value
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):r.validate_pr(pr,allow_original=True)
    def test_intent_cannot_broaden_or_change_original_fresh_scope(self):
        r.validate_intent(self.intent)
        for key in self.intent:
            changed=dict(self.intent);changed[key]='other'
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):r.validate_intent(changed)
        with self.assertRaises(ExecutionBlocked):r.validate_intent(dict(self.intent,extra=True))

    def complete(self, *, lost=False, mutate=True, guard=None, execute=True):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.audit=Path(temp.name)/'audit.json';self.effects=[]
        test=self
        class API:
            repository='Tran-Steven/jobpipe'
            def request(self,method,endpoint,payload=None):
                if method=='GET':
                    if endpoint.startswith('git/ref/'):return {'object':{'sha':r.HEAD}}
                    if endpoint.startswith('pulls?'):return [copy.deepcopy(test.pr)]
                    if endpoint=='pulls/28':return copy.deepcopy(test.pr)
                    raise AssertionError(endpoint)
                test.effects.append((method,endpoint,payload))
                assert method=='PATCH' and endpoint=='pulls/28' and payload=={'title':r.TITLE,'body':r.BODY}
                assert json.loads(test.audit.read_text())['state']=='started'
                if mutate:test.pr.update(payload)
                if lost:raise OSError('lost PATCH response')
        self.api=API();self.guard=guard or (lambda:None)
        return r.complete_metadata(self.api,self.audit,r.proposed_action(),self.guard,execute_approved_action=execute)

    def test_reserved_before_sole_metadata_patch_no_git_effects(self):
        self.assertEqual(self.complete()['state'],'metadata_verified')
        self.assertEqual(len(self.effects),1)
        self.assertEqual(json.loads(self.audit.read_text())['state'],'started')

    def test_lost_patch_reply_only_readback_can_recover(self):
        with self.assertRaises(OSError):self.complete(lost=True)
        result=r.complete_metadata(self.api,self.audit,r.proposed_action(),self.guard,execute_approved_action=True)
        self.assertEqual(result['state'],'metadata_verified');self.assertEqual(len(self.effects),1)

    def test_lost_reply_without_observed_metadata_never_replays(self):
        with self.assertRaises(OSError):self.complete(lost=True,mutate=False)
        with self.assertRaisesRegex(ExecutionBlocked,'no replay'):
            r.complete_metadata(self.api,self.audit,r.proposed_action(),self.guard,execute_approved_action=True)
        self.assertEqual(len(self.effects),1)

    def test_guard_failure_preserves_reservation_without_effect(self):
        def stop():raise ExecutionBlocked('paused')
        with self.assertRaises(ExecutionBlocked):self.complete(guard=stop)
        self.assertTrue(self.audit.exists());self.assertEqual(self.effects,[])

    def test_dry_run_cannot_reserve_or_patch(self):
        self.assertEqual(self.complete(execute=False)['state'],'prepared')
        self.assertFalse(self.audit.exists());self.assertEqual(self.effects,[])

    def test_complete_dry_run_with_or_without_reservation_is_read_only(self):
        self.pr.update(title=r.TITLE,body=r.BODY)
        self.assertEqual(self.complete(execute=False)['state'],'prepared')
        self.assertFalse(self.audit.exists())
        self.audit.write_text(json.dumps({'proposal':r.proposed_action(),'state':'started'}))
        original=self.audit.read_bytes()
        result=r.complete_metadata(self.api,self.audit,r.proposed_action(),self.guard,execute_approved_action=False)
        self.assertEqual(result['state'],'prepared');self.assertEqual(self.audit.read_bytes(),original)
        self.assertEqual(self.effects,[])

class FullRecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        import contextlib,fcntl,importlib.util,os,sqlite3,sys
        from types import SimpleNamespace
        from do_again.supervisor.macos_execution import ExecutionLedger
        from do_again.supervisor.publication_quarantine import append_quarantine
        self.ctx=contextlib.ExitStack();self.addCleanup(self.ctx.close)
        tmp=self.ctx.enter_context(tempfile.TemporaryDirectory());self.root=Path(tmp).resolve()
        self.stage=self.root/'stage';self.stage.mkdir();(self.root/'current').mkdir()
        self.key='a'*64;self.repo=self.root/'repo';self.repo.mkdir()
        project={'key':self.key,'repo':str(self.repo),'worktree':str(self.repo),'account':'_doagain_jp','uid':402,'github_repository':'Tran-Steven/jobpipe'}
        self.config={'source_sha':r.SOURCE,'production_ready':False,'authority_path':str(self.root/'ledger.sqlite'),'projects':[project]}
        (self.root/'current/config.json').write_text(json.dumps(self.config));(self.stage/'config.json').write_text(json.dumps(self.config))
        (self.stage/'publication-metadata-recovery.json').write_text(json.dumps(r.proposed_action()))
        (self.stage/'install.py').write_bytes((Path(__file__).resolve().parents[1]/'tools/macos_supervisor_install.py').read_bytes())
        self.state=self.root/'state'/self.key;self.state.mkdir(parents=True)
        lock=self.state/'admission.lock';lock.write_bytes(b'');lock.chmod(0o600)
        self.ledger=ExecutionLedger(Path(self.config['authority_path']))
        self.old='offline-3efb655c06613370021a-1-publication';self.oldfp='4b34694fa5f16e808412ff157105bea4ad6dc4902035483c56803e49d572e182'
        oldintent={'operation':'git_publish','source_sha':'43a45ca7a0259ddded1858225ea365f0240e4752','request_fingerprint':self.oldfp}
        self.ledger.reserve(self.key,self.old,'c'*64,intent=oldintent)
        with sqlite3.connect(self.ledger.path) as db:append_quarantine(db,self.key,self.old,expected_intent=oldintent,approval_reference='explicit recovery')
        intent={'operation':'git_publish','repository':'Tran-Steven/jobpipe','head':r.HEAD,'branch':r.BRANCH,'source_sha':r.SOURCE,
            'title':r.TITLE,'body':r.BODY,'request_fingerprint':r.FP,'classification':'operator_approved_tested_checkpoint'}
        self.ledger.reserve(self.key,r.REQUEST,'d'*64,intent=intent)
        self.original=self.ledger.observe_request(self.key,self.old,self.oldfp)
        self.pr={'number':28,'state':'open','draft':True,'html_url':'https://github.com/Tran-Steven/jobpipe/pull/28',
                 'head':{'sha':r.HEAD,'ref':r.BRANCH,'repo':{'full_name':'Tran-Steven/jobpipe'}},
                 'base':{'ref':'main','repo':{'full_name':'Tran-Steven/jobpipe'}},'title':r.OLD_TITLE,'body':r.OLD_BODY}
        self.effects=[];self.lose=False;test=self
        class API:
            repository='Tran-Steven/jobpipe'
            def request(self,method,endpoint,payload=None):
                if method=='GET':
                    if endpoint.startswith('git/ref/'):return {'object':{'sha':r.HEAD}}
                    if endpoint.startswith('pulls?'):return [copy.deepcopy(test.pr)]
                    if endpoint=='pulls/28':return copy.deepcopy(test.pr)
                    raise AssertionError(endpoint)
                assert method=='PATCH' and endpoint=='pulls/28' and payload=={'title':r.TITLE,'body':r.BODY}
                test.effects.append((method,endpoint,payload));test.pr.update(payload)
                if test.lose:raise OSError('lost response')
        status={'intent':'maintenance','epoch':37};self.api=API()
        spec_fn=importlib.util.spec_from_file_location
        def spec(name,path):
            item=spec_fn(name,path);execute=item.loader.exec_module
            def load(module):
                execute(module);module.ROOT=test.root;module.verify_worker_quiescence=lambda *a,**kw:None
            item.loader.exec_module=load;return item
        fstat=os.fstat
        def root_stat(fd):
            actual=fstat(fd);fields={n:getattr(actual,n) for n in dir(actual) if n.startswith('st_')};fields['st_uid']=0
            return SimpleNamespace(**fields)
        flags={n:getattr(sys.flags,n) for n in dir(sys.flags) if not n.startswith('_') and not callable(getattr(sys.flags,n))}
        flags.update(isolated=1,no_site=1,dont_write_bytecode=1)
        for mocked in (patch.object(r,'INSTALL_ROOT',self.root),patch.object(r,'private_root_file'),patch('do_again.supervisor.macos_execution.private_root_file'),
            patch.object(r.sys,'platform','darwin'),patch.object(r.os,'geteuid',return_value=0),patch.object(r.sys,'flags',SimpleNamespace(**flags)),
            patch.object(r.os,'fstat',side_effect=root_stat),patch('importlib.util.spec_from_file_location',side_effect=spec),
            patch.object(r,'AuthorityRegistry',return_value=SimpleNamespace(status=lambda repo:dict(status))),
            patch.object(r,'MacOSProcesses',return_value=SimpleNamespace(owned=lambda uid:[])),
            patch('do_again.supervisor.macos_server.verify_installation'),
            patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':r.HEAD,'repo_branch':r.BRANCH}),
            patch.object(r,'verify_checkpoints',return_value={self.key:{'commit':r.HEAD}}),patch.object(r,'read_credential',return_value='x'*40),patch('do_again.supervisor.publication.read_credential',return_value='x'*40),
            patch.object(r,'GitHubRepository',return_value=self.api),patch('do_again.supervisor.publication.GitHubRepository',return_value=self.api)):
            self.ctx.enter_context(mocked)
    def test_actual_recovery_preview_complete_and_terminal_reentry(self):
        before=self.ledger.evidence(self.key)
        self.assertEqual(r.recover(self.stage)['state'],'prepared');self.assertEqual(self.ledger.evidence(self.key),before);self.assertEqual(self.effects,[])
        result=r.recover(self.stage,execute_approved_action=True);self.assertEqual(result['state'],'succeeded')
        audit=self.state/(r.REQUEST+'-metadata-completion.json');original_audit=audit.read_bytes()
        terminal=self.ledger.evidence(self.key)
        self.assertEqual(r.recover(self.stage)['state'],'prepared');self.assertEqual(self.ledger.evidence(self.key),terminal)
        self.assertEqual(r.recover(self.stage,execute_approved_action=True)['state'],'succeeded')
        self.assertEqual(self.ledger.evidence(self.key),terminal);self.assertEqual(audit.read_bytes(),original_audit);self.assertEqual(len(self.effects),1)
        self.assertEqual(self.ledger.observe_request(self.key,self.old,self.oldfp),self.original)
    def test_lost_patch_response_full_recovery_uses_only_original_reads(self):
        self.lose=True
        with self.assertRaises(OSError):r.recover(self.stage,execute_approved_action=True)
        before=self.ledger.evidence(self.key);audit=(self.state/(r.REQUEST+'-metadata-completion.json')).read_bytes()
        self.assertEqual(r.recover(self.stage)['state'],'prepared');self.assertEqual(self.ledger.evidence(self.key),before)
        self.assertEqual(r.recover(self.stage,execute_approved_action=True)['state'],'succeeded');self.assertEqual(len(self.effects),1)
        self.assertEqual((self.state/(r.REQUEST+'-metadata-completion.json')).read_bytes(),audit)

    def test_terminal_without_reservation_never_updates_reverted_metadata(self):
        self.pr.update(title=r.TITLE,body=r.BODY)
        self.assertEqual(r.recover(self.stage,execute_approved_action=True)['state'],'succeeded')
        self.assertFalse((self.state/(r.REQUEST+'-metadata-completion.json')).exists())
        before=self.ledger.evidence(self.key)
        self.pr.update(title=r.OLD_TITLE,body=r.OLD_BODY)
        with self.assertRaisesRegex(ExecutionBlocked,'no additional PATCH'):
            r.recover(self.stage,execute_approved_action=True)
        self.assertEqual(self.effects,[]);self.assertEqual(self.ledger.evidence(self.key),before)
