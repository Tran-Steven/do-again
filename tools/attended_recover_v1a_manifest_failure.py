"""One attended, irreversible, fail-closed recovery of the known V1A probe.

The operator invokes this explicitly after fetching a verified source branch.
No retry, no autonomous controller start or model call. Root audit requires
administrator approval and independently verifies the exact original evidence.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

OLD_SOURCE = "7d0af8e1a5a0a2e96d9d3cdb8cd1f11ba1344191"
APPROVED_BASE = "cb5ac2ea74eb960eb550bf2df51d29a60f7c26f3"
REQUEST = "offline-5d95b82d0952982adda5-1-probe"
FINGERPRINT = "ec7355ba8ca0c9270c00a8386c3c9c3eb376fc289ec6ee416fc8bcd84f63d747"
SUPERVISOR = Path("/Library/Application Support/DoAgainSupervisor")


def stop(reason):
    raise SystemExit("STOP: " + reason + " — no automatic retry")


def output(*args, cwd=None):
    completed = subprocess.run(args,cwd=cwd,capture_output=True,text=True,timeout=40)
    if completed.returncode:
        stop("read-only source verification failed: " + " ".join(args))
    return completed.stdout.strip()


def call(key, operation, **extra):
    packet={"operation":operation,"project":key,**extra}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
        conn.settimeout(20)
        conn.connect("/private/var/run/do-again-supervisor/operator.sock")
        conn.sendall(json.dumps(packet).encode()+b"\n")
        data=bytearray()
        while b"\n" not in data:
            chunk=conn.recv(4096)
            if not chunk or len(data)+len(chunk)>65536:
                stop("operator reply incomplete; inspect before any new action")
            data.extend(chunk)
    reply=json.loads(data)
    if not reply.get("ok"):
        stop("operator refused "+operation+": "+str(reply.get("error")))
    return reply["result"]


def main():
    if sys.platform!="darwin" or os.getuid()!=501 or os.geteuid()!=501:
        stop("requires the approved normal Mac operator account")
    candidate=Path.home()/"do-again-v1a-setup/v1a-candidate"
    if candidate.is_symlink() or not candidate.is_dir():
        stop("verified source candidate unavailable")
    if output("git","rev-parse","--abbrev-ref","HEAD",cwd=candidate)!="fix/v1a-safe-recovery":
        stop("unexpected source branch")
    if output("git","status","--porcelain",cwd=candidate):
        stop("candidate checkout contains uncommitted work")
    current=output("git","rev-parse","HEAD",cwd=candidate)
    remote=output("git","rev-parse","refs/remotes/origin/fix/v1a-safe-recovery",cwd=candidate)
    if current!=remote:
        stop("candidate source differs from fetched remote; update manually before attempting recovery")
    subprocess.run(["git","merge-base","--is-ancestor",APPROVED_BASE,current],
                   cwd=candidate,check=True,timeout=30)

    config=json.loads((SUPERVISOR/"current/config.json").read_text())
    if config.get("source_sha")!=OLD_SOURCE or config.get("production_ready") is not False:
        stop("installed supervisor no longer matches stranded original source")
    project=next((p for p in config["projects"] if p["account"]=="_doagain_jp"),None)
    if not project or project.get("source_sha")!="71fcf63791385799972638aa10b66cdea049134e":
        stop("protected jobpipe source differs")
    status=call(project["key"],"status")
    if (status.get("operator_intent")!="active" or status.get("epoch")!=14
            or status.get("inflight_request_ids")
            or status.get("controller_enabled") is not True):
        stop("jobpipe is not in the exact known stranded active epoch 14")
    pending=status.get("unresolved_executions")
    if (not isinstance(pending,list) or len(pending)!=1
            or pending[0].get("request_id")!=REQUEST
            or pending[0].get("fingerprint")!=FINGERPRINT
            or pending[0].get("state")!="started"):
        stop("original execution ledger differs; no mutation attempted")
    controller=call(project["key"],"controller_status")
    if (controller.get("state")!="blocked" or controller.get("requests")!=[]
            or controller.get("acknowledged")!=[]
            or controller.get("outstanding") is not None):
        stop("controller contains unexpected development progress")
    reconciler=candidate/"tools/reconcile_v1a_abandoned_probe.py"
    python=Path("/usr/bin/python3")
    sudo=Path("/usr/bin/sudo")
    if (not reconciler.is_file() or reconciler.is_symlink()
            or not python.is_file() or not sudo.is_file()):
        stop("approved administrator reconciliation runtime unavailable")
    grant=Path.home()/("do-again-v1a-setup/v1a-browser-repair-grants-"+OLD_SOURCE[:12])/"browser.json"
    if not grant.is_file() or grant.is_symlink():
        stop("previous operator browser grant missing; do not change authority")
    print("=== VERIFIED STRANDED CONTROLLER ===",flush=True)
    print("Original execution:",REQUEST)
    print("State: started, no terminal result")
    print("Controller: blocked, zero development requests")
    print("Browser calls: no calls indicated; root will independently verify journals")
    print("Git source fix present:",current[:12])
    print("\n=== CLOSING OLD ADMISSION ===",flush=True)
    transition=call(project["key"],"set_intent",intent="maintenance")
    if transition.get("intent")!="maintenance" or transition.get("epoch")!=15:
        stop("authority transition uncertain; inspect the live status")
    print("Jobpipe: MAINTENANCE epoch 15")

    print("\n=== ADMINISTRATOR INSPECTION (READ-ONLY) ===",flush=True)
    inspect=subprocess.run([str(sudo),str(python),"-B",str(reconciler),"--inspect"],
                           timeout=120,check=True,capture_output=True,text=True)
    payload=json.loads(inspect.stdout.strip())
    if (payload.get("operation")!="would_quarantine"
            or payload.get("request_id")!=REQUEST
            or payload.get("old_state")!="started"
            or payload.get("new_state")!="abandoned"
            or payload.get("original_result") is not None):
        stop("administrator read-only preview differs")
    print("Root inspection: exact zero-result request confirmed")
    print("\n=== IRREVERSIBLE AUDITED QUARANTINE ===",flush=True)
    print("Preserves fingerprint/intent and blocked journal; never marks execution successful.")
    # This exact explicitly authorized operation cannot be retried automatically.
    apply=subprocess.run([str(sudo),str(python),"-B",str(reconciler),"--apply",
        "--acknowledge-irreversible-abandonment"],timeout=120,check=True,
        capture_output=True,text=True)
    proof=json.loads(apply.stdout.strip())
    if (proof.get("operation")!="quarantined" or proof.get("request_id")!=REQUEST
            or proof.get("state")!="abandoned"
            or proof.get("replay_permitted") is not False
            or proof.get("terminal_success") is not False
            or not proof.get("controller_journal_retained")):
        stop("root quarantine result differs; original state now requires inspection")
    print("AUDITED_PROBE_QUARANTINED")
    print("Retained blocked journal:",proof["controller_journal_retained"])

    print("\n=== PREPARING FRESH EPOCH-15 CONTROLLER ===",flush=True)
    environment=dict(os.environ,PYTHONPATH=str(candidate/"src"),PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable,"-B",str(candidate/"tools/repair_v1a_browser_controller.py")],
                   cwd=candidate,env=environment,check=True,timeout=420)
    print("\n=== ATTENDED RECOVERY COMPLETE ===",flush=True)
    print("Original probe: permanently quarantined, never replayable")
    print("New browser + native boundary: independently qualified by repair helper")
    print("Controller: NOT STARTED; no development tick or application submission")


if __name__=="__main__":
    try:main()
    except (subprocess.TimeoutExpired,subprocess.CalledProcessError,
            OSError,ValueError,KeyError,RuntimeError) as exc:
        stop(type(exc).__name__+": "+str(exc))
