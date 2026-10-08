from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from do_again.supervisor.authority import AuthorityDenied, AuthorityRegistry


class OperatorAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "authorized"
        self.registry = AuthorityRegistry(self.root / "supervisor" / "journal.sqlite")

    def test_missing_and_unregistered_authority_fail_closed(self):
        with self.assertRaises(AuthorityDenied):
            with self.registry.admit(self.repo, "spawn"):
                self.fail("effect admitted")
        self.assertFalse(self.registry.path.exists())
        self.registry.initialize()
        with self.assertRaises(AuthorityDenied):
            self.registry.status(self.repo)

    def test_intent_is_durable_and_other_projects_are_not_authorized(self):
        self.registry.initialize()
        self.registry.set_intent(self.repo, "active", goal_revision="accepted-plan-1")
        reopened = AuthorityRegistry(self.registry.path)
        with reopened.admit(self.repo, "claim") as grant:
            self.assertEqual(grant["epoch"], 1)
        with self.assertRaises(AuthorityDenied):
            with reopened.admit(self.root / "other", "claim"):
                self.fail("forged project admitted")
        for intent in ("paused", "maintenance", "stopped"):
            reopened.set_intent(self.repo, intent, goal_revision="accepted-plan-1")
            for action in ("install", "run", "claim", "spawn", "delivery", "rollover", "archive", "upgrade", "restart"):
                with self.assertRaises(AuthorityDenied):
                    with reopened.admit(self.repo, action):
                        self.fail("inactive project admitted")

    def test_pause_waits_for_effect_boundary_then_closes_admission(self):
        self.registry.initialize()
        self.registry.set_intent(self.repo, "active", goal_revision="plan")
        started, finished = threading.Event(), threading.Event()
        def pause():
            started.set()
            self.registry.set_intent(self.repo, "paused", goal_revision="plan")
            finished.set()
        with self.registry.admit(self.repo, "spawn"):
            thread = threading.Thread(target=pause)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(.1))
        thread.join(3)
        self.assertTrue(finished.is_set())
        with self.assertRaises(AuthorityDenied):
            with self.registry.admit(self.repo, "spawn"):
                self.fail("post-pause effect admitted")

    def test_corruption_and_unknown_schema_are_not_reinitialized(self):
        self.registry.path.parent.mkdir()
        self.registry.path.write_bytes(b"corrupt")
        with self.assertRaises(AuthorityDenied):
            self.registry.status(self.repo)
        self.assertEqual(self.registry.path.read_bytes(), b"corrupt")

    def test_symlink_authority_is_rejected(self):
        target = self.root / "target"
        target.write_bytes(b"sentinel")
        self.registry.path.parent.mkdir()
        self.registry.path.symlink_to(target)
        with self.assertRaises(AuthorityDenied):
            self.registry.initialize()
        self.assertEqual(target.read_bytes(), b"sentinel")

    def test_unknown_schema_fails_closed_and_is_preserved(self):
        import sqlite3
        self.registry.initialize()
        with sqlite3.connect(self.registry.path) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaises(AuthorityDenied):
            self.registry.status(self.repo)
        with self.assertRaises(AuthorityDenied):
            self.registry.initialize()

    def test_shared_permissions_and_hardlink_alias_are_rejected(self):
        import os
        self.registry.initialize()
        self.registry.path.chmod(0o644)
        if os.name == "posix":
            with self.assertRaises(AuthorityDenied):
                self.registry.status(self.repo)
        self.registry.path.chmod(0o600)
        os.link(self.registry.path, self.root / "alias")
        with self.assertRaises(AuthorityDenied):
            self.registry.status(self.repo)
