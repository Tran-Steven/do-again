"""Authenticated operator-side browser planner request handler."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.browser.planner_bridge_server import OperatorBrowserBridge, DurableDeliveryReservation
from do_again.supervisor.macos_execution import ExecutionBlocked


class FakeConnection:
    def __init__(self, request):
        self.incoming = json.dumps(request).encode() + bytes([10])
        self.outgoing = []
    def recv(self, size):
        data, self.incoming = self.incoming, b''
        return data
    def sendall(self, data):
        self.outgoing.append(data)


class OperatorBridgeTests(unittest.TestCase):
    def setUp(self):
        self.call = 'planner-'+'a'*20+'-1'
        self.conversation = '01234567-89ab-4cde-8012-0123456789ab'
        self.profile = 'f'*64
        self.request = {
            'call_id': self.call, 'conversation_id': self.conversation,
            'profile_sha256': self.profile, 'message': '{}',
        }
        self.grant = {
            'conversation_id': self.conversation,
            'profile_sha256': self.profile,
            'max_prompt_bytes': 32768,
            'max_answer_bytes': 65536,
        }
        self.events = []
        class Driver:
            def send_once(inner, message, **kwargs):
                self.events.append('send')
                return {'conversation_id': kwargs['conversation_id'],
                        'response': '{"result":"test"}'}
        self.handler = OperatorBrowserBridge(
            grant=self.grant, driver=Driver(),
            reserve_delivery=lambda identity: self.events.append('reserve'))

    def test_delivery_reservation_is_exclusive_and_fsynced(self):
        with tempfile.TemporaryDirectory() as directory:
            reservation = DurableDeliveryReservation(directory)
            identity = {
                'call_id': self.call, 'conversation_id': self.conversation,
                'profile_sha256': self.profile, 'message_sha256': 'a'*64,
            }
            with patch('do_again.browser.planner_bridge_server.private_operator_directory',
                       return_value=True):
                reservation(identity)
                journal = Path(directory) / (self.call + '.json')
                self.assertEqual(json.loads(journal.read_text()), identity)
                self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(ExecutionBlocked, 'already reserved'):
                    reservation(identity)

    def test_authenticated_probe_is_read_only(self):
        probe = {'operation':'probe','nonce':'a'*32}
        conn = FakeConnection(probe)
        with patch('do_again.browser.planner_bridge_server.os.geteuid', return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid', return_value=0):
            result = self.handler.handle_once(conn)
        self.assertEqual(result, {'state':'probed','model_calls':0})
        self.assertEqual(self.events, [])
        self.assertEqual(json.loads(conn.outgoing[0]), {
            'operation':'probe-result', 'nonce':'a'*32,
            'conversation_id':self.conversation,
            'profile_sha256':self.profile,
        })

    def test_authenticated_root_request_is_reserved_before_send(self):
        conn = FakeConnection(self.request)
        with patch('do_again.browser.planner_bridge_server.os.geteuid', return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid', return_value=0):
            result = self.handler.handle_once(conn)
        self.assertEqual(result['call_id'], self.call)
        self.assertEqual(self.events, ['reserve', 'send'])
        self.assertEqual(json.loads(conn.outgoing[0]), {
            'call_id': self.call, 'conversation_id': self.conversation,
            'response': '{"result":"test"}',
        })

    def test_untrusted_peer_cannot_reserve_or_send(self):
        conn = FakeConnection(self.request)
        with patch('do_again.browser.planner_bridge_server.os.geteuid', return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid', return_value=501):
            with self.assertRaisesRegex(ExecutionBlocked, 'not from root'):
                self.handler.handle_once(conn)
        self.assertEqual(self.events, [])

    def test_wrong_conversation_is_rejected_before_reserving(self):
        bad = dict(self.request, conversation_id='wrong')
        with patch('do_again.browser.planner_bridge_server.os.geteuid', return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid', return_value=0):
            with self.assertRaises(ExecutionBlocked):
                self.handler.handle_once(FakeConnection(bad))
        self.assertEqual(self.events, [])

    def test_reservation_failure_prevents_browser_send(self):
        self.handler.reserve_delivery = lambda identity: (_ for _ in ()).throw(
            ExecutionBlocked('duplicate reservation'))
        with patch('do_again.browser.planner_bridge_server.os.geteuid', return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid', return_value=0):
            with self.assertRaisesRegex(ExecutionBlocked, 'duplicate'):
                self.handler.handle_once(FakeConnection(self.request))
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
