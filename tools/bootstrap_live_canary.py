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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Exact public Do Again main commit SHA")
    parser.add_argument("--grant-file", type=Path, required=True, help="New private grant JSON path")
    parser.add_argument("--nonce", help="Optional fresh 24-character lowercase hex identity")
    args = parser.parse_args()
    config_path = Path("/Library/Application Support/DoAgainSupervisor/current/config.json")
    config = json.loads(config_path.read_text())
    result = bootstrap_live_canary(
        config, baseline=args.baseline, output=args.grant_file, nonce=args.nonce)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
