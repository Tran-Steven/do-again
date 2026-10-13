"""Offline regression: repair entrypoint is parseable and non-automatic."""
import ast
import unittest
from pathlib import Path


class RepairEntrypointTests(unittest.TestCase):
    def test_guarded_repair_entrypoint_is_valid_python(self):
        script = Path(__file__).resolve().parents[1] / "tools/repair_v1a_browser_controller.py"
        source = script.read_text()
        tree = ast.parse(source, filename=str(script))
        self.assertTrue(any(isinstance(node, ast.FunctionDef) and node.name == "main"
                            for node in tree.body))
        self.assertIn('if __name__ == "__main__":', source)
        self.assertIn('qualify_browser_planner', source)
        self.assertIn('qualify_native_enforcement', source)
        self.assertIn('NATIVE_ENFORCEMENT_VERIFIED', source)
        self.assertIn("OLD_SOURCE = \"7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191\"", source)
        self.assertIn('v1a-browser-repair-grants-', source)

        self.assertIn('browser_calls") != 0', source)
        self.assertIn('CONTROLLER', source.upper())
        self.assertNotIn('controller_start', source)
        self.assertNotIn('controller_tick', source)
        self.assertNotIn('application_submit', source)


if __name__ == "__main__":
    unittest.main()
