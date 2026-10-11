from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "storage_inventory.py"
SPEC = importlib.util.spec_from_file_location("storage_inventory", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


class StorageInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "do-again"
        self.root.mkdir()

    def write(self, relative, content=b"data", *, age_days=None):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if age_days is not None:
            old = time.time() - age_days * 86400
            os.utime(path, (old, old))
            os.utime(path.parent, (old, old))
        return path

    def test_reports_storage_without_changing_files(self):
        old = self.write("src/__pycache__/module.pyc", b"a" * 700, age_days=50)
        recent = self.write("tests/.pytest_cache/recent", b"b" * 500)
        protected = self.write("state/receipts/receipt.json", b"c" * 300)
        root = module.inventory([self.root], now=time.time())
        entry = root["roots"][0]
        self.assertEqual(entry["bytes"], 1500)
        self.assertEqual(entry["files"], 3)
        self.assertFalse(root["deletion_enabled"])
        self.assertEqual({item["path"] for item in entry["review_candidates"]},
                         {str(old.parent), str(recent.parent)})
        old_item = next(item for item in entry["review_candidates"] if item["path"] == str(old.parent))
        self.assertTrue(old_item["older_than_min_age"])
        self.assertTrue(protected.is_file())
        self.assertTrue(old.is_file())

    def test_active_runtime_is_flagged_as_protected(self):
        self.write("projects/example/runtime/__pycache__/code.pyc")
        result = module.inventory([self.root])
        candidate = result["roots"][0]["review_candidates"][0]
        self.assertEqual(candidate["protected_ancestors"], ["runtime"])
        self.assertFalse(candidate["eligible_for_deletion"])

    def test_symlink_escape_not_followed(self):
        if os.name == "nt":
            self.skipTest("Windows symlink privileges are not guaranteed")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"x" * 1000)
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        result = module.inventory([self.root])["roots"][0]
        self.assertEqual(result["skipped_symlinks"], 1)
        self.assertEqual(result["bytes"], 0)
        self.assertTrue((outside / "secret").exists())

    def test_rejects_symlink_root_and_overlapping_roots(self):
        if os.name == "nt":
            self.skipTest("Windows symlink privileges are not guaranteed")
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(module.InventoryError):
            module.inventory([link])
        nested = self.root / "nested"
        nested.mkdir()
        with self.assertRaises(module.InventoryError):
            module.inventory([self.root, nested])

    def test_json_output_is_non_destructive(self):
        self.write("build/generated", b"123")
        with patch("sys.stdout") as stdout:
            self.assertEqual(module.main(["--root", str(self.root), "--json"]), 0)
        result = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertFalse(result["deletion_enabled"])
        self.assertEqual(result["roots"][0]["total_review_candidates"], 1)
        self.assertTrue((self.root / "build" / "generated").exists())

    def test_invalid_ranges_are_rejected(self):
        for kwargs in ({"min_age_days": -1}, {"limit": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(module.InventoryError):
                module.inventory([self.root], **kwargs)


if __name__ == "__main__":
    unittest.main()
