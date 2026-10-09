"""Root-owned worker service staging. No legacy runtime, profile reset or replay."""
from __future__ import annotations
import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from ..core.schema import atomic_json
from .macos_execution import INSTALL_ROOT,ExecutionBlocked,private_root_file
from .control_history import gate

AGENTS=Path('/Library/LaunchAgents')


def service_spec(broker,epoch):
    key=broker.project.key[:12];label='io.github.tran-steven.do-again.worker.'+key
    name={'_doagain_da':'do-again','_doagain_jp':'jobpipe'}.get(broker.project.account)
    if name is None:raise ExecutionBlocked('worker service project is excluded')
    return label,{'Label':label,'ProgramArguments':[broker.config['python'],'-I','-S','-B',
        str(INSTALL_ROOT/'current/worker-bootstrap.py'),'--project',name,
        '--expected-source',broker.config['source_sha'],'--expected-epoch',str(epoch)],
        'RunAtLoad':False,'KeepAlive':False,'ThrottleInterval':30,
        'WorkingDirectory':str(INSTALL_ROOT/'current'),
        'EnvironmentVariables':{'HOME':broker.config['operator_home'],'PATH':'/usr/bin:/bin'}}


def launchctl(broker,*argv,allow_missing=False):
    result=subprocess.run(['/bin/launchctl',*argv],capture_output=True,text=True,timeout=30,
        user=broker.config['operator_uid'],group=broker.config['operator_gid'],extra_groups=[],
        env={'PATH':'/usr/bin:/bin','HOME':broker.config['operator_home']})
    if result.returncode and not allow_missing:
        raise ExecutionBlocked('worker service operation failed; inspect durable deployment state')
    return result


def service_present(broker,label):
    result=launchctl(broker,'print',f"gui/{broker.config['operator_uid']}/{label}",allow_missing=True)
    if result.returncode==0:return True
    if 'Could not find service' in result.stderr:return False
    raise ExecutionBlocked('worker service observation is inconclusive')


def native_root():
    if sys.platform!='darwin' or os.geteuid()!=0:
        raise ExecutionBlocked('worker lifecycle requires the immutable root macOS helper')


def provision_mirror(broker):
    from .authority import project_identity
    base=Path(broker.config['operator_home'])/'.do_again/projects'/project_identity(broker.project.repo)[:12]
    for parent in (base,*base.parents):
        if parent.is_symlink() or (parent.exists() and (parent.stat().st_uid not in {0,broker.config['operator_uid']} or parent.stat().st_mode&0o022)):
            raise ExecutionBlocked('worker mirror ancestor is unsafe')
    if not base.is_dir():raise ExecutionBlocked('existing project state is required; no legacy state is replaced')
    mirror=base/'sealed-control';marker=mirror/'.broker-scope.json'
    identity={'repo':str(broker.project.repo),'project':broker.project.key,'schema':1}
    if mirror.exists():
        if mirror.is_symlink() or mirror.stat().st_uid!=broker.config['operator_uid'] or not marker.is_file():
            raise ExecutionBlocked('existing mirror lacks safe ownership evidence')
        info=marker.lstat()
        if marker.is_symlink() or info.st_uid!=0 or info.st_nlink!=1 or info.st_mode&0o022:
            raise ExecutionBlocked('worker mirror scope marker is mutable or aliased')
        if mirror.stat().st_mode&0o077 or json.loads(marker.read_text())!=identity:
            raise ExecutionBlocked('worker mirror project binding differs')
        return
    mirror.mkdir(mode=0o700);os.chown(mirror,broker.config['operator_uid'],broker.config['operator_gid'])
    atomic_json(marker,identity);marker.chmod(0o644)


def stage_worker(broker):
    from .macos_server import verify_installation
    native_root()
    with broker.lock,broker.admission():
        status=broker.registry.status(broker.project.repo)
        if status['intent']!='maintenance' or not broker._verified() or broker.ledger.pending(broker.project.key):
            raise ExecutionBlocked('worker staging requires verified quiescent maintenance')
        verify_installation(broker.config)
        private_root_file(INSTALL_ROOT/'current/worker-bootstrap.py')
        # Stage for the next explicit resume, which advances authority once.
        provision_mirror(broker)
        label,spec=service_spec(broker,status['epoch']+1)
        target=AGENTS/(label+'.plist');journal=broker.state/'worker-deployment.json'
        if service_present(broker,label):
            raise ExecutionBlocked("loaded worker must be withdrawn before staging")
        if target.exists():
            private_root_file(target)
            if not journal.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=json.loads(journal.read_text()).get('plist_sha256'):
                raise ExecutionBlocked('existing worker service lacks matching ownership evidence')
        data=plistlib.dumps(spec);digest=hashlib.sha256(data).hexdigest()
        record={'phase':'stage_started','label':label,'source_sha':broker.config['source_sha'],
                'epoch':status['epoch']+1,'staged_epoch':status['epoch'],'plist_sha256':digest,'previous':None}
        if journal.exists():record['previous']=json.loads(journal.read_text())
        atomic_json(journal,record)  # Durable before writing a service definition.
        if AGENTS.is_symlink() or AGENTS.stat().st_uid!=0 or AGENTS.stat().st_mode&0o022:
            raise ExecutionBlocked('worker service directory is not protected')
        temporary=target.with_suffix('.plist.pending')
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o644)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary,target)
        from .git_broker import sync_directory
        sync_directory(AGENTS)
        record['phase']='staged';atomic_json(journal,record)
        return {'staged':True,'started':False,'source_sha':record['source_sha'],'label':label}


def start_worker(broker,epoch):
    native_root()
    from .macos_server import verify_installation
    with broker.lock,broker.admission():
        gate(broker,epoch);verify_installation(broker.config)
        journal=broker.state/'worker-deployment.json'
        record=json.loads(journal.read_text());label,spec=service_spec(broker,epoch)
        target=AGENTS/(label+'.plist');private_root_file(target)
        if (record['phase']!='staged' or record['source_sha']!=broker.config['source_sha']
                or record['epoch']!=epoch or record['plist_sha256']!=hashlib.sha256(target.read_bytes()).hexdigest()
                or plistlib.loads(target.read_bytes())!=spec):
            raise ExecutionBlocked('worker deployment source, epoch or service definition changed')
        if broker.ledger.pending(broker.project.key):raise ExecutionBlocked('unresolved execution blocks worker startup')
        domain=f"gui/{broker.config['operator_uid']}";service=domain+'/'+label
        if service_present(broker,label):
            raise ExecutionBlocked('worker service is already loaded; duplicate start denied')
        record['phase']='start_started';atomic_json(journal,record)
        # Pause cannot overtake either service creation or its one start trigger.
        launchctl(broker,'bootstrap',domain,str(target))
        launchctl(broker,'kickstart',service)
        record['phase']='started_unverified';atomic_json(journal,record)
        return {'state':'started_unverified','label':label,'source_sha':record['source_sha']}


def withdraw_worker(broker):
    native_root()
    # Withdrawal closes admission first. No old effect journal is restored.
    with broker.lock,broker.admission():
        status=broker.registry.status(broker.project.repo)
        if status['intent']!='maintenance':
            broker.registry.set_intent(broker.project.repo,'maintenance',goal_revision=status['goal_revision'])
        record=json.loads((broker.state/'worker-deployment.json').read_text())
        label,_=service_spec(broker,status['epoch']);target=AGENTS/(label+'.plist')
        private_root_file(target)
        if record['plist_sha256']!=hashlib.sha256(target.read_bytes()).hexdigest():
            raise ExecutionBlocked('changed service definition blocks withdrawal')
        result=launchctl(broker,'bootout',f"gui/{broker.config['operator_uid']}/{label}",allow_missing=True)
        if service_present(broker,label):
            raise ExecutionBlocked('worker withdrawal is uncertain; maintenance and journals retained')
        record['phase']='withdrawn';atomic_json(broker.state/'worker-deployment.json',record)
        return {'withdrawn':True,'operator_intent':'maintenance','journals_retained':True}


def register_worker(broker,packet):
    native_root()
    if (set(packet)!={'operation','pid','epoch','source_sha'} or packet['operation']!='worker_register'
            or type(packet['pid']) is not int or packet['pid']<=1
            or packet['source_sha']!=broker.config['source_sha']):
        raise ExecutionBlocked('invalid immutable worker registration')
    from .macos_execution import MacOSProcesses
    from .macos_server import verify_installation
    with broker.lock,broker.admission():
        gate(broker,packet['epoch']);verify_installation(broker.config)
        record=json.loads((broker.state/'worker-deployment.json').read_text())
        label,_=service_spec(broker,packet['epoch'])
        if (record['phase'] not in {'start_started','started_unverified','running'} or record['source_sha']!=packet['source_sha']
                or record['epoch']!=packet['epoch']):
            raise ExecutionBlocked('worker has no matching guarded deployment')
        observed=launchctl(broker,'print',f"gui/{broker.config['operator_uid']}/{label}")
        import re
        match=re.search(r'^\s*pid = ([0-9]+)\s*$',observed.stdout,re.MULTILINE)
        if observed.returncode or not match or int(match[1])!=packet['pid']:
            raise ExecutionBlocked('worker PID does not belong to the guarded service')
        processes=MacOSProcesses();identity=processes.identity(packet['pid'])
        if not identity or identity[0]!=broker.config['operator_uid'] or identity[3]==5:
            raise ExecutionBlocked('worker kernel identity is unavailable or changed')
        lease=broker.state/'worker-instance.json'
        if lease.exists():
            previous=json.loads(lease.read_text())
            live=processes.identity(previous['pid'])
            if (live and live[3]!=5 and list(live)==previous['identity']
                    and previous['pid']!=packet['pid']):
                raise ExecutionBlocked('another engineering worker still owns this project')
        from .macos_server import machine_identity
        atomic_json(lease,{'pid':packet['pid'],'identity':list(identity),'epoch':packet['epoch'],
                           'runtime':machine_identity(broker.config)})
        record['phase']='running';atomic_json(broker.state/'worker-deployment.json',record)
        return {'registered':True,'epoch':packet['epoch'],'pid':packet['pid']}


def verified_worker_pid(broker,epoch):
    """A browser lease belongs to the live service worker, never its helper."""
    from .macos_execution import MacOSProcesses
    try:
        lease=json.loads((broker.state/'worker-instance.json').read_text())
        record=json.loads((broker.state/'worker-deployment.json').read_text())
        pid=lease['pid']
        identity=MacOSProcesses().identity(pid)
        label,_=service_spec(broker,epoch)
        observed=launchctl(broker,'print',f"gui/{broker.config['operator_uid']}/{label}")
        import re
        match=re.search(r'^\s*pid = ([0-9]+)\s*$',observed.stdout,re.MULTILINE)
        if (observed.returncode or not match or int(match[1])!=pid or not identity
                or identity[0]!=broker.config['operator_uid'] or identity[3]==5
                or list(identity)!=lease['identity'] or lease['epoch']!=epoch
                or record['phase']!='running' or record['epoch']!=epoch
                or record['source_sha']!=broker.config['source_sha']):
            raise ExecutionBlocked('browser worker ownership is unavailable or changed')
        return pid
    except (OSError,ValueError,KeyError,TypeError):
        raise ExecutionBlocked('browser worker ownership evidence is invalid') from None
