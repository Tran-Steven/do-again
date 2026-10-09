from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


class InventoryError(Exception):
    pass


def _github(path: str, *, paginate: bool = False) -> Any:
    argv = ["gh", "api"]
    if paginate:
        argv.extend(["--paginate", "--slurp"])
    argv.append(path)
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryError(f"GitHub read failed: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise InventoryError(f"GitHub read failed for {path.split('?')[0]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InventoryError("GitHub returned invalid JSON") from exc
    if not paginate:
        return payload
    if not isinstance(payload, list) or any(not isinstance(page, list) for page in payload):
        raise InventoryError("GitHub pagination was incomplete or malformed")
    return [item for page in payload for item in page]


def _worktrees(path: Path | None) -> dict[str, list[str]] | None:
    if path is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InventoryError(f"Local worktree inspection failed: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise InventoryError("Local worktree inspection failed")
    result: dict[str, list[str]] = {}
    current_path = ""
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            ref = line[len("branch refs/heads/"):]
            result.setdefault(ref, []).append(current_path)
        elif not line:
            current_path = ""
    return result


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


def inventory(repo: str, *, local_repo: Path | None = None) -> dict[str, Any]:
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo)
        or any(segment in {".", ".."} for segment in repo.split("/"))
    ):
        raise InventoryError("Repository must be an owner/name slug")
    metadata = _github(f"repos/{repo}")
    if not isinstance(metadata, dict):
        raise InventoryError("Repository metadata is malformed")
    default = metadata.get("default_branch")
    if not isinstance(default, str) or not default:
        raise InventoryError("Repository default branch is unknown")
    branch_data = _github(f"repos/{repo}/branches?per_page=100", paginate=True)
    pull_data = _github(f"repos/{repo}/pulls?state=all&per_page=100", paginate=True)
    if not branch_data or not all(isinstance(b, dict) for b in branch_data):
        raise InventoryError("Branch inventory is missing or malformed")
    if not all(isinstance(p, dict) for p in pull_data):
        raise InventoryError("Pull request inventory is malformed")
    branches: dict[str, dict[str, Any]] = {}
    for row in branch_data:
        name = row.get("name")
        commit = row.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(name, str) or not name or not _sha(sha) or name in branches:
            raise InventoryError("Branch identity is missing, invalid, or duplicated")
        branches[name] = row
    if default not in branches:
        raise InventoryError("Default branch is missing from the complete inventory")
    base_sha = branches[default]["commit"]["sha"]
    worktrees = _worktrees(local_repo)
    result: list[dict[str, Any]] = []
    for name, row in sorted(branches.items()):
        sha = row["commit"]["sha"]
        related = []
        for pr in pull_data:
            head = pr.get("head")
            if not isinstance(head, dict) or head.get("ref") != name:
                continue
            origin = head.get("repo")
            if not isinstance(origin, dict) or str(origin.get("full_name") or "").casefold() != repo.casefold():
                continue
            related.append(pr)
        open_prs = sorted(p["number"] for p in related if p.get("state") == "open" and type(p.get("number")) is int)
        merged = sorted(
            (p for p in related if p.get("merged_at") and p.get("state") == "closed"),
            key=lambda p: (str(p.get("merged_at")), int(p.get("number") or 0)),
            reverse=True,
        )
        matching = next(
            (p for p in merged if isinstance(p.get("head"), dict) and p["head"].get("sha") == sha),
            None,
        )
        reasons = []
        if name in {default, "main", "operator-control"}:
            reasons.append("reserved_branch")
        if row.get("protected") is not False:
            reasons.append("protected_or_unknown")
        if open_prs:
            reasons.append("open_pull_request")
        local_paths = None if worktrees is None else worktrees.get(name, [])
        if worktrees is None:
            reasons.append("local_worktrees_unverified")
        elif local_paths:
            reasons.append("local_worktree")
        if not merged:
            reasons.append("no_merged_pull_request")
        elif matching is None:
            reasons.append("branch_head_differs_from_merged_pull_request")
        comparison = None
        if name != default:
            try:
                value = _github(f"repos/{repo}/compare/{base_sha}...{sha}")
                if (
                    not isinstance(value, dict)
                    or type(value.get("ahead_by")) is not int
                    or type(value.get("behind_by")) is not int
                    or not isinstance(value.get("status"), str)
                ):
                    raise InventoryError("Comparison response is malformed")
                comparison = {
                    "ahead": value["ahead_by"],
                    "behind": value["behind_by"],
                    "status": value["status"],
                }
            except InventoryError:
                reasons.append("comparison_unavailable")
        else:
            comparison = {"ahead": 0, "behind": 0, "status": "identical"}
        candidate = not reasons
        result.append({
            "branch": name,
            "sha": sha,
            "protected": row.get("protected"),
            "comparison_to_default": comparison,
            "open_pull_requests": open_prs,
            "merged_pull_requests": [
                {
                    "number": p.get("number"),
                    "head_sha": (p.get("head") or {}).get("sha"),
                    "merge_sha": p.get("merge_commit_sha"),
                    "merged_at": p.get("merged_at"),
                }
                for p in merged
            ],
            "matching_merged_pull_request": matching.get("number") if matching else None,
            "local_worktrees": local_paths,
            "candidate_for_operator_review": candidate,
            "eligible_for_deletion": False,
            "exclusions": reasons + (["agent_ownership_and_lease_unverified"] if candidate else []),
        })
    return {
        "repository": repo,
        "default_branch": default,
        "remote_branch_count": len(result),
        "local_worktrees_checked": worktrees is not None,
        "review_candidate_count": sum(item["candidate_for_operator_review"] for item in result),
        "deletion_enabled": False,
        "branches": result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only inventory of remote GitHub branches")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--local-repo", type=Path)
    args = parser.parse_args(argv)
    try:
        result = inventory(args.repo, local_repo=args.local_repo)
    except InventoryError as exc:
        parser.exit(2, f"branch inventory unavailable: {exc}\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
