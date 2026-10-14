import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.core.schema import OperatorError
from do_again.service.isolated_launchd import (
    install_isolated_service, isolated_service_status,
    serve_isolated_enrollment, start_isolated_service, stop_isolated_service,
)


class IsolatedLaunchdTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "Sonary"
        self.project.mkdir()
        self.isolated = self.root / "isolated"
        self.isolated.mkdir(mode=0o700)
        self.plist = self.root / "LaunchAgents" / "isolated-service.plist"
        self.label = "io.github.tran-steven.do-again.isolated.fixture"
        self.layout = {
            "root": self.isolated,
            "source_repo": self.project,
            "key": "fixture",
            "state_dir": self.isolated / "state",
        }
        self.config = {"goal": "synthetic", "cycles": 2}
        layout = patch(
            "do_again.service.isolated_launchd._service_layout",
            return_value=(self.layout, self.config, self.label, self.plist))
        layout.start()
        self.addCleanup(layout.stop)
        domain = patch(
            "do_again.service.isolated_launchd._domain",
            return_value="gui/501")
        domain.start()
        self.addCleanup(domain.stop)

    def test_install_uses_only_separate_user_label_and_bounded_python_entry(self):
        states = [False, True]
        with patch(
            "do_again.service.isolated_launchd._service_loaded",
            side_effect=lambda label: states.pop(0) if states else True
        ), patch(
            "do_again.service.isolated_launchd._launchctl",
            return_value=subprocess.CompletedProcess([], 0, "pid = 456\n", "")
        ) as ctl:
            result = install_isolated_service(self.project)
        self.assertTrue(result["installed"])
        self.assertTrue(result["launchd_loaded"])
        self.assertTrue(result["process_running"])
        self.assertEqual(result["pid"], 456)
        plist = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(plist["Label"], self.label)
        self.assertEqual(
            plist["ProgramArguments"][1:4],
            ["-m", "do_again.cli", "isolated-service-run"])
        self.assertEqual(
            plist["ProgramArguments"][-2:], ["--source", str(self.project)])
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertNotIn("operator-control", json.dumps(plist))
        self.assertNotIn("/bin/sh", json.dumps(plist))
        self.assertTrue((self.isolated / "runtime" / "do_again" / "__init__.py").exists())
        self.assertTrue(any(call.args[0] == "bootstrap" for call in ctl.call_args_list))

    def test_start_does_not_interrupt_a_running_browser_send(self):
        with patch(
            "do_again.service.isolated_launchd._service_loaded",
            return_value=False
        ), patch(
            "do_again.service.isolated_launchd._launchctl",
            return_value=subprocess.CompletedProcess([], 0, "", "")
        ):
            install_isolated_service(self.project)
        with patch(
            "do_again.service.isolated_launchd._service_loaded",
            return_value=True
        ), patch(
            "do_again.service.isolated_launchd._launchctl",
            return_value=subprocess.CompletedProcess([], 0, "pid = 987\n", "")
        ) as ctl:
            current = start_isolated_service(self.project)
        self.assertTrue(current["process_running"])
        self.assertEqual(current["pid"], 987)
        self.assertTrue(all(call.args[0] == "print" for call in ctl.call_args_list))

    def test_tampered_installation_is_not_executed(self):
        with patch(
            "do_again.service.isolated_launchd._service_loaded",
            return_value=False
        ), patch(
            "do_again.service.isolated_launchd._launchctl",
            return_value=subprocess.CompletedProcess([], 0, "", "")
        ):
            install_isolated_service(self.project)
        data = plistlib.loads(self.plist.read_bytes())
        data["ProgramArguments"] = ["/bin/sh", "-c", "echo unsafe"]
        self.plist.write_bytes(plistlib.dumps(data))
        with self.assertRaisesRegex(OperatorError, "no longer matches"):
            start_isolated_service(self.project)

    def test_stop_preserves_private_development_state(self):
        with patch(
            "do_again.service.isolated_launchd._service_loaded",
            return_value=False
        ), patch(
            "do_again.service.isolated_launchd._launchctl",
            return_value=subprocess.CompletedProcess([], 0, "", "")
        ):
            install_isolated_service(self.project)
        marker = self.isolated / "state" / "resume.json"
        marker.parent.mkdir()
        marker.write_text('{"state":"uncertain"}')
        with patch(
            "do_again.service.isolated_launchd._service_loaded",
            side_effect=[True, False]
        ), patch(
            "do_again.service.isolated_launchd._launchctl",
            return_value=subprocess.CompletedProcess([], 0, "", "")
        ) as ctl:
            stopped = stop_isolated_service(self.project)
        self.assertFalse(stopped["launchd_loaded"])
        self.assertEqual(json.loads(marker.read_text())["state"], "uncertain")
        self.assertTrue(self.plist.exists())
        self.assertEqual(ctl.call_args.args[0], "bootout")

    def test_missing_enrollment_does_not_restart_background_process(self):
        with patch(
            "do_again.service.isolated_launchd.enrolled_config",
            side_effect=OperatorError("enrollment metadata is damaged")
        ), patch(
            "do_again.service.isolated_launchd._private_layout",
            return_value=self.layout
        ), patch(
            "do_again.service.isolated_launchd.run_enrolled_isolated"
        ) as run:
            result = serve_isolated_enrollment(self.project)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("damaged", result["error"])
        run.assert_not_called()
        self.assertEqual(
            json.loads((self.isolated / "run-status.json").read_text())["state"],
            "blocked")

    def test_delayed_receipt_reconciles_in_same_background_run(self):
        answers = [
            {"state": "awaiting_receipt", "request_id": "isolated-123", "completed_cycles": 0},
            {"state": "awaiting_receipt", "request_id": "isolated-123", "completed_cycles": 0},
            {"state": "completed_bounded_cycles", "completed_cycles": 1},
        ]
        with patch(
            "do_again.service.isolated_launchd.enrolled_config",
            return_value=(self.layout, self.config)
        ), patch(
            "do_again.service.isolated_launchd.run_enrolled_isolated",
            side_effect=answers
        ) as run, patch(
            "do_again.service.isolated_launchd.time.sleep"
        ) as sleep:
            result = serve_isolated_enrollment(
                self.project, poll_seconds=30, max_rechecks=5)
        self.assertEqual(result["state"], "completed_bounded_cycles")
        self.assertEqual(result["completed_cycles"], 1)
        self.assertEqual(result["rechecks"], 2)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_any_call(30)
        self.assertEqual(
            json.loads((self.isolated / "run-status.json").read_text())["state"],
            "completed_bounded_cycles")

    def test_missing_receipt_stops_after_fixed_budget_with_durable_status(self):
        pending = {"state": "awaiting_receipt", "request_id": "isolated-456",
                   "completed_cycles": 0}
        with patch(
            "do_again.service.isolated_launchd.enrolled_config",
            return_value=(self.layout, self.config)
        ), patch(
            "do_again.service.isolated_launchd.run_enrolled_isolated",
            return_value=pending
        ) as run, patch(
            "do_again.service.isolated_launchd.time.sleep"
        ) as sleep:
            result = serve_isolated_enrollment(
                self.project, poll_seconds=20, max_rechecks=2)
        self.assertEqual(result["state"], "awaiting_receipt")
        self.assertEqual(result["rechecks"], 2)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(
            json.loads((self.isolated / "run-status.json").read_text())["state"],
            "awaiting_receipt")

    def test_uncertain_receipt_delivery_blocks_instead_of_retrying(self):
        pending = {"state": "awaiting_receipt", "request_id": "isolated-789"}
        with patch(
            "do_again.service.isolated_launchd.enrolled_config",
            return_value=(self.layout, self.config)
        ), patch(
            "do_again.service.isolated_launchd.run_enrolled_isolated",
            side_effect=[pending, OperatorError("browser delivery uncertain")]
        ) as run, patch(
            "do_again.service.isolated_launchd.time.sleep"
        ) as sleep:
            result = serve_isolated_enrollment(
                self.project, poll_seconds=20, max_rechecks=3)
        self.assertEqual(result["state"], "blocked")
        self.assertIn("uncertain", result["error"])
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(20)

    def test_invalid_poll_policy_fails_before_any_service_or_chat_io(self):
        with patch("do_again.service.isolated_launchd.enrolled_config") as load:
            with self.assertRaisesRegex(OperatorError, "poll policy"):
                serve_isolated_enrollment(self.project, max_rechecks=61)
            with self.assertRaisesRegex(OperatorError, "poll policy"):
                serve_isolated_enrollment(self.project, poll_seconds=-1)
        load.assert_not_called()

    def test_service_records_terminal_and_blocked_outcome_without_retry(self):
        with patch(
            "do_again.service.isolated_launchd.enrolled_config",
            return_value=(self.layout, self.config)
        ), patch(
            "do_again.service.isolated_launchd.run_enrolled_isolated",
            return_value={"state": "model_declared_complete", "completed_cycles": 1}
        ) as run:
            completed = serve_isolated_enrollment(self.project)
        self.assertEqual(completed["state"], "model_declared_complete")
        self.assertEqual(completed["completed_cycles"], 1)
        run.assert_called_once()
        with patch(
            "do_again.service.isolated_launchd.enrolled_config",
            return_value=(self.layout, self.config)
        ), patch(
            "do_again.service.isolated_launchd.run_enrolled_isolated",
            side_effect=OperatorError("browser outcome uncertain")
        ) as run:
            blocked = serve_isolated_enrollment(self.project)
        self.assertEqual(blocked["state"], "blocked")
        self.assertIn("uncertain", blocked["error"])
        run.assert_called_once()
        self.assertEqual(
            json.loads((self.isolated / "run-status.json").read_text())["state"],
            "blocked")


if __name__ == "__main__":
    unittest.main()
