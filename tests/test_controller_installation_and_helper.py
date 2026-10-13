import copy
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from do_again.supervisor.controller_codex_helper import helper_profile,bounded_process,SealedCodexHelper,validate_hosted_scope
from do_again.supervisor.controller_installation import apply_controller_grant,validate_controller_stage
from do_again.supervisor.controller_configuration import validate_controller_configuration
from do_again.service.offline_continuation import digest
from do_again.supervisor.macos_execution import ExecutionBlocked

class GrantTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc);goal={'goal':'offline fixture qualification'}
        self.scope=dict(kind='jobpipe-offline-controller-v1',repository='Tran-Steven/jobpipe',branch='operator-control',
            source_sha='a'*40,goal_sha256=digest(goal),epoch=12,issued_at_utc=self.now.isoformat(),
            expires_at_utc=(self.now+timedelta(hours=1)).isoformat(),max_requests=3,max_cycles=3,
            max_outstanding=1,max_retries=0,planner='sealed-offline',integration='proposal-only',
            network_access=False,application_submissions=False,browser_messages=False)
        self.profile={'kind':'native-development-v1','test_runner':'unittest','allowed_paths':['src/recovery.py'],
            'test_cases':['test_recovery','test_dedupe','test_handoff'],'test_files':{'test_recovery.py':'b'*40},
            'public_evidence':True,'offline_tasks':[{'case':c,'gap':'gap','acceptance':'test','script':'pass'}
                for c in ('test_recovery','test_dedupe','test_handoff')]}
        self.package={'scope':self.scope,'goal':goal,'profile':self.profile}
        self.config={'source_sha':'a'*40,'development_scope':{'kind':'jobpipe-offline-v1'},
            'production_ready':False,'operator_uid':501,'operator_gid':20}
    def apply(self):apply_controller_grant(self.config,self.package,now=self.now)
    def test_exact_bounded_grant_applies_only_to_prepared_configuration(self):
        self.apply();self.assertEqual(validate_controller_configuration(self.config,epoch=12,now=self.now),self.scope)
    def test_unknown_authority_wrong_identity_and_tamper_reject(self):
        self.apply();original=copy.deepcopy(self.config)
        for section,key,value in [('controller_scope','max_requests',4),('controller_scope','integration','merge'),
            ('controller_scope','source_sha','c'*40),('controller_scope','network_access',True),
            ('controller_profile','allowed_paths',['../secret']),('controller_profile','test_cases',['x; curl']),
            ('controller_profile','allowed_paths',['test_recovery.py'])]:
            self.config=copy.deepcopy(original);self.config[section][key]=value
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):validate_controller_configuration(self.config,now=self.now)
        self.config=copy.deepcopy(original);self.config['controller_goal']={'goal':'changed'}
        with self.assertRaises(ExecutionBlocked):validate_controller_configuration(self.config,now=self.now)
    def test_v1a_hosted_authority_missing_fails_before_any_stage_or_git_probe(self):
        import importlib.util
        entry=Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py'
        spec=importlib.util.spec_from_file_location('v1a_preflight_fixture',entry)
        bundler=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundler)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);proposal=root/'controller.json';output=root/'output'
            proposal.write_text(json.dumps(self.package | {'scope':dict(self.scope,planner='codex-cli')}))
            proposal.chmod(0o600)
            with patch.object(bundler,'git') as git:
                with self.assertRaisesRegex(ValueError,'hosted-planner-grant'):
                    bundler.prepare(root/'source',root/'jobpipe',root/'do-again',output,
                                    controller_grant=proposal)
            git.assert_not_called()
            self.assertFalse(output.exists())

    def test_v1a_hosted_preflight_rejects_unapproved_network_before_stage(self):
        import importlib.util
        entry=Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py'
        spec=importlib.util.spec_from_file_location('v1a_preflight_network_fixture',entry)
        bundler=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundler)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);proposal=root/'controller.json';hosted=root/'hosted.json'
            proposal.write_text(json.dumps(self.package | {'scope':dict(self.scope,planner='codex-cli')}))
            proposal.chmod(0o600)
            hosted.write_text(json.dumps({'network_endpoints':[],'dns_resolvers':[]}))
            hosted.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'endpoint and DNS resolver'):
                bundler.preflight_controller_grants(proposal,hosted,uid=os.getuid())
            hosted.write_text(json.dumps({'network_endpoints':['8.8.8.8'],'dns_resolvers':['1.1.1.1']}))
            self.assertEqual(bundler.preflight_controller_grants(proposal,hosted,uid=os.getuid())[1]['network_endpoints'],['8.8.8.8'])
            hosted.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'private and unaliased'):
                bundler.preflight_controller_grants(proposal,hosted,uid=os.getuid())

    def test_v1a_scope_and_expiration_checked_before_any_bundle_copy(self):
        import importlib.util
        entry=Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py'
        spec=importlib.util.spec_from_file_location('v1a_preflight_scope_fixture',entry)
        bundler=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundler)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);proposal=root/'controller.json'
            for head,expired in [('c'*40,False),('a'*40,True)]:
                with self.subTest(head=head,expired=expired):
                    scope=dict(self.scope)
                    if expired:
                        scope['expires_at_utc']=(self.now-timedelta(seconds=1)).isoformat()
                    proposal.write_text(json.dumps(self.package | {'scope':scope}))
                    proposal.chmod(0o600)
                    output=root/('expired' if expired else 'source-mismatch')
                    def committed(repo,*args):
                        if args==('status','--porcelain'):return ''
                        if args==('rev-parse','HEAD'):return head
                        raise AssertionError('bundle proceeded to unapproved Git operation')
                    with patch.object(bundler,'git',side_effect=committed):
                        with self.assertRaises(ExecutionBlocked):
                            bundler.prepare(root/'source',root/'jobpipe',
                                            root/'do-again',output,
                                            controller_grant=proposal)
                    self.assertFalse(output.exists())

    def test_v1a_offline_controller_rejects_hosted_grant_without_stage(self):
        import importlib.util
        entry=Path(__file__).resolve().parents[1]/'tools/prepare_macos_supervisor.py'
        spec=importlib.util.spec_from_file_location('v1a_preflight_offline_fixture',entry)
        bundler=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundler)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);proposal=root/'controller.json';hosted=root/'hosted.json'
            proposal.write_text(json.dumps(self.package));proposal.chmod(0o600)
            hosted.write_text('{}');hosted.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'sealed-offline'):
                bundler.preflight_controller_grants(proposal,hosted,uid=os.getuid())
            self.assertEqual(bundler.preflight_controller_grants(proposal,None,uid=os.getuid())[1],None)

    def test_separate_hosted_authority_required(self):
        self.scope['planner']='codex-cli';self.profile['offline_tasks']=[]
        with self.assertRaises(ExecutionBlocked):self.apply()
    def test_offline_scope_rejects_hosted_payload_and_orphan_grants(self):
        self.apply()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'runtimes/codex').mkdir(parents=True)
            with self.assertRaises(ExecutionBlocked):validate_controller_stage(tmp,self.config,now=self.now)
        with self.assertRaises(ExecutionBlocked):validate_controller_configuration({'hosted_planning_scope':{}},now=self.now)
    def test_unprivileged_factory_rejected_before_backend_creation(self):
        from do_again.supervisor.controller_backend import create_controller_backend
        with patch('os.geteuid',return_value=501),patch('do_again.supervisor.controller_backend.ControllerBackend') as backend:
            with self.assertRaises(ExecutionBlocked):create_controller_backend(None)
        backend.assert_not_called()

    def test_missing_approved_pytest_dependencies_reject_stage(self):
        self.apply();self.config['controller_profile']['test_runner']='pytest'
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(ExecutionBlocked):
            validate_controller_stage(tmp,self.config,now=self.now)

    def test_preapproved_wheel_preparation_is_offline_and_rejects_tampering(self):
        from test_dependency_capability import wheel_fixture
        from do_again.supervisor.controller_installation import prepare_test_wheels
        content,artifact=wheel_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);wheels=root/'wheels';wheels.mkdir()
            (wheels/(artifact['id']+'.whl')).write_bytes(content)
            config={'projects':[{'account':'_doagain_jp','key':'jp'}],'dependency_artifacts':{'jp':[artifact]}}
            with patch('subprocess.Popen') as process:
                prepare_test_wheels(root/'payload',config,wheels)
            process.assert_not_called()
            self.assertTrue((root/'payload/runtimes/test-packages/fixture/__init__.py').is_file())
            (wheels/(artifact['id']+'.whl')).write_bytes(b'tampered')
            with self.assertRaises(ExecutionBlocked):prepare_test_wheels(root/'other',config,wheels)

    def test_exact_hosted_scope_validation_is_pure_and_rejects_network_tampering(self):
        self.apply();self.scope['planner']='codex-cli';self.profile['offline_tasks']=[]
        grant={'provider':'openai','transport':'codex-cli','model':'gpt-6.1-sol',
            'execution_uid':501,'execution_gid':20,'source_sha':'a'*40,'goal_sha256':self.scope['goal_sha256'],
            'epoch':12,'issued_at_utc':self.scope['issued_at_utc'],'expires_at_utc':self.scope['expires_at_utc'],
            'max_calls_per_cycle':1,'max_total_calls':3,'max_call_seconds':120,'max_total_seconds':360,
            'max_prompt_bytes':32768,'max_answer_bytes':65536,'worker_network_access':False,
            'browser_messages':False,'application_submissions':False,'network_endpoints':['8.8.8.8'],
            'dns_resolvers':['1.1.1.1'],'payload_sha256':'b'*64}
        # Public addresses are inert scope fixtures, never contacted.
        grant['backend_sha256']=digest({'payload_sha256':grant['payload_sha256'],'helper':'confined-codex-v1',
            'source_sha':'a'*40,'network_endpoints':grant['network_endpoints'],'dns_resolvers':grant['dns_resolvers']})
        with patch('subprocess.Popen') as process:
            self.assertEqual(validate_hosted_scope(self.config,grant,self.now),grant)
        process.assert_not_called()
        for key,value in [('network_endpoints',['127.0.0.1']),('dns_resolvers',[]),('backend_sha256','c'*64),
                           ('worker_network_access',True),('epoch',13)]:
            with self.subTest(key=key),self.assertRaises(ExecutionBlocked):
                validate_hosted_scope(self.config,dict(grant,**{key:value}),self.now)

    def test_factory_helper_absent_grant_has_no_process_effect(self):
        with patch('subprocess.Popen') as process,self.assertRaises(ExecutionBlocked):
            SealedCodexHelper({'hosted_planning_scope':None},Path('/unused'),lambda:None)
        process.assert_not_called()

class HelperConfinementTests(unittest.TestCase):
    def test_default_profile_denies_network_and_unrelated_data_and_exec(self):
        profile=helper_profile('/sealed/codex','/sealed/work')
        self.assertIn('(deny default)',profile);self.assertNotIn('network-outbound',profile)
        self.assertNotIn('/Users',profile);self.assertNotIn('/bin/sh',profile)
        with self.assertRaises(ExecutionBlocked):helper_profile('/sealed/codex','/sealed/work',endpoints=['127.0.0.1'])
    def test_child_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(ExecutionBlocked):
            bounded_process([os.sys.executable,'-c','import time;time.sleep(5)'],work=tmp,
                environment={'PATH':'/usr/bin:/bin'},timeout=0.05)
    @unittest.skipUnless(os.sys.platform=='darwin','macOS native offline qualification')
    def test_existing_native_cli_help_under_network_denied_profile(self):
        binary=Path('/Users/steventran/.do_again/codex-tools/node_modules/@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex')
        if not binary.is_file():self.skipTest('existing native Codex payload unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            result=bounded_process(['/usr/bin/sandbox-exec','-p',helper_profile(binary,tmp),str(binary),'exec','--help'],
                work=tmp,environment={'HOME':tmp,'CODEX_HOME':tmp,'PATH':'/usr/bin:/bin'},timeout=15)
            self.assertEqual(result['returncode'],0,result['stderr'])
            for option in ('--output-schema','--output-last-message','--ephemeral'):
                self.assertIn(option,result['stdout'])

class NativeDenialTests(unittest.TestCase):
    @unittest.skipUnless(os.sys.platform=='darwin','macOS sandbox qualification')
    def test_unrelated_file_read_and_unapproved_exec_are_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir();secret=root/'unrelated';secret.write_text('fixture secret')
            profile=helper_profile('/bin/cat',work)
            result=bounded_process(['/usr/bin/sandbox-exec','-p',profile,'/bin/cat',str(secret)],
                work=work,environment={'PATH':'/usr/bin:/bin'},timeout=5)
            self.assertNotEqual(result['returncode'],0);self.assertNotIn('fixture secret',result['stdout'])
            result=bounded_process(['/usr/bin/sandbox-exec','-p',profile,'/bin/sh','-c','echo forbidden'],
                work=work,environment={'PATH':'/usr/bin:/bin'},timeout=5)
            self.assertNotEqual(result['returncode'],0);self.assertNotIn('forbidden',result['stdout'])
