"""Authenticated operator-side browser planner request handler."""
import json
import tempfile
import unittest
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.browser.planner_bridge_server import OperatorBrowserBridge, DurableDeliveryReservation
from do_again.service.browser_planner import browser_grant_digest
from do_again.supervisor.macos_execution import ExecutionBlocked


@contextmanager
def operator_record_fixture():
    """Protocol fixture's UID501 on POSIX CI; never native identity evidence."""
    original=os.fstat
    def measured(fd):
        info=original(fd)
        values={name:getattr(info,name) for name in dir(info) if name.startswith('st_')}
        values['st_uid']=501
        return SimpleNamespace(**values)
    with patch('do_again.browser.planner_bridge_server.os.fstat',side_effect=measured):yield


class FakeConnection:
    def __init__(self, request):
        self.incoming = json.dumps(request).encode() + bytes([10])
        self.outgoing = []
    def recv(self, size):
        data, self.incoming = self.incoming, b''
        return data
    def sendall(self, data):
        self.outgoing.append(data)


@unittest.skipUnless(os.name=="posix", "native ownership, descriptors or process control require POSIX")
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
        self.request['grant_sha256'] = browser_grant_digest(self.grant)
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
        probe = {'operation':'probe','nonce':'a'*32,
                 'grant_sha256':browser_grant_digest(self.grant)}
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
            'grant_sha256':browser_grant_digest(self.grant),
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
            'grant_sha256':browser_grant_digest(self.grant),
            'response': '{"result":"test"}',
        })

    def test_typed_pre_dispatch_refusal_returns_exact_identity_without_success(self):
        from do_again.browser.errors import BrowserPreDispatchBlocked
        self.handler.driver.send_once=Mock(side_effect=BrowserPreDispatchBlocked('composer is not ready'))
        conn=FakeConnection(self.request)
        with patch('do_again.browser.planner_bridge_server.os.geteuid',return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid',return_value=0):
            outcome=self.handler.handle_once(conn)
        reply=json.loads(conn.outgoing[0])
        self.assertEqual(reply['state'],'pre_dispatch_blocked')
        self.assertEqual(reply['call_id'],self.call)
        self.assertEqual(reply['grant_sha256'],self.request['grant_sha256'])
        self.assertNotIn('response',reply)
        self.assertEqual(outcome['state'],'pre_dispatch_blocked')
        self.assertEqual(self.events,['reserve'])

    def test_uncertain_driver_failure_is_never_labeled_non_dispatch(self):
        from do_again.browser.errors import BrowserSubmissionUncertain
        self.handler.driver.send_once=Mock(side_effect=BrowserSubmissionUncertain('Send outcome unknown'))
        conn=FakeConnection(self.request)
        with patch('do_again.browser.planner_bridge_server.os.geteuid',return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid',return_value=0):
            with self.assertRaises(BrowserSubmissionUncertain):self.handler.handle_once(conn)
        self.assertEqual(conn.outgoing,[])

    def test_lost_refusal_frame_is_durably_observed_without_second_send(self):
        from do_again.browser.errors import BrowserPreDispatchBlocked
        from do_again.browser.planner_bridge_server import DurableReplyJournal
        with tempfile.TemporaryDirectory() as temp,operator_record_fixture():
            root=Path(temp);delivery=root/'delivery';replies=root/'replies'
            delivery.mkdir(mode=0o700);replies.mkdir(mode=0o700)
            driver=SimpleNamespace(send_once=Mock(side_effect=BrowserPreDispatchBlocked('bound composer unavailable')),observe_once=Mock())
            handler=OperatorBrowserBridge(grant=self.grant,driver=driver,
                reserve_delivery=DurableDeliveryReservation(delivery),replies=DurableReplyJournal(replies))
            conn=FakeConnection(self.request);conn.sendall=Mock(side_effect=OSError('connection lost'))
            with patch('do_again.browser.planner_bridge_server.os.geteuid',return_value=501),\
                    patch('do_again.browser.planner_bridge_server.authenticated_peer_uid',return_value=0):
                with self.assertRaises(OSError):handler.handle_once(conn)
                cached=handler.replies.read(self.call);self.assertEqual(cached['reply']['state'],'pre_dispatch_blocked')
                observer=FakeConnection(dict(self.request,operation='observe-original'))
                handler.handle_once(observer)
            self.assertEqual(json.loads(observer.outgoing[0]),cached['reply'])
            driver.send_once.assert_called_once();driver.observe_once.assert_not_called()

    def test_observer_refusal_cannot_relabel_original_unknown_call(self):
        from do_again.browser.errors import BrowserPreDispatchBlocked
        from do_again.browser.planner_bridge_server import DurableReplyJournal
        with tempfile.TemporaryDirectory() as temp,operator_record_fixture():
            root=Path(temp);delivery=root/'delivery';replies=root/'replies'
            delivery.mkdir(mode=0o700);replies.mkdir(mode=0o700)
            reservation=DurableDeliveryReservation(delivery)
            import hashlib
            reservation({'call_id':self.call,'conversation_id':self.conversation,
                'profile_sha256':self.profile,'message_sha256':hashlib.sha256(self.request['message'].encode()).hexdigest()})
            original=(delivery/(self.call+'.json')).read_bytes()
            handler=OperatorBrowserBridge(grant=self.grant,
                driver=SimpleNamespace(observe_once=Mock(side_effect=BrowserPreDispatchBlocked('observer cannot read tab')),send_once=Mock()),
                reserve_delivery=reservation,replies=DurableReplyJournal(replies))
            conn=FakeConnection(dict(self.request,operation='observe-original'))
            with patch('do_again.browser.planner_bridge_server.os.geteuid',return_value=501),\
                    patch('do_again.browser.planner_bridge_server.authenticated_peer_uid',return_value=0):
                with self.assertRaises(BrowserPreDispatchBlocked):handler.handle_once(conn)
            self.assertEqual((delivery/(self.call+'.json')).read_bytes(),original)
            self.assertIsNone(handler.replies.read(self.call));self.assertEqual(conn.outgoing,[])
            handler.driver.send_once.assert_not_called()

    def test_untrusted_peer_cannot_reserve_or_send(self):
        conn = FakeConnection(self.request)
        with patch('do_again.browser.planner_bridge_server.os.geteuid', return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid', return_value=501):
            with self.assertRaisesRegex(ExecutionBlocked, 'not from root'):
                self.handler.handle_once(conn)
        self.assertEqual(self.events, [])

    def test_cross_grant_request_cannot_reserve_or_dispatch(self):
        wrong = dict(self.request, grant_sha256='0'*64)
        with patch('do_again.browser.planner_bridge_server.os.geteuid', return_value=501), \
             patch('do_again.browser.planner_bridge_server.authenticated_peer_uid', return_value=0):
            with self.assertRaisesRegex(ExecutionBlocked, 'approved scope'):
                self.handler.handle_once(FakeConnection(wrong))
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
