from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "branch_inventory.py"
SPEC = importlib.util.spec_from_file_location("branch_inventory", SCRIPT)
assert SPEC and SPEC.loader
inventory_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inventory_module
SPEC.loader.exec_module(inventory_module)


def branch(name, sha, protected=False):
    return {"name": name, "commit": {"sha": sha}, "protected": protected}


def pull(number, name, sha, *, state="closed", merged=True, repo="Tran-Steven/do-again"):
    return {
        "number": number,
        "state": state,
        "head": {"ref": name, "sha": sha, "repo": {"full_name": repo}},
        "merged_at": "2026-10-08T20:00:00Z" if merged else None,
        "merge_commit_sha": "e" * 40 if merged else None,
    }


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.main_sha = "a" * 40
        self.merged_sha = "b" * 40
        self.changed_sha = "c" * 40
        self.open_sha = "d" * 40
        self.branches = [
            branch("main", self.main_sha),
            branch("operator-control", "1" * 40),
            branch("fix/merged", self.merged_sha),
            branch("fix/changed", self.changed_sha),
            branch("fix/open", self.open_sha),
            branch("fix/protected", "2" * 40, True),
        ]
        self.pulls = [
            pull(12, "fix/merged", self.merged_sha),
            pull(13, "fix/changed", "9" * 40),
            pull(14, "fix/open", self.open_sha, state="open", merged=False),
            pull(15, "fix/protected", "2" * 40),
        ]
        self.local = {}
        self.calls = []

    def github(self, path, *, paginate=False):
        self.calls.append((path, paginate))
        if path == "repos/Tran-Steven/do-again":
            return {"default_branch": "main"}
        if path.endswith("/branches?per_page=100"):
            return self.branches
        if path.endswith("/pulls?state=all&per_page=100"):
            return self.pulls
        if "/compare/" in path:
            return {"ahead_by": 0, "behind_by": 1, "status": "behind"}
        raise AssertionError(path)

    def inspect(self, *, local=True):
        with patch.object(inventory_module, "_github", side_effect=self.github), patch.object(
            inventory_module, "_worktrees", return_value=self.local if local else None,
        ):
            return inventory_module.inventory(
                "Tran-Steven/do-again",
                local_repo=Path("/synthetic/repo") if local else None,
            )

    def test_merged_branch_is_review_only_never_automatically_deletable(self):
        payload = self.inspect()
        by_name = {item["branch"]: item for item in payload["branches"]}
        merged = by_name["fix/merged"]
        self.assertTrue(merged["candidate_for_operator_review"])
        self.assertFalse(merged["eligible_for_deletion"])
        self.assertEqual(merged["matching_merged_pull_request"], 12)
        self.assertEqual(merged["comparison_to_default"]["behind"], 1)
        self.assertIn("agent_ownership_and_lease_unverified", merged["exclusions"])
        self.assertEqual(payload["review_candidate_count"], 1)
        self.assertFalse(payload["deletion_enabled"])

    def test_protects_transport_open_pr_changed_tip_protection_and_worktree(self):
        self.local["fix/merged"] = ["/synthetic/active-worktree"]
        payload = self.inspect()
        by_name = {item["branch"]: item for item in payload["branches"]}
        self.assertIn("reserved_branch", by_name["main"]["exclusions"])
        self.assertIn("reserved_branch", by_name["operator-control"]["exclusions"])
        self.assertIn("open_pull_request", by_name["fix/open"]["exclusions"])
        self.assertIn("branch_head_differs_from_merged_pull_request", by_name["fix/changed"]["exclusions"])
        self.assertIn("protected_or_unknown", by_name["fix/protected"]["exclusions"])
        self.assertIn("local_worktree", by_name["fix/merged"]["exclusions"])
        self.assertEqual(payload["review_candidate_count"], 0)
        self.assertTrue(all(not item["eligible_for_deletion"] for item in payload["branches"]))

    def test_missing_local_state_fails_closed_for_every_branch(self):
        payload = self.inspect(local=False)
        self.assertFalse(payload["local_worktrees_checked"])
        self.assertEqual(payload["review_candidate_count"], 0)
        for item in payload["branches"]:
            self.assertIn("local_worktrees_unverified", item["exclusions"])

    def test_fork_head_cannot_authorize_cleanup_of_local_branch(self):
        self.pulls.append(pull(18, "fix/changed", self.changed_sha, repo="Unrelated/fork"))
        payload = self.inspect()
        changed = next(item for item in payload["branches"] if item["branch"] == "fix/changed")
        self.assertIsNone(changed["matching_merged_pull_request"])
        self.assertEqual([pr["number"] for pr in changed["merged_pull_requests"]], [13])

    def test_missing_comparison_never_becomes_review_candidate(self):
        original = self.github

        def github(path, *, paginate=False):
            if "/compare/" in path:
                raise inventory_module.InventoryError("unavailable")
            return original(path, paginate=paginate)

        with patch.object(inventory_module, "_github", side_effect=github), patch.object(
            inventory_module, "_worktrees", return_value={},
        ):
            payload = inventory_module.inventory("Tran-Steven/do-again", local_repo=Path("/synthetic"))
        merged = next(item for item in payload["branches"] if item["branch"] == "fix/merged")
        self.assertIsNone(merged["comparison_to_default"])
        self.assertIn("comparison_unavailable", merged["exclusions"])

    def test_incomplete_branch_or_pr_response_aborts(self):
        self.branches = [branch("fix/merged", self.merged_sha)]
        with self.assertRaises(inventory_module.InventoryError):
            self.inspect()
        self.branches.append(branch("main", self.main_sha))
        self.pulls = [None]
        with self.assertRaises(inventory_module.InventoryError):
            self.inspect()

    def test_repo_input_cannot_change_endpoint(self):
        with patch.object(inventory_module, "_github") as github:
            for bad in ("../other", "a/b/../c", "x/y?state=open", "x y", "a//b"):
                with self.subTest(bad=bad), self.assertRaises(inventory_module.InventoryError):
                    inventory_module.inventory(bad)
            github.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_pagination_malformed_fails_closed(self):
        for raw in ("{}", '{"error":"denied"}', '[{"name":"branch"}]', 'null'):
            with self.subTest(raw=raw):
                with patch.object(
                    inventory_module.subprocess,
                    "run",
                    return_value=Mock(returncode=0, stdout=raw),
                ):
                    with self.assertRaises(inventory_module.InventoryError):
                        inventory_module._github("repos/test/repo/branches?per_page=100", paginate=True)

    def test_api_errors_do_not_produce_partial_inventory(self):
        with patch.object(
            inventory_module.subprocess,
            "run",
            return_value=Mock(returncode=1, stdout="", stderr="secret-bearing error"),
        ):
            with self.assertRaises(inventory_module.InventoryError) as raised:
                inventory_module._github("repos/test/repo")
        self.assertNotIn("secret-bearing", str(raised.exception))

    def test_worktree_porcelain_tracks_local_references(self):
        output = (
            "worktree /safe/repo\n"
            "HEAD " + "a" * 40 + "\n"
            "branch refs/heads/main\n\n"
            "worktree /safe/other\n"
            "HEAD " + "b" * 40 + "\n"
            "branch refs/heads/fix/merged\n\n"
            "worktree /safe/detached\n"
            "HEAD " + "c" * 40 + "\n"
            "detached\n"
        )
        with patch.object(
            inventory_module.subprocess,
            "run",
            return_value=Mock(returncode=0, stdout=output),
        ):
            result = inventory_module._worktrees(Path("/safe/repo"))
        self.assertEqual(result, {"main": ["/safe/repo"], "fix/merged": ["/safe/other"]})

    def test_no_remote_delete_command_exists(self):
        with patch.object(
            inventory_module.subprocess,
            "run",
            return_value=Mock(returncode=0, stdout="[[{\"name\": \"main\"}]]"),
        ) as run:
            self.assertEqual(inventory_module._github("repos/test/repo/branches?per_page=100", paginate=True), [{"name": "main"}])
        self.assertIn("--paginate", run.call_args.args[0])
        self.assertNotIn("DELETE", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
