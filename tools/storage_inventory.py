from __future__ import annotations

import argparse
import heapq
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CACHE_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
REBUILDABLE_NAMES = frozenset({"node_modules", ".venv", ".tox", "dist", "build"})
PROTECTED_NAMES = frozenset({
    ".git", "state", "receipts", "outbox", "journal", "journals", "snapshots",
    "control", "runtime", "execution", "credentials", "secrets", "current",
})


class InventoryError(Exception):
    pass


def _human_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError


def _classify(path: Path, root: Path) -> dict[str, Any] | None:
    name = path.name
    if name in CACHE_NAMES:
        kind = "cache"
    elif name in REBUILDABLE_NAMES or name.endswith(".egg-info"):
        kind = "rebuildable_review"
    else:
        return None
    ancestry = path.relative_to(root).parts[:-1]
    protected = sorted({part for part in ancestry if part in PROTECTED_NAMES})
    return {"kind": kind, "protected_ancestors": protected, "eligible_for_deletion": False}


def _scan_root(root: Path, *, cutoff: float, limit: int) -> dict[str, Any]:
    try:
        original_stat = root.lstat()
    except OSError as exc:
        raise InventoryError(f"cannot access root {root}: {type(exc).__name__}") from exc
    if not stat.S_ISDIR(original_stat.st_mode) or stat.S_ISLNK(original_stat.st_mode):
        raise InventoryError(f"root is not a regular directory: {root}")
    root = root.resolve(strict=True)
    totals: dict[Path, int] = {}
    newest: dict[Path, float] = {}
    missing: set[Path] = set()
    errors: list[dict[str, str]] = []
    file_count = 0
    symlink_count = 0
    biggest_files: list[tuple[int, str]] = []

    def failed(error: OSError) -> None:
        path = getattr(error, "filename", None)
        errors.append({"path": str(path or root), "error": type(error).__name__})
        affected = Path(path) if path else root
        missing.add(affected)
        missing.add(affected.parent)

    for base, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=failed):
        directory = Path(base)
        try:
            newest[directory] = directory.lstat().st_mtime
        except OSError as exc:
            failed(exc)
            missing.add(directory)
            newest[directory] = 0
        totals[directory] = 0
        retained = []
        for name in directories:
            child = directory / name
            try:
                mode = child.lstat().st_mode
            except OSError as exc:
                failed(exc)
                missing.add(directory)
                continue
            if stat.S_ISLNK(mode):
                symlink_count += 1
            elif stat.S_ISDIR(mode):
                retained.append(name)
            else:
                missing.add(directory)
        directories[:] = retained
        for name in files:
            child = directory / name
            try:
                info = child.lstat()
            except OSError as exc:
                failed(exc)
                missing.add(directory)
                continue
            if stat.S_ISLNK(info.st_mode):
                symlink_count += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            totals[directory] += info.st_size
            newest[directory] = max(newest[directory], info.st_mtime)
            file_count += 1
            item = (info.st_size, str(child))
            if len(biggest_files) < limit:
                heapq.heappush(biggest_files, item)
            elif item > biggest_files[0]:
                heapq.heapreplace(biggest_files, item)
    for directory in sorted(totals, key=lambda item: len(item.parts), reverse=True):
        if directory == root:
            continue
        parent = directory.parent
        if parent in totals:
            totals[parent] += totals[directory]
            newest[parent] = max(newest[parent], newest[directory])
            if directory in missing:
                missing.add(parent)
    candidates = []
    for directory, size in totals.items():
        classified = _classify(directory, root)
        if classified is None:
            continue
        last_modified = newest[directory]
        candidates.append({
            "path": str(directory),
            "bytes": size,
            "last_modified_utc": datetime.fromtimestamp(last_modified, timezone.utc).isoformat(),
            "older_than_min_age": last_modified <= cutoff,
            "complete": directory not in missing,
            **classified,
        })
    candidates.sort(key=lambda item: item["bytes"], reverse=True)
    immediate = [
        {"path": str(path), "bytes": size}
        for path, size in totals.items()
        if path.parent == root
    ]
    immediate.sort(key=lambda item: item["bytes"], reverse=True)
    biggest_files.sort(reverse=True)
    return {
        "root": str(root),
        "bytes": totals.get(root, 0),
        "files": file_count,
        "directories": len(totals),
        "skipped_symlinks": symlink_count,
        "complete": root not in missing and not errors,
        "errors": errors[:limit],
        "error_count": len(errors),
        "largest_directories": immediate[:limit],
        "largest_files": [{"path": path, "bytes": size} for size, path in biggest_files[:limit]],
        "review_candidates": candidates[:limit],
        "total_review_candidates": len(candidates),
    }


def inventory(roots: list[Path], *, min_age_days: int = 14, limit: int = 20, now: float | None = None) -> dict[str, Any]:
    if not roots or limit < 1 or min_age_days < 0:
        raise InventoryError("provide roots, nonnegative age, and positive result limit")
    roots_normalized = []
    for root in roots:
        expanded = root.expanduser().absolute()
        if expanded == Path(expanded.anchor) or expanded == Path.home().resolve():
            raise InventoryError(f"refusing broad inventory root: {expanded}")
        if expanded.is_symlink():
            raise InventoryError(f"refusing symbolic-link root: {expanded}")
        try:
            resolved = expanded.resolve(strict=True)
        except OSError as exc:
            raise InventoryError(f"cannot resolve root: {expanded}") from exc
        if resolved in roots_normalized:
            continue
        roots_normalized.append(resolved)
    for i, left in enumerate(roots_normalized):
        for right in roots_normalized[i + 1:]:
            if left in right.parents or right in left.parents:
                raise InventoryError("overlapping roots would double-count storage")
    instant = time.time() if now is None else now
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.fromtimestamp(instant, timezone.utc).isoformat(),
        "minimum_age_days": min_age_days,
        "deletion_enabled": False,
        "roots": [_scan_root(root, cutoff=instant - min_age_days * 86400, limit=limit) for root in roots_normalized],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only inventory of Do Again disk usage and review candidates")
    parser.add_argument("--root", action="append", type=Path, default=[])
    parser.add_argument("--min-age-days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    roots = args.root or [Path.home() / ".do_again"]
    try:
        result = inventory(roots, min_age_days=args.min_age_days, limit=args.limit)
    except InventoryError as exc:
        parser.exit(2, f"storage inventory unavailable: {exc}\n")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Do Again storage inventory — READ ONLY; deletion disabled")
        for root in result["roots"]:
            print(f"\n{root['root']}: {_human_size(root['bytes'])}, {root['files']} files, {root['directories']} directories")
            if not root["complete"]:
                print(f"  Incomplete inspection: {root['error_count']} errors")
            print("  Largest direct subdirectories:")
            for item in root["largest_directories"]:
                print(f"    {_human_size(item['bytes']):>10}  {item['path']}")
            print("  Review candidates (never automatically deleted):")
            for item in root["review_candidates"]:
                age = "old" if item["older_than_min_age"] else "recent"
                protection = " protected" if item["protected_ancestors"] else ""
                completeness = " incomplete" if not item["complete"] else ""
                print(f"    {_human_size(item['bytes']):>10}  [{item['kind']}; {age}{protection}{completeness}] {item['path']}")
            print("  Largest individual files:")
            for item in root["largest_files"]:
                print(f"    {_human_size(item['bytes']):>10}  {item['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
