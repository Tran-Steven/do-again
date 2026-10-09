import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.core.schema import canonical_json
from do_again.supervisor.capability_probe import ARTIFACT, qualification_gate, reconcile_session
from do_again.supervisor.dependencies import approved_artifact
from do_again.supervisor.macos_execution import ExecutionBlocked, ExecutionLedger


class CapabilityProbeTests(unittest.TestCase):
    def test_qualification_approval_does_not_grant_another_project(self):
        config = {'dependency_artifacts':{'original':[ARTIFACT]}}
        self.assertEqual(approved_artifact(config,'original',ARTIFACT['id']),ARTIFACT)
        with self.assertRaises(ExecutionBlocked):
            approved_artifact(config,'other',ARTIFACT['id'])

    def test_unsupported_platform_rejects_before_inspection_or_effects(self):
        with patch('do_again.supervisor.capability_probe.sys.platform','win32'):
            with self.assertRaises(ExecutionBlocked):qualification_gate(None)

    @unittest.skipUnless(hasattr(__import__('os'), 'geteuid'), 'native identity requires POSIX')
    def test_pause_production_unverified_or_owned_process_blocks_qualification(self):
        broker=SimpleNamespace(config={'production_ready':False},project=SimpleNamespace(repo=Path('/repo'),uid=401,worktree=Path('/worktree')),
            registry=SimpleNamespace(status=lambda repo:{'intent':'maintenance'}),_verified=lambda:True)
        with patch('do_again.supervisor.capability_probe.sys.platform','darwin'), \
             patch('do_again.supervisor.capability_probe.os.geteuid',return_value=0), \
             patch('do_again.supervisor.macos_server.verify_installation'), \
             patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'a'*40}), \
             patch('do_again.supervisor.capability_probe.MacOSProcesses') as processes:
            processes.return_value.owned.return_value=[]
            self.assertEqual(qualification_gate(broker)['operator']['intent'],'maintenance')
            for intent,production,verified,pids in [('paused',False,True,[]),('maintenance',True,True,[]),
                    ('maintenance',False,False,[]),('maintenance',False,True,[1])]:
                broker.registry.status=lambda repo,intent=intent:{'intent':intent}
                broker.config['production_ready']=production
                broker._verified=lambda verified=verified:verified
                processes.return_value.owned.return_value=pids
                with self.assertRaises(ExecutionBlocked):qualification_gate(broker)

    def test_interrupted_preparation_never_restarts(self):
        with self.assertRaises(ExecutionBlocked):
            reconcile_session(None,{'phase':'preparation_started'})
        self.assertEqual(reconcile_session(None,{'phase':'complete','result':{'verified':True}}),{'verified':True})

    def test_lost_publication_receipt_only_reconciles_original_identity(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'ledger.sqlite'
            packet={'operation':'git_publish','request_id':'qualification-original'}
            fingerprint=hashlib.sha256(canonical_json(packet)).hexdigest()
            ledger=ExecutionLedger(path)
            ledger.reserve('project',packet['request_id'],fingerprint,intent={'operation':'git_publish'})
            restarted=SimpleNamespace(ledger=ExecutionLedger(path),project=SimpleNamespace(key='project'))
            session={'phase':'publication_started','publication_packet':packet}
            with patch('do_again.supervisor.publication.reconcile_publication',return_value={'state':'post_dispatch_uncertain'}) as observe:
                with self.assertRaises(ExecutionBlocked):reconcile_session(restarted,session)
                observe.assert_called_once_with(restarted,{'operation':'git_publication_reconcile','request_id':packet['request_id']})
            result={'state':'succeeded','returncode':0}
            restarted.ledger.finish('project',packet['request_id'],result)
            with patch('do_again.supervisor.publication.reconcile_publication') as observe:
                self.assertEqual(reconcile_session(restarted,session),result)
                observe.assert_not_called()


if __name__ == '__main__':unittest.main()
