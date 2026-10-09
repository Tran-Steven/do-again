import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.supervisor import operator_service
from do_again.supervisor.macos_execution import ExecutionBlocked


class OperatorServiceTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name).resolve();(self.root/'trusted-state/agent').mkdir(parents=True)
        self.nonce='a'*24;self.binding=self.root/'operator-binding.json';self.binding.write_text('{}')
        self.broker=SimpleNamespace(config={'production_ready':False,'operator_uid':501,'operator_gid':20,
            'operator_home':str(self.root),'python':'/sealed/python','source_sha':'b'*40},project=SimpleNamespace(key='c'*64))
        for target,kwargs in [('do_again.supervisor.operator_service.sys.platform',{'new':'darwin'}),
                ('do_again.supervisor.operator_service.os.geteuid',{'return_value':0,'create':True}),
                ('do_again.supervisor.operator_service.os.chown',{'create':True}),
                ('do_again.supervisor.operator_service.MacOSProcesses',{})]:
            p=patch(target,**kwargs);mock=p.start();self.addCleanup(p.stop)
            if target.endswith('MacOSProcesses'):mock.return_value.identity.return_value=(501,1,2,1)

    def test_exact_daemon_service_starts_once_and_verifies_withdrawal(self):
        calls=[]
        def launch(broker,*args,**kwargs):
            calls.append(args)
            if args[0]=='kickstart':
                (self.root/'trusted-state/agent/service.stdout').write_text(json.dumps({
                    'controller_uid':501,'source_sha':'b'*40,'engine':'sealed_daemon','tasks':3}))
            return SimpleNamespace(stdout=' pid = 123\n')
        with patch('do_again.supervisor.worker_service.service_present',return_value=False), \
             patch('do_again.supervisor.worker_service.launchctl',side_effect=launch), \
             patch('do_again.supervisor.service_probe.verify_process_withdrawn') as withdrawal:
            result=operator_service.run_operator_service(self.broker,self.root,self.binding,self.nonce,nullcontext)
            self.assertTrue(result['launchd_service']);self.assertTrue(result['service_withdrawn'])
            self.assertEqual([row[0] for row in calls],['bootstrap','kickstart','print','bootout'])
            withdrawal.assert_called_once_with(123,(501,1,2,1))
            with self.assertRaisesRegex(ExecutionBlocked,'no replay'):
                operator_service.run_operator_service(self.broker,self.root,self.binding,self.nonce,nullcontext)
            self.assertEqual(len(calls),4)
        receipt=json.loads((self.root/'operator-service.json').read_text())
        self.assertEqual(receipt['phase'],'complete')

    def test_uncertain_bootstrap_is_withdrawn_and_never_replayed(self):
        calls=[]
        def launch(broker,*args,**kwargs):
            calls.append(args[0])
            if args[0]=='bootstrap':raise ExecutionBlocked('lost bootstrap response')
            return SimpleNamespace(stdout='')
        with patch('do_again.supervisor.worker_service.service_present',return_value=False), \
             patch('do_again.supervisor.worker_service.launchctl',side_effect=launch):
            with self.assertRaises(ExecutionBlocked):
                operator_service.run_operator_service(self.broker,self.root,self.binding,self.nonce,nullcontext)
            with self.assertRaisesRegex(ExecutionBlocked,'no replay'):
                operator_service.run_operator_service(self.broker,self.root,self.binding,self.nonce,nullcontext)
        self.assertEqual(calls,['bootstrap','bootout'])
        self.assertEqual(json.loads((self.root/'operator-service.json').read_text())['phase'],'dispatch_started')

    def test_production_flag_cannot_be_used_for_qualification_service(self):
        self.broker.config['production_ready']=True
        with patch('do_again.supervisor.worker_service.launchctl') as effects:
            with self.assertRaises(ExecutionBlocked):
                operator_service.run_operator_service(self.broker,self.root,self.binding,self.nonce,nullcontext)
            effects.assert_not_called()


    def test_root_xpcproxy_handoff_accepts_only_same_pid_and_birth(self):
        calls=[]
        def launch(broker,*args,**kwargs):
            calls.append(args[0])
            if args[0]=='kickstart':
                (self.root/'trusted-state/agent/service.stdout').write_text(json.dumps({
                    'controller_uid':501,'source_sha':'b'*40,'engine':'sealed_daemon','tasks':3}))
            return SimpleNamespace(stdout=' pid = 123\n' if args[0]=='print' else '')
        with patch('do_again.supervisor.operator_service.MacOSProcesses') as processes, \
             patch('do_again.supervisor.operator_service.time.sleep'), \
             patch('do_again.supervisor.worker_service.service_present',return_value=False), \
             patch('do_again.supervisor.worker_service.launchctl',side_effect=launch), \
             patch('do_again.supervisor.service_probe.verify_process_withdrawn') as withdrawal:
            processes.return_value.identity.side_effect=[(0,10,20,1),(501,10,20,1)]
            result=operator_service.run_operator_service(
                self.broker,self.root,self.binding,self.nonce,nullcontext)
            self.assertTrue(result['launchd_service'])
            self.assertEqual(calls,['bootstrap','kickstart','print','print','bootout'])
            withdrawal.assert_called_once_with(123,(501,10,20,1))
        evidence=json.loads((self.root/'operator-service.json').read_text())
        self.assertTrue(evidence['root_proxy_observed'])

    def test_xpcproxy_changed_kernel_birth_fails_closed(self):
        calls=[]
        def launch(broker,*args,**kwargs):
            calls.append(args[0])
            return SimpleNamespace(stdout=' pid = 123\n')
        with patch('do_again.supervisor.operator_service.MacOSProcesses') as processes, \
             patch('do_again.supervisor.operator_service.time.sleep'), \
             patch('do_again.supervisor.worker_service.service_present',return_value=False), \
             patch('do_again.supervisor.worker_service.launchctl',side_effect=launch):
            processes.return_value.identity.side_effect=[(0,10,20,1),(501,11,20,1)]
            with self.assertRaisesRegex(ExecutionBlocked,'birth identity changed'):
                operator_service.run_operator_service(
                    self.broker,self.root,self.binding,self.nonce,nullcontext)
        self.assertEqual(calls,['bootstrap','kickstart','print','print','bootout'])
        self.assertEqual(json.loads((self.root/'operator-service.json').read_text())['phase'],'dispatch_started')

    def test_unexpected_uid_fails_without_wait_or_second_start(self):
        calls=[]
        def launch(broker,*args,**kwargs):
            calls.append(args[0])
            return SimpleNamespace(stdout=' pid = 123\n')
        with patch('do_again.supervisor.operator_service.MacOSProcesses') as processes, \
             patch('do_again.supervisor.operator_service.time.sleep') as sleep, \
             patch('do_again.supervisor.worker_service.service_present',return_value=False), \
             patch('do_again.supervisor.worker_service.launchctl',side_effect=launch):
            processes.return_value.identity.return_value=(502,10,20,1)
            with self.assertRaisesRegex(ExecutionBlocked,'unexpected kernel UID'):
                operator_service.run_operator_service(
                    self.broker,self.root,self.binding,self.nonce,nullcontext)
            sleep.assert_not_called()
        self.assertEqual(calls,['bootstrap','kickstart','print','bootout'])

    def test_root_xpcproxy_timeout_fails_closed(self):
        calls=[]
        def launch(broker,*args,**kwargs):
            calls.append(args[0])
            return SimpleNamespace(stdout=' pid = 123\n')
        with patch('do_again.supervisor.operator_service.MacOSProcesses') as processes, \
             patch('do_again.supervisor.operator_service.time.monotonic',side_effect=[0,4]), \
             patch('do_again.supervisor.operator_service.time.sleep') as sleep, \
             patch('do_again.supervisor.worker_service.service_present',return_value=False), \
             patch('do_again.supervisor.worker_service.launchctl',side_effect=launch):
            processes.return_value.identity.return_value=(0,10,20,1)
            with self.assertRaisesRegex(ExecutionBlocked,'handoff timed out'):
                operator_service.run_operator_service(
                    self.broker,self.root,self.binding,self.nonce,nullcontext)
            sleep.assert_not_called()
        self.assertEqual(calls,['bootstrap','kickstart','print','bootout'])

    def test_launchd_pid_change_after_root_probe_fails_closed(self):
        calls=[]
        pid_values=iter([123,124])
        def launch(broker,*args,**kwargs):
            calls.append(args[0])
            return SimpleNamespace(stdout=(' pid = %s\n' % next(pid_values)) if args[0]=='print' else '')
        with patch('do_again.supervisor.operator_service.MacOSProcesses') as processes, \
             patch('do_again.supervisor.operator_service.time.sleep'), \
             patch('do_again.supervisor.worker_service.service_present',return_value=False), \
             patch('do_again.supervisor.worker_service.launchctl',side_effect=launch):
            processes.return_value.identity.return_value=(0,10,20,1)
            with self.assertRaisesRegex(ExecutionBlocked,'PID changed'):
                operator_service.run_operator_service(
                    self.broker,self.root,self.binding,self.nonce,nullcontext)
        self.assertEqual(calls,['bootstrap','kickstart','print','print','bootout'])
