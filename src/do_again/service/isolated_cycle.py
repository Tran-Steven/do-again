"""Explicit, isolated coding-cycle primitive; not an unattended agent.

Only a pinned source snapshot is modified. Model-supplied code is executed in a
network-disabled, unprivileged container with no host home or Docker socket.
Fail closed when the required container runtime/image is unavailable.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

from ..core.schema import OperatorError, atomic_json

IMAGE_RE = re.compile(r"[a-z0-9._/-]+@sha256:[a-f0-9]{64}\Z")
HEAD_RE = re.compile(r"[a-f0-9]{40,64}\Z")
SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".css", ".html"}
TEST_COMMANDS = {
    ("python", "-m", "unittest", "discover"),
    ("python3", "-m", "unittest", "discover"),
    ("python", "-m", "unittest", "discover", "-s", "tests"),
    ("python3", "-m", "unittest", "discover", "-s", "tests"),
    ("npm", "test", "--", "--run"),
}


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null",
                             "-c", "commit.gpgsign=false", "-C", str(cwd), *args],
                            input=stdin, capture_output=True, timeout=90, env=env)
    if result.returncode:
        raise OperatorError("isolated Git operation failed: " + args[0])
    return result


def _path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise OperatorError("invalid relative coding path")
    item = PurePosixPath(value)
    if (item.is_absolute() or not item.parts or
            any(part in {".", "..", ".git"} for part in item.parts)
            or item.suffix not in SUFFIXES):
        raise OperatorError("coding path escapes allowed source files")
    return item


def _export(repo: Path, workspace: Path, sha: str) -> set[str]:
    archive = _git(repo, "archive", "--format=tar", sha).stdout
    if len(archive) > 100_000_000:
        raise OperatorError("source snapshot exceeds 100 MB")
    tracked = set()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            name = member.name.rstrip("/")
            if not name:
                continue
            item = PurePosixPath(name)
            if (item.is_absolute() or any(p in {".", "..", ".git"} for p in item.parts)
                    or "\\" in name):
                raise OperatorError("unsafe source archive path")
            target = workspace.joinpath(*item.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                if member.size > 10_000_000:
                    raise OperatorError("oversized source archive member")
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                if stream is None:
                    raise OperatorError("unreadable source archive member")
                target.write_bytes(stream.read())
                tracked.add(name)
            else:
                # No symlinks, hardlinks, device files, or fifos in the
                # separately executable snapshot.
                raise OperatorError("source snapshot contains unsupported special files")
    return tracked


def _docker_test(workspace: Path, image: str, argv: list[str]) -> bool:
    if not IMAGE_RE.fullmatch(image):
        raise OperatorError("test image must be pinned by immutable SHA-256 digest")
    if not isinstance(argv, list) or tuple(argv) not in TEST_COMMANDS:
        raise OperatorError("test command is not an admitted fixed test runner")
    # Never pull automatically: image preparation is an explicit user action.
    inspect = subprocess.run(["docker", "image", "inspect", image],
                             capture_output=True, timeout=15)
    if inspect.returncode:
        raise OperatorError("pinned offline test image is not installed")
    cmd = [
        "docker", "run", "--rm", "--pull=never", "--network=none",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=128", "--memory=2g", "--cpus=2",
        "--user=65534:65534",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--mount=type=bind,source=" + str(workspace) + ",target=/workspace,readonly",
        "--workdir=/workspace", image, *argv,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OperatorError("bounded offline container test unavailable") from exc
    if result.returncode != 0:
        return False
    # unittest exits zero even when it discovers zero tests. Such a run must
    # never be counted as a successful verified coding cycle.
    if argv[0] in {"python", "python3"}:
        report = (result.stdout + result.stderr).decode("utf-8", "replace")
        return bool(re.search(r"Ran [1-9][0-9]* tests?\b", report))
    return True


def run_isolated_cycle(repo: Path, manifest: dict, *, image: str, output_dir: Path) -> dict:
    """Apply an exact-head patch, test offline, commit in temp snapshot, save receipt.

    Never edits the input repository, publishes Git refs, or submits messages.
    A successful receipt means *only* the submitted patch passed the specified
    isolated test command; it is not an autonomous ChatGPT planning cycle.
    """
    repo = Path(repo).resolve()
    output_dir = Path(output_dir).resolve()
    if output_dir == repo or repo in output_dir.parents:
        raise OperatorError("receipt output must be outside source repository")
    if (not isinstance(manifest, dict) or set(manifest) !=
            {"expected_head", "changes", "test_command"}
            or not isinstance(manifest["expected_head"], str)
            or not HEAD_RE.fullmatch(manifest["expected_head"])):
        raise OperatorError("isolated coding manifest identity invalid")
    changes = manifest["changes"]
    if not isinstance(changes, list) or not 1 <= len(changes) <= 10:
        raise OperatorError("isolated cycle requires 1 to 10 exact file changes")
    if _git(repo, "rev-parse", "HEAD").stdout.decode().strip() != manifest["expected_head"]:
        raise OperatorError("source HEAD changed since authorization")
    if _git(repo, "status", "--porcelain", "--untracked-files=all").stdout.strip():
        raise OperatorError("source workspace is dirty; isolate before editing")
    with tempfile.TemporaryDirectory(prefix="do-again-isolated-") as temp:
        snapshot = Path(temp) / "snapshot"
        snapshot.mkdir(mode=0o777)
        tracked = _export(repo, snapshot, manifest["expected_head"])
        planned: dict[str, bytes] = {}
        for item in changes:
            if not isinstance(item, dict) or set(item) != {"path", "before_sha256", "content"}:
                raise OperatorError("coding change fields differ")
            path = _path(item["path"])
            if (str(path) not in tracked or str(path) in planned
                    or not isinstance(item["before_sha256"], str)
                    or not re.fullmatch("[a-f0-9]{64}", item["before_sha256"])
                    or not isinstance(item["content"], str)):
                raise OperatorError("coding change identity invalid or path not tracked")
            before = snapshot.joinpath(*path.parts).read_bytes()
            if hashlib.sha256(before).hexdigest() != item["before_sha256"]:
                raise OperatorError("file content does not match authorized base")
            content = item["content"].encode("utf-8")
            if len(content) > 128_000:
                raise OperatorError("coding change exceeds size bound")
            planned[str(path)] = content
        _git(snapshot, "init", "-q")
        _git(snapshot, "add", "-f", "--all")
        _git(snapshot, "-c", "user.name=Do Again Fixture", "-c",
             "user.email=do-again@invalid.example", "commit", "-qm", "isolated baseline")
        for name, content in planned.items():
            (snapshot / name).write_bytes(content)
        passed = _docker_test(snapshot, image, manifest["test_command"])
        if not passed:
            raise OperatorError("isolated coding test failed; no patch commit or receipt")
        for name, content in planned.items():
            if (snapshot / name).read_bytes() != content:
                raise OperatorError("test unexpectedly modified candidate source")
        changed = set(_git(snapshot, "diff", "--name-only").stdout.decode().splitlines())
        if changed != set(planned):
            raise OperatorError("candidate contains unintended changes")
        _git(snapshot, "add", "--", *sorted(planned))
        _git(snapshot, "-c", "user.name=Do Again Fixture", "-c",
             "user.email=do-again@invalid.example", "commit", "-qm", "validated isolated change")
        commit = _git(snapshot, "rev-parse", "HEAD").stdout.decode().strip()
        patch = _git(snapshot, "format-patch", "-1", "--stdout").stdout
        if len(patch) > 800_000:
            raise OperatorError("candidate patch exceeds output bound")
        output_dir.mkdir(parents=True, exist_ok=True)
        identifier = hashlib.sha256(patch).hexdigest()[:24]
        patch_path = output_dir / (identifier + ".patch")
        if patch_path.exists():
            raise OperatorError("patch output already exists; do not overwrite")
        with patch_path.open("xb") as stream:
            stream.write(patch)
        receipt = {
            "state": "verified_isolated_patch", "source_head": manifest["expected_head"],
            "candidate_commit": commit, "patch_sha256": hashlib.sha256(patch).hexdigest(),
            "patch_file": patch_path.name, "tested_offline": True,
            "files": sorted(planned), "production_deployed": False,
            "automatic_merge": False, "chatgpt_planning_verified": False,
        }
        atomic_json(output_dir / (identifier + ".receipt.json"), receipt)
        return receipt
