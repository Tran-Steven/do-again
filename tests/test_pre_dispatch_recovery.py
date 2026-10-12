import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from do_again.core.schema import atomic_json
from do_again.supervisor.macos_execution import ExecutionBlocked
from do_again.supervisor.pre_dispatch_recovery import (
    ORIGINAL_EPOCH,
    ORIGINAL_REQUEST,
    ORIGINAL_SOURCE,
    MAINTENANCE_EPOCH,
    verify_pre_dispatch,
    reconcile_verified_pre_dispatch,
)


@unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership required")
class PreDispatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = {
            "account": "_doagain_jp",
            "repo": str(self.home / "jobpipe"),
            "key": "f" * 64,
        }
        self.config = {"operator_home": str(self.home), "operator_uid": os.getuid(),
                       "authority_path": str(self.home / "root" / "authority.db"),
                       "projects": [{**self.project, "uid": 450}], "production_ready": False}
        self.status = {"intent": "maintenance", "epoch": MAINTENANCE_EPOCH}
        self.intent = {
            "operation": "browser_tick",
            "source_sha": ORIGINAL_SOURCE,
            "repo": self.project["repo"],
            "epoch": ORIGINAL_EPOCH,
        }
        self.state = self.home / ".do_again" / "projects" / ("f" * 12) / "state"
        self.state.mkdir(parents=True)
        self.current = self.state / "browser_submission_uncertain.json"
        self.archive = self.state / ("browser_submission_presend_" + ORIGINAL_REQUEST + ".json")
        self.record = {
            "schema_version": 1,
            "state": "preparing",
            "request_ids": ["message-0001"],
            "batch_marker": "DO_AGAIN_RECEIPTS_READY request_ids=message-0001",
        }
        atomic_json(self.current, self.record)
        atomic_json(self.state / "browser_outbox" / "message-0001.json", {"request_id": "message-0001"})

    def verify(self, **overrides):
        return verify_pre_dispatch(
            overrides.get("config", self.config),
            overrides.get("project", self.project),
            overrides.get("request", ORIGINAL_REQUEST),
            overrides.get("intent", self.intent),
            overrides.get("status", self.status),
        )

    def recovery_broker(self, *, finish=None):
        registry = SimpleNamespace(status=Mock(return_value=self.status))
        ledger = SimpleNamespace(
            pending=Mock(return_value=[{"request_id": ORIGINAL_REQUEST}]),
            intent=Mock(return_value=self.intent),
            finish=Mock(side_effect=finish),
        )
        return SimpleNamespace(
            config=self.config,
            project=SimpleNamespace(key=self.project["key"], repo=Path(self.project["repo"])),
            ledger=ledger, registry=registry,
        )

    def test_one_time_recovery_preserves_outbox_and_records_original_evidence(self):
        broker = self.recovery_broker()
        root = Path(self.config["authority_path"]).parent
        root.mkdir(exist_ok=True)
        original_outbox = (self.state / "browser_outbox/message-0001.json").read_bytes()
        with patch("do_again.supervisor.macos_execution.MacOSProcesses.owned", return_value=False), \
             patch("do_again.supervisor.worker_service.service_spec", return_value=("label", {})), \
             patch("do_again.supervisor.worker_service.service_present", return_value=False):
            result = reconcile_verified_pre_dispatch(broker, ORIGINAL_REQUEST)
        self.assertEqual(result["delivered"], 0)
        self.assertTrue(result["outbox_preserved"])
        self.assertTrue(result["reconciled_pre_dispatch"])
        self.assertFalse(self.current.exists())
        self.assertTrue(self.archive.exists())
        self.assertEqual((self.state / "browser_outbox/message-0001.json").read_bytes(), original_outbox)
        broker.ledger.finish.assert_called_once()
        audit = root / ("pre_dispatch_recovery_" + ORIGINAL_REQUEST + ".json")
        self.assertEqual(json.loads(audit.read_text())["request_id"], ORIGINAL_REQUEST)

    def test_crash_after_root_audit_and_archive_can_resume_without_replay(self):
        broker = self.recovery_broker(finish=[OSError("simulated interrupted commit"), None])
        Path(self.config["authority_path"]).parent.mkdir(exist_ok=True)
        with patch("do_again.supervisor.macos_execution.MacOSProcesses.owned", return_value=False), \
             patch("do_again.supervisor.worker_service.service_spec", return_value=("label", {})), \
             patch("do_again.supervisor.worker_service.service_present", return_value=False):
            with self.assertRaisesRegex(OSError, "simulated interrupted commit"):
                reconcile_verified_pre_dispatch(broker, ORIGINAL_REQUEST)
            self.assertFalse(self.current.exists())
            self.assertTrue(self.archive.exists())
            result = reconcile_verified_pre_dispatch(broker, ORIGINAL_REQUEST)
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(broker.ledger.finish.call_count, 2)
        self.assertEqual(result["liveness"], "reconciled_before_send")

    def test_ambiguous_existing_root_audit_never_finishes_started_effect(self):
        broker = self.recovery_broker()
        root = Path(self.config["authority_path"]).parent
        root.mkdir(exist_ok=True)
        audit = root / ("pre_dispatch_recovery_" + ORIGINAL_REQUEST + ".json")
        atomic_json(audit, {"state": "different_evidence"})
        with patch("do_again.supervisor.macos_execution.MacOSProcesses.owned", return_value=False), \
             patch("do_again.supervisor.worker_service.service_spec", return_value=("label", {})), \
             patch("do_again.supervisor.worker_service.service_present", return_value=False):
            with self.assertRaisesRegex(ExecutionBlocked, "audit differs"):
                reconcile_verified_pre_dispatch(broker, ORIGINAL_REQUEST)
        broker.ledger.finish.assert_not_called()
        self.assertTrue(self.current.exists())

    def test_exact_pre_send_evidence_is_read_only(self):
        initial = self.current.read_bytes()
        proof = self.verify()
        self.assertEqual(proof["count"], 1)
        self.assertEqual(proof["current"], self.current)
        self.assertEqual(initial, self.current.read_bytes())
        self.assertFalse(self.archive.exists())

    def test_explicit_original_identity_is_required(self):
        for altered in (
            {"request": "browser-another-request"},
            {"intent": {**self.intent, "epoch": 5}},
            {"intent": {**self.intent, "source_sha": "a" * 40}},
            {"status": {"intent": "active", "epoch": MAINTENANCE_EPOCH}},
            {"status": {"intent": "maintenance", "epoch": MAINTENANCE_EPOCH + 1}},
            {"project": {**self.project, "account": "_doagain_da"}},
        ):
            with self.subTest(altered=altered):
                with self.assertRaises(ExecutionBlocked):
                    self.verify(**altered)

    def test_any_dispatch_or_acknowledgment_blocks_recovery(self):
        for change in (
            {"state": "dispatch_started"},
            {"payload_sha256": "a" * 64},
            {"acknowledgment_token": "ack"},
            {"request_ids": []},
            {"request_ids": ["message-0001", "message-0001"]},
            {"request_ids": ["../message-0001"]},
        ):
            with self.subTest(change=change):
                atomic_json(self.current, {**self.record, **change})
                with self.assertRaises(ExecutionBlocked):
                    self.verify()

    def test_missing_or_aliased_outbox_blocks_recovery(self):
        item = self.state / "browser_outbox" / "message-0001.json"
        item.unlink()
        with self.assertRaises(ExecutionBlocked):
            self.verify()
        item.symlink_to(self.current)
        with self.assertRaises(ExecutionBlocked):
            self.verify()

    def test_archived_pre_send_proof_remains_verifiable(self):
        self.current.rename(self.archive)
        self.assertEqual(self.verify()["archive"], self.archive)
        atomic_json(self.current, self.record)
        with self.assertRaises(ExecutionBlocked):
            self.verify()

    def test_terminal_receipt_blocks_fabrication(self):
        atomic_json(
            self.state / "browser_tick_receipts" / (ORIGINAL_REQUEST + ".json"),
            {"state": "completed"},
        )
        with self.assertRaises(ExecutionBlocked):
            self.verify()


if __name__ == "__main__":
    unittest.main()
