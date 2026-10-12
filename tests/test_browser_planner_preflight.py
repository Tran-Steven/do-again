"""Browser planner packaging must fail before expensive runtime staging."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class BrowserPreflightTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'macos_preparer',
            Path(__file__).resolve().parents[1] / 'tools/prepare_macos_supervisor.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.controller = self.root / 'controller.json'
        self.browser = self.root / 'browser.json'
        self.write_controller('chatgpt-browser-cdp')
        self.browser.write_text('{}')
        self.browser.chmod(0o600)

    def write_controller(self, planner):
        self.controller.write_text(json.dumps({
            'scope':{'planner':planner}, 'goal':{}, 'profile':{},
        }))
        self.controller.chmod(0o600)

    def test_explicit_browser_grant_preflight_succeeds_without_staging(self):
        self.module.require_planner_grants_before_staging(
            self.controller, None, None, self.browser)
        self.assertEqual(set(self.root.iterdir()), {self.controller, self.browser})

    def test_missing_browser_authority_rejected(self):
        with self.assertRaisesRegex(ValueError, 'separate private browser grant'):
            self.module.require_planner_grants_before_staging(
                self.controller, None, None)

    def test_mixed_codex_authority_rejected(self):
        with self.assertRaisesRegex(ValueError, 'cannot inherit Codex'):
            self.module.require_planner_grants_before_staging(
                self.controller, None, self.root / 'codex', self.browser)

    def test_browser_scope_cannot_be_borrowed_by_codex(self):
        self.write_controller('codex-cli')
        with self.assertRaisesRegex(ValueError, 'cannot inherit a browser'):
            self.module.require_planner_grants_before_staging(
                self.controller, self.root / 'hosted.json',
                self.root / 'codex', self.browser)

    def test_non_private_browser_grant_refused(self):
        self.browser.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'operator-owned and private'):
            self.module.require_planner_grants_before_staging(
                self.controller, None, None, self.browser)

    def test_unpaired_browser_grant_never_creates_output(self):
        output = self.root / 'output'
        with self.assertRaises(ValueError):
            self.module.prepare(self.root, self.root, self.root, output,
                                browser_planner_grant=self.browser)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
