"""ChatGPT project binding generations must survive harmless re-registration."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from do_again.browser import runtime


class ProjectBindingIdempotenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(runtime.os.environ, {"DO_AGAIN_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.repo = Path(self.temp.name) / "jobpipe"
        self.repo.mkdir()
        self.url = "https://chatgpt.com/c/project-original"

    def test_identical_explicit_chat_url_preserves_binding(self):
        first = runtime.register_project(
            self.repo, remote_url="https://github.com/Tran-Steven/jobpipe.git",
            control_branch="operator-control", chat_url=self.url)
        original = runtime.binding_identity(first)
        generation = first["binding_generation"]

        # A durable browser delivery intent is pinned to the original
        # conversation identity, and remains valid after ordinary setup.
        state = Path(self.temp.name) / "protected-browser-synthetic"
        state.mkdir()
        saved = state / "browser_submission_uncertain.json"
        saved.write_text(json.dumps({
            "schema_version": 1, "state": "preparing", "chat_url": self.url,
            "binding_identity": original, "request_ids": ["receipt-original"]}))
        later = runtime.register_project(
            self.repo, chat_url=self.url, control_branch="operator-control")
        self.assertEqual(later["binding_generation"], generation)
        self.assertEqual(runtime.binding_identity(later), original)
        self.assertEqual(json.loads(saved.read_text())["binding_identity"], original)

    def test_explicit_different_chat_creates_new_generation(self):
        initial = runtime.register_project(self.repo, chat_url=self.url)
        replacement = runtime.register_project(
            self.repo, chat_url="https://chatgpt.com/c/project-successor")
        self.assertNotEqual(initial["binding_generation"], replacement["binding_generation"])
        self.assertNotEqual(runtime.binding_identity(initial), runtime.binding_identity(replacement))
        self.assertEqual(replacement["chat_url"], "https://chatgpt.com/c/project-successor")

    def test_legacy_binding_without_explicit_generation_stays_stable(self):
        # Old installs hashed the literal "legacy" generation. A same-URL
        # registration must not silently invalidate their persisted intents.
        old = runtime.register_project(self.repo)
        old["chat_url"] = self.url
        self.assertNotIn("binding_generation", old)
        runtime._atomic_json(runtime._project_record_path(self.repo), old)
        identity = runtime.binding_identity(old)
        again = runtime.register_project(self.repo, chat_url=self.url)
        self.assertEqual(runtime.binding_identity(again), identity)
        self.assertNotIn("binding_generation", again)

    def test_no_url_registration_preserves_generation_and_url(self):
        initial = runtime.register_project(self.repo, chat_url=self.url)
        later = runtime.register_project(self.repo, remote_url="https://github.com/Tran-Steven/jobpipe.git")
        self.assertEqual(later["chat_url"], initial["chat_url"])
        self.assertEqual(runtime.binding_identity(later), runtime.binding_identity(initial))


if __name__ == "__main__":
    unittest.main()
