"""Fixed inert launchd canary; production admission never opens for this proof."""
from __future__ import annotations
import hashlib
import json
import os
import plistlib
import time
from pathlib import Path

from ..core.schema import atomic_json, canonical_json
from .macos_execution import EXECUTION_ROOT, ExecutionBlocked, MacOSProcesses


def verify_process_withdrawn(pid, original, timeout=5):
    """Observe the exact kernel identity; never signal an operator-owned PID."""
    deadline=time.monotonic()+timeout
    while True:
        observed=MacOSProcesses().identity(pid)
        if observed is None or observed[:3]!=original[:3] or observed[3]==5:
            return
        if time.monotonic()>=deadline:
            raise ExecutionBlocked('canary process remains live after service withdrawal')
        time.sleep(0.1)


def qualify_service(broker):
    from .capability_probe import qualification_gate
    from .macos_server import machine_identity
    from .worker_service import launchctl, service_present
    with broker.lock:
        before=qualification_gate(broker);identity=machine_identity(broker.config)
        nonce=hashlib.sha256(canonical_json(identity)).hexdigest()[:24]
        journal=broker.state/('service-qualification-'+nonce+'.json')
        if journal.exists():
            record=json.loads(journal.read_text())
            if record['identity']!=identity or record['before']!=before or record['phase']!='complete':
                raise ExecutionBlocked('interrupted service canary requires read-only evidence review; no automatic start')
            return record['result']
        if broker.ledger.pending(broker.project.key):
            raise ExecutionBlocked('unresolved execution blocks service canary')
        root=EXECUTION_ROOT/broker.project.key/'service-probes'/nonce
        root.mkdir(parents=True,mode=0o711);root.chmod(0o711);root.parent.chmod(0o711)
        state=root/'operator-canary';state.mkdir(mode=0o700)
        os.chown(state,broker.config['operator_uid'],broker.config['operator_gid'])
        heartbeat=state/'heartbeat.json'
        label='io.github.tran-steven.do-again.qualification.'+broker.project.key[:12]+'.'+nonce
        if service_present(broker,label):raise ExecutionBlocked('existing canary service is not adopted')
        # Inert immutable code: report identity and wait for guarded withdrawal.
        # No broker connection, repository mutation, browser or network access.
        code=('import os,json,sys,time;'
              'assert os.getuid()==int(sys.argv[2]);'
              'f=open(sys.argv[1],"x");json.dump({"pid":os.getpid(),"uid":os.getuid(),"source_sha":sys.argv[3]},f);'
              'f.flush();os.fsync(f.fileno());f.close();time.sleep(30)')
        spec={'Label':label,'ProgramArguments':[broker.config['python'],'-I','-S','-B','-c',code,
              str(heartbeat),str(broker.config['operator_uid']),broker.config['source_sha']],
              'RunAtLoad':False,'KeepAlive':False,'WorkingDirectory':str(root),
              'EnvironmentVariables':{'PATH':'/usr/bin:/bin','HOME':broker.config['operator_home']}}
        definition=root/'canary.plist'
        with definition.open('xb') as stream:
            stream.write(plistlib.dumps(spec));stream.flush();os.fsync(stream.fileno())
        definition.chmod(0o644)
        record={'identity':identity,'before':before,'label':label,'phase':'dispatch_started',
                'definition_sha256':hashlib.sha256(definition.read_bytes()).hexdigest()}
        atomic_json(journal,record)
        domain=f"gui/{broker.config['operator_uid']}";service=domain+'/'+label
        # This real maintenance fence also protects the one start trigger.
        with broker.probe_admission():
            if qualification_gate(broker)!=before:raise ExecutionBlocked('service canary authority changed')
            try:
                launchctl(broker,'bootstrap',domain,str(definition))
                launchctl(broker,'kickstart',service)
                deadline=time.monotonic()+15
                while not heartbeat.exists() and time.monotonic()<deadline:time.sleep(0.1)
                if not heartbeat.is_file() or heartbeat.is_symlink() or heartbeat.stat().st_nlink!=1:
                    raise ExecutionBlocked('service canary did not produce safe identity evidence')
                value=json.loads(heartbeat.read_text())
                observed=launchctl(broker,'print',service)
                import re
                match=re.search(r'^\s*pid = ([0-9]+)\s*$',observed.stdout,re.MULTILINE)
                kernel=MacOSProcesses().identity(value['pid'])
                if (not match or int(match[1])!=value['pid'] or value['uid']!=broker.config['operator_uid']
                        or value['source_sha']!=broker.config['source_sha'] or not kernel
                        or kernel[0]!=value['uid'] or kernel[3]==5):
                    raise ExecutionBlocked('canary service and kernel identity differ')
                record['worker_identity']={'pid':value['pid'],'kernel':list(kernel)}
                record['phase']='running_observed';atomic_json(journal,record)
            finally:
                # Only this exact canary label may be withdrawn. No operator
                # process enumeration, generic signals or Chrome cleanup.
                launchctl(broker,'bootout',service,allow_missing=True)
            if service_present(broker,label):raise ExecutionBlocked('canary withdrawal remains uncertain')
            verify_process_withdrawn(value['pid'],kernel)
        if qualification_gate(broker)!=before:
            raise ExecutionBlocked('service qualification changed live authority')
        result={'verified':True,'identity':identity,'operator_uid':broker.config['operator_uid'],
                'kernel_identity_verified':True,'one_start_trigger':True,'withdrawal_absence_verified':True,
                'withdrawn_process_identity_verified':True,
                'automatic_restart':False,'live_authority_unchanged':True,
                'service_kind':'inert_qualification_canary','production_worker_start':'not_measured'}
        record.update(phase='complete',result=result);atomic_json(journal,record)
        return result
