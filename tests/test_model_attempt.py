"""Offline one-shot Codex proposal journal and attested binary tests."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from do_again.model_attempt import (
    run_first_codex_canary_proposal, observe_original_codex_attempt,
)
from do_again.model_transport import CodexTransportUncertain
from do_again.model_quota import CodexQuotaUnavailable
from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(os.name == "posix", "private operator journal requires POSIX")
class CodexAttemptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        root = self.home / ".do_again" / "codex-tools"
        self.binary = root / "node_modules" / ".bin" / "codex"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("#!/usr/bin/env node\n")
        self.binary.chmod(0o700)
        self.digest = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        self.now = datetime.now(timezone.utc)
        self.config = {
            "schema_version": 1, "production_ready": False,
            "source_sha": "a"*40, "operator_home": str(self.home),
            "operator_uid": os.getuid(),
            "projects": [
                {"account": "_doagain_da", "repo": str(self.home/"do-again"),
                 "github_repository": "Tran-Steven/do-again"},
                {"account": "_doagain_jp", "repo": str(self.home/"jobpipe"),
                 "github_repository": "Tran-Steven/jobpipe"},
            ],
            "codex_canary": {
                "transport": "codex-cli", "nonce": "b"*24,
                "baseline": "c"*40, "parent_epoch": 1,
                "cli_sha256": self.digest, "max_model_calls": 2,
                "expires_at_utc": (self.now + timedelta(minutes=60)).isoformat(),
            },
        }
        self.sample = {
            "implementation": "def canonical_label(text):\n    return '-'.join(text.lower().split())\n",
            "tests": "import unittest\nfrom canary_live_" + "b"*24
                     + " import canonical_label\n"
                     "class TestLabel(unittest.TestCase):\n"
                     "    def test_lower(self):\n"
                     "        self.assertEqual(canonical_label('A B'), 'a-b')\n",
        }
        self.home_patch = patch("do_again.model_attempt.Path.home", return_value=self.home)
        self.login_patch = patch("do_again.model_attempt.login_ready", return_value=True)
        self.quota_patch = patch("do_again.model_attempt.require_model_capacity",
                                 return_value={"allowed": True})
        self.parent_patch = patch("do_again.model_attempt.require_codex_parent_maintenance",
                                  return_value={"_doagain_da": {"maintenance": True}})
        self.home_patch.start()
        self.login_patch.start()
        self.quota = self.quota_patch.start()
        self.parent_gate = self.parent_patch.start()
        self.addCleanup(self.parent_patch.stop)
        self.addCleanup(self.home_patch.stop)
        self.addCleanup(self.login_patch.stop)
        self.addCleanup(self.quota_patch.stop)

    def journal(self):
        return self.home/".do_again"/"codex-canary-attempts"/("b"*24)/"task-1.json"

    def call(self):
        return run_first_codex_canary_proposal(self.config,allow_model_call=True)

    def test_unconsented_attempt_never_checks_auth_or_reserves_call(self):
        with patch("do_again.model_attempt.generate_structured") as model:
            with self.assertRaisesRegex(RuntimeError, "explicit"):
                run_first_codex_canary_proposal(self.config)
        model.assert_not_called()
        self.assertFalse(self.journal().exists())

    def test_valid_once_reserves_and_reuses_only_original_candidate_readonly(self):
        with patch("do_again.model_attempt.generate_structured",return_value=self.sample) as model:
            first=self.call()
            self.assertEqual(model.call_count,1)
            self.assertEqual(first["state"],"candidate_ready")
            self.assertFalse(first["published"])
            self.assertFalse(first["executed"])
            self.assertEqual(first["model_calls_reserved"],1)
            self.assertEqual(first["request"]["request_id"],
                             "canary-"+"b"*24+"-1-edit")
            saved=json.loads(self.journal().read_text())
            self.assertEqual(saved["state"],"candidate_ready")
            self.assertEqual(saved["model_calls_reserved"],1)
            self.assertEqual(self.journal().stat().st_mode & 0o077,0)
            with self.assertRaisesRegex(ExecutionBlocked, "already exists"):
                self.call()
            self.assertEqual(model.call_count,1)
        observed=observe_original_codex_attempt(self.config)
        self.assertEqual(observed["state"],"candidate_ready")
        self.assertFalse(observed["replay"])
        self.assertEqual(observed["request"],first["request"])

    def test_revoked_parent_authority_never_attempts_inference_or_reservation(self):
        with patch("do_again.model_attempt.require_codex_parent_maintenance",
                   side_effect=ExecutionBlocked("parent epoch changed")) as parent, patch(
                   "do_again.model_attempt.generate_structured") as model:
            with self.assertRaisesRegex(ExecutionBlocked, "parent epoch changed"):
                self.call()
        parent.assert_called_once()
        model.assert_not_called()
        self.assertFalse(self.journal().exists())

    def test_quota_exhaustion_does_not_consume_reserved_model_call(self):
        with patch("do_again.model_attempt.require_model_capacity",
                   side_effect=CodexQuotaUnavailable("weekly usage exhausted")), patch(
                   "do_again.model_attempt.generate_structured") as model:
            with self.assertRaises(CodexQuotaUnavailable):
                self.call()
        model.assert_not_called()
        self.assertFalse(self.journal().exists())

    def test_failed_or_uncertain_model_effect_keeps_started_marker_and_never_replays(self):
        with patch("do_again.model_attempt.generate_structured",
                   side_effect=CodexTransportUncertain("inference uncertain")) as model:
            with self.assertRaises(CodexTransportUncertain):
                self.call()
            self.assertEqual(json.loads(self.journal().read_text())["state"],
                             "model_started")
            self.assertEqual(observe_original_codex_attempt(self.config)["state"],
                             "model_started_uncertain")
            with self.assertRaisesRegex(ExecutionBlocked, "already exists"):
                self.call()
            model.assert_called_once()

    def test_digest_and_aliased_cli_rejected_before_attempt(self):
        with patch("do_again.model_attempt.generate_structured") as model:
            self.config["codex_canary"]["cli_sha256"]="0"*64
            with self.assertRaisesRegex(ExecutionBlocked, "digest"):
                self.call()
            self.config["codex_canary"]["cli_sha256"]=self.digest
            self.binary.unlink()
            elsewhere=self.home/"outside"
            elsewhere.write_text("evil")
            self.binary.symlink_to(elsewhere)
            with self.assertRaises(ExecutionBlocked):
                self.call()
        model.assert_not_called()
        self.assertFalse(self.journal().exists())

    def test_changed_journal_request_fingerprint_is_denied(self):
        with patch("do_again.model_attempt.generate_structured",return_value=self.sample):
            self.call()
        saved=json.loads(self.journal().read_text())
        saved["request_sha256"]="0"*64
        self.journal().write_text(json.dumps(saved))
        with self.assertRaisesRegex(ExecutionBlocked, "hash"):
            observe_original_codex_attempt(self.config)

    def test_browser_grant_and_production_mode_cannot_activate_new_transport(self):
        with patch("do_again.model_attempt.generate_structured") as model:
            self.config["live_canary"]={"nonce":"z"*24}
            with self.assertRaises(ExecutionBlocked):
                self.call()
            del self.config["live_canary"]
            self.config["production_ready"]=True
            with self.assertRaises(ExecutionBlocked):
                self.call()
        model.assert_not_called()
        self.assertFalse(self.journal().exists())


if __name__ == "__main__":
    unittest.main()
