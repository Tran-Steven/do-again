"""Check the private Mac health template does not expand public CI authority."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "private-mac-control" / "mac-health.yml"


class PrivateMacHealthTemplateTests(unittest.TestCase):
    def test_template_is_outside_the_public_workflows_directory(self):
        self.assertTrue(TEMPLATE.is_file())
        self.assertNotIn(".github/workflows", str(TEMPLATE.relative_to(ROOT)))

    def test_requires_private_control_repo_and_manual_or_scoped_push(self):
        text = TEMPLATE.read_text()
        self.assertIn("github.repository == 'Tran-Steven/do-again-mac-control'", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("'requests/health-*.json'", text)
        self.assertIn("runs-on: [self-hosted, macOS, ARM64, do-again-control]", text)
        self.assertNotIn("pull_request:", text)
        self.assertNotIn("pull_request_target:", text)
        self.assertNotIn("repository_dispatch:", text)

    def test_no_checkout_arbitrary_commands_write_scopes_or_installers(self):
        text = TEMPLATE.read_text()
        self.assertIn("permissions: {}", text)
        self.assertNotIn("actions/checkout", text)
        self.assertNotIn("sudo ", text)
        self.assertNotIn("administrator-install", text)
        self.assertNotIn("activate-canary", text)
        self.assertNotIn("start-worker", text)
        self.assertNotIn("workflow_call:", text)
        self.assertNotIn("${{ inputs.", text)

    def test_scope_remains_fixed_status_and_production_disabled(self):
        text = TEMPLATE.read_text()
        self.assertIn('if config.get("production_ready") is not False:', text)
        self.assertIn('{"operation":"status"}', text)
        self.assertIn("operator_identity_matches", text)
        self.assertIn("broker_status=not_requested_runner_identity_isolated", text)
        self.assertNotIn('{"operation":"probe"}', text)
        self.assertNotIn('{"operation":"set_intent"}', text)


if __name__ == "__main__":
    unittest.main()
