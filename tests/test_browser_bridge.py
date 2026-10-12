"""Local browser bridge framing and socket ownership checks."""
import json
import stat
import unittest
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

    def test_broker_requires_operator_owned_socket_and_kernel_peer(self):
        reply = {'call_id': 'planner-' + 'a'*20 + '-1',
                 'conversation_id': '01234567-89ab-4cde-8012-0123456789ab',
                 'response': '{}'}
        fake = BufferedSocket(json.dumps(reply).encode()+bytes([10]))
        metadata = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=501,
                                   st_nlink=1)
        bridge = BrokerBrowserBridge('/tmp/browser-planner-test.sock')
        with patch('pathlib.Path.lstat', return_value=metadata), \
             patch('do_again.service.browser_bridge.sys.platform', 'darwin'), \
             patch('do_again.service.browser_bridge.socket.socket', return_value=fake):
            result = bridge.send_once(
                '{}', conversation_id=reply['conversation_id'],
                call_id=reply['call_id'], profile_sha256='f'*64)
        self.assertEqual(result['response'], '{}')
        self.assertEqual(fake.timeout, 120.0)
        self.assertEqual(len(fake.sent), 1)

    def test_zero_model_call_probe_requires_exact_session(self):
        identity = {
            'operation':'probe-result', 'nonce':'a'*32,
            'conversation_id':'01234567-89ab-4cde-8012-0123456789ab',
            'profile_sha256':'f'*64,
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
                profile_sha256=identity['profile_sha256'])
        self.assertEqual(result['peer_uid'], 501)
        self.assertEqual(json.loads(fake.sent[0]),
                         {'operation':'probe','nonce':'a'*32})

    def test_wrong_operator_owner_blocks_before_connection(self):
        metadata = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600,
                                   st_uid=0, st_nlink=1)
        bridge = BrokerBrowserBridge('/tmp/browser-planner-test.sock')
        with patch('pathlib.Path.lstat', return_value=metadata), \
             patch('do_again.service.browser_bridge.socket.socket') as make_socket:
            with self.assertRaises(ExecutionBlocked):
                bridge.send_once('hi', conversation_id='test',
                                 call_id='planner-'+'a'*20+'-1',
                                 profile_sha256='f'*64)
            make_socket.assert_not_called()


if __name__ == '__main__':
    unittest.main()
