from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


TOOL = Path(__file__).resolve().parents[1] / "tools" / "agent_soak.py"
SPEC = importlib.util.spec_from_file_location("agent_soak", TOOL)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentSoakTests(unittest.TestCase):
    def test_soak_preserves_exact_once_across_duplicates_and_restart(self) -> None:
        result = MODULE.run_soak(40)
        self.assertTrue(result["exact_once"])
        self.assertEqual(result["executions"], 40)
        self.assertEqual(result["unique_executions"], 40)
        self.assertEqual(result["duplicate_replays"], 40)
        self.assertEqual(result["restart_replays"], 40)
        self.assertGreaterEqual(result["latency_ms"]["max"], result["latency_ms"]["median"])
        self.assertIn("excludes Git remote", result["scope"])


if __name__ == "__main__":
    unittest.main()
