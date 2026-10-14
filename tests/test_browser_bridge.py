"""Local browser bridge framing and socket ownership checks."""
import json
import stat
import unittest
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.service.browser_bridge import (
    BrokerBrowserBridge, read_frame, write_frame,
)
from do_again.supervisor.macos_execution import ExecutionBlocked


class BufferedSocket:
    def __init__(self, received):
        self.received = [received]
        self.sent = []
    def recv(self, size):
        return self.received.pop(0) if self.received else b''
    def sendall(self, value):
        self.sent.append(value)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def settimeout(self, seconds):
        self.timeout = seconds
    def connect(self, path):
        self.path = path
    def getpeereid(self):
        return (501, 20)


class BridgeTests(unittest.TestCase):
    def test_macos_native_getpeereid_fallback_when_socket_wrapper_lacks_it(self):
        from do_again.service.browser_bridge import authenticated_peer_uid
        import ctypes

        class NativeOnlySocket:
            def fileno(self):
                return 42

        class NativeLibrary:
            pass

        lib = NativeLibrary()

        def native(fd, uid, gid):
            self.assertEqual(fd, 42)
            ctypes.cast(uid, ctypes.POINTER(ctypes.c_uint))[0] = 501
            ctypes.cast(gid, ctypes.POINTER(ctypes.c_uint))[0] = 20
            return 0

        lib.getpeereid = native
        with patch('do_again.service.browser_bridge.sys.platform', 'darwin'), \
             patch('ctypes.CDLL', return_value=lib):
            self.assertEqual(authenticated_peer_uid(NativeOnlySocket()), 501)

    def test_macos_native_getpeereid_failure_is_not_a_trust_fallback(self):
        from do_again.service.browser_bridge import authenticated_peer_uid

        class NativeOnlySocket:
            def fileno(self):
                return 42

        class NativeLibrary:
            pass

        def native(fd, uid, gid):
            return -1

        library = NativeLibrary()
        library.getpeereid = native
        with patch('do_again.service.browser_bridge.sys.platform', 'darwin'), \
             patch('ctypes.CDLL', return_value=library):
            with self.assertRaisesRegex(ExecutionBlocked, 'could not be verified'):
                authenticated_peer_uid(NativeOnlySocket())

    def test_frame_round_trip(self):
        value = {'call_id': 'planner-' + 'a'*20 + '-1', 'response': 'valid'}
        target = BufferedSocket(b'')
        write_frame(target, value)
        self.assertEqual(read_frame(BufferedSocket(target.sent[0])), value)

    def test_reject_nonjson_oversize_and_extra_frames(self):
        for data in (b'not-json'+bytes([10]),
                     b'{}'+bytes([10])+b'{}'+bytes([10]),
                     b'a'*100001, b''):
            with self.subTest(data=data[:20]):
                with self.assertRaises(ExecutionBlocked):
                    read_frame(BufferedSocket(data))

    @unittest.skipUnless(os.name=="posix", "native POSIX fixture; Windows native parity is not claimed")
    def test_broker_requires_operator_owned_socket_and_kernel_peer(self):
        reply = {'call_id': 'planner-' + 'a'*20 + '-1',
                 'conversation_id': '01234567-89ab-4cde-8012-0123456789ab',
                 'grant_sha256':'e'*64, 'response': '{}'}
        fake = BufferedSocket(json.dumps(reply).encode()+bytes([10]))
        metadata = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=501,
                                   st_nlink=1)
        bridge = BrokerBrowserBridge('/tmp/browser-planner-test.sock')
        with patch('pathlib.Path.lstat', return_value=metadata), \
             patch('do_again.service.browser_bridge.sys.platform', 'darwin'), \
             patch('do_again.service.browser_bridge.socket.socket', return_value=fake):
            result = bridge.send_once(
                '{}', conversation_id=reply['conversation_id'],
                call_id=reply['call_id'], profile_sha256='f'*64,
                grant_sha256='e'*64)
        self.assertEqual(result['response'], '{}')
        self.assertEqual(fake.timeout, 480.0)
        self.assertEqual(len(fake.sent), 1)

    @unittest.skipUnless(os.name=="posix", "native POSIX fixture; Windows native parity is not claimed")
    def test_non_dispatch_refusal_must_match_the_original_call_and_grant(self):
        from do_again.service.browser_planner import BrowserPlanningNotDispatched
        call='planner-'+'a'*20+'-1';conversation='01234567-89ab-4cde-8012-0123456789ab'
        reply={'call_id':call,'conversation_id':conversation,'grant_sha256':'e'*64,
               'state':'pre_dispatch_blocked','error':'composer not ready'}
        metadata=SimpleNamespace(st_mode=stat.S_IFSOCK|0o600,st_uid=501,st_nlink=1)
        for mismatched in (False,True):
            value=dict(reply,grant_sha256='f'*64) if mismatched else reply
            fake=BufferedSocket(json.dumps(value).encode()+bytes([10]))
            with patch('pathlib.Path.lstat',return_value=metadata), \
                 patch('do_again.service.browser_bridge.sys.platform','darwin'), \
                 patch('do_again.service.browser_bridge.socket.socket',return_value=fake):
                try:
                    BrokerBrowserBridge('/tmp/browser-planner-test.sock').send_once('{}',
                        conversation_id=conversation,call_id=call,profile_sha256='f'*64,grant_sha256='e'*64)
                except BrowserPlanningNotDispatched:
                    self.assertFalse(mismatched)
                except ExecutionBlocked:
                    self.assertTrue(mismatched)
                else:self.fail('refusal accepted as an answer')
            self.assertEqual(len(fake.sent),1)

    @unittest.skipUnless(os.name=="posix", "native POSIX fixture; Windows native parity is not claimed")
    def test_zero_model_call_probe_requires_exact_session(self):
        identity = {
            'operation':'probe-result', 'nonce':'a'*32,
            'conversation_id':'01234567-89ab-4cde-8012-0123456789ab',
            'profile_sha256':'f'*64, 'grant_sha256':'e'*64,
        }
        fake = BufferedSocket(json.dumps(identity).encode()+bytes([10]))
        metadata = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=501,
                                   st_nlink=1)
        bridge = BrokerBrowserBridge('/tmp/browser-planner-test.sock')
        with patch('pathlib.Path.lstat', return_value=metadata), \
             patch('do_again.service.browser_bridge.sys.platform', 'darwin'), \
             patch('do_again.service.browser_bridge.secrets.token_hex',
                   return_value='a'*32), \
             patch('do_again.service.browser_bridge.socket.socket', return_value=fake):
            result = bridge.probe_authorized(
                conversation_id=identity['conversation_id'],
                profile_sha256=identity['profile_sha256'],
                grant_sha256=identity['grant_sha256'])
        self.assertEqual(result['peer_uid'], 501)
        self.assertEqual(json.loads(fake.sent[0]),
                         {'operation':'probe','nonce':'a'*32,
                          'grant_sha256':'e'*64})

    def test_wrong_operator_owner_blocks_before_connection(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600,
                                   st_uid=0, st_nlink=1)
        bridge = BrokerBrowserBridge('/tmp/browser-planner-test.sock')
        with patch('pathlib.Path.lstat', return_value=metadata), \
             patch('do_again.service.browser_bridge.socket.socket') as make_socket:
            with self.assertRaises(ExecutionBlocked):
                bridge.send_once('hi', conversation_id='test',
                                 call_id='planner-'+'a'*20+'-1',
                                 profile_sha256='f'*64, grant_sha256='e'*64)
            make_socket.assert_not_called()


if __name__ == '__main__':
    unittest.main()
