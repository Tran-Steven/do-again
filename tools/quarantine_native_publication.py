"""Root-owned staged entrypoint; no automatic or unsealed invocation."""
from pathlib import Path
import argparse,json,os,sys

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--execute-approved-action',action='store_true')
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('administrator execution is required')
    stage=args.stage
    if not stage.is_absolute() or stage.parent!=Path('/Library/Application Support/DoAgainSupervisor') or not stage.name.startswith('staging-'):
        raise RuntimeError('only the hash-verified protected installation stage is permitted')
    for p in (stage,stage/'package',stage/'package/do_again',*stage.parents):
        if p.is_symlink() or p.stat().st_uid!=0 or p.stat().st_mode&0o022:raise RuntimeError('stage ownership or ancestry differs')
    sys.path.insert(0,str(stage/'package'))
    from do_again.supervisor.native_publication_recovery import recover
    print(json.dumps(recover(stage,execute_approved_action=args.execute_approved_action),sort_keys=True))
if __name__=='__main__':main()
