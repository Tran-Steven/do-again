"""Authenticated operator inspection and maintenance control; no script admin API."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from do_again.supervisor.macos_client import broker_request, operator_request, enroll_github_credential, worker_service_request

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=('status','probe','pause','maintenance','stop','resume','enroll-github','qualify-capabilities','qualify-worker','qualify-service','stage-worker','start-worker','withdraw-worker','reconcile-browser'))
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument("--epoch",type=int)
    parser.add_argument("--request-id")
    args=parser.parse_args()
    if args.operation=='reconcile-browser':
        if not args.request_id:parser.error('--request-id is required')
        result=broker_request(args.repo,{'operation':'browser_reconcile','request_id':args.request_id})
    elif args.operation in ('stage-worker','start-worker','withdraw-worker'):
        if args.operation=='start-worker' and args.epoch is None:parser.error('--epoch is required for start-worker')
        result=worker_service_request(args.repo,args.operation.replace('-','_'),epoch=args.epoch)
    elif args.operation == 'enroll-github':
        import shutil
        import subprocess
        command = shutil.which('gh')
        if command is None:raise RuntimeError('authenticated GitHub CLI is required for enrollment')
        credential = subprocess.run([command,'auth','token','--hostname','github.com'],
                                    text=True,capture_output=True,check=True,timeout=15).stdout.strip()
        result=enroll_github_credential(args.repo,credential)
        credential=None
    elif args.operation in ('qualify-capabilities','qualify-worker','qualify-service'):
        result=broker_request(args.repo,{'operation':args.operation.replace('-','_')})
    elif args.operation in ('status','probe'):
        result=broker_request(args.repo,{'operation':args.operation})
    else:
        intent={'pause':'paused','maintenance':'maintenance','stop':'stopped','resume':'active'}[args.operation]
        result=operator_request(args.repo,intent)
    print(json.dumps(result,indent=2,sort_keys=True))
