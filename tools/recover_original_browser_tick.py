"""One-time supervised recovery of the original, verifiably pre-Send browser tick.

This tool intentionally does NOT change installed supervisor code, startup
authority, or projects. It is a narrowly pinned, offline maintenance recovery
invoked only by a human administrator. No database SQL updates or browser
calls are made here; the recovery helper records a root audit before using
the existing installed execution ledger's terminal transition.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ORIGINAL_ID = "browser-48b9fd1c09734faf98f77944a64581f2"
ORIGINAL_SOURCE = "279350b2a3a280904b72068f96785343945f97b9"
INSTALLED = Path("/Library/Application Support/DoAgainSupervisor/current")
REPO = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-c", "core.fsmonitor=false",
         "-c", "core.hooksPath=/dev/null", "-C", str(REPO), *args],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, check=True, timeout=20)
    return result.stdout.strip()


def _verified_candidate(commit: str) -> Path:
    """Pin this tool and the helper to exact tracked Git objects before root use."""
    if not isinstance(commit, str) or len(commit) != 40 or any(
            char not in "0123456789abcdef" for char in commit):
        raise RuntimeError("explicit exact source commit is required")
    if _git("rev-parse", "HEAD") != commit:
        raise RuntimeError("candidate checkout must be at the approved exact SHA")
    for relative in ("tools/recover_original_browser_tick.py",
                     "src/do_again/supervisor/pre_dispatch_recovery.py"):
        path = REPO / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("recovery source path is missing or aliased")
        expected = _git("rev-parse", "HEAD:" + relative)
        data = path.read_bytes()
        actual = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if actual != expected:
            raise RuntimeError("recovery source does not match exact committed tree")
    return REPO / "src/do_again/supervisor/pre_dispatch_recovery.py"


def _root_config() -> dict:
    from do_again.supervisor.macos_execution import load_configuration
    from do_again.supervisor.macos_server import verify_installation
    config = load_configuration(INSTALLED / "config.json")
    verify_installation(config)
    if (config.get("source_sha") != ORIGINAL_SOURCE
            or config.get("production_ready") is not False
            or not isinstance(config.get("development_scope"), dict)
            or config["development_scope"].get("kind") != "jobpipe-offline-v1"):
        raise RuntimeError("original protected supervisor or offline authority changed")
    projects = config["projects"]
    if (len(projects) != 3 or {p["account"] for p in projects}
            != {"_doagain_da", "_doagain_jp", "_doagain_so"}):
        raise RuntimeError("unexpected protected project roster")
    home = Path(config["operator_home"])
    expected = {"_doagain_da": home / "do-again",
                "_doagain_jp": home / "jobpipe",
                "_doagain_so": home / "Sonary"}
    if any(Path(p["repo"]) != expected[p["account"]] for p in projects):
        raise RuntimeError("protected repository paths changed")
    return config


def _import_helper(path: Path):
    # Prevent a user-writable checkout from changing between source validation
    # and root import: copy the exact Git-tracked object to a root-private
    # temporary file, then import only that immutable-by-operator snapshot.
    relative = "src/do_again/supervisor/pre_dispatch_recovery.py"
    source = path.read_bytes()
    expected = _git("rev-parse", "HEAD:" + relative)
    actual = hashlib.sha1(
        b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest()
    if actual != expected:
        raise RuntimeError("recovery helper changed since committed source validation")
    state_root = INSTALLED.parent / "state"
    if state_root.is_symlink() or not state_root.is_dir() or state_root.stat().st_uid != 0:
        raise RuntimeError("private root recovery staging area unavailable")
    with tempfile.TemporaryDirectory(prefix="original-browser-recovery-", dir=state_root) as temp:
        staged = Path(temp) / "pre_dispatch_recovery.py"
        descriptor = os.open(
            staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        name = "do_again.supervisor.pre_dispatch_recovery"
        spec = importlib.util.spec_from_file_location(name, staged)
        if spec is None or spec.loader is None:
            raise RuntimeError("sealed recovery helper could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    if module.ORIGINAL_REQUEST != ORIGINAL_ID or module.ORIGINAL_SOURCE != ORIGINAL_SOURCE:
        raise RuntimeError("recovery helper identity changed")
    return module


def _read_original(config: dict, module):
    """Validate original evidence without creating a ledger or touching Chrome."""
    import sqlite3
    from do_again.supervisor.authority import AuthorityRegistry
    projects = config["projects"]
    project = next(p for p in projects if p["account"] == "_doagain_jp")
    registry = AuthorityRegistry(Path(config["authority_path"]), owner_uid=0)
    status = registry.status(Path(project["repo"]))
    database = Path(config["authority_path"])
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT project, request, state FROM execution WHERE state='started'"
        ).fetchall()
        if rows != [(project["key"], ORIGINAL_ID, "started")]:
            raise RuntimeError("original request is not the sole pending protected effect")
        original = conn.execute(
            "SELECT payload FROM capability_intent WHERE project=? AND request=?",
            (project["key"], ORIGINAL_ID)).fetchone()
    if original is None:
        raise RuntimeError("original effect intent is absent")
    intent = json.loads(original[0])
    proof = module.verify_pre_dispatch(config, project, ORIGINAL_ID, intent, status)
    return project, status, proof


def run(argv=None):
    parser = argparse.ArgumentParser(
        description="Root-authorized one-time original browser pre-Send recovery")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--request-id", required=True)
    decision = parser.add_mutually_exclusive_group(required=True)
    decision.add_argument("--preview", action="store_true")
    decision.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if (sys.platform != "darwin" or os.geteuid() != 0
            or os.getuid() != 0 or args.request_id != ORIGINAL_ID):
        raise SystemExit("administrator and exact original browser request required")
    if INSTALLED.is_symlink() or not INSTALLED.is_dir():
        raise SystemExit("protected installed runtime unavailable")

    path = _verified_candidate(args.candidate_sha)
    # Do not import the candidate as the supervisor: load only the independently
    # verified one-time recovery helper. Everything else is from the root-sealed
    # installed package, verified against its manifest.
    sys.path.insert(0, str(INSTALLED / "package"))
    config = _root_config()
    helper = _import_helper(path)

    # Root's own admission fence excludes all new project effects and operator
    # intent transitions while evidence is verified and optionally reconciled.
    project = next(p for p in config["projects"] if p["account"] == "_doagain_jp")
    fence = INSTALLED.parent / "state" / project["key"] / "admission.lock"
    if fence.is_symlink() or not fence.is_file() or fence.stat().st_uid != 0:
        raise SystemExit("protected admission fence is unavailable")
    import fcntl
    with fence.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity, status, proof = _read_original(config, helper)
        from do_again.supervisor.macos_execution import MacOSProcesses
        from do_again.supervisor.worker_service import service_present, service_spec
        from do_again.supervisor.macos_server import ProjectBroker
        broker = ProjectBroker(config, identity)
        if MacOSProcesses().owned(identity["uid"]):
            raise RuntimeError("original dedicated process is not stopped")
        label, _ = service_spec(broker, status["epoch"])
        if service_present(broker, label):
            raise RuntimeError("original worker service remains loaded")
        if any(p["account"] != "_doagain_jp" and (
                broker.registry.status(Path(p["repo"]))["intent"] != "maintenance"
                or broker.ledger.pending(p["key"]))
                for p in config["projects"]):
            raise RuntimeError("other projects are not in drained maintenance")
        result = {
            "request_id": ORIGINAL_ID,
            "installed_source": ORIGINAL_SOURCE,
            "operator_intent": status["intent"],
            "epoch": status["epoch"],
            "preparing_sha256": proof["sha256"],
            "outbox_entries_preserved": proof["count"],
            "worker_running": False,
            "replayed": False,
        }
        if args.preview:
            print("ORIGINAL_BROWSER_RECOVERY_PREVIEW " + json.dumps(result, sort_keys=True))
            return 0
        recovered = helper.reconcile_verified_pre_dispatch(broker, ORIGINAL_ID)
        if (recovered.get("liveness") != "reconciled_before_send"
                or recovered.get("delivered") != 0
                or recovered.get("outbox_preserved") is not True):
            raise RuntimeError("original browser recovery result differs")
        pending = broker.ledger.pending(project["key"])
        if pending:
            raise RuntimeError("original browser ledger is still unresolved; stop and review")
        print("ORIGINAL_BROWSER_RECOVERED " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
