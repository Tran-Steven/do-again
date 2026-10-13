"""Explicit root-only quarantine for the single known failed V1A planning probe.

This is NOT an execution receipt, terminal success, or permission to replay the
original request. The immutable capability intent and original fingerprint are
retained. Requires a separate operator transition to maintenance before use.
No automatic invocation by installation, controller startup or recovery.
"""
from __future__ import annotations
import hashlib
import fcntl
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from contextlib import ExitStack, contextmanager
from pathlib import Path

OLD_SOURCE = "7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191"
JOBPIPE_HEAD = "71fcf63791385799972638aa10b66cdea049134e"
REQUEST = "offline-5d95b82d0952982adda5-1-probe"
FINGERPRINT = "ec7355ba8ca0c9270c00a8386c3c9c3eb376fc289ec6ee416fc8bcd84f63d747"
ROOT = Path("/Library/Application Support/DoAgainSupervisor")
DATABASE = ROOT / "state/supervisor.sqlite"
EXPECTED_EPOCH = 15


def refused(message):
    raise RuntimeError("RECONCILIATION REFUSED: " + message)


def validate(config, status, observed, *, no_workers, worktree_head, state, planning_files):
    if config.get("source_sha") != OLD_SOURCE or config.get("production_ready") is not False:
        refused("current sealed source or production state differs")
    project = next((p for p in config.get("projects", []) if p.get("account") == "_doagain_jp"), None)
    if not project or project.get("source_sha") != JOBPIPE_HEAD:
        refused("protected jobpipe snapshot differs")
    if status.get("intent") != "maintenance" or status.get("epoch") != EXPECTED_EPOCH:
        refused("operator must separately enter maintenance epoch 15")
    if not no_workers:
        refused("dedicated project identity still owns an execution process")
    if worktree_head != JOBPIPE_HEAD:
        refused("protected source commit changed")
    if (state.get("state") != "blocked" or state.get("requests") != []
            or state.get("outstanding") is not None or state.get("acknowledged") != []):
        refused("controller made progress or does not match blocked zero-request probe")
    if planning_files:
        refused("planning or model-call journal exists; cannot classify zero-call failure")
    if (observed.get("state") != "started" or observed.get("result") is not None
            or observed.get("fingerprint") != FINGERPRINT
            or observed.get("intent") != {
                "operation":"execute","source_sha":OLD_SOURCE,
                "request_fingerprint":FINGERPRINT
            }):
        refused("original started execution or intent differs")
    return project


def classify(db, project_key, expected, *, dry_run=True):
    """Atomic append-only audit + quarantine, never a terminal execution result."""
    db.execute("BEGIN IMMEDIATE")
    try:
        observed = db.execute(
            "SELECT e.state,e.fingerprint,e.result,i.payload FROM execution e "
            "LEFT JOIN capability_intent i ON e.project=i.project AND e.request=i.request "
            "WHERE e.project=? AND e.request=?", (project_key,REQUEST)).fetchone()
        if observed is None:
            refused("original ledger row missing")
        state,fp,result,intent = observed
        if state != "started" or fp != FINGERPRINT or result is not None:
            refused("original execution was already altered")
        if json.loads(intent or "null") != expected:
            refused("original intent differs")
        prior = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v1a_abandoned_probe_audit'"
        ).fetchone()
        if prior:
            refused("abandonment journal already exists; no repeat effect")
        if dry_run:
            db.rollback()
            return {"operation":"would_quarantine", "request_id":REQUEST,
                    "old_state":"started","new_state":"abandoned","original_result":None}
        # Ledger state 'abandoned' is never terminal; observe_request remains
        # post_dispatch_uncertain and lookup still refuses replay.
        db.execute("CREATE TABLE v1a_abandoned_probe_audit ("
                   "project TEXT NOT NULL, request TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                   "source_sha TEXT NOT NULL, reason TEXT NOT NULL, original_intent_sha256 TEXT NOT NULL, "
                   "prior_state TEXT NOT NULL, disposition TEXT NOT NULL, created_at TEXT NOT NULL, "
                   "PRIMARY KEY(project,request))")
        db.execute("INSERT INTO v1a_abandoned_probe_audit VALUES(?,?,?,?,?,?,?,?,?)",
            (project_key,REQUEST,FINGERPRINT,OLD_SOURCE,
             "sealed Git manifest subprocess error; no terminal result; no model call",
             hashlib.sha256(intent.encode()).hexdigest(),
             "started","quarantined_unresolved_not_replayable",
             datetime.now(timezone.utc).isoformat()))
        changed = db.execute("UPDATE execution SET state='abandoned' "
            "WHERE project=? AND request=? AND fingerprint=? "
            "AND state='started' AND result IS NULL",
            (project_key,REQUEST,FINGERPRINT)).rowcount
        if changed != 1:
            refused("lost compare-and-set ledger lock")
        db.commit()
        return {"operation":"quarantined", "request_id":REQUEST,
                "prior_state":"started", "state":"abandoned",
                "terminal_success":False,"replay_permitted":False}

    except BaseException:
        db.rollback()
        raise

def retain_blocked_controller(controller_path):
    """Durably archive the consumed old-scope controller, without resetting it.

    Runs only after the original execution is atomically quarantined and audited.
    An interruption between quarantine and archive is recoverable by explicitly
    inspecting the DB and requesting a separate archive action; no auto-retry.
    """
    archived = controller_path.with_name(
        "controller-blocked-" + OLD_SOURCE[:12] + "-retained.json")
    if archived.exists() or archived.is_symlink():
        refused("original controller journal was already retained")
    if not controller_path.is_file() or controller_path.is_symlink():
        refused("original controller journal missing after quarantine")
    current = json.loads(controller_path.read_text())
    if (current.get("state") != "blocked" or current.get("requests") != []
            or current.get("acknowledged") != []
            or current.get("outstanding") is not None):
        refused("controller progress changed after quarantine")
    os.rename(controller_path, archived)
    fd = os.open(archived.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return archived


@contextmanager
def installation_fences(config):
    """Serialize classification against installer cutover/admission fences."""
    with ExitStack() as stack:
        for project in sorted(config["projects"], key=lambda p:p["key"]):
            lock = ROOT / "state" / project["key"] / "admission.lock"
            if lock.is_symlink() or not lock.is_file() or lock.stat().st_uid != 0:
                refused("installed admission fence missing or aliased")
            stream = stack.enter_context(lock.open("r"))
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                refused("existing root installation or admission owns an execution fence")
        yield


def run(*, dry_run=True):
    if sys.platform != "darwin" or os.geteuid() != 0:
        refused("only approved macOS administrator can inspect or quarantine root ledger")
    # Verify native identities with the currently installed immutable root code,
    # never import a developer checkout as root for authority decisions.
    sys.path.insert(0, str(ROOT / "current/package"))
    from do_again.supervisor.macos_execution import (
        MacOSProcesses, load_configuration, project_from_dict)
    from do_again.supervisor.macos_server import verify_installation,worktree_authority
    from do_again.supervisor.authority import AuthorityRegistry
    config = load_configuration(ROOT / "current/config.json")
    verify_installation(config)
    if DATABASE != Path(config["authority_path"]):
        refused("installed root authority database path changed")
    with installation_fences(config):
        project = next(p for p in config["projects"] if p["account"] == "_doagain_jp")
        rootstate = ROOT / "state" / project["key"]
        status = AuthorityRegistry(DATABASE,owner_uid=0).status(Path(project["repo"]))
        controller_path = rootstate / "controller.json"
        if not controller_path.is_file() or controller_path.is_symlink():
            refused("original blocked controller journal missing")
        if controller_path.stat().st_uid != 0 or controller_path.stat().st_mode & 0o022:
            refused("controller journal ownership differs")
        state = json.loads(controller_path.read_text())
        planning_files = any((rootstate / n).exists() for n in (
            "browser-planning-calls.json", "planning-calls.json"))
        own = MacOSProcesses()
        no_workers = not any(own.owned(p["uid"]) for p in config["projects"])
        original_head = worktree_authority(Path(project["worktree"]))["repo_head"]
        with sqlite3.connect(DATABASE,timeout=15,isolation_level=None) as db:
            record = db.execute(
                "SELECT e.state,e.fingerprint,e.result,i.payload FROM execution e "
                "LEFT JOIN capability_intent i ON e.project=i.project AND e.request=i.request "
                "WHERE e.project=? AND e.request=?", (project["key"],REQUEST)).fetchone()
            if record is None:
                refused("original request missing")
            observed = {"state":record[0], "fingerprint":record[1],
                        "result":record[2], "intent":json.loads(record[3]) if record[3] else None}
            validate(config,status,observed,no_workers=no_workers,
                     worktree_head=original_head,state=state,planning_files=planning_files)
            for sibling in config["projects"]:
                if sibling["account"] != "_doagain_jp":
                    sibling_status=AuthorityRegistry(DATABASE,owner_uid=0).status(Path(sibling["repo"]))
                    if sibling_status["intent"] != "maintenance":
                        refused("sibling project not in maintenance")
            result=classify(db,project["key"],observed["intent"],dry_run=dry_run)
        if not dry_run:
            archive = retain_blocked_controller(controller_path)
            result["controller_journal_retained"] = archive.name
        return result


def main():
    args=sys.argv[1:]
    if args not in (["--inspect"],["--apply","--acknowledge-irreversible-abandonment"]):
        raise SystemExit("Usage: ... --inspect OR --apply --acknowledge-irreversible-abandonment")
    print(json.dumps(run(dry_run=args==["--inspect"]),sort_keys=True))


if __name__ == "__main__":
    main()
