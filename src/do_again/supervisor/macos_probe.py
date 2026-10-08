"""Post-install fault injection against synthetic resources only."""
from __future__ import annotations
import json
import os
import plistlib
import subprocess
import tempfile
import uuid
from pathlib import Path
from .macos_execution import EXECUTION_ROOT, SOCKET_ROOT, ExecutionBlocked, capture, launch_spec


def verify_dedicated_boundary(config: dict, project, *, start_guard) -> dict:
    nonce = uuid.uuid4().hex
    root = EXECUTION_ROOT / project.key / 'probes' / nonce
    root.mkdir(parents=True, mode=0o711)
    for ancestor in (root, root.parent):
        os.chmod(ancestor, 0o711)
    scratch, cache = root / 'scratch', root / 'cache'
    for path in (scratch, cache):
        path.mkdir(mode=0o700)
        os.chown(path, project.uid, project.gid)
    label = 'io.github.tran-steven.do-again.synthetic-proof-' + nonce
    target = f"gui/{config['operator_uid']}/{label}"
    domain = f"gui/{config['operator_uid']}"
    fixture = root / 'service.plist'
    fixture.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': ['/usr/bin/true'], 'RunAtLoad': False}))
    fixture.chmod(0o644)
    sentinel = root / 'protected-sentinel'
    sentinel.write_text('SYNTHETIC_UNCHANGED')
    sentinel.chmod(0o600)
    other = next(item for item in config['projects'] if item['uid'] != project.uid)
    other_sentinel = root / 'other-project-sentinel'
    other_sentinel.write_text('OTHER_SYNTHETIC_UNCHANGED');other_sentinel.chmod(0o600)
    os.chown(other_sentinel,other['uid'],other['gid'])
    # Run fixture management as the operator identity, never an execution user.
    def launchctl(*args):
        return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True,
                              user=config['operator_uid'], group=config['operator_gid'], extra_groups=[],
                              env={'PATH': '/usr/bin:/bin'}, timeout=15)
    with start_guard():
        if launchctl('bootstrap', domain, str(fixture)).returncode:
            raise ExecutionBlocked('cannot install synthetic operator-domain proof fixture')
    try:
        targets = {'other_sentinel':str(other_sentinel), 'sentinel': str(sentinel), 'service': target,
                   'socket': str(SOCKET_ROOT / f'{project.key[:24]}.sock'),
                   'operator_pid': os.getpid(), 'uid': project.uid, 'gid': project.gid,
                   'allowed': str(project.worktree / ('.native-proof-' + nonce)), 'scratch': str(scratch)}
        script = '''import errno,json,os,socket,subprocess,time
from pathlib import Path
T=TARGETS
result={}
def denied(name, action):
    try: action()
    except OSError as e: result[name]=e.errno in (errno.EPERM,errno.EACCES)
    else: result[name]=False
result['identity']=os.getuid()==T['uid'] and os.geteuid()==T['uid'] and not (set(os.getgroups())-{T['gid']})
Path(T['allowed']).write_text('allowed')
result['allowed_write']=Path(T['allowed']).read_text()=='allowed'
denied('other_project_write_denied',lambda:Path(T['other_sentinel']).write_text('ESCAPE'))
denied('read_denied',lambda:Path(T['sentinel']).read_bytes())
denied('write_denied',lambda:Path(T['sentinel']).write_text('ESCAPE'))
denied('hardlink_denied',lambda:os.link(T['sentinel'],str(Path(T['scratch'])/'hardlink-proof')))
(Path(T['scratch'])/'symlink-proof').symlink_to(T['sentinel'])
denied('symlink_denied',lambda:(Path(T['scratch'])/'symlink-proof').write_text('ESCAPE'))
denied('root_identity_denied',lambda:os.setuid(0))
denied('signal_denied',lambda:os.kill(T['operator_pid'],0))
def network():
    with socket.socket() as s:s.connect(('127.0.0.1',9224))
denied('network_denied',network)
def broker():
    with socket.socket(socket.AF_UNIX) as s:s.connect(T['socket'])
denied('broker_denied',broker)
result['bash_denied']=subprocess.run(['/bin/bash','-c','echo ESCAPE > '+T['sentinel']],capture_output=True).returncode!=0
for operation in ('kickstart','disable','bootout'):
    result['service_'+operation+'_denied']=subprocess.run(['/bin/launchctl',operation,T['service']],capture_output=True).returncode!=0
# A detached descendant must be drained before the helper writes terminal evidence.
pid=os.fork()
if pid==0:
    os.setsid();time.sleep(30);os._exit(0)
print(json.dumps(result),flush=True)
'''.replace('TARGETS', repr(targets))
        packet = {'operation': 'execute', 'request_id': 'native-proof-' + nonce,
                  'argv': [config['python'], '-I', '-S', '-B', '-c', script], 'cwd': '.', 'timeout': 15}
        outcome = capture(launch_spec(project, packet, scratch, cache), 15, start_guard=start_guard)
        if outcome['returncode'] or outcome['timed_out']:
            raise ExecutionBlocked('dedicated-identity proof failed to execute allowed work')
        result = json.loads(outcome['stdout'])
        observed = launchctl('print', target)
        if observed.returncode or 'runs = 0' not in observed.stdout:
            raise ExecutionBlocked('synthetic operator service changed during confinement proof')
        disabled = launchctl('print-disabled', domain)
        if f'"{label}" => disabled' in disabled.stdout:
            raise ExecutionBlocked('synthetic operator service was disabled')
        if other_sentinel.read_text() != 'OTHER_SYNTHETIC_UNCHANGED' or sentinel.read_text() != 'SYNTHETIC_UNCHANGED' or not result or not all(value is True for value in result.values()):
            raise ExecutionBlocked('native execution boundary proof rejected an escape or lost allowed work')
        from .macos_execution import MacOSProcesses
        if MacOSProcesses().owned(project.uid):
            raise ExecutionBlocked('detached execution descendants survived proof')
        return {'verified': True, 'checks': sorted(result), 'detached_descendants_drained': True,
                'execution_uid': project.uid, 'fixture': label}
    finally:
        (project.worktree / ('.native-proof-' + nonce)).unlink(missing_ok=True)
        launchctl('enable', target)
        launchctl('bootout', target)
        # Preserve proof work and failures for inspection; no synthetic chat,
        # real application, other project, or user browser is mutated.
