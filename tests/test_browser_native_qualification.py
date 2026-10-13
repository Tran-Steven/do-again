"""Native bridge qualification is offline, exclusive, and maintenance-only."""
import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from do_again.supervisor.controller_browser_helper import qualify_operator_browser_bridge
from do_again.supervisor.macos_execution import ExecutionBlocked


class NativeBrowserQualificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.scope = {'planner':'chatgpt-browser-cdp',
                      'goal_sha256':'b'*64, 'epoch':13}
        self.grant = {'conversation_id':'01234567-89ab-4cde-8012-0123456789ab',
                      'profile_sha256':'c'*64}
        self.broker = SimpleNamespace(
            state=Path(self.directory.name),
            config={'source_sha':'a'*40,
                    'operator_home':'/Users/isolated-operator',
                    'browser_planning_scope':self.grant},
            project=SimpleNamespace(account='_doagain_jp',repo=Path('/tmp/jobpipe')),
            registry=SimpleNamespace(status=lambda repo:{'intent':'maintenance',
                                                          'epoch':13}))

    def qualify(self):
        with patch('sys.platform', 'darwin'), \
             patch('os.geteuid', return_value=0), \
             patch('do_again.supervisor.macos_server.verify_installation'), \
             patch('do_again.supervisor.controller_configuration.validate_controller_configuration',
                   return_value=self.scope), \
             patch('do_again.supervisor.controller_browser_helper.BrokerBrowserBridge') as client, \
             patch('do_again.supervisor.controller_browser_helper.private_root_file'):
            client.return_value.probe_authorized.return_value = {
                'peer_uid':501, 'conversation_id':self.grant['conversation_id'],
                'profile_sha256':self.grant['profile_sha256']}
            result = qualify_operator_browser_bridge(self.broker)
        return result

    def test_operator_broker_exposes_qualification_without_model_call(self):
        # The operator socket must route only this exact one-shot packet to
        # the pre-existing root gate; a typo or missing route strands activation.
        import inspect
        from do_again.supervisor import macos_server
        source = inspect.getsource(macos_server.serve_operator)
        dispatch = inspect.getsource(macos_server.ProjectBroker._dispatch)
        self.assertIn("packet['operation']=='qualify_browser_planner'", source)
        self.assertIn("broker.dispatch({'operation':'qualify_browser_planner'}", source)
        self.assertIn("packet == {'operation':'qualify_browser_planner'}", dispatch)
        self.assertIn("qualify_operator_browser_bridge(self)", dispatch)

    def test_controller_lifecycle_reachable_only_by_exact_operator_packets(self):
        import inspect
        from do_again.supervisor import macos_server
        source = inspect.getsource(macos_server.serve_operator)
        for operation in ('controller_start', 'controller_stop',
                          'controller_status', 'controller_tick'):
            self.assertIn("'"+operation+"'", source)
        self.assertIn("broker.dispatch({'operation':packet['operation']}", source)

    def test_authenticated_operator_exposes_native_probe(self):
        import inspect
        from do_again.supervisor.macos_server import serve_operator
        source = inspect.getsource(serve_operator)
        self.assertIn("'qualify_native_enforcement'", source)
        self.assertIn("broker.dispatch({'operation':'probe'}", source)

    def test_audited_epoch_upgrade_requires_exact_quarantine_and_retained_controller(self):
        import sqlite3
        from types import SimpleNamespace
        from unittest.mock import patch
        from do_again.supervisor.controller_browser_helper import _audited_epoch_13_to_15
        previous_source='7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191'
        fingerprint='ec7355ba8ca0c9270c00a8386c3c9c3eb376fc289ec6ee416fc8bcd84f63d747'
        state=self.broker.state
        (state/('controller-blocked-'+previous_source[:12]+'-retained.json')).write_text(
            json.dumps({'state':'blocked','requests':[],'outstanding':None,'acknowledged':[]}))
        dbpath=state/'audit.sqlite'
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('CREATE TABLE v1a_abandoned_probe_audit '
                       '(project TEXT,request TEXT,fingerprint TEXT,source_sha TEXT,'
                       'prior_state TEXT,disposition TEXT)')
            db.execute('CREATE TABLE execution (project TEXT,request TEXT,fingerprint TEXT,state TEXT,result TEXT)')
            db.execute('INSERT INTO v1a_abandoned_probe_audit VALUES(?,?,?,?,?,?)',
                ('fixture','offline-5d95b82d0952982adda5-1-probe',fingerprint,
                 previous_source,'started','quarantined_unresolved_not_replayable'))
            db.execute('INSERT INTO execution VALUES(?,?,?,?,NULL)',
                ('fixture','offline-5d95b82d0952982adda5-1-probe',fingerprint,'abandoned'))
        broker=SimpleNamespace(state=state,config={'authority_path':str(dbpath)},
                               project=SimpleNamespace(key='fixture'))
        proof={'source_sha':previous_source,'epoch':13,'browser_calls':0}
        with patch('do_again.supervisor.macos_execution.private_root_file'):
            self.assertTrue(_audited_epoch_13_to_15(broker,proof,{'epoch':15},previous_source))
            self.assertFalse(_audited_epoch_13_to_15(broker,proof,{'epoch':16},previous_source))
            archive = state / ('controller-blocked-'+previous_source[:12]+'-retained.json')
            valid_archive = archive.read_text()
            for invalid in ('{', json.dumps({'state':'blocked','requests':['used'],
                                            'outstanding':None,'acknowledged':[]})):
                archive.write_text(invalid)
                self.assertFalse(_audited_epoch_13_to_15(broker,proof,{'epoch':15},previous_source))
            archive.write_text(valid_archive)
            with patch.object(Path, 'read_text', side_effect=PermissionError('private archive')):
                self.assertFalse(_audited_epoch_13_to_15(broker,proof,{'epoch':15},previous_source))
            with patch('sqlite3.connect', side_effect=sqlite3.OperationalError('unable to open database file')):
                self.assertFalse(_audited_epoch_13_to_15(broker,proof,{'epoch':15},previous_source))
            for column, value in (('fingerprint','wrong'), ('source_sha','wrong'),
                                  ('prior_state','wrong'), ('disposition','wrong')):
                with closing(sqlite3.connect(dbpath)) as db, db:
                    original_value = db.execute('SELECT '+column+' FROM v1a_abandoned_probe_audit').fetchone()[0]
                    db.execute('UPDATE v1a_abandoned_probe_audit SET '+column+'=?', (value,))
                self.assertFalse(_audited_epoch_13_to_15(broker,proof,{'epoch':15},previous_source))
                with closing(sqlite3.connect(dbpath)) as db, db:
                    db.execute('UPDATE v1a_abandoned_probe_audit SET '+column+'=?', (original_value,))
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.execute("UPDATE execution SET state='started'")
            self.assertFalse(_audited_epoch_13_to_15(broker,proof,{'epoch':15},previous_source))

    def test_root_epoch_upgrade_rejects_invalid_audit_without_replacing_proof(self):
        previous_source = '7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191'
        self.scope['epoch'] = 15
        path = self.broker.state / 'browser-planner-qualification.json'
        prior = json.dumps({'state':'qualified_operator_bridge',
                            'source_sha':previous_source, 'epoch':13,
                            'goal_sha256':self.scope['goal_sha256'],
                            'operator_uid':501,'peer_uid':501,'browser_calls':0})
        path.write_text(prior)
        with patch('do_again.supervisor.controller_browser_helper._audited_epoch_13_to_15',
                   return_value=False) as audit:
            with self.assertRaisesRegex(ExecutionBlocked, 'not eligible'):
                self.qualify()
        audit.assert_called_once()
        self.assertEqual(path.read_text(), prior)
        self.assertFalse(path.with_name('browser-planner-qualification-'+previous_source[:12]+'-retained.json').exists())

    def test_success_seals_exact_zero_call_proof_once(self):
        result = self.qualify()
        self.assertEqual(result['browser_calls'],0)
        self.assertEqual(result['source_sha'],'a'*40)
        path = self.broker.state / 'browser-planner-qualification.json'
        self.assertEqual(json.loads(path.read_text()),result)
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaisesRegex(ExecutionBlocked,'already exists'):
            self.qualify()

    def test_guarded_source_upgrade_retains_zero_call_qualification(self):
        previous_source = '7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191'
        self.broker.config['source_sha'] = 'c' * 40
        prior = {'state':'qualified_operator_bridge',
                 'source_sha':previous_source,
                 'goal_sha256':self.scope['goal_sha256'],
                 'epoch':13,'grant_sha256':'d'*64,
                 'operator_uid':501,'peer_uid':501,'browser_calls':0}
        path = self.broker.state / 'browser-planner-qualification.json'
        path.write_text(json.dumps(prior))
        path.chmod(0o600)
        updated = self.qualify()
        self.assertEqual(updated['source_sha'], 'c'*40)
        retained = self.broker.state / (
            'browser-planner-qualification-' + previous_source[:12] + '-retained.json')
        self.assertEqual(json.loads(retained.read_text()), prior)
        self.assertEqual(json.loads(path.read_text()), updated)

    def test_guarded_source_upgrade_denies_prior_planning_activity(self):
        previous_source = '7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191'
        self.broker.config['source_sha'] = 'c'*40
        path = self.broker.state / 'browser-planner-qualification.json'
        path.write_text(json.dumps({
            'state':'qualified_operator_bridge','source_sha':previous_source,
            'goal_sha256':self.scope['goal_sha256'],'epoch':13,
            'operator_uid':501,'peer_uid':501,'browser_calls':0}))
        path.chmod(0o600)
        (self.broker.state / 'browser-planning-calls.json').write_text('{}')
        with self.assertRaisesRegex(ExecutionBlocked, 'manual reconciliation'):
            self.qualify()
        self.assertTrue(path.exists())

    def _v1a_previous_proof(self, *, source, previous_epoch, now_epoch=17, calls=0):
        self.scope['epoch'] = now_epoch
        self.broker.registry = SimpleNamespace(status=lambda repo:{
            'intent':'maintenance','epoch':now_epoch})
        self.broker.config['source_sha']='f'*40
        path = self.broker.state/'browser-planner-qualification.json'
        proof={'state':'qualified_operator_bridge','source_sha':source,
               'goal_sha256':self.scope['goal_sha256'],'epoch':previous_epoch,
               'grant_sha256':'d'*64,'operator_uid':501,'peer_uid':501,
               'browser_calls':calls}
        path.write_text(json.dumps(proof))
        path.chmod(0o600)
        return path,proof

    def test_epoch17_accepts_only_zero_call_de3_maintenance_proof(self):
        old='de3de3314a4195133f25132ec7874785ee661b9a'
        path,proof=self._v1a_previous_proof(source=old,previous_epoch=15)
        result=self.qualify()
        self.assertEqual(result['epoch'],17)
        self.assertEqual(result['source_sha'],'f'*40)
        retained=path.with_name('browser-planner-qualification-'+old[:12]+'-retained.json')
        self.assertEqual(json.loads(retained.read_text()),proof)
        self.assertEqual(json.loads(path.read_text()),result)

    def test_epoch17_never_inherits_known_prior_browser_calls_or_journals(self):
        old='de3de3314a4195133f25132ec7874785ee661b9a'
        for prior_calls,controller_journal in ((1,False),(0,True)):
            with self.subTest(prior_calls=prior_calls,journal=controller_journal):
                path,_=self._v1a_previous_proof(
                    source=old,previous_epoch=15,calls=prior_calls)
                journal=self.broker.state/'controller.json'
                if controller_journal:journal.write_text('{}')
                with self.assertRaisesRegex(ExecutionBlocked,
                    'not eligible' if prior_calls else 'manual reconciliation'):
                    self.qualify()
                self.assertTrue(path.exists())
                self.assertFalse(path.with_name(
                    'browser-planner-qualification-'+old[:12]+'-retained.json').exists())
                if journal.exists():journal.unlink()

    def test_epoch17_does_not_generalize_other_previous_sources_or_epochs(self):
        old='de3de3314a4195133f25132ec7874785ee661b9a'
        for sha,epoch in ((old,13),(old,16),('e'*40,15),('e'*40,17)):
            with self.subTest(source=sha,epoch=epoch):
                path,_=self._v1a_previous_proof(source=sha,previous_epoch=epoch)
                with self.assertRaisesRegex(ExecutionBlocked,'not eligible'):
                    self.qualify()
                self.assertTrue(path.exists())

    def test_exact_legacy_epoch13_to17_still_requires_root_quarantine_audit(self):
        old='7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191'
        path,_=self._v1a_previous_proof(source=old,previous_epoch=13)
        with patch('do_again.supervisor.controller_browser_helper._audited_epoch_13_to_15',
                   return_value=False) as check:
            with self.assertRaisesRegex(ExecutionBlocked,'not eligible'):
                self.qualify()
            check.assert_called_once()
        self.assertTrue(path.exists())
        with patch('do_again.supervisor.controller_browser_helper._audited_epoch_13_to_15',
                   return_value=True) as check:
            result=self.qualify()
            check.assert_called_once()
        self.assertEqual(result['epoch'],17)
        self.assertEqual(result['browser_calls'],0)

    def test_maintenance_is_required(self):
        self.broker.registry = SimpleNamespace(status=lambda repo:{
            'intent':'active','epoch':13})
        with self.assertRaisesRegex(ExecutionBlocked,'maintenance'):
            self.qualify()
        self.assertFalse((self.broker.state / 'browser-planner-qualification.json').exists())

    def test_non_root_non_mac_rejected_without_filesystem_effects(self):
        with self.assertRaisesRegex(ExecutionBlocked,'root macOS'):
            qualify_operator_browser_bridge(self.broker)
        self.assertFalse((self.broker.state / 'browser-planner-qualification.json').exists())


if __name__=='__main__':
    unittest.main()
