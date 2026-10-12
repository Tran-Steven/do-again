"""Audited, one-shot recovery for the first v1 background-mode browser refusal.

Administrator-only. Never contacts ChatGPT, launches workers, replays a request,
alters the outbox, or fabricates a browser receipt. Only an exact original
root-ledger effect is terminalized after immutable-source and temporal proof
that the installed helper rejected background Chrome before preparing Send.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys

ORIGINAL = "browser-9c19c15694354ba88afbcc2d842c55fb"
SOURCE = "9726d24943b9c8a301341364beb37f5b43d3fc7a"
ORIGIN_EPOCH = 6
MAINTENANCE_EPOCH = 7
ROOT = Path("/Library/Application Support/DoAgainSupervisor/current")
REPO = Path(__file__).resolve().parents[1]


def _git(*args):
    return subprocess.check_output(
        ["/usr/bin/git", "-c", "core.fsmonitor=false",
         "-c", "core.hooksPath=/dev/null", "-C", str(REPO), *args],
        text=True, stderr=subprocess.DEVNULL, timeout=20).strip()


def _candidate(sha):
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise RuntimeError("explicit candidate Git SHA is required")
    if _git("rev-parse", "HEAD") != sha:
        raise RuntimeError("candidate checkout is not the approved exact source")
    relative = "tools/recover_first_loop_browser_refusal.py"
    path = REPO / relative
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("recovery source is missing or aliased")
    content = path.read_bytes()
    digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    if digest != _git("rev-parse", "HEAD:" + relative):
        raise RuntimeError("recovery source differs from the pinned Git object")


def _file_bytes(path, uid, *, root_file=False, maximum=262144):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("protected recovery input is missing or aliased")
    item = path.stat()
    expected_uid = 0 if root_file else uid
    if (not stat.S_ISREG(item.st_mode) or item.st_uid != expected_uid
            or item.st_nlink != 1 or item.st_mode & 0o022 or item.st_size > maximum):
        raise RuntimeError("recovery input ownership or integrity differs")
    return path.read_bytes()


def _fingerprint_outbox(folder, uid):
    if not folder.is_dir() or folder.is_symlink():
        raise RuntimeError("original browser outbox is unavailable")
    entries = sorted(folder.glob("*.json"))
    if not 1 <= len(entries) <= 20:
        raise RuntimeError("original browser outbox count differs")
    digest = hashlib.sha256()
    for item in entries:
        content = _file_bytes(item, uid)
        digest.update(item.name.encode() + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return {"count": len(entries), "sha256": digest.hexdigest()}


def _proof(broker, config):
    """Read-only original incident proof under the installed root admission fence."""
    from do_again.supervisor.macos_execution import MacOSProcesses
    from do_again.supervisor.worker_service import service_present, service_spec

    project = next(p for p in config["projects"] if p["account"] == "_doagain_jp")
    if broker.project.key != project["key"]:
        raise RuntimeError("incident project differs from protected broker")
    status = broker.registry.status(Path(project["repo"]))
    if (status["intent"] != "maintenance" or status["epoch"] != MAINTENANCE_EPOCH
            or status["goal_revision"] != project["goal_revision"]):
        raise RuntimeError("original jobpipe incident is not in the expected maintenance epoch")
    for sibling in config["projects"]:
        if sibling["account"] == "_doagain_jp":
            continue
        st = broker.registry.status(Path(sibling["repo"]))
        if st["intent"] != "maintenance" or broker.ledger.pending(sibling["key"]):
            raise RuntimeError("sibling projects are not quiescent")
    pending = broker.ledger.pending(project["key"])
    if len(pending) != 1 or pending[0]["request_id"] != ORIGINAL:
        raise RuntimeError("original browser tick is not the sole pending effect")
    with sqlite3.connect(Path(config["authority_path"]).as_uri() + "?mode=ro", uri=True) as db:
        started = db.execute("SELECT project,request FROM execution WHERE state='started'").fetchall()
        if started != [(project["key"], ORIGINAL)]:
            raise RuntimeError("other protected executions remain unfinished")
    intent = broker.ledger.intent(project["key"], ORIGINAL)
    if (not isinstance(intent, dict) or intent.get("operation") != "browser_tick"
            or intent.get("source_sha") != SOURCE
            or intent.get("repo") != project["repo"]
            or intent.get("epoch") != ORIGIN_EPOCH):
        raise RuntimeError("original browser tick intent differs")
    if MacOSProcesses().owned(project["uid"]):
        raise RuntimeError("dedicated project execution is not quiescent")
    label, _ = service_spec(broker, MAINTENANCE_EPOCH)
    if service_present(broker, label):
        raise RuntimeError("failed worker service remains loaded")

    home = Path(config["operator_home"])
    bbase = home / ".do_again/browser"
    cfgpath, statepath = bbase / "config.json", bbase / "state.json"
    cfg_bytes = _file_bytes(cfgpath, config["operator_uid"])
    state_bytes = _file_bytes(statepath, config["operator_uid"])
    policy, browser = json.loads(cfg_bytes), json.loads(state_bytes)
    if (not isinstance(policy, dict) or not isinstance(browser, dict)
            or policy.get("preferred_mode") != "auto"
            or policy.get("allow_visible_fallback") is not False
            or browser.get("mode") != "background"
            or browser.get("profile_dir") != str(bbase / "profile")
            or type(browser.get("pid")) is not int or browser["pid"] <= 1):
        raise RuntimeError("deterministic pre-Send auto/background refusal proof differs")

    # This protected policy existed before the failed service was staged.
    # The immutable 9726d249 browser runtime rejects precisely this mode pair
    # before it creates a browser_submission_uncertain intent or clicks Send.
    process = MacOSProcesses().identity(browser["pid"])
    if not process or process[0] != config["operator_uid"] or process[3] == 5:
        raise RuntimeError("original background Chrome kernel identity differs")
    plist = Path("/Library/LaunchAgents") / (label + ".plist")
    _file_bytes(plist, 0, root_file=True)
    if not cfgpath.stat().st_mtime_ns < plist.stat().st_mtime_ns:
        raise RuntimeError("browser mode refusal does not predate the failed worker service")
    source = ROOT / "package/do_again/browser/runtime.py"
    runtime = _file_bytes(source, 0, root_file=True, maximum=500000)
    if b"unattended auto mode refuses an existing GUI browser; no browser submission attempted" not in runtime:
        raise RuntimeError("installed browser source does not prove pre-Send refusal")

    state = home / ".do_again/projects" / project["key"][:12] / "state"
    paths = [
        state / "browser_submission_uncertain.json",
        state / ("browser_submission_presend_" + ORIGINAL + ".json"),
        state / "browser_tick_receipts" / (ORIGINAL + ".json"),
    ]
    if any(p.exists() or p.is_symlink() for p in paths):
        raise RuntimeError("a Send journal or terminal browser receipt exists; manual review required")
    outbox = _fingerprint_outbox(state / "browser_outbox", config["operator_uid"])
    return {
        "schema_version": 1,
        "request_id": ORIGINAL,
        "source_sha": SOURCE,
        "origin_epoch": ORIGIN_EPOCH,
        "maintenance_epoch": MAINTENANCE_EPOCH,
        "browser_config_sha256": hashlib.sha256(cfg_bytes).hexdigest(),
        "browser_state_sha256": hashlib.sha256(state_bytes).hexdigest(),
        "runtime_sha256": hashlib.sha256(runtime).hexdigest(),
        "outbox": outbox,
        "worker_service_staged_after_browser_policy": True,
        "no_send_journal_or_terminal_receipt": True,
    }


def run(argv=None):
    parser = argparse.ArgumentParser(description="Supervised one-time v1 browser startup recovery")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--request-id", required=True)
    choose = parser.add_mutually_exclusive_group(required=True)
    choose.add_argument("--preview", action="store_true")
    choose.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if (sys.platform != "darwin" or os.getuid() != 0 or os.geteuid() != 0
            or args.request_id != ORIGINAL):
        raise SystemExit("original browser request and administrator privileges required")
    _candidate(args.candidate_sha)
    if ROOT.is_symlink() or not ROOT.is_dir():
        raise RuntimeError("installed protected runtime unavailable")
    sys.path.insert(0, str(ROOT / "package"))
    from do_again.supervisor.macos_execution import load_configuration
    from do_again.supervisor.macos_server import verify_installation, ProjectBroker
    from do_again.core.schema import atomic_json
    from do_again.supervisor.macos_execution import ExecutionBlocked
    config = load_configuration(ROOT / "config.json")
    verify_installation(config)
    home = Path(config["operator_home"])
    if (config.get("source_sha") != SOURCE
            or config.get("production_ready") is not False
            or config.get("development_scope", {}).get("kind") != "jobpipe-offline-v1"
            or config.get("live_canary") is not None
            or config.get("codex_canary") is not None
            or len(config.get("projects", [])) != 3
            or {p["account"] for p in config["projects"]}
               != {"_doagain_da", "_doagain_jp", "_doagain_so"}
            or any(Path(p["repo"]) != home / {"_doagain_da": "do-again",
                                               "_doagain_jp": "jobpipe",
                                               "_doagain_so": "Sonary"}[p["account"]]
                   for p in config["projects"])):
        raise RuntimeError("protected supervisor or offline project scope changed")
    project = next(p for p in config["projects"] if p["account"] == "_doagain_jp")
    broker = ProjectBroker(config, project)
    import fcntl
    fence = ROOT.parent / "state" / project["key"] / "admission.lock"
    if fence.is_symlink() or fence.stat().st_uid != 0 or not fence.is_file():
        raise RuntimeError("root admission fence differs")
    with fence.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proof = _proof(broker, config)
        audit = ROOT.parent / "state" / ("pre_send_refusal_recovery_" + ORIGINAL + ".json")
        result = {"state": "completed", "delivered": 0,
                  "liveness": "pre_dispatch_rejected_auto_background_mode",
                  "delivery_acknowledged": False, "outbox_preserved": True}
        document = dict(proof, result=result)
        if args.preview:
            print("V1_BROWSER_REFUSAL_PREVIEW " + json.dumps({
                "request_id": ORIGINAL, "mode_preexisted": True,
                "browser_send_journal_absent": True,
                "outbox_count": proof["outbox"]["count"],
                "administrator_mutation": False,
            }, sort_keys=True), flush=True)
            return 0
        if audit.exists() or audit.is_symlink():
            previous = json.loads(_file_bytes(audit, 0, root_file=True))
            if previous != document:
                raise RuntimeError("root recovery audit differs; do not repeat")
        else:
            atomic_json(audit, document)
        if _proof(broker, config) != proof:
            raise RuntimeError("pre-Send refusal evidence changed during root recovery")
        broker.ledger.finish(project["key"], ORIGINAL, result)
        if broker.ledger.pending(project["key"]):
            raise RuntimeError("original browser request remains pending")
        print("V1_BROWSER_REFUSAL_RECOVERED " + json.dumps({
            "request_id": ORIGINAL, "delivered": 0,
            "outbox_preserved": True, "production": False,
            "no_replay": True, "audit_written": True,
        }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
