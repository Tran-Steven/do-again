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

    @unittest.skipUnless(sys.platform=='darwin','Darwin native symbol required')
    def test_actual_native_symbol_returns_bounded_kernel_groups(self):
        groups=self.runner.kernel_groups()
        self.assertIsInstance(groups,set)
        self.assertLessEqual(len(groups),128)
