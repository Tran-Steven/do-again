from __future__ import annotations
import os
import sys
import unittest
from unittest.mock import Mock,patch

@unittest.skipUnless(os.name=='posix','child runner requires POSIX resource limits')
class KernelGroupTests(unittest.TestCase):
    def setUp(self):
        from do_again.supervisor import execution_runner
        self.runner=execution_runner

    def test_kernel_attestation_does_not_consult_directory_group_access(self):
        def query(size,buffer):buffer[0]=401;return 1
        library=Mock(getgroups=query)
        with patch.object(self.runner.sys,'platform','darwin'),patch.object(self.runner.ctypes,'CDLL',return_value=library),patch.object(self.runner.os,'getgroups',side_effect=AssertionError('directory lookup forbidden')):
            self.assertEqual(self.runner.kernel_groups(),{401})

    def test_failed_kernel_attestation_has_no_directory_fallback(self):
        library=Mock(getgroups=lambda size,buffer:-1)
        with patch.object(self.runner.sys,'platform','darwin'),patch.object(self.runner.ctypes,'CDLL',return_value=library),self.assertRaises(SystemExit):self.runner.kernel_groups()

    def test_additional_group_authority_blocks_exec(self):
        with patch.object(self.runner,'kernel_groups',return_value={401,80}),patch.object(self.runner.os,'getuid',return_value=401),patch.object(self.runner.os,'geteuid',return_value=401),patch.object(self.runner.os,'getgid',return_value=401),patch.object(self.runner.os,'execve') as execute:
            with self.assertRaises(SystemExit):self.runner.main(['401','5','/usr/bin/true'])
            execute.assert_not_called()

    def test_bootstrap_and_registered_authority_are_removed_before_exec(self):
        bootstrap=Mock(value=2059);task=Mock(value=515)
        library=Mock();library.task_set_special_port.return_value=0
        library.mach_ports_register.return_value=0;library.mach_port_destroy.return_value=0
        with patch.object(self.runner.sys,'platform','darwin'),patch.object(self.runner.ctypes,'CDLL',return_value=library),patch.object(self.runner.ctypes.c_uint,'in_dll',side_effect=[task,bootstrap]):
            self.runner.revoke_bootstrap_capabilities()
        self.assertEqual(bootstrap.value,0)
        library.task_set_special_port.assert_called_once_with(515,4,0)
        library.mach_ports_register.assert_called_once_with(515,None,0)
        library.mach_port_destroy.assert_called_once_with(515,2059)

    def test_failed_bootstrap_revocation_blocks_exec_without_fallback(self):
        library=Mock();library.task_set_special_port.return_value=5
        with patch.object(self.runner.sys,'platform','darwin'),patch.object(self.runner.ctypes,'CDLL',return_value=library),patch.object(self.runner.ctypes.c_uint,'in_dll',side_effect=[Mock(value=515),Mock(value=2059)]),self.assertRaises(SystemExit):
            self.runner.revoke_bootstrap_capabilities()
        library.mach_ports_register.assert_not_called()

    @unittest.skipUnless(sys.platform=='darwin','Darwin native symbol required')
    def test_actual_native_symbol_returns_bounded_kernel_groups(self):
        groups=self.runner.kernel_groups()
        self.assertIsInstance(groups,set)
        self.assertLessEqual(len(groups),128)

class ProbeFailureEvidenceTests(unittest.TestCase):
    def test_failure_invalidates_old_proof_and_survives_restart(self):
        import tempfile,json,threading
        from pathlib import Path
        from do_again.supervisor.macos_server import ProjectBroker
        from do_again.supervisor.macos_probe import BoundaryProbeBlocked
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);identity={'source_sha':'synthetic'}
            (root/'enforcement.json').write_text(json.dumps({'identity':identity,'result':{'verified':True}}))
            broker=object.__new__(ProjectBroker);broker.config={'operator_uid':501};broker.state=root
            broker.project=Mock(repo=root/'repo');broker.registry=Mock();broker.lock=threading.Lock()
            broker.registry.status.return_value={'intent':'maintenance'}
            evidence={'checks':{'service_kickstart_denied':False},'service_state':['runs = 1']}
            with patch('do_again.supervisor.macos_server.verify_installation'),patch('do_again.supervisor.macos_server.machine_identity',return_value=identity),patch('do_again.supervisor.macos_probe.verify_dedicated_boundary',side_effect=BoundaryProbeBlocked('service changed',evidence)):
                with self.assertRaises(BoundaryProbeBlocked):broker.dispatch({'operation':'probe'},501)
                self.assertFalse(broker._verified())
                restored=object.__new__(ProjectBroker);restored.state=root;restored.config=broker.config
                self.assertEqual(restored._probe_blocker()['evidence'],evidence)
