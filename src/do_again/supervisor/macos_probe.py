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


class BoundaryProbeBlocked(ExecutionBlocked):
    def __init__(self, reason: str, evidence: dict):
        super().__init__(reason)
        self.evidence=evidence


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
    same_uid_sentinel=root/'same-uid-outside-scope'
    same_uid_sentinel.write_text('SAME_UID_SYNTHETIC_UNCHANGED');same_uid_sentinel.chmod(0o600)
    os.chown(same_uid_sentinel,project.uid,project.gid)
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
        targets = {'same_uid_sentinel':str(same_uid_sentinel),'other_sentinel':str(other_sentinel), 'sentinel': str(sentinel), 'service': target,
                   'socket': str(SOCKET_ROOT / f'{project.key[:24]}.sock'),
                   'runner':str(Path(__file__).with_name('execution_runner.py')), 'operator_pid': os.getpid(), 'uid': project.uid, 'gid': project.gid,
                   'package':str(Path(__file__).resolve().parents[2]),
                   'allowed': str(project.worktree / ('.native-proof-' + nonce)), 'scratch': str(scratch)}
        script = '''import errno,json,os,socket,subprocess,time
from pathlib import Path
T=TARGETS
result={}
import sys
sys.path.insert(0,T['package'])
from do_again.supervisor.wheels import validate_wheel
from do_again.supervisor.git_export import export_commit
from do_again.supervisor.control_history import publish_control
from do_again.supervisor.browser_broker import browser_tick
from do_again.supervisor.worker_service import register_worker
from do_again.core.control_transport import BrokerControlHistory
result['immutable_module_import']=all(callable(value) for value in (validate_wheel,export_commit,publish_control,browser_tick,register_worker,BrokerControlHistory))
def denied(name, action):
    try: action()
    except OSError as e: result[name]=e.errno in (errno.EPERM,errno.EACCES)
    else: result[name]=False
import runpy,ctypes
lib=ctypes.CDLL('/usr/lib/libSystem.B.dylib')
result['bootstrap_absent']=ctypes.c_uint.in_dll(lib,'bootstrap_port').value==0
task=ctypes.c_uint.in_dll(lib,'mach_task_self_').value
get_special=lib.task_get_special_port
get_special.argtypes=[ctypes.c_uint,ctypes.c_int,ctypes.POINTER(ctypes.c_uint)];get_special.restype=ctypes.c_int
port=ctypes.c_uint()
result['kernel_bootstrap_absent']=get_special(task,4,ctypes.byref(port))==0 and port.value==0
host=ctypes.c_uint();privileged=ctypes.c_uint()
get_host=lib.host_get_special_port
get_host.argtypes=[ctypes.c_uint,ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_uint)];get_host.restype=ctypes.c_int
result['host_privilege_denied']=get_special(task,2,ctypes.byref(host))!=0 or get_host(host.value,-1,2,ctypes.byref(privileged))!=0 or privileged.value==0
checked_groups=runpy.run_path(T['runner'])['kernel_groups']()
result['identity']=os.getuid()==T['uid'] and os.geteuid()==T['uid'] and os.getgid()==T['gid'] and not (checked_groups-{T['gid']})
Path(T['allowed']).write_text('allowed')
result['allowed_write']=Path(T['allowed']).read_text()=='allowed'
denied('other_project_write_denied',lambda:Path(T['other_sentinel']).write_text('ESCAPE'))
denied('same_uid_outside_scope_denied',lambda:Path(T['same_uid_sentinel']).write_text('ESCAPE'))
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
            raise BoundaryProbeBlocked('dedicated-identity proof failed to execute allowed work', {'phase':'allowed_work','outcome':outcome})
        result = json.loads(outcome['stdout'])
        observed = launchctl('print', target)
        evidence={'phase':'enforcement','checks':result,'observer_returncode':observed.returncode,
                  'service_state':[line.strip() for line in observed.stdout.splitlines()
                      if any(key in line for key in ('runs =','state =','active count =','last exit'))]}
        if observed.returncode or 'runs = 0' not in observed.stdout:
            raise BoundaryProbeBlocked('synthetic operator service changed during confinement proof',evidence)
        disabled = launchctl('print-disabled', domain)
        if f'"{label}" => disabled' in disabled.stdout:
            raise BoundaryProbeBlocked('synthetic operator service was disabled',evidence)
        if same_uid_sentinel.read_text() != 'SAME_UID_SYNTHETIC_UNCHANGED' or other_sentinel.read_text() != 'OTHER_SYNTHETIC_UNCHANGED' or sentinel.read_text() != 'SYNTHETIC_UNCHANGED' or not result or not all(value is True for value in result.values()):
            raise BoundaryProbeBlocked('native execution boundary proof rejected an escape or lost allowed work',evidence)
        from .macos_execution import MacOSProcesses
        if MacOSProcesses().owned(project.uid):
            raise ExecutionBlocked('detached execution descendants survived proof')
        git_scratch, git_cache = git_proof_directories(root, project.uid, project.gid)
        git_evidence = verify_native_git(config, project, git_scratch, git_cache, nonce, start_guard=start_guard)
        verify_literal_source_scope(config, project, root, scratch, cache, start_guard=start_guard)
        snapshot_evidence = verify_reviewed_metadata_snapshot(config, project, root, start_guard=start_guard)
        result['reviewed_metadata_snapshot'] = True
        result['literal_source_scope'] = True
        result['git_transaction'] = True
        result['git_broker_promotion'] = True
        return {'verified': True, 'git': git_evidence, 'reviewed_metadata_snapshot': snapshot_evidence,
                'checks': sorted(result), 'detached_descendants_drained': True,
                'execution_uid': project.uid, 'fixture': label}
    finally:
        (project.worktree / ('.native-proof-' + nonce)).unlink(missing_ok=True)
        launchctl('enable', target)
        launchctl('bootout', target)
        # Preserve proof work and failures for inspection; no synthetic chat,
        # real application, other project, or user browser is mutated.


def git_proof_directories(root: Path, uid: int, gid: int) -> tuple[Path, Path]:
    # Preserve escape-test evidence, including forbidden aliases, in the original
    # scratch. Allowed-work qualification gets fresh roots rather than weakening
    # the execution profile's alias checks or deleting diagnostic evidence.
    paths = root / 'git-scratch', root / 'git-cache'
    for path in paths:
        path.mkdir(mode=0o700)
        os.chown(path, uid, gid)
    return paths


def verify_native_git(config: dict, project, scratch: Path, cache: Path, nonce: str, *, start_guard) -> dict:
    """Real local edit-to-commit proof in scratch; assigned engineering Git is untouched."""
    from .git_capabilities import CONFIG, GitTransaction
    from .macos_execution import MacOSProcesses
    git = Path(config.get('git', ''))
    expected_git = Path('/Library/Application Support/DoAgainSupervisor/current/runtimes/git/bin/git')
    if git != expected_git or git not in project.executables:
        raise ExecutionBlocked('attested native Git capability is not installed')
    fixture = scratch / 'git-fixture'
    fixture.mkdir(mode=0o700)
    os.chown(fixture, project.uid, project.gid)
    selected, excluded = fixture / 'selected.txt', fixture / 'excluded.txt'
    for path in (selected, excluded):
        path.write_text('before\n');os.chown(path, project.uid, project.gid)
    sequence = 0
    def run(argv):
        nonlocal sequence
        sequence += 1
        packet = {'operation':'execute', 'request_id':f'native-git-{nonce}-{sequence}',
                  'argv':argv, 'cwd':'.', 'timeout':20}
        outcome = capture(launch_spec(project, packet, scratch, cache), 20, start_guard=start_guard)
        if outcome['returncode'] or outcome['timed_out'] or outcome.get('stdout_truncated') or outcome.get('stderr_truncated'):
            raise BoundaryProbeBlocked('native Git transaction proof failed',
                                       {'phase':'native_git','step':sequence,'outcome':outcome})
        return outcome['stdout'].strip()
    run([str(git), 'init', '--template=', str(fixture)])
    (fixture / '.git/config').write_bytes(CONFIG)
    run([str(git), '-C', str(fixture), 'add', '--', 'selected.txt', 'excluded.txt'])
    run([str(git), '-C', str(fixture), 'commit', '--no-verify', '--no-gpg-sign', '-m', 'synthetic baseline'])
    before = run([str(git), '-C', str(fixture), 'rev-parse', '--verify', 'HEAD'])
    selected.write_text('after\n');excluded.write_text('excluded change\n')
    tx = GitTransaction(git, fixture, scratch / 'git-candidate', before, 'do-again/native-proof')
    tx.prepare(fixture / '.git')
    for path in (tx.metadata, *tx.metadata.rglob('*')):
        os.chown(path, project.uid, project.gid)
    head = ''
    for command in tx.commit_commands(['selected.txt'], 'synthetic selected change'):
        head = run(command)
    tx.validate_candidate(head, scratch / 'git-sealed')
    changed = run(tx._command('diff-tree', '--no-commit-id', '--name-only', '-r', head))
    parent = run(tx._command('rev-parse', f'{head}^'))
    original = run([str(git), '-C', str(fixture), 'rev-parse', '--verify', 'HEAD'])
    if changed != 'selected.txt' or parent != before or original != before:
        raise ExecutionBlocked('native Git proof changed an unreserved path or authority')
    if MacOSProcesses().owned(project.uid):
        raise ExecutionBlocked('native Git descendants survived the transaction proof')
    broker_evidence = verify_native_git_broker(config, project, fixture, scratch.parent, before,
                                              nonce, start_guard=start_guard)
    return {'verified':True, 'base_sha':before, 'candidate_sha':head,
            'exact_paths':['selected.txt'], 'authoritative_metadata_unchanged':True,
            'execution_uid':project.uid, 'commands':sequence, 'broker':broker_evidence}


def verify_native_git_broker(config: dict, project, fixture: Path, root: Path, base: str,
                             nonce: str, *, start_guard) -> dict:
    """Installed promotion/replay proof; only the root-created synthetic tree changes.

    The live configuration stays maintenance-only. A private fixture authority and
    journal admit only this fixed synthetic transaction through the actual broker
    implementation. No socket operation exposes fixture bindings or this authority.
    """
    import sys
    import re
    import threading
    from dataclasses import replace
    from types import SimpleNamespace
    from .git_broker import commit_via_broker
    from .jobpipe_development import synthetic_qualification_config
    from .macos_execution import ExecutionLedger, MacOSProcesses
    from .macos_server import worktree_authority
    expected_root = EXECUTION_ROOT / project.key / 'probes' / nonce
    if (sys.platform != 'darwin' or os.geteuid() != 0 or config.get('production_ready') is not False
            or not re.fullmatch('[0-9a-f]{32}', nonce)
            or root != expected_root or fixture != root / 'git-scratch/git-fixture'
            or root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o022):
        raise ExecutionBlocked('broker qualification requires a protected synthetic maintenance fixture')
    real_authority = worktree_authority(project.worktree)
    private = root / 'broker-state';private.mkdir(mode=0o700)
    ledger = ExecutionLedger(private / 'proof.sqlite')
    status = {'intent':'active','epoch':1,'goal_revision':'synthetic-native-proof-' + nonce}
    # All spawn and promotion decisions still hold the real maintenance fence.
    fixture_broker = SimpleNamespace(
        project=replace(project, worktree=fixture),
        config=synthetic_qualification_config(config),
        registry=SimpleNamespace(status=lambda repo:dict(status)),
        ledger=ledger, lock=threading.Lock(), admission=start_guard, _verified=lambda:True)
    packet = {'operation':'git_commit','request_id':'native-broker-' + nonce,
              'expected_head':base,'expected_epoch':1,'paths':['selected.txt'],
              'message':'synthetic broker selected change'}
    receipt = commit_via_broker(fixture_broker, packet)
    if receipt.get('state') != 'succeeded' or receipt.get('paths') != ['selected.txt']:
        raise BoundaryProbeBlocked('installed Git broker promotion failed', {'phase':'git_broker','receipt':receipt})
    promoted = worktree_authority(fixture)
    if promoted != receipt['authority'] or promoted['repo_head'] == base:
        raise ExecutionBlocked('synthetic Git promotion lacks independent authority evidence')
    for path in (fixture / '.git', *(fixture / '.git').rglob('*')):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ExecutionBlocked('promoted Git metadata is mutable or aliased')
    # Return the identical terminal receipt without entering candidate execution.
    replay = commit_via_broker(fixture_broker, packet)
    if replay != receipt or worktree_authority(fixture) != promoted or ledger.pending(project.key):
        raise ExecutionBlocked('installed broker receipt replay changed authority')
    if (config.get('production_ready') is not False
            or worktree_authority(project.worktree) != real_authority
            or (fixture / 'excluded.txt').read_text() != 'excluded change\n'
            or MacOSProcesses().owned(project.uid)):
        raise ExecutionBlocked('installed broker proof changed live authority, excluded edits or left descendants')
    return {'verified':True,'promoted_sha':promoted['repo_head'], 'base_sha':base,
            'metadata_root_owned':True,'terminal_receipt_replayed':True,
            'live_authority_unchanged':True,'production_configuration_unchanged':True}


def verify_reviewed_metadata_snapshot(config, project, root, *, start_guard):
    """Actual root copy/exchange of synthetic metadata owned by other identities."""
    from .reviewed_task import copy_git_metadata, metadata_digest
    from .git_broker import atomic_swap
    directory = root / 'reviewed-metadata-proof'
    directory.mkdir(mode=0o700)
    original, saved = directory / 'original', directory / 'saved'
    original.mkdir(mode=0o755)
    (original / 'objects').mkdir(mode=0o750)
    (original / 'HEAD').write_bytes(b'ref: refs/heads/synthetic-original\n')
    (original / 'objects' / 'inert').write_bytes(b'synthetic byte-preservation proof\n')
    for path in (original, original / 'HEAD'):
        os.chown(path, config['operator_uid'], config['operator_gid'])
    for path in (original / 'objects', original / 'objects' / 'inert'):
        os.chown(path, project.uid, project.gid)
    with start_guard():
        before = metadata_digest(original)
        copy_git_metadata(original, saved)
        if metadata_digest(saved) != before or metadata_digest(original) != before:
            raise ExecutionBlocked('cross-identity metadata snapshot was not exact')
        (original / 'HEAD').write_bytes(b'ref: refs/heads/synthetic-candidate\n')
        atomic_swap(original, saved)
        if metadata_digest(original) != before:
            raise ExecutionBlocked('cross-identity metadata recovery was not exact')
    return {'verified': True, 'root_uid': os.geteuid(),
            'original_owner_uids': [config['operator_uid'], project.uid],
            'byte_mode_owner_digest': before, 'atomic_recovery_verified': True,
            'fixture_only': True}


def verify_literal_source_scope(config, project, root, scratch, cache, *, start_guard):
    """Actual Seatbelt positive/negative proof on root-created synthetic source.

    The real engineering worktree and its Git metadata are never modified.
    The synthetic worktree is outside scratch/cache so their general write
    permissions cannot accidentally make the negative test pass.
    """
    from dataclasses import replace
    scratch, cache = root / 'literal-scope-scratch', root / 'literal-scope-cache'
    for directory in (scratch,cache):
        directory.mkdir(mode=0o700); os.chown(directory,project.uid,project.gid)
    fixture = root / 'literal-source-fixture'
    fixture.mkdir(mode=0o700)
    os.chown(fixture, project.uid, project.gid)
    allowed, blocked = fixture / 'allowed.py', fixture / 'blocked.py'
    for path in (allowed, blocked):
        path.write_text('BEFORE'); path.chmod(0o600); os.chown(path, project.uid, project.gid)
    synthetic = replace(project, worktree=fixture)
    script = ('import errno,json;from pathlib import Path;'
        'a=Path('+repr(str(allowed))+');b=Path('+repr(str(blocked))+');'
        'a.write_text("AFTER");ok=a.read_text()=="AFTER";'
        '\ntry: b.write_text("ESCAPE")'
        '\nexcept OSError as e: denied=e.errno in (errno.EPERM,errno.EACCES)'
        '\nelse: denied=False'
        '\ntry: __import__("os").link(b,'+repr(str(cache/'blocked-link'))+')'
        '\nexcept OSError as e: link_denied=e.errno in (errno.EPERM,errno.EACCES)'
        '\nelse: link_denied=False'
        '\ntry:'
        '\n Path('+repr(str(cache/'blocked-symlink'))+').symlink_to(b)'
        '\n Path('+repr(str(cache/'blocked-symlink'))+').write_text("ESCAPE")'
        '\nexcept OSError as e: symlink_denied=e.errno in (errno.EPERM,errno.EACCES)'
        '\nelse: symlink_denied=False'
        '\nprint(json.dumps({"allowed":ok,"other_denied":denied,"hardlink_denied":link_denied,"symlink_denied":symlink_denied}))')
    packet={'operation':'execute','request_id':'literal-scope-'+root.name,
        'argv':[config['python'],'-I','-S','-B','-c',script], 'cwd':'.','timeout':15}
    result=capture(launch_spec(synthetic,packet,scratch,cache,source_write_paths=('allowed.py',)),
                   15,start_guard=start_guard)
    if (result['returncode'] or result['timed_out']
            or json.loads(result['stdout'])!={'allowed':True,'other_denied':True,'hardlink_denied':True,'symlink_denied':True}
            or allowed.read_text()!='AFTER' or blocked.read_text()!='BEFORE'):
        raise BoundaryProbeBlocked('literal source write scope failed',{'phase':'literal_source_scope','outcome':result})
    script=('import errno,json;from pathlib import Path;'
        '\ntry: Path('+repr(str(allowed))+').write_text("ESCAPE")'
        '\nexcept OSError as e: denied=e.errno in (errno.EPERM,errno.EACCES)'
        '\nelse: denied=False'
        '\nprint(json.dumps({"readonly_denied":denied}))')
    packet=dict(packet, request_id='readonly-scope-'+root.name,
                argv=[config['python'],'-I','-S','-B','-c',script])
    result=capture(launch_spec(synthetic,packet,scratch,cache,source_write_paths=()),
                   15,start_guard=start_guard)
    if (result['returncode'] or result['timed_out']
            or json.loads(result['stdout'])!={'readonly_denied':True}
            or allowed.read_text()!='AFTER' or blocked.read_text()!='BEFORE'):
        raise BoundaryProbeBlocked('read-only native test source scope failed',{'phase':'readonly_source_scope','outcome':result})
