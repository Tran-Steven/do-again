from __future__ import annotations

import hashlib
import io
import stat
import tempfile
import os
import sys
import threading
import shutil
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from do_again.supervisor.dependencies import approved_artifact, install_via_broker
from do_again.supervisor.macos_execution import ExecutionBlocked, ExecutionLedger, ProjectExecution
from do_again.supervisor.network import https_bytes
from do_again.supervisor.wheels import install_wheel, validate_wheel


def wheel_fixture(extra=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as wheel:
        files = {'fixture/__init__.py':'VALUE = 7\n',
                 'fixture-1.0.dist-info/METADATA':'Name: fixture\nVersion: 1.0\n',
                 'fixture-1.0.dist-info/WHEEL':'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
        for name, content in (files | (extra or {})).items():
            wheel.writestr(name, content)
    content = data.getvalue()
    return content, {'id':'fixture-1','name':'fixture','version':'1.0',
                     'url':'https://files.pythonhosted.org/packages/fixture.whl',
                     'sha256':hashlib.sha256(content).hexdigest()}


class DependencyTests(unittest.TestCase):
    def test_exact_approved_wheel_installs_offline_without_overwriting(self):
        content, artifact = wheel_fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); wheel = root/'artifact.whl'; wheel.write_bytes(content)
            destination = root/'site-packages'
            install_wheel(wheel, destination, artifact)
            self.assertEqual((destination/'fixture/__init__.py').read_text(), 'VALUE = 7\n')
            with self.assertRaises(FileExistsError):install_wheel(wheel, destination, artifact)

    def test_hash_identity_version_and_platform_are_required(self):
        content, artifact = wheel_fixture()
        for change in ({'sha256':'0'*64}, {'name':'different'}, {'version':'2.0'}):
            with self.subTest(change=change), self.assertRaises(ExecutionBlocked):
                validate_wheel(content, artifact | change)
        content, artifact = wheel_fixture({'fixture-1.0.dist-info/WHEEL':'Root-Is-Purelib: false\nTag: cp313-cp313-macosx\n'})
        with self.assertRaises(ExecutionBlocked):validate_wheel(content, artifact)

    def test_traversal_aliases_startup_hooks_and_install_scripts_are_denied(self):
        for name in ('../outside','/absolute','foo/../outside','foo//bar','foo\\bar','C:/outside',
                     'startup.pth','fixture-1.0.data/scripts/executable'):
            with self.subTest(name=name):
                content, artifact = wheel_fixture({name:'bad'})
                with self.assertRaises(ExecutionBlocked):validate_wheel(content, artifact)

    def test_symlink_and_duplicate_archive_members_are_denied(self):
        content, artifact = wheel_fixture()
        data = io.BytesIO(content)
        with zipfile.ZipFile(data, 'a') as wheel:
            info = zipfile.ZipInfo('alias');info.external_attr = (stat.S_IFLNK | 0o777) << 16
            wheel.writestr(info, '/outside')
        content = data.getvalue();artifact['sha256'] = hashlib.sha256(content).hexdigest()
        with self.assertRaises(ExecutionBlocked):validate_wheel(content, artifact)

    def test_project_approval_does_not_expand_from_a_model_identifier(self):
        _, artifact = wheel_fixture()
        config = {'dependency_artifacts':{'project':[artifact]}}
        self.assertEqual(approved_artifact(config,'project','fixture-1'), artifact)
        for key, identifier in (('other','fixture-1'),('project','unapproved'),('project','../fixture-1')):
            with self.assertRaises(ExecutionBlocked):approved_artifact(config,key,identifier)
        config['dependency_artifacts']['project'].append(artifact)
        with self.assertRaises(ExecutionBlocked):approved_artifact(config,'project','fixture-1')

    def test_unapproved_requests_cannot_contact_network_or_execute(self):
        with patch('do_again.supervisor.dependencies.sys.platform','darwin'), \
                patch('do_again.supervisor.dependencies.os.geteuid',return_value=0,create=True), \
                patch('do_again.supervisor.dependencies.https_bytes') as fetch, \
                patch('do_again.supervisor.dependencies.capture') as capture:
            broker = Mock(config={},project=Mock(key='project'))
            packet = {'operation':'dependency_install','request_id':'dependency-request',
                      'expected_head':'a'*40,'expected_epoch':1,'artifact_id':'unapproved',
                      'request_fingerprint':'b'*64}
            for change in ({}, {'url':'https://evil.invalid/pkg.whl'}, {'expected_epoch':True}):
                with self.assertRaises(ExecutionBlocked):install_via_broker(broker, packet | change)
            fetch.assert_not_called();capture.assert_not_called()

    def test_network_origin_redirect_and_byte_budget_fail_closed(self):
        with patch('do_again.supervisor.network.http.client.HTTPSConnection') as constructor:
            for url in ('http://files.pythonhosted.org/pkg', 'https://evil.invalid/pkg',
                        'https://user:secret@files.pythonhosted.org/pkg',
                        'https://files.pythonhosted.org:444/pkg', 'https://files.pythonhosted.org/pkg?q=1'):
                with self.assertRaises(ExecutionBlocked):https_bytes(url,host='files.pythonhosted.org',limit=4)
            constructor.assert_not_called()
            response = constructor.return_value.getresponse.return_value
            response.status = 302
            with self.assertRaises(ExecutionBlocked):https_bytes('https://files.pythonhosted.org/pkg',host='files.pythonhosted.org',limit=4)
            response.status = 200;response.read.side_effect = [b'12345']
            with self.assertRaises(ExecutionBlocked):https_bytes('https://files.pythonhosted.org/pkg',host='files.pythonhosted.org',limit=4)
            constructor.return_value.close.assert_called()


class DependencyBrokerTests(unittest.TestCase):
    def setUp(self):
        from do_again.supervisor import dependencies
        self.module = dependencies
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        package = self.root/'install/current/package/do_again'
        shutil.copytree(Path(__file__).resolve().parents[1]/'src/do_again',package,
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.repo = self.root/'worktree';self.repo.mkdir()
        self.content, self.artifact = wheel_fixture()
        project = ProjectExecution(self.repo,401,401,'_doagain_da',self.repo,(Path(sys.executable),))
        self.status = {'intent':'active','epoch':1,'goal_revision':'approved-fixture'}
        @contextmanager
        def admission():yield
        self.broker = SimpleNamespace(project=project,lock=threading.Lock(),admission=admission,
                    registry=SimpleNamespace(status=lambda repo:dict(self.status)),_verified=lambda:True,
                    ledger=ExecutionLedger(self.root/'ledger.sqlite'),config={'production_ready':True,
                    'python':sys.executable,'source_sha':'fixture-source',
                    'dependency_artifacts':{project.key:[self.artifact]}})
        self.packet = {'operation':'dependency_install','request_id':'dependency-request-1',
                       'expected_head':'a'*40,'expected_epoch':1,'artifact_id':'fixture-1','request_fingerprint':'b'*64}
        self.calls = 0
        def capture(spec, timeout, *, start_guard):
            with start_guard():
                self.calls += 1
                result = subprocess.run(spec['argv'],capture_output=True,text=True,timeout=timeout)
            return {'returncode':result.returncode,'timed_out':False,'stdout':result.stdout}
        # Native confinement is separately qualified; this fixture runs the real
        # immutable extraction module through a substituted execution backend.
        patches = [patch.object(dependencies.sys,'platform','darwin'),
                   patch.object(dependencies.os,'geteuid',return_value=0,create=True),
                   patch.object(dependencies.os,'chown',create=True),
                   patch.object(dependencies,'INSTALL_ROOT',self.root/'install'),
                   patch.object(dependencies,'EXECUTION_ROOT',self.root/'execution'),
                   patch.object(dependencies,'MacOSProcesses',return_value=Mock(owned=lambda uid:[])),
                   patch.object(dependencies,'launch_spec',side_effect=lambda project,packet,scratch,cache:{'argv':packet['argv']}),
                   patch.object(dependencies,'capture',side_effect=capture),
                   patch.object(dependencies,'https_bytes',return_value=(200,self.content)),
                   patch('do_again.supervisor.macos_server.verify_installation'),
                   patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'a'*40})]
        for item in patches:item.start();self.addCleanup(item.stop)

    def test_real_extraction_and_durable_receipt_replay(self):
        receipt = install_via_broker(self.broker,self.packet)
        self.assertEqual(receipt['state'],'succeeded')
        self.assertEqual((Path(receipt['site_packages'])/'fixture/__init__.py').read_text(),'VALUE = 7\n')
        self.assertEqual(install_via_broker(self.broker,self.packet),receipt)
        self.assertEqual(self.calls,1)

    def test_pause_after_download_never_installs_and_is_conclusively_terminal(self):
        def download(*args,**kwargs):
            self.status.update(intent='paused',epoch=2)
            return 200,self.content
        with patch.object(self.module,'https_bytes',side_effect=download):
            receipt = install_via_broker(self.broker,self.packet)
        self.assertEqual(receipt['state'],'failed_pre_install')
        self.assertEqual(self.calls,0)
        self.assertEqual(self.broker.ledger.pending(self.broker.project.key),[])

    def test_crash_after_admission_survives_restart_without_replay(self):
        def crash(spec,timeout,*,start_guard):
            with start_guard():raise OSError('synthetic crash after admission')
        with patch.object(self.module,'capture',side_effect=crash),self.assertRaises(OSError):
            install_via_broker(self.broker,self.packet)
        self.broker.ledger = ExecutionLedger(self.root/'ledger.sqlite')
        with self.assertRaisesRegex(ExecutionBlocked,'ambiguous started'):
            install_via_broker(self.broker,self.packet)
        self.assertEqual(self.calls,0)
