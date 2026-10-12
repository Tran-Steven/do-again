import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.supervisor.macos_server import ProjectBroker
from do_again.supervisor.macos_execution import ExecutionLedger, ExecutionBlocked


class BrokerInflightTests(unittest.TestCase):
    def broker(self, path):
        broker=object.__new__(ProjectBroker)
        broker.config={'operator_uid':501}
        broker.project=SimpleNamespace(key='project')
        broker.ledger=ExecutionLedger(path)
        return broker

    def test_new_reservation_is_live_only_while_its_trusted_dispatch_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker=self.broker(Path(tmp)/'journal.sqlite')
            entered=threading.Event();release=threading.Event();errors=[]
            def operation(packet,uid):
                if packet['operation']=='status':
                    return {'pending':broker.ledger.pending('project'),'inflight':broker._inflight()}
                broker.ledger.reserve('project','original','fingerprint')
                entered.set()
                if not release.wait(5):raise AssertionError('fixture timed out')
                raise RuntimeError('injected crash before terminal receipt')
            def run():
                try:broker.dispatch({'operation':'execute'},501)
                except Exception as exc:errors.append(exc)
            with patch.object(broker,'_dispatch',side_effect=operation):
                thread=threading.Thread(target=run);thread.start()
                try:
                    self.assertTrue(entered.wait(5))
                    live=broker.dispatch({'operation':'status'},501)
                    self.assertEqual(live['inflight'],['original'])
                    self.assertEqual(live['pending'][0]['request_id'],'original')
                finally:
                    release.set();thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(errors[0],RuntimeError)
                crashed=broker.dispatch({'operation':'status'},501)
                self.assertEqual(crashed['inflight'],[])
                self.assertEqual(crashed['pending'][0]['request_id'],'original')
            # Retrying an ambiguous row does not make it a newly admitted operation.
            restored=self.broker(broker.ledger.path)
            with patch.object(restored,'_dispatch',side_effect=lambda *args:restored.ledger.reserve('project','original','fingerprint')):
                with self.assertRaises(ExecutionBlocked):restored.dispatch({'operation':'execute'},501)
            self.assertEqual(restored._inflight(),[])

    def test_untrusted_peer_cannot_establish_inflight_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker=self.broker(Path(tmp)/'journal.sqlite')
            with patch.object(broker,'_dispatch') as effect:
                with self.assertRaises(ExecutionBlocked):broker.dispatch({'operation':'execute'},401)
                effect.assert_not_called()
            self.assertEqual(broker.ledger.pending('project'),[])
