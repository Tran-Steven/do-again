"""Single attended V1A recovery is bounded, inspected and never retries."""
import ast
from pathlib import Path
import unittest


class AttendedRecoveryTests(unittest.TestCase):
    def test_single_recovery_has_strict_irreversible_gates(self):
        script=Path(__file__).resolve().parents[1]/"tools/attended_recover_v1a_manifest_failure.py"
        source=script.read_text()
        tree=ast.parse(source,filename=str(script))
        self.assertTrue(any(isinstance(node,ast.FunctionDef) and node.name=="main" for node in tree.body))
        for marker in ("EXPECTED", "REQUEST", "FINGERPRINT"):
            if marker!="EXPECTED":
                self.assertIn(marker,source)
        self.assertIn("AUDITED_PROBE_QUARANTINED",source)
        self.assertIn("would_quarantine",source)
        self.assertIn("acknowledge-irreversible-abandonment",source)
        self.assertIn('"maintenance"',source)
        self.assertIn('transition.get("epoch")!=15',source)
        self.assertIn("controller_journal_retained",source)
        self.assertIn("repair_v1a_browser_controller.py",source)
        self.assertNotIn('"controller_tick"',source)
        self.assertNotIn('"controller_start"',source)
        self.assertNotIn("application_submit",source)
        self.assertNotIn("automatic_merge",source)

    def test_rejects_unverified_source_and_operator_epoch_prior_to_write(self):
        source=(Path(__file__).resolve().parents[1]/"tools/attended_recover_v1a_manifest_failure.py").read_text()
        self.assertIn("APPROVED_BASE",source)
        self.assertIn("merge-base",source)
        self.assertIn('status.get("epoch")!=14',source)
        self.assertIn('status.get("operator_intent")!="active"',source)
        self.assertIn('pending[0].get("state")!="started"',source)
        self.assertIn('controller.get("requests")!=[]',source)
        self.assertIn('grant.is_file()',source)


if __name__=="__main__":
    unittest.main()
