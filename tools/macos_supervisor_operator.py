"""Authenticated operator inspection and maintenance control; no script admin API."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from do_again.supervisor.macos_client import broker_request, operator_request

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=('status','probe','pause','maintenance','stop','resume'))
    parser.add_argument('--repo',type=Path,required=True)
    args=parser.parse_args()
    if args.operation in ('status','probe'):
        result=broker_request(args.repo,{'operation':args.operation})
    else:
        intent={'pause':'paused','maintenance':'maintenance','stop':'stopped','resume':'active'}[args.operation]
        result=operator_request(args.repo,intent)
    print(json.dumps(result,indent=2,sort_keys=True))
