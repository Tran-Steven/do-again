"""Headless/background, one-shot canary conversation and private grant preparation.

Runs as the normal Mac operator. Does NOT activate or install any worker.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from do_again.supervisor.canary_bootstrap import bootstrap_live_canary
from do_again.supervisor.canary_branch import provision_control_branch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Exact public Do Again main commit SHA")
    parser.add_argument("--grant-file", type=Path, required=True, help="New private grant JSON path")
    parser.add_argument("--nonce", help="Optional fresh 24-character lowercase hex identity")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume-control-ref", action="store_true",
                      help="GET-only reconciliation of an already-reserved GitHub ref; no browser Send or POST")
    mode.add_argument("--finish-control-ref", action="store_true",
                      help="Finish first GitHub ref creation from a sealed grant; no browser Send")
    args = parser.parse_args()
    config_path = Path("/Library/Application Support/DoAgainSupervisor/current/config.json")
    config = json.loads(config_path.read_text())
    if args.resume_control_ref or args.finish_control_ref:
        grant = json.loads(args.grant_file.read_text())
        if (grant.get("baseline") != args.baseline
                or (args.nonce is not None and grant.get("nonce") != args.nonce)):
            parser.error("resumed branch must match the original sealed grant")
        result = {"nonce": grant["nonce"], "grant_path": str(args.grant_file),
                  "chat_url": grant["chat_url"], "production_ready": False}
    else:
        result = bootstrap_live_canary(
            config, baseline=args.baseline, output=args.grant_file, nonce=args.nonce)
    # The ChatGPT initialization is one-shot. If the GitHub API fails after
    # the grant is sealed, its independent journal permits read-only recovery,
    # never another browser Send or blind GitHub POST.
    control = provision_control_branch(
        nonce=result["nonce"], baseline=args.baseline,
        grant=args.grant_file,
        journal_root=Path(config["operator_home"]) / ".do_again" / "canary-github-ref",
        installed=config, reconcile_only=args.resume_control_ref)
    result["control_branch_verified"] = control["branch"]
    result["control_branch_sha"] = control["sha"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
