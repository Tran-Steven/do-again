"""Offline regression: repair entrypoint is parseable and non-automatic."""
import ast
import unittest
from pathlib import Path


class RepairEntrypointTests(unittest.TestCase):
    def test_path_binding_is_module_scoped_not_shadowed_in_main(self):
        script = Path(__file__).resolve().parents[1] / "tools/repair_v1a_browser_controller.py"
        tree = ast.parse(script.read_text(), filename=str(script))
        self.assertTrue(any(
            isinstance(node, ast.ImportFrom)
            and node.module == "pathlib"
            and any(alias.name == "Path" for alias in node.names)
            for node in tree.body
        ))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "main")
        self.assertFalse(any(
            isinstance(node, ast.ImportFrom)
            and node.module == "pathlib"
            and any(alias.name == "Path" for alias in node.names)
            for node in ast.walk(main)
        ), "Importing Path inside main shadows Path.home() during preflight")

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
        self.assertIn("OLD_SOURCE = \"33e91f0a66093cef10e797eb8ba3f687ccd410cd\"", source)
        self.assertIn('v1a-browser-repair-grants-', source)
        self.assertIn('original audited probe quarantine differs', source)

        self.assertIn('browser_calls") != 0', source)
        self.assertIn('CONTROLLER', source.upper())
        self.assertNotIn('controller_start', source)
        self.assertNotIn('controller_tick', source)
        self.assertNotIn('application_submit', source)


if __name__ == "__main__":
    unittest.main()
