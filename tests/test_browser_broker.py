import tempfile
import os
import json
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from do_again.supervisor.browser_broker import browser_tick, ci_evidence, reconcile_browser
from do_again.supervisor.macos_execution import ExecutionBlocked,ExecutionLedger


class BrowserBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.active=False;self.status={'intent':'active','epoch':1,'goal_revision':'goal'}
        @contextmanager
        def admission():
            self.assertFalse(self.active);self.active=True
            try:yield
            finally:self.active=False
        project={'key':'original','repo':str(Path(self.temp.name).resolve()/'do-again')}
        self.broker=SimpleNamespace(project=SimpleNamespace(**{'key':'original','repo':Path(project['repo'])}),
            config={'projects':[project],'operator_home':str(Path(self.temp.name).resolve()),'operator_uid':os.getuid() if hasattr(os,'getuid') else 501,'operator_gid':20,
                    'python':'/sealed/python','source_sha':'a'*40,'production_ready':True},
            registry=SimpleNamespace(status=lambda repo:dict(self.status)),_verified=lambda:True,
            ledger=ExecutionLedger(Path(self.temp.name).resolve()/'ledger.sqlite'),lock=threading.Lock(),admission=admission)
        self.packet={'operation':'browser_tick','request_id':'browser-synthetic-proof','epoch':1}
        # Synthetic sealed namespace above the disposable test root. Production
        # continues to inspect every real ancestor and reject writable ancestry.
        original_stat=Path.stat
        higher=set(Path(self.temp.name).resolve().parents)
        def fixture_stat(path,*args,**kwargs):
            value=original_stat(path,*args,**kwargs)
            if path in higher:
                return SimpleNamespace(st_uid=0,st_mode=value.st_mode & ~0o022,
                    st_nlink=value.st_nlink,st_size=value.st_size)
            if os.name!='posix':
                return SimpleNamespace(st_uid=501,st_mode=value.st_mode & ~0o022,
                    st_nlink=value.st_nlink,st_size=value.st_size)
            return value
        p=patch.object(Path,'stat',fixture_stat);p.start();self.addCleanup(p.stop)

        for target,kwargs in [('do_again.supervisor.browser_broker.sys.platform',{'new':'darwin'}),
                ('do_again.supervisor.browser_broker.os.geteuid',{'return_value':0,'create':True}),
                ('do_again.supervisor.worker_service.verified_worker_pid',{'return_value':123}),
                ('do_again.supervisor.macos_server.verify_installation',{})]:
            p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)

    def test_fence_covers_spawn_and_browser_effect_until_terminal_receipt(self):
        proc=Mock(returncode=0)
        def communicate(timeout):
            self.assertTrue(self.active)
            self.assertEqual(len(self.broker.ledger.pending('original')),1)
            self.write_receipt({'state':'completed','delivered':1})
            return b'{"state":"completed","delivered":1}',None
        proc.communicate.side_effect=communicate
        with patch('do_again.supervisor.browser_broker.subprocess.Popen',return_value=proc) as spawn:
            result=browser_tick(self.broker,self.packet)
            self.assertEqual(result['delivered'],1)
            self.assertFalse(self.active)
            browser_tick(self.broker,self.packet);self.assertEqual(spawn.call_count,1)
            self.assertEqual(spawn.call_args.kwargs['user'],self.broker.config['operator_uid'])
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])

    def test_paused_or_forged_project_never_spawns(self):
        with patch('do_again.supervisor.browser_broker.subprocess.Popen') as spawn:
            self.status['intent']='paused'
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,self.packet)
            self.status['intent']='active'
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,{**self.packet,'project':'other'})
            spawn.assert_not_called()

    def test_uncertain_child_never_repeats_after_restart(self):
        proc=Mock(returncode=1);proc.communicate.return_value=(b'',None)
        with patch('do_again.supervisor.browser_broker.subprocess.Popen',return_value=proc) as spawn:
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,self.packet)
            self.broker.ledger=ExecutionLedger(self.broker.ledger.path)
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,self.packet)
            self.assertEqual(spawn.call_count,1)
            self.assertEqual(len(self.broker.ledger.pending('original')),1)

    def write_receipt(self, result, **changes):
        from do_again.supervisor.authority import project_identity
        from do_again.core.schema import atomic_json
        path=Path(self.temp.name).resolve()/'.do_again/projects'/project_identity(self.broker.project.repo)[:12]/'state/browser_tick_receipts'/('browser-synthetic-proof.json')
        record={'schema_version':1,'request_id':self.packet['request_id'],'source_sha':'a'*40,'epoch':1,'result':result}
        record.update(changes)
        atomic_json(path,record)
        return path

    def test_lost_helper_response_reconciles_durable_receipt_without_any_effect(self):
        proc=Mock(returncode=1)
        def communicate(timeout):
            self.write_receipt({'state':'completed','delivered':0,'liveness':'awaiting_ack',
                                'delivery_acknowledged':False,'outbox_preserved':True})
            return b'',None
        proc.communicate.side_effect=communicate
        with patch('do_again.supervisor.browser_broker.subprocess.Popen',return_value=proc) as spawn:
            with self.assertRaises(ExecutionBlocked):browser_tick(self.broker,self.packet)
            self.broker.ledger=ExecutionLedger(self.broker.ledger.path)
            self.status['intent']='maintenance'
            result=reconcile_browser(self.broker,{'operation':'browser_reconcile','request_id':self.packet['request_id']})
            self.assertTrue(result['reconciled_read_only'])
            self.assertFalse(result['delivery_acknowledged'])
            self.assertEqual(result['delivered'],0)
            self.assertEqual(self.broker.ledger.pending('original'),[])
            spawn.assert_called_once()

    def test_missing_or_changed_source_receipt_cannot_clear_uncertainty(self):
        from do_again.core.schema import canonical_json
        import hashlib
        self.broker.ledger.reserve('original',self.packet['request_id'],hashlib.sha256(canonical_json(self.packet)).hexdigest(),
            intent={'operation':'browser_tick','source_sha':'a'*40,'repo':str(self.broker.project.repo),'epoch':1})
        packet={'operation':'browser_reconcile','request_id':self.packet['request_id']}
        with patch('do_again.supervisor.browser_broker.subprocess.Popen') as spawn:
            self.assertEqual(reconcile_browser(self.broker,packet)['state'],'post_dispatch_uncertain')
            self.write_receipt({'state':'completed','delivered':0},source_sha='b'*40)
            with self.assertRaises(ExecutionBlocked):reconcile_browser(self.broker,packet)
            self.write_receipt({'state':'completed','delivered':0},epoch=2)
            with self.assertRaises(ExecutionBlocked):reconcile_browser(self.broker,packet)
            spawn.assert_not_called()
            self.assertEqual(len(self.broker.ledger.pending('original')),1)

    def test_aliased_or_writable_receipt_does_not_release_original_intent(self):
        self.broker.ledger.reserve('original',self.packet['request_id'],'fingerprint',
            intent={'operation':'browser_tick','source_sha':'a'*40,'repo':str(self.broker.project.repo),'epoch':1})
        path=self.write_receipt({'state':'completed','delivered':0})
        packet={'operation':'browser_reconcile','request_id':self.packet['request_id']}
        if os.name=='posix':
            path.chmod(0o666)
            with self.assertRaises(ExecutionBlocked):reconcile_browser(self.broker,packet)
            path.chmod(0o600)
        alias=path.with_name('hardlink.json')
        os.link(path,alias)
        with self.assertRaises(ExecutionBlocked):reconcile_browser(self.broker,packet)
        alias.unlink()
        original=path.with_name('original.json')
        path.rename(original)
        try:path.symlink_to(original)
        except OSError:return  # Windows may lack symlink privileges; hardlink checks already ran.
        with self.assertRaises(ExecutionBlocked):reconcile_browser(self.broker,packet)
        self.assertEqual(len(self.broker.ledger.pending('original')),1)

    def test_trusted_runner_outbox_and_broker_round_trip_survive_ack_wait_and_restart(self):
        import io
        from do_again.browser import runtime as browser
        from do_again.service import daemon
        from do_again.supervisor import browser_runner
        from do_again.supervisor.authority import project_identity
        repo=self.broker.project.repo
        base=Path(self.broker.config['operator_home'])/'.do_again/projects'/project_identity(repo)[:12]
        state=base/'state'
        record={'chat_url':'https://chatgpt.com/c/fixture','binding_generation':'generation'}
        item=daemon._queue_continuation(state,'Inspect exact CI head.',purpose='ci_continuation',marker='CI synthetic exact-head',binding=record)
        target=SimpleNamespace(url=record['chat_url'])
        visible=False
        def one_gesture(target,message,**options):
            options['before_dispatch']()
            return {'response':'submitted'}
        proc=Mock(returncode=0)
        def spawn(argv,**kwargs):
            def communicate(timeout):
                output=io.StringIO()
                # Execute the actual immutable helper path with deterministic
                # browser observations; no fixture network or desktop effects.
                with patch('sys.argv',['runner',*argv[-8:]]),patch('sys.stdout',output):
                    browser_runner.main()
                return output.getvalue().encode(),None
            proc.communicate.side_effect=communicate
            return proc
        with patch.object(daemon,'activate_project'), \
             patch.object(daemon,'ensure_browser_running',return_value={'port':9224}), \
             patch.object(browser,'ensure_browser_running',return_value={'port':9224}), \
             patch.object(browser,'project_record',return_value=record), \
             patch.object(browser,'_find_chatgpt_target',return_value=target), \
             patch.object(browser,'_rollover_needed',return_value=False), \
             patch.object(browser,'wait_for_authenticated',return_value=(target,{})), \
             patch.object(browser,'_assistant_snapshot',return_value={'busy':False}), \
             patch.object(browser,'_page_contains',side_effect=lambda *args:visible), \
             patch.object(browser,'receipt_acknowledgment',return_value={'visible':True,'acknowledged':True}), \
             patch.object(browser,'send_message',side_effect=one_gesture) as gesture, \
             patch.object(daemon,'notify_receipts',side_effect=browser.notify_receipts.__wrapped__), \
             patch('do_again.supervisor.browser_broker.subprocess.Popen',side_effect=spawn) as process:
            first=browser_tick(self.broker,self.packet)
            self.assertEqual(first['liveness'],'awaiting_ack')
            self.assertEqual(first['delivered'],0)
            self.assertEqual(self.broker.ledger.pending('original'),[])
            self.assertTrue(item.exists())
            self.broker.ledger=ExecutionLedger(self.broker.ledger.path)
            self.assertEqual(browser_tick(self.broker,self.packet),first)
            visible=True
            second=browser_tick(self.broker,{**self.packet,'request_id':'browser-ack-observation'})
            self.assertEqual(second['delivered'],1)
            self.assertFalse(item.exists())
            self.status['intent']='paused'
            with self.assertRaises(ExecutionBlocked):
                browser_tick(self.broker,{**self.packet,'request_id':'browser-paused'})
            gesture.assert_called_once()
            self.assertEqual(process.call_count,2)

    def test_ci_continuation_requires_exact_repository_run_and_current_head(self):
        self.broker.project.worktree=Path(self.temp.name).resolve()/'worktree'
        self.broker.config['projects'][0]['github_repository']='Tran-Steven/do-again'
        goal={'goal_state':'waiting_for_ci','ci':{'repository':'Tran-Steven/do-again','run_id':123,'head_sha':'b'*40}}
        with patch('do_again.service.liveness._latest_goal',return_value=goal),              patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'b'*40}),              patch('do_again.supervisor.control_history.api_for') as selected:
            api=selected.return_value
            api.request.return_value={'id':123,'head_sha':'b'*40,'status':'completed','conclusion':'success'}
            self.assertEqual(ci_evidence(self.broker,Path(self.temp.name).resolve())['state'],'terminal')
            api.request.assert_called_once_with('GET','actions/runs/123')
            for response in ({'id':124,'head_sha':'b'*40},{'id':123,'head_sha':'c'*40}):
                api.request.return_value=response
                self.assertEqual(ci_evidence(self.broker,Path(self.temp.name).resolve())['state'],'mismatch')
            api.reset_mock();goal['ci']['repository']='Tran-Steven/sonary'
            self.assertEqual(ci_evidence(self.broker,Path(self.temp.name).resolve())['state'],'mismatch')
            api.request.assert_not_called()

    def test_ci_head_change_invalidates_previously_successful_run(self):
        self.broker.project.worktree=Path(self.temp.name).resolve()/'worktree'
        self.broker.config['projects'][0]['github_repository']='Tran-Steven/do-again'
        goal={'goal_state':'waiting_for_ci','ci':{'repository':'Tran-Steven/do-again','run_id':123,'head_sha':'b'*40}}
        with patch('do_again.service.liveness._latest_goal',return_value=goal),              patch('do_again.supervisor.macos_server.worktree_authority',return_value={'repo_head':'c'*40}),              patch('do_again.supervisor.control_history.api_for') as api:
            self.assertEqual(ci_evidence(self.broker,Path(self.temp.name).resolve())['state'],'mismatch')
            api.assert_not_called()


if __name__=='__main__':unittest.main()
