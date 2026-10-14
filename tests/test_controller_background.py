import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from do_again.service import controller_background as runner
from do_again.supervisor.macos_execution import ExecutionBlocked


@unittest.skipUnless(runner.fcntl is not None, "POSIX controller lock")
class UnattendedControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "jobpipe"
        self.repo.mkdir()
        self.sha = "a" * 40
        self.authority = {
            "operator_intent": "active", "production_ready": False,
            "enforcement_verified": True, "source_sha": self.sha, "epoch": 15,
        }
        target = patch.object(runner, "_target", return_value=self.repo)
        state = patch.object(runner, "_root", return_value=self.root)
        target.start()
        state.start()
        self.addCleanup(target.stop)
        self.addCleanup(state.stop)

    def test_finishes_three_real_receipt_states_without_user_ticks(self):
        calls = []
        phases = iter([
            {"state": "waiting", "acknowledged": []},
            {"state": "planning", "acknowledged": ["one"]},
            {"state": "completed", "acknowledged": ["one", "two", "three"]},
        ])
        def broker(repo, packet):
            calls.append(packet["operation"])
            if packet["operation"] == "status":
                return self.authority
            if packet["operation"] == "controller_status":
                return {"state": "stopped", "acknowledged": []}
            if packet["operation"] == "controller_start":
                return {"state": "planning", "acknowledged": []}
            return next(phases)
        with patch.object(runner, "broker_request", side_effect=broker), \
             patch.object(runner.time, "sleep") as sleep:
            outcome = runner.run_bounded(self.repo, source=self.sha, epoch=15,
                                         poll_seconds=1, max_seconds=100)
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(outcome["acknowledged_cycles"], 3)
        self.assertEqual(calls.count("controller_tick"), 3)
        self.assertEqual(calls.count("controller_start"), 1)
        self.assertEqual(sleep.call_count, 2)

    def test_paused_operator_never_starts_controller_or_ticks(self):
        observations = []
        def broker(repo, packet):
            observations.append(packet["operation"])
            return dict(self.authority, operator_intent="maintenance")
        with patch.object(runner, "broker_request", side_effect=broker):
            with self.assertRaisesRegex(ExecutionBlocked, "active and qualified"):
                runner.run_bounded(self.repo, source=self.sha, epoch=15)
        self.assertEqual(observations, ["status"])
        self.assertEqual(json.loads((self.root/"status.json").read_text())["state"], "blocked")

    def test_uncertain_tick_is_not_retried(self):
        calls = []
        def broker(repo, packet):
            calls.append(packet["operation"])
            if packet["operation"] == "status":
                return self.authority
            if packet["operation"] == "controller_status":
                return {"state": "planning", "acknowledged": []}
            raise ExecutionBlocked("original browser dispatch uncertain")
        with patch.object(runner, "broker_request", side_effect=broker), \
             patch.object(runner.time, "sleep") as sleep:
            with self.assertRaisesRegex(ExecutionBlocked, "uncertain"):
                runner.run_bounded(self.repo, source=self.sha, epoch=15)
        self.assertEqual(calls.count("controller_tick"), 1)
        self.assertEqual(sleep.call_count, 0)
        self.assertIn("uncertain", json.loads((self.root/"status.json").read_text())["blocker"])

    def test_background_launcher_is_frozen_to_native_source_and_epoch(self):
        # No launchd or root process is invoked; plist is inspected in a fixture.
        fake_home = self.root/"home"
        fake_home.mkdir()
        def broker(repo, packet):
            if packet["operation"] == "status":
                return self.authority
            return {"state": "stopped"}
        unloaded = Mock(returncode=113, stdout=b"", stderr=b"not loaded")
        started = Mock(returncode=0, stdout=b"", stderr=b"")
        with patch.object(Path, "home", return_value=fake_home), \
             patch.object(runner, "broker_request", side_effect=broker), \
             patch.object(runner.subprocess, "run", side_effect=[unloaded, started]) as launch:
            result = runner.background_start(self.repo)
        self.assertEqual(result["state"], "launched")
        self.assertEqual(result["epoch"], 15)
        plist = fake_home/"Library"/"LaunchAgents"/(runner.LABEL+".plist")
        args = plistlib.loads(plist.read_bytes())["ProgramArguments"]
        self.assertEqual(args[-4:], ["--source-sha", self.sha, "--epoch", "15"])
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(launch.call_args.args[0][:2], ["launchctl", "bootstrap"])


if __name__ == "__main__":
    unittest.main()
