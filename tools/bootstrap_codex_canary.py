"""Prepare a sealed Codex grant without Chrome, inference or administrator effects.

Default: write exactly one private operator grant. A separate explicit
--create-control-ref gesture reserves one fixed GitHub branch creation;
--reconcile-control-ref can only GET the original attempted ref.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from do_again.supervisor.macos_execution import private_root_file
from do_again.supervisor.model_bootstrap import bootstrap_codex_canary
from do_again.supervisor.model_branch import provision_codex_control_branch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline",required=True,
                        help="Exact public Do Again main source commit")
    parser.add_argument("--grant-file",required=True,type=Path,
                        help="A new exclusive operator-owned private JSON grant")
    parser.add_argument("--nonce",
                        help="Optional fresh lower-case 24-hex task identity")
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--create-control-ref",action="store_true")
    modes.add_argument("--reconcile-control-ref",action="store_true")
    args=parser.parse_args()
    config_path=Path("/Library/Application Support/DoAgainSupervisor/current/config.json")
    private_root_file(config_path)
    installed=json.loads(config_path.read_text())
    if args.create_control_ref or args.reconcile_control_ref:
        if not args.grant_file.is_file():
            parser.error("exact original grant must already exist for a GitHub effect")
        candidate=json.loads(args.grant_file.read_text())
        if (candidate.get("baseline")!=args.baseline
                or (args.nonce is not None and candidate.get("nonce")!=args.nonce)
                or candidate.get("transport")!="codex-cli"):
            parser.error("original Codex grant identity differs")
        result={"nonce":candidate["nonce"],"baseline":candidate["baseline"],
                "grant_path":str(args.grant_file),"model_calls":0,
                "browser_calls":0,"production_ready":False}
        proof=provision_codex_control_branch(
            grant=args.grant_file,installed=installed,
            journal_root=Path(installed["operator_home"])/".do_again/codex-github-ref",
            reconcile_only=args.reconcile_control_ref)
        result["control"]=proof
    else:
        result=bootstrap_codex_canary(
            installed,baseline=args.baseline,output=args.grant_file,
            nonce=args.nonce)
    print(json.dumps(result,sort_keys=True))


if __name__=="__main__":
    main()
