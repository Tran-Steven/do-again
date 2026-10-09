import hashlib
import json
import plistlib
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from do_again.supervisor import worker_service as service
from do_again.supervisor.macos_execution import ExecutionBlocked


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=self.root/'state';self.state.mkdir()
        self.status={'intent':'active','epoch':2,'goal_revision':'accepted'}
        self.broker=SimpleNamespace(project=SimpleNamespace(key='a'*64,account='_doagain_da',repo=self.root/'do-again'),
            state=self.state,config={'python':'/sealed/python','source_sha':'b'*40,'production_ready':True,
                'operator_uid':501,'operator_gid':20,'operator_home':str(self.root)},
            registry=SimpleNamespace(status=lambda repo:dict(self.status),set_intent=Mock()),
            _verified=lambda:True,ledger=SimpleNamespace(pending=lambda key:[]),lock=threading.Lock(),admission=nullcontext)
        label,spec=service.service_spec(self.broker,2);self.target=self.root/(label+'.plist')
        self.target.write_bytes(plistlib.dumps(spec))
        self.record={'phase':'staged','source_sha':'b'*40,'epoch':2,'plist_sha256':hashlib.sha256(self.target.read_bytes()).hexdigest()}
        (self.state/'worker-deployment.json').write_text(json.dumps(self.record))
        for target,kwargs in [('do_again.supervisor.worker_service.AGENTS',{'new':self.root}),
            ('do_again.supervisor.worker_service.native_root',{}),
            ('do_again.supervisor.worker_service.private_root_file',{}),
            ('do_again.supervisor.macos_server.verify_installation',{})]:
            p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)

    def test_service_spec_binds_project_source_epoch_and_disables_automatic_replay(self):
        label,spec=service.service_spec(self.broker,2)
        self.assertFalse(spec['RunAtLoad']);self.assertFalse(spec['KeepAlive'])
        self.assertIn('b'*40,spec['ProgramArguments']);self.assertEqual(spec['ProgramArguments'][-1],'2')
        self.broker.project.account='_doagain_jp'
        self.assertIn('jobpipe',service.service_spec(self.broker,2)[1]['ProgramArguments'])
        self.broker.project.account='_doagain_other'
        with self.assertRaises(ExecutionBlocked):service.service_spec(self.broker,2)

    def test_pause_changed_source_or_epoch_never_invokes_launchd(self):
        with patch.object(service,'launchctl') as effect:
            for field,value in [('intent','paused'),('epoch',3)]:
                previous=self.status[field];self.status[field]=value
                with self.assertRaises(ExecutionBlocked):service.start_worker(self.broker,2)
                self.status[field]=previous
            self.broker.config['source_sha']='c'*40
            with self.assertRaises(ExecutionBlocked):service.start_worker(self.broker,2)
            effect.assert_not_called()

    def test_one_start_trigger_with_durable_pre_effect_marker_and_no_repeat(self):
        def launch(*args,**kwargs):
            if args[1]=='print':return SimpleNamespace(returncode=113,stderr='Could not find service')
            self.assertEqual(json.loads((self.state/'worker-deployment.json').read_text())['phase'],'start_started')
            return SimpleNamespace(returncode=0,stderr='')
        with patch.object(service,'launchctl',side_effect=launch) as effect:
            self.assertEqual(service.start_worker(self.broker,2)['state'],'started_unverified')
            count=effect.call_count
            with self.assertRaises(ExecutionBlocked):service.start_worker(self.broker,2)
            self.assertEqual(effect.call_count,count)
            self.assertEqual(sum(c.args[1]=='kickstart' for c in effect.call_args_list),1)

    def test_interrupted_bootstrap_preserves_uncertainty(self):
        def launch(*args,**kwargs):
            if args[1]=='print':return SimpleNamespace(returncode=113,stderr='Could not find service')
            raise ExecutionBlocked('lost launch response')
        with patch.object(service,'launchctl',side_effect=launch):
            with self.assertRaises(ExecutionBlocked):service.start_worker(self.broker,2)
        self.assertEqual(json.loads((self.state/'worker-deployment.json').read_text())['phase'],'start_started')
        with patch.object(service,'launchctl') as effect:
            with self.assertRaises(ExecutionBlocked):service.start_worker(self.broker,2)
            effect.assert_not_called()

    def test_inconclusive_service_observation_does_not_claim_absence(self):
        with patch.object(service,'launchctl',return_value=SimpleNamespace(returncode=1,stderr='Permission denied')):
            with self.assertRaises(ExecutionBlocked):service.service_present(self.broker,'label')

    def test_withdrawal_closes_admission_and_keeps_journals(self):
        with patch.object(service,'launchctl',return_value=SimpleNamespace(returncode=113,stderr='Could not find service')):
            result=service.withdraw_worker(self.broker)
        self.broker.registry.set_intent.assert_called_once()
        self.assertTrue(result['journals_retained'])
        self.assertEqual(json.loads((self.state/'worker-deployment.json').read_text())['phase'],'withdrawn')

    def registration_fixture(self):
        self.record['phase']='started_unverified'
        (self.state/'worker-deployment.json').write_text(json.dumps(self.record))
        return {'operation':'worker_register','pid':123,'epoch':2,'source_sha':'b'*40}

    def test_registration_binds_kernel_birth_and_service_pid(self):
        packet=self.registration_fixture()
        identity=(501,10,20,1)
        with patch.object(service,'launchctl',return_value=SimpleNamespace(returncode=0,stdout='pid = 123\n')),             patch('do_again.supervisor.macos_execution.MacOSProcesses') as processes,             patch('do_again.supervisor.macos_server.machine_identity',return_value={'source_sha':'b'*40}):
            processes.return_value.identity.return_value=identity
            self.assertTrue(service.register_worker(self.broker,packet)['registered'])
            self.assertEqual(service.verified_worker_pid(self.broker,2),123)
            processes.return_value.identity.return_value=(501,10,21,1)
            with self.assertRaises(ExecutionBlocked):service.verified_worker_pid(self.broker,2)

    def test_registration_recovers_lost_start_response_without_another_start(self):
        packet=self.registration_fixture();self.record['phase']='start_started'
        (self.state/'worker-deployment.json').write_text(json.dumps(self.record))
        with patch.object(service,'launchctl',return_value=SimpleNamespace(returncode=0,stdout='pid = 123\n')) as effect,              patch('do_again.supervisor.macos_execution.MacOSProcesses') as processes,              patch('do_again.supervisor.macos_server.machine_identity',return_value={}):
            processes.return_value.identity.return_value=(501,10,20,1)
            self.assertTrue(service.register_worker(self.broker,packet)['registered'])
            self.assertTrue(all(call.args[1]=='print' for call in effect.call_args_list))

    def test_registration_rejects_other_live_owner_and_wrong_service(self):
        packet=self.registration_fixture()
        (self.state/'worker-instance.json').write_text(json.dumps({'pid':124,'identity':[501,9,20,1]}))
        with patch.object(service,'launchctl',return_value=SimpleNamespace(returncode=0,stdout='pid = 123\n')),             patch('do_again.supervisor.macos_execution.MacOSProcesses') as processes:
            processes.return_value.identity.side_effect=lambda pid:(501,10,20,1) if pid==123 else (501,9,20,1)
            with self.assertRaises(ExecutionBlocked):service.register_worker(self.broker,packet)
        with patch.object(service,'launchctl',return_value=SimpleNamespace(returncode=0,stdout='pid = 999\n')):
            with self.assertRaises(ExecutionBlocked):service.register_worker(self.broker,packet)


if __name__=='__main__':unittest.main()
