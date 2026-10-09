"""Real inert child/heartbeat and fixture launchd; native launchd measured installed."""
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from do_again.supervisor.service_probe import qualify_service
from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(os.name=='posix','macOS qualification uses POSIX identity')
class ServiceProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.state=self.root/'state';self.state.mkdir()
        self.before={'operator':{'intent':'maintenance'}};self.proc=None;self.definition=None;self.starts=0
        self.broker=SimpleNamespace(project=SimpleNamespace(key='a'*64),state=self.state,
            config={'operator_uid':os.getuid(),'operator_gid':os.getgid(),'operator_home':str(self.root),
                    'python':sys.executable,'source_sha':'b'*40,'production_ready':False},
            ledger=SimpleNamespace(pending=lambda key:[]),lock=threading.Lock(),probe_admission=nullcontext)
        self.addCleanup(self.stop)
        for target,kwargs in [('do_again.supervisor.service_probe.EXECUTION_ROOT',{'new':self.root/'execution'}),
            ('do_again.supervisor.service_probe.os.chown',{}),
            ('do_again.supervisor.capability_probe.qualification_gate',{'return_value':self.before}),
            ('do_again.supervisor.macos_server.machine_identity',{'return_value':{'source_sha':'b'*40}}),
            ('do_again.supervisor.service_probe.MacOSProcesses',{})]:
            p=patch(target,**kwargs);mock=p.start();self.addCleanup(p.stop)
            if target.endswith('MacOSProcesses'):mock.return_value.identity.return_value=(os.getuid(),10,20,1)
    def stop(self):
        if self.proc and self.proc.poll() is None:self.proc.terminate();self.proc.wait(timeout=5)
    def launch(self,broker,operation,*args,**kwargs):
        if operation=='print':
            if self.proc and self.proc.poll() is None:return SimpleNamespace(returncode=0,stdout=f'pid = {self.proc.pid}\n')
            return SimpleNamespace(returncode=113,stderr='Could not find service')
        if operation=='bootstrap':self.definition=plistlib.loads(Path(args[1]).read_bytes())
        elif operation=='kickstart':
            self.starts+=1;self.proc=subprocess.Popen(self.definition['ProgramArguments'],cwd=self.definition['WorkingDirectory'])
        elif operation=='bootout':self.stop()
        else:raise AssertionError(operation)
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    def test_one_real_heartbeat_start_kernel_binding_withdrawal_and_replay(self):
        with patch('do_again.supervisor.worker_service.launchctl',side_effect=self.launch):
            result=qualify_service(self.broker)
            self.assertTrue(result['verified']);self.assertEqual(self.starts,1)
            self.assertFalse(self.definition['KeepAlive']);self.assertFalse(self.definition['RunAtLoad'])
            self.assertEqual(qualify_service(self.broker),result);self.assertEqual(self.starts,1)
            self.assertEqual(result['production_worker_start'],'not_measured')
    def test_lost_bootstrap_response_never_restarts(self):
        def fail(broker,operation,*args,**kwargs):
            if operation=='bootstrap':raise ExecutionBlocked('synthetic lost response')
            return self.launch(broker,operation,*args,**kwargs)
        with patch('do_again.supervisor.worker_service.launchctl',side_effect=fail):
            with self.assertRaises(ExecutionBlocked):qualify_service(self.broker)
            with self.assertRaisesRegex(ExecutionBlocked,'no automatic start'):qualify_service(self.broker)
        self.assertEqual(self.starts,0)


if __name__=='__main__':unittest.main()
