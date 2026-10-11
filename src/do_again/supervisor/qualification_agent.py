"""Immutable operator-side Agent for a fixed root-owned qualification binding."""
import json
import os
import sys
from pathlib import Path
from .macos_execution import EXECUTION_ROOT, SOCKET_ROOT, INSTALL_ROOT, private_root_file, ExecutionBlocked


def validate_binding(value,config,path):
    from .authority import project_identity
    import re
    if (config.get('production_ready') is not False or value.get('source_sha')!=config['source_sha']
            or not re.fullmatch('[0-9a-f]{24}',str(value.get('nonce','')))):
        raise ExecutionBlocked('qualification binding differs from the sealed maintenance build')
    projects=[p for p in config['projects'] if p['repo']==value.get('repo')]
    if len(projects)!=1:raise ExecutionBlocked('qualification project is excluded')
    key=project_identity(Path(value['repo']));nonce=value['nonce']
    root=EXECUTION_ROOT/key/'worker-probes'/nonce
    expected={'binding':root/'operator-binding.json','control':root/'trusted-state/control',
              'state':root/'trusted-state/agent','policy':root/'trusted-state/policy.json',
              'socket':SOCKET_ROOT/('q-'+key[:12]+'-'+nonce+'.sock')}
    if path!=expected['binding'] or any(value.get(k)!=str(v) for k,v in expected.items() if k!='binding'):
        raise ExecutionBlocked('qualification binding paths escape their fixed scope')
    return expected


def main():
    from .immutable_worker import read_worker_configuration
    from .macos_server import verify_installation
    from .macos_client import _request
    from ..service.daemon import _run_admitted_worker
    from argparse import Namespace
    from ..core.broker_executor import BrokerExecutor
    from ..core.control_transport import BrokerControlHistory
    if sys.platform!='darwin' or not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise ExecutionBlocked('operator qualification requires the installed isolated macOS runtime')
    config=read_worker_configuration();verify_installation(config)
    if (Path(sys.executable)!=Path(config['python']) or Path(__file__)!=
            INSTALL_ROOT/'current/package/do_again/supervisor/qualification_agent.py'):
        raise ExecutionBlocked('qualification must use its immutable module and interpreter')
    if (os.getuid()!=config['operator_uid'] or os.geteuid()!=os.getuid()
            or os.getgid()!=config['operator_gid'] or os.getegid()!=os.getgid()):
        raise ExecutionBlocked('qualification controller identity differs')
    path=Path(sys.argv[1]);private_root_file(path);value=json.loads(path.read_text())
    paths=validate_binding(value,config,path);private_root_file(paths['policy'])
    repo=Path(value['repo']);done=set()
    def rpc(selected,packet):
        if selected!=repo:raise ExecutionBlocked('qualification cannot choose another project')
        return _request(paths['socket'],packet)
    def check():
        status=rpc(repo,{'operation':'status'})
        if (status['source_sha']!=config['source_sha'] or status['operator_intent']!='active'
                or status['epoch']!=1 or status['goal_revision']!='synthetic-worker-'+value['nonce']
                or status['enforcement_verified'] is not True or status['production_ready'] is not True):
            raise ExecutionBlocked('fixed qualification authority changed')
    def receipt(row):
        if str(row.get('request_id','')).startswith('operator-proof-'+value['nonce']+'-'):
            if row.get('state')!='succeeded':raise ExecutionBlocked('operator fixture task failed')
            done.add(row['request_id'])
            return len(done)==3
    args=Namespace(control_worktree=str(paths['control']),branch='operator-control',remote='origin',
        policy=str(paths['policy']),once=len(sys.argv)>2 and sys.argv[2]=='--once')
    result=_run_admitted_worker(args,repo,paths['state'],False,admission_check=check,
        receipt_observer=receipt,executor=BrokerExecutor(repo=repo,policy_path=paths['policy'],
        state_dir=paths['state'],rpc=rpc),control_transport=BrokerControlHistory(repo,paths['control'],1,rpc=rpc))
    if result:raise ExecutionBlocked('operator qualification Agent did not complete')
    print(json.dumps({'controller_uid':os.getuid(),'tasks':len(done),'source_sha':config['source_sha'],'engine':'sealed_daemon'}),flush=True)
