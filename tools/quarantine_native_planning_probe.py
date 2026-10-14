"""Only the hash-verified protected administrator installation stage may run."""
from pathlib import Path
import argparse,json,os,sys
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--stage',type=Path,required=True);parser.add_argument('--execute-approved-action',action='store_true');args=parser.parse_args();stage=args.stage
    if os.geteuid()!=0 or not stage.is_absolute() or stage.parent!=Path('/Library/Application Support/DoAgainSupervisor') or not stage.name.startswith('staging-'):raise RuntimeError('protected administrator staging required')
    for p in (stage,stage/'package',stage/'package/do_again',*stage.parents):
        if p.is_symlink() or p.stat().st_uid!=0 or p.stat().st_mode&0o022:raise RuntimeError('stage ownership or ancestry differs')
    sys.path.insert(0,str(stage/'package'))
    from do_again.supervisor.native_planning_probe_recovery import recover
    print(json.dumps(recover(stage,execute_approved_action=args.execute_approved_action),sort_keys=True))
if __name__=='__main__':main()
