"""Offline tests of the one-shot, least-privileged canary GitHub ref path."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.supervisor import canary_branch
from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(os.name == "posix", "operator-owned canary ref tests require POSIX identity")
class CanaryControlBranchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.nonce = "a" * 24
        self.base = "b" * 40
        self.branch = "do-again/canary-" + self.nonce + "/control"
        self.grant = self.root / "grant.json"
        self.grant.write_text(json.dumps({
            "nonce": self.nonce, "baseline": self.base,
            "parent_epoch": 1,
            "chat_url": "https://chatgpt.com/c/" + "c" * 36,
            "binding_identity": "d" * 64,
        }))
        self.grant.chmod(0o600)
        self.ledger = self.root / "branch-journal"

    def provision(self):
        return canary_branch.provision_control_branch(
            nonce=self.nonce, baseline=self.base, grant=self.grant,
            journal_root=self.ledger)

    def remote(self):
        return {"ref": "refs/heads/" + self.branch, "object": {"sha": self.base}}

    def ticket(self):
        return json.loads((self.ledger / (self.nonce + ".json")).read_text())

    def test_only_one_create_after_exact_404_then_verified_readback(self):
        seen=[]
        def api(method, endpoint, *, body=None):
            seen.append((method,endpoint,body,self.ticket().get("phase") if self.ledger.exists() else None))
            if len(seen) == 1:
                return 404, {}
            return (201 if len(seen) == 2 else 200), self.remote()
        with patch.object(canary_branch, "_api", side_effect=api):
            value = self.provision()
        self.assertEqual(value, {"branch": self.branch, "sha": self.base, "reconciled": False})
        self.assertEqual([row[0] for row in seen], ["GET", "POST", "GET"])
        self.assertIsNone(seen[0][3])
        self.assertEqual(seen[1][3], "publication_reserved")
        self.assertEqual(seen[1][2],
                         {"ref": "refs/heads/" + self.branch, "sha": self.base})
        self.assertEqual(self.ticket()["phase"], "verified")
        self.assertEqual((self.ledger / (self.nonce + ".json")).stat().st_mode & 0o777, 0o600)

    def test_uncertain_creation_is_never_reposted_and_can_be_reconciled_by_get(self):
        with patch.object(canary_branch, "_api", side_effect=[
            (404, {}), ExecutionBlocked("lost POST")]) as call:
            with self.assertRaisesRegex(ExecutionBlocked, "lost POST"):
                self.provision()
            self.assertEqual(call.call_count, 2)
        self.assertEqual(self.ticket()["phase"], "publication_reserved")
        with patch.object(canary_branch, "_api", return_value=(200, self.remote())) as call:
            self.assertTrue(self.provision()["reconciled"])
            self.assertEqual(call.call_count, 1)
            self.assertEqual(call.call_args.args[0], "GET")
        self.assertEqual(self.ticket()["phase"], "verified")

    def test_existing_branch_refuses_to_create_any_effect(self):
        with patch.object(canary_branch, "_api", return_value=(200, self.remote())) as api:
            with self.assertRaisesRegex(ExecutionBlocked, "already present"):
                self.provision()
            api.assert_called_once()
        self.assertFalse(self.ledger.exists())

    def test_unknown_preflight_status_never_creates_branch(self):
        with patch.object(canary_branch, "_api", side_effect=ExecutionBlocked("403")) as api:
            with self.assertRaisesRegex(ExecutionBlocked, "403"):
                self.provision()
            api.assert_called_once()
        self.assertFalse(self.ledger.exists())

    def test_changed_readback_blocks_even_after_one_create(self):
        foreign = {"ref": "refs/heads/" + self.branch, "object": {"sha": "e" * 40}}
        with patch.object(canary_branch, "_api", side_effect=[
                (404, {}), (201, self.remote()), (200, foreign)]) as api:
            with self.assertRaisesRegex(ExecutionBlocked, "readback differs"):
                self.provision()
            self.assertEqual(api.call_count, 3)
        self.assertEqual(self.ticket()["phase"], "publication_reserved")
        with patch.object(canary_branch, "_api", return_value=(200, foreign)) as api:
            with self.assertRaisesRegex(ExecutionBlocked, "unresolved"):
                self.provision()
            api.assert_called_once()

    def test_private_grant_and_nonce_are_required_without_github_effects(self):
        self.grant.chmod(0o644)
        with patch.object(canary_branch, "_api") as api:
            with self.assertRaisesRegex(ExecutionBlocked, "not a private"):
                self.provision()
            api.assert_not_called()
        self.grant.chmod(0o600)
        with patch.object(canary_branch, "_api") as api:
            with self.assertRaisesRegex(ExecutionBlocked, "authority differs"):
                canary_branch.provision_control_branch(
                    nonce="f" * 24, baseline=self.base, grant=self.grant,
                    journal_root=self.ledger)
            api.assert_not_called()

    def test_http_404_is_distinguished_from_denied_and_ambiguous_responses(self):
        endpoint = "repos/Tran-Steven/do-again/git/ref/heads/" + self.branch
        example = SimpleNamespace(
            returncode=1, stdout='HTTP/2 404 Not Found\nContent-Type: application/json\n\n{"message":"Not Found"}',
            stderr="error")
        with patch.object(canary_branch.subprocess, "run", return_value=example) as runner:
            self.assertEqual(canary_branch._api("GET", endpoint), (404, {}))
            self.assertNotIn("Authorization", str(runner.call_args))
        for value in (
            SimpleNamespace(returncode=1, stdout="HTTP/2 403 Forbidden\n\n{}", stderr="403"),
            SimpleNamespace(returncode=1, stdout="", stderr="http 404"),
            SimpleNamespace(returncode=0, stdout="HTTP/2 200 OK\n\n{}", stderr=""),
        ):
            with patch.object(canary_branch.subprocess, "run", return_value=value):
                if value.returncode == 0:
                    self.assertEqual(canary_branch._api("GET", endpoint), (200, {}))
                else:
                    with self.assertRaises(ExecutionBlocked):
                        canary_branch._api("GET", endpoint)


if __name__ == "__main__":
    unittest.main()
