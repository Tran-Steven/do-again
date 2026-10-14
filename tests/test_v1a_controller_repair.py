"""Offline regression: repair entrypoint is parseable and non-automatic."""
import ast
import unittest
import os
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

    @unittest.skipUnless(os.name=="posix", "native POSIX fixture; Windows native parity is not claimed")
    def test_normal_user_preflight_never_reads_root_private_evidence(self):
        import importlib.util
        from unittest.mock import patch
        script = Path(__file__).resolve().parents[1] / "tools/repair_v1a_browser_controller.py"
        spec = importlib.util.spec_from_file_location("repair", script)
        repair = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repair)
        project = {"account": "_doagain_jp", "source_sha": repair.JOBPIPE_SOURCE,
                   "key": "fixture"}
        config = {"source_sha": repair.OLD_SOURCE, "production_ready": False,
                  "controller_scope": {"planner": "chatgpt-browser-cdp"},
                  "projects": [project], "authority_path": "/root/private.sqlite"}
        import json
        def read(path, *args, **kwargs):
            if path == repair.ROOT / "current/config.json":
                return json.dumps(config)
            raise PermissionError("root-private evidence")
        def is_file(path):
            if path.name == "browser.json":
                return True
            raise PermissionError("root-private archive")
        with patch.object(repair.sys, "platform", "darwin"), \
             patch.object(repair.os, "getuid", return_value=501), \
             patch.object(repair.os, "geteuid", return_value=501), \
             patch.object(Path, "read_text", read), \
             patch.object(Path, "is_file", is_file), \
             patch("sqlite3.connect", side_effect=PermissionError("root-private DB")) as db, \
             patch.object(repair, "operator_packet", return_value={
                 "operator_intent": "maintenance", "epoch": 15,
                 "unresolved_executions": [], "inflight_request_ids": []}) as operator, \
             patch.object(repair, "output", return_value="dirty"), \
             patch.object(repair, "run") as run:
            with self.assertRaisesRegex(SystemExit, "candidate repository is dirty"):
                repair.main()
        db.assert_not_called()
        operator.assert_called_once_with("fixture", "status")
        run.assert_not_called()

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

        self.assertIn('browser_calls") != 0', source)
        self.assertIn('CONTROLLER', source.upper())
        self.assertNotIn('controller_start', source)
        self.assertNotIn('controller_tick', source)
        self.assertNotIn('application_submit', source)


if __name__ == "__main__":
    unittest.main()
