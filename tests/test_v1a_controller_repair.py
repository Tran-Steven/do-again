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
        self.assertIn("OLD_SOURCE = \"9ea8d9306354c3a6b44da57a1caf57e0019abd8e\"", source)
        self.assertIn('v1a-browser-repair-grants-', source)

        self.assertIn('browser_calls") != 0', source)
        self.assertIn('CONTROLLER', source.upper())
        self.assertNotIn('controller_start', source)
        self.assertNotIn('controller_tick', source)
        self.assertNotIn('application_submit', source)


if __name__ == "__main__":
    unittest.main()
