"""Read-only protected operator maintenance gate tests for the Codex canary."""
from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from do_again.model_admission import require_codex_parent_maintenance
from do_again.model_grant import sealed_codex_canary
from do_again.supervisor.macos_execution import ExecutionBlocked


class CodexMaintenanceGateTests(unittest.TestCase):
    def setUp(self):
        self.home="/Users/isolated"
        self.config={
            "source_sha":"a"*40,"production_ready":False,"operator_home":self.home,
            "projects":[
                {"account":"_doagain_da","repo":self.home+"/do-again","github_repository":"Tran-Steven/do-again"},
                {"account":"_doagain_jp","repo":self.home+"/jobpipe","github_repository":"Tran-Steven/jobpipe"},
            ],
            "codex_canary":{
                "transport":"codex-cli","nonce":"b"*24,"baseline":"c"*40,
                "parent_epoch":7,"cli_sha256":"d"*64,
                "max_model_calls":2,
                "expires_at_utc":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            },
        }
        self.scope=sealed_codex_canary(self.config)
        self.base={
            "source_sha":"a"*40, "production_ready":False,
            "operator_intent":"maintenance", "canary_authorized":False,
            "enforcement_verified":True, "enforcement_blocker":None,
            "unresolved_executions":[], "inflight_request_ids":[], "epoch":7,
        }
        self.sibling={**self.base,"epoch":9}

    def rpc(self, repo, packet):
        self.assertEqual(packet, {"operation":"status"})
        return copy.deepcopy(self.base if str(repo)==self.home+"/do-again" else self.sibling)

    def test_exact_both_parent_maintenance_read_only(self):
        fake=Mock(side_effect=self.rpc)
        result=require_codex_parent_maintenance(self.config,self.scope,rpc=fake)
        self.assertEqual(fake.call_count,2)
        self.assertEqual([x.args[0] for x in fake.call_args_list],
                         [Path(self.home+"/do-again"), Path(self.home+"/jobpipe")])
        self.assertTrue(result["_doagain_da"]["maintenance"])
        self.assertEqual(result["_doagain_da"]["epoch"],7)
        self.assertEqual(result["_doagain_jp"]["epoch"],9)

    def test_any_unsafe_parent_state_is_rejected_before_second_read(self):
        bad=[
            {"operator_intent":"active"},
            {"production_ready":True},
            {"canary_authorized":True},
            {"enforcement_verified":False},
            {"enforcement_blocker":{"error":"native gate"}},
            {"unresolved_executions":[{"request_id":"old-started"}]},
            {"inflight_request_ids":["prior-effect"]},
            {"source_sha":"e"*40},
            {"epoch":8},
            {"epoch":True},
        ]
        for change in bad:
            with self.subTest(change=change):
                state={**self.base,**change}
                fake=Mock(return_value=state)
                with self.assertRaises(ExecutionBlocked):
                    require_codex_parent_maintenance(self.config,self.scope,rpc=fake)
                fake.assert_called_once()

    def test_jobpipe_not_in_maintenance_blocks_all_model_proposals(self):
        original=self.sibling
        try:
            self.sibling={**self.base,"operator_intent":"paused","epoch":9}
            fake=Mock(side_effect=self.rpc)
            with self.assertRaises(ExecutionBlocked):
                require_codex_parent_maintenance(self.config,self.scope,rpc=fake)
            self.assertEqual(fake.call_count,2)
        finally:
            self.sibling=original

    def test_status_missing_or_malformed_fails_closed(self):
        for observed in (None, [], {"operator_intent":"maintenance"}):
            fake=Mock(return_value=observed)
            with self.subTest(observed=observed),self.assertRaises(ExecutionBlocked):
                require_codex_parent_maintenance(self.config,self.scope,rpc=fake)

    def test_no_status_reads_without_validated_scope(self):
        bad=copy.deepcopy(self.config)
        bad["production_ready"]=True
        fake=Mock()
        with self.assertRaises(ExecutionBlocked):
            require_codex_parent_maintenance(bad,self.scope,rpc=fake)
        fake.assert_not_called()


if __name__=="__main__":
    unittest.main()
