"""One fixed launchd qualification actor; no production authority or browser access."""
from __future__ import annotations
import hashlib
import json
import os
import plistlib
import re
import sys
import time
from pathlib import Path
from ..core.schema import atomic_json
from .macos_execution import INSTALL_ROOT, MacOSProcesses, ExecutionBlocked


def _await_operator_identity(broker, service, *, timeout=3.0):
    """Admit only a birth-pinned launchd PID after the operator UID appears.

    launchd may expose root-owned xpcproxy briefly before its UID transition.
    A root observation is never accepted as the qualified worker identity.
    """
    from .worker_service import launchctl
    operator_uid = broker.config['operator_uid']
    deadline = time.monotonic() + timeout
    processes = MacOSProcesses()
    pid = None
    birth = None
    saw_root_proxy = False
    while True:
        observed = launchctl(broker, 'print', service)
        match = re.search(r'^\s*pid = ([0-9]+)\s*
    from .worker_service import launchctl, service_present
    from .service_probe import verify_process_withdrawn
    if sys.platform!='darwin' or os.geteuid()!=0 or broker.config.get('production_ready') is not False:
        raise ExecutionBlocked('qualification service requires the sealed macOS maintenance broker')
    label='io.github.tran-steven.do-again.worker-qualification.'+broker.project.key[:12]+'.'+nonce
    if binding!=root/'operator-binding.json' or not re.fullmatch('[0-9a-f]{24}',nonce):
        raise ExecutionBlocked('qualification service binding differs')
    if service_present(broker,label):
        raise ExecutionBlocked('existing qualification service is not adopted')
    journal=root/'operator-service.json'
    if journal.exists():
        raise ExecutionBlocked('qualification service has an original intent; no replay')
    state=root/'trusted-state/agent'
    stdout=state/'service.stdout';stderr=state/'service.stderr'
    for path in (stdout,stderr):
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd);os.chown(path,broker.config['operator_uid'],broker.config['operator_gid'])
    code=('import sys;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
          'from do_again.supervisor.qualification_agent import main;main()')
    spec={'Label':label,'ProgramArguments':[broker.config['python'],'-I','-S','-B','-c',code,str(binding)],
          'RunAtLoad':False,'KeepAlive':False,'WorkingDirectory':str(INSTALL_ROOT/'current'),
          'StandardOutPath':str(stdout),'StandardErrorPath':str(stderr),
          'EnvironmentVariables':{'PATH':'/usr/bin:/bin','HOME':broker.config['operator_home']}}
    definition=root/'operator-service.plist'
    with definition.open('xb') as stream:
        stream.write(plistlib.dumps(spec));stream.flush();os.fsync(stream.fileno())
    definition.chmod(0o644)
    record={'phase':'dispatch_started','source_sha':broker.config['source_sha'],'label':label,
            'definition_sha256':hashlib.sha256(definition.read_bytes()).hexdigest()}
    atomic_json(journal,record)
    domain='gui/'+str(broker.config['operator_uid']);service=domain+'/'+label
    kernel=None;pid=None;result=None
    try:
        with guard():
            launchctl(broker,'bootstrap',domain,str(definition))
            launchctl(broker,'kickstart',service)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if pid is None:
                pid,kernel,saw_root_proxy=_await_operator_identity(broker,service)
                record.update(phase='running_observed',pid=pid,kernel=list(kernel),
                              root_proxy_observed=saw_root_proxy)
                atomic_json(journal,record)
            if stdout.stat().st_size>65536 or stderr.stat().st_size>65536:
                raise ExecutionBlocked('qualification service output exceeds its budget')
            if stdout.stat().st_size:
                try:result=json.loads(stdout.read_text())
                except ValueError:result=None
                if result is not None:break
            time.sleep(0.1)
        if (pid is None or not isinstance(result,dict) or result.get('controller_uid')!=broker.config['operator_uid']
                or result.get('source_sha')!=broker.config['source_sha'] or result.get('engine')!='sealed_daemon'
                or result.get('tasks')!=3):
            raise ExecutionBlocked('qualification service lacks exact daemon completion evidence')
    finally:
        with guard():launchctl(broker,'bootout',service,allow_missing=True)
        if service_present(broker,label):
            raise ExecutionBlocked('qualification service withdrawal remains uncertain')
        if pid is not None:verify_process_withdrawn(pid,kernel)
    record.update(phase='complete',result=result,withdrawn=True)
    atomic_json(journal,record)
    return dict(result,launchd_service=True,service_pid=pid,service_withdrawn=True)
, observed.stdout, re.MULTILINE)
        if not match:
            if pid is not None:
                raise ExecutionBlocked('qualification launchd PID disappeared during handoff')
        else:
            candidate = int(match[1])
            if pid is not None and candidate != pid:
                raise ExecutionBlocked('qualification launchd PID changed during handoff')
            pid = candidate
            kernel = processes.identity(pid)
            if not kernel or kernel[3] == 5:
                raise ExecutionBlocked('qualification daemon kernel identity is unavailable')
            if birth is None:
                birth = kernel[1:3]
            elif kernel[1:3] != birth:
                raise ExecutionBlocked('qualification daemon kernel birth identity changed')
            if kernel[0] == operator_uid:
                return pid, kernel, saw_root_proxy
            if kernel[0] != 0:
                raise ExecutionBlocked('qualification daemon has an unexpected kernel UID')
            saw_root_proxy = True
        if time.monotonic() >= deadline:
            raise ExecutionBlocked('qualification launchd identity handoff timed out')
        time.sleep(0.025)


def run_operator_service(broker, root, binding, nonce, guard):
    from .worker_service import launchctl, service_present
    from .service_probe import verify_process_withdrawn
    if sys.platform!='darwin' or os.geteuid()!=0 or broker.config.get('production_ready') is not False:
        raise ExecutionBlocked('qualification service requires the sealed macOS maintenance broker')
    label='io.github.tran-steven.do-again.worker-qualification.'+broker.project.key[:12]+'.'+nonce
    if binding!=root/'operator-binding.json' or not re.fullmatch('[0-9a-f]{24}',nonce):
        raise ExecutionBlocked('qualification service binding differs')
    if service_present(broker,label):
        raise ExecutionBlocked('existing qualification service is not adopted')
    journal=root/'operator-service.json'
    if journal.exists():
        raise ExecutionBlocked('qualification service has an original intent; no replay')
    state=root/'trusted-state/agent'
    stdout=state/'service.stdout';stderr=state/'service.stderr'
    for path in (stdout,stderr):
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd);os.chown(path,broker.config['operator_uid'],broker.config['operator_gid'])
    code=('import sys;sys.path.insert(0,'+repr(str(INSTALL_ROOT/'current/package'))+');'
          'from do_again.supervisor.qualification_agent import main;main()')
    spec={'Label':label,'ProgramArguments':[broker.config['python'],'-I','-S','-B','-c',code,str(binding)],
          'RunAtLoad':False,'KeepAlive':False,'WorkingDirectory':str(INSTALL_ROOT/'current'),
          'StandardOutPath':str(stdout),'StandardErrorPath':str(stderr),
          'EnvironmentVariables':{'PATH':'/usr/bin:/bin','HOME':broker.config['operator_home']}}
    definition=root/'operator-service.plist'
    with definition.open('xb') as stream:
        stream.write(plistlib.dumps(spec));stream.flush();os.fsync(stream.fileno())
    definition.chmod(0o644)
    record={'phase':'dispatch_started','source_sha':broker.config['source_sha'],'label':label,
            'definition_sha256':hashlib.sha256(definition.read_bytes()).hexdigest()}
    atomic_json(journal,record)
    domain='gui/'+str(broker.config['operator_uid']);service=domain+'/'+label
    kernel=None;pid=None;result=None
    try:
        with guard():
            launchctl(broker,'bootstrap',domain,str(definition))
            launchctl(broker,'kickstart',service)
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if pid is None:
                pid,kernel,saw_root_proxy=_await_operator_identity(broker,service)
                record.update(phase='running_observed',pid=pid,kernel=list(kernel),
                              root_proxy_observed=saw_root_proxy)
                atomic_json(journal,record)
            if stdout.stat().st_size>65536 or stderr.stat().st_size>65536:
                raise ExecutionBlocked('qualification service output exceeds its budget')
            if stdout.stat().st_size:
                try:result=json.loads(stdout.read_text())
                except ValueError:result=None
                if result is not None:break
            time.sleep(0.1)
        if (pid is None or not isinstance(result,dict) or result.get('controller_uid')!=broker.config['operator_uid']
                or result.get('source_sha')!=broker.config['source_sha'] or result.get('engine')!='sealed_daemon'
                or result.get('tasks')!=3):
            raise ExecutionBlocked('qualification service lacks exact daemon completion evidence')
    finally:
        with guard():launchctl(broker,'bootout',service,allow_missing=True)
        if service_present(broker,label):
            raise ExecutionBlocked('qualification service withdrawal remains uncertain')
        if pid is not None:verify_process_withdrawn(pid,kernel)
    record.update(phase='complete',result=result,withdrawn=True)
    atomic_json(journal,record)
    return dict(result,launchd_service=True,service_pid=pid,service_withdrawn=True)
