"""Attended, one-shot V1A browser controller repair; never submits or starts work.

Prepare an exact sealed controller grant, stage and install its audited runtime,
reconcile only the owned, old operator bridge, and qualify with zero model calls.
No automatic retries, background activation, or unbounded process termination.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone


OLD_SOURCE = "753647b8579728492e5298c2e5f89cd156217c6f"
JOBPIPE_SOURCE = "71fcf63791385799972638aa10b66cdea049134e"
BASELINE_TEST = "tests/test_ats_question_triage.py"
ROOT = Path("/Library/Application Support/DoAgainSupervisor")


def fail(reason):
    raise SystemExit("STOP: " + reason + " (do not rerun automatically)")


def run(*argv, cwd=None, timeout=180):
    return subprocess.run(argv, cwd=cwd, check=True, timeout=timeout)


def output(*argv, cwd=None):
    return subprocess.check_output(argv, cwd=cwd, text=True, timeout=40).strip()


def operator_packet(project, operation):
    endpoint = "/private/var/run/do-again-supervisor/operator.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(20)
        connection.connect(endpoint)
        connection.sendall(json.dumps({"operation": operation, "project": project}).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            piece = connection.recv(4096)
            if not piece or len(data) + len(piece) > 65536:
                fail("operator response incomplete")
            data.extend(piece)
    reply = json.loads(data)
    if not reply.get("ok"):
        fail("operator rejected " + operation + ": " + str(reply.get("error")))
    return reply["result"]


def private_write(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def reconcile_prior_bridge(browser_root, old_grant):
    sock = browser_root / "planner.sock"
    marker = ("-m do_again.browser.planner_service --repo "
              + str(Path.home() / "jobpipe") + " --grant " + str(old_grant))
    process_list = output("/bin/ps", "-axo", "pid=,uid=,command=")
    matches = []
    for row in process_list.splitlines():
        fields = row.strip().split(None, 2)
        if len(fields) == 3 and marker in fields[2]:
            if int(fields[1]) != os.getuid():
                fail("old browser bridge is owned by another UID")
            matches.append(int(fields[0]))
    if len(matches) > 1:
        fail("multiple existing browser services need manual reconciliation")
    if matches:
        pid = matches[0]
        print("Stopping previous operator bridge PID", pid, flush=True)
        os.kill(pid, signal.SIGTERM)
        for _ in range(80):
            if pid not in [int(line.strip().split(None, 1)[0])
                           for line in output("/bin/ps", "-axo", "pid=,command=").splitlines()
                           if line.strip() and line.strip().split(None, 1)[0].isdigit()]:
                break
            time.sleep(0.1)
        else:
            fail("old bridge did not stop conclusively")
    if sock.exists() or sock.is_symlink():
        info = sock.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            fail("existing browser socket ownership/type differs")
        with socket.socket(socket.AF_UNIX) as probe:
            probe.settimeout(2)
            try:
                probe.connect(str(sock))
            except ConnectionRefusedError:
                pass
            else:
                fail("existing browser socket still accepts connections")
        if (sock.lstat().st_ino != info.st_ino or sock.lstat().st_dev != info.st_dev):
            fail("browser socket changed during reconciliation")
        sock.unlink()


def main():
    if sys.platform != "darwin" or os.getuid() != 501 or os.geteuid() != 501:
        fail("requires the normal approved Mac operator account")
    home = Path.home()
    candidate = home / "do-again-v1a-setup/v1a-candidate"
    jobpipe = home / "jobpipe"
    setup = home / "do-again-v1a-setup/controller-pytest"
    old_grant = home / "do-again-v1a-setup/v1a-controller-grants/browser.json"
    installed = json.loads((ROOT / "current/config.json").read_text())
    if installed.get("source_sha") != OLD_SOURCE or installed.get("production_ready") is not False:
        fail("installed supervisor source or authority differs")
    if installed.get("controller_scope", {}).get("planner") != "chatgpt-browser-cdp":
        fail("installed browser controller differs")
    project = next(p for p in installed["projects"] if p["account"] == "_doagain_jp")
    if project["source_sha"] != JOBPIPE_SOURCE:
        fail("protected jobpipe source differs")
    current = operator_packet(project["key"], "status")
    if (current.get("operator_intent") != "maintenance"
            or current.get("epoch") != 13
            or current.get("unresolved_executions")
            or current.get("inflight_request_ids")):
        fail("jobpipe is not quiescent in maintenance at epoch 13")
    if output("git", "-C", str(candidate), "status", "--porcelain"):
        fail("candidate repository is dirty")
    if output("git", "-C", str(candidate), "branch", "--show-current") != "fix/v1a-safe-recovery":
        fail("unexpected candidate branch")
    if output("git", "-C", str(jobpipe), "rev-parse", "refs/remotes/origin/main") != JOBPIPE_SOURCE:
        fail("jobpipe origin/main changed")
    if output("git", "-C", str(candidate), "rev-parse", "HEAD") != OLD_SOURCE:
        fail("candidate already differs; no automatic recovery")
    print("=== UPDATING VERIFIED SOURCE ===", flush=True)
    run("git", "fetch", "origin", "fix/v1a-safe-recovery", cwd=candidate)
    run("git", "merge", "--ff-only", "FETCH_HEAD", cwd=candidate)
    source_sha = output("git", "rev-parse", "HEAD", cwd=candidate)
    if source_sha == OLD_SOURCE:
        fail("new Mac identity fix not available")
    if not (candidate / "src/do_again/service/browser_bridge.py").is_file():
        fail("candidate source missing browser bridge")
    sys.path.insert(0, str(candidate / "src"))
    from do_again.browser import runtime
    from do_again.browser.planner_service import profile_fingerprint
    from do_again.service.offline_continuation import digest
    from do_again.supervisor.controller_installation import apply_controller_grant
    from do_again.supervisor.wheels import validate_wheel

    fingerprint = profile_fingerprint()
    conversation = runtime._chat_id(str(runtime.project_record(jobpipe).get("chat_url") or ""))
    if not conversation:
        fail("jobpipe bound ChatGPT conversation missing")
    dependencies = json.loads((setup / "dependency-lock.json").read_text())["jobpipe"]
    if {x["id"] for x in dependencies} != {"pytest", "pluggy", "packaging", "iniconfig", "pygments"}:
        fail("approved pytest package set differs")
    for item in dependencies:
        wheel = setup / "wheels" / (item["id"] + ".whl")
        content = wheel.read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            fail("wheel hash differs: " + item["id"])
        validate_wheel(content, item)
    test_blob = output("git", "-C", str(jobpipe), "rev-parse", JOBPIPE_SOURCE + ":" + BASELINE_TEST)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires = now + timedelta(minutes=90)
    goal = {"goal": "Improve isolated jobpipe ATS question triage reliability",
            "repository": "Tran-Steven/jobpipe",
            "constraints": ["Only approved local source and regression tests", "No private candidate data",
                            "Preserve human-review safeguards", "No application submissions",
                            "No worker network access", "No production deployment",
                            "No automatic merge", "Verify commits, tests and receipts"]}
    scope = {"kind":"jobpipe-offline-controller-v1", "repository":"Tran-Steven/jobpipe",
             "branch":"operator-control","goal_sha256":digest(goal),"source_sha":source_sha,
             "epoch":13,"issued_at_utc":now.isoformat(),"expires_at_utc":expires.isoformat(),
             "max_requests":3,"max_cycles":3,"max_outstanding":1,"max_retries":0,
             "planner":"chatgpt-browser-cdp","integration":"proposal-only",
             "network_access":False,"application_submissions":False,"browser_messages":False}
    profile = {"kind":"native-development-v1",
               "allowed_paths":["tools/ats_question_triage.py",
                                "tests/test_ats_question_triage_regression.py"],
               "test_cases":[BASELINE_TEST + "::" + case for case in (
                   "test_unknown_or_sensitive_never_autofills",
                   "test_only_exact_verified_contact_field_is_candidate",
                   "test_yes_no_label_not_treated_as_question")],
               "test_files":{BASELINE_TEST:test_blob}, "test_runner":"pytest",
               "public_evidence":True,"offline_tasks":[]}
    browser = {"kind":"browser-planning-v1","transport":"chatgpt-browser-cdp",
               "repository":"Tran-Steven/jobpipe","branch":"operator-control",
               "source_sha":source_sha,"goal_sha256":digest(goal),"epoch":13,
               "issued_at_utc":now.isoformat(),"expires_at_utc":expires.isoformat(),
               "conversation_id":conversation,"profile_sha256":fingerprint,
               "max_calls":3,"max_prompt_bytes":32768,"max_answer_bytes":65536,
               "application_submissions":False,"worker_network_access":False,
               "production_deployment":False,"automatic_merge":False}
    controller = {"scope":scope,"goal":goal,"profile":profile}
    apply_controller_grant({"source_sha":source_sha,"production_ready":False,
                            "development_scope":{"kind":"jobpipe-offline-v1"}},
                           controller, browser=browser, now=now)

    base = home / "do-again-v1a-setup"
    grants = base / ("v1a-browser-repair-grants-" + source_sha[:12])
    output_dir = base / ("v1a-browser-repair-package-" + source_sha[:12])
    if any(p.exists() or p.is_symlink() for p in (grants, output_dir)):
        fail("repair package/grant directory already exists; reconcile instead")
    os.umask(0o077)
    grants.mkdir(mode=0o700)
    private_write(grants / "controller.json", controller)
    private_write(grants / "browser.json", browser)
    print("=== STAGING ROOT-SEALED CONTROLLER ===", flush=True)
    run(sys.executable, str(candidate / "tools/prepare_macos_supervisor.py"),
        "--source", str(candidate), "--do-again-repo", str(home / "do-again"),
        "--jobpipe-repo", str(jobpipe), "--sonary-repo", str(home / "Sonary"),
        "--jobpipe-offline-development",
        "--dependency-lock", str(setup / "dependency-lock.json"),
        "--controller-grant", str(grants / "controller.json"),
        "--browser-planner-grant", str(grants / "browser.json"),
        "--controller-test-wheels", str(setup / "wheels"),
        "--output", str(output_dir), timeout=250)
    stage_config = json.loads((output_dir / "payload/config.json").read_text())
    if (stage_config["source_sha"] != source_sha
            or stage_config.get("browser_planning_scope") != browser
            or stage_config.get("controller_scope") != scope
            or stage_config.get("production_ready") is not False):
        fail("staged authorization differs; no install attempted")
    if (expires - datetime.now(timezone.utc)).total_seconds() < 900:
        fail("insufficient authorization lifetime after staging")
    for row in installed["projects"]:
        label = "io.github.tran-steven.do-again.worker." + row["key"][:12]
        observed = subprocess.run(["/bin/launchctl","print",
                    "gui/" + str(os.getuid()) + "/" + label],
                    capture_output=True, text=True, timeout=15)
        if observed.returncode != 113 or "Could not find service" not in observed.stderr:
            fail("loaded worker blocks cutover: " + row["account"])
    print("=== REQUESTING ADMINISTRATOR INSTALLATION ===", flush=True)
    run("/usr/bin/osascript", str(output_dir / "authenticate.applescript"), timeout=250)
    installed_new = json.loads((ROOT / "current/config.json").read_text())
    if (installed_new.get("source_sha") != source_sha
            or installed_new.get("controller_scope") != scope
            or installed_new.get("browser_planning_scope") != browser):
        fail("installed controller differs; do not reactivate old bridge")
    print("=== RESTORING OPERATOR BRIDGE ===", flush=True)
    browser_root = home / ".do_again/browser"
    info = browser_root.stat()
    if browser_root.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail("browser root permissions differ")
    reconcile_prior_bridge(browser_root, old_grant)
    environment = dict(os.environ, PYTHONPATH=str(candidate / "src"), PYTHONDONTWRITEBYTECODE="1")
    log = browser_root / "planner-service.log"
    with log.open("a") as stream:
        process = subprocess.Popen([sys.executable,"-B","-m",
             "do_again.browser.planner_service","--repo",str(jobpipe),
             "--grant",str(grants / "browser.json")],
             cwd=candidate, env=environment, stdin=subprocess.DEVNULL,
             stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    socket_path = browser_root / "planner.sock"
    for _ in range(60):
        if socket_path.exists():
            break
        if process.poll() is not None:
            fail("new bridge exited (see " + str(log) + ")")
        time.sleep(0.1)
    if not socket_path.exists() or process.poll() is not None:
        fail("new bridge did not establish socket")
    info = socket_path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600):
        fail("new bridge socket invalid")
    print("=== QUALIFYING ZERO-CALL NATIVE BRIDGE ===", flush=True)
    proof = operator_packet(project["key"], "qualify_browser_planner")
    if (proof.get("state") != "qualified_operator_bridge"
            or proof.get("browser_calls") != 0
            or proof.get("peer_uid") != os.getuid()
            or proof.get("source_sha") != source_sha):
        fail("root bridge proof differs")
    print("BROWSER_BRIDGE_QUALIFIED", flush=True)
    print("Installed source:", source_sha)
    print("Bridge PID:", process.pid)
    print("Grant expires:", expires.isoformat())
    print("ChatGPT planning calls consumed: 0")
    print("Controller: NOT STARTED")
    print("Applications, merges, deployments: DISABLED")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        fail(str(exc))
