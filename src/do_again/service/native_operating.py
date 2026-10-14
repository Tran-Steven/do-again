"""Persistent operator runner over the sealed native controller.

A restart only reconciles durable root state. An uncertain planning tick is
never replayed. The root broker owns authorization, budgets and all effects.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

from ..core.schema import atomic_json
from ..supervisor.macos_client import broker_request, operator_request, worker_service_request
from ..supervisor.macos_execution import ExecutionBlocked
from .controller_background import _target, _authorized, _write_status, SOURCE
from .offline_continuation import PERSISTENT_KIND

LABEL = 'io.github.tran-steven.do-again.native-jobpipe'


def configured(repo):
    from ..supervisor.macos_execution import INSTALL_ROOT, private_root_file
    path = INSTALL_ROOT / 'current/config.json'
    if sys.platform != 'darwin' or not path.exists():
        return False
    private_root_file(path)
    config = json.loads(path.read_text())
    if (config.get('controller_scope') or {}).get('kind') != PERSISTENT_KIND:
        return False
    return any(p.get('account') == '_doagain_jp' and p.get('repo') == str(Path(repo).resolve())
               for p in config.get('projects', []))


def summary(repo):
    from .planner_contract import completed_changes
    repo = _target(repo)
    state = broker_request(repo, {'operation': 'controller_status'})
    return {'state': state['state'], 'verified_completed_changes': completed_changes(state),
            'acknowledged_cycles': len(state['acknowledged']),
            'outstanding_request': state.get('outstanding'), 'blocker': state.get('blocker'),
            'completion_reason':state.get('completion_reason'),'goal_assessment':state.get('goal_assessment'),
            'v1_release_accepted': False}


def state_directory():
    parent = Path.home() / '.do_again'
    root = parent / 'native-jobpipe-runner'
    for path in (parent, root):
        if path.is_symlink():
            raise ExecutionBlocked('native runner directory is aliased')
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ExecutionBlocked('native runner directory is writable by another identity')
    if root.stat().st_mode & 0o077:
        raise ExecutionBlocked('native runner state is not private')
    return root


def _read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 1048576:
            raise ExecutionBlocked('native runner record ownership, size or privacy differs')
        with os.fdopen(fd, closefd=False) as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ExecutionBlocked('native runner record is not an object')
        return value
    finally:
        os.close(fd)


def _authority(repo, source, epoch):
    value = _authorized(repo, source, epoch)
    if value.get('controller_mode') != PERSISTENT_KIND:
        raise ExecutionBlocked('persistent runner requires separately sealed persistent authority')
    return value


def _reconcile(previous, state, source, epoch):
    if previous.get('source_sha') != source or previous.get('epoch') != epoch:
        raise ExecutionBlocked('native runner intent belongs to another source or epoch')
    if previous.get('state') not in {'tick_started', 'tick_returned'}:
        raise ExecutionBlocked('native runner tick intent is unknown')
    if state.get('state') in {'blocked', 'stopped', 'completed'}:
        return
    # The sealed root backend owns each planning-call/probe reservation.
    # Re-entering planning consults that journal; an uncertain call is rejected
    # before Send. The operator runner cannot erase or override that record.
    if state.get('state') == 'planning':
        return
    if (state.get('outstanding') or
            len(state.get('acknowledged', [])) > previous.get('acknowledged_cycles', 0)):
        return
    raise ExecutionBlocked('interrupted planning tick is uncertain; original call journals retained')


def run_persistent(repo, *, source, epoch, poll_seconds=20):
    repo = _target(repo)
    if fcntl is None:
        raise ExecutionBlocked('native runner needs a POSIX lock')
    if type(poll_seconds) is not int or not 1 <= poll_seconds <= 300:
        raise ExecutionBlocked('native runner polling budget differs')
    if not isinstance(source, str) or not SOURCE.fullmatch(source) or type(epoch) is not int or epoch < 1:
        raise ExecutionBlocked('native runner requires an exact installed source and epoch')
    root = state_directory()
    fd = os.open(root / 'runner.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ExecutionBlocked('native runner lock identity differs')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cycles = 0
        intent = root / ('tick-intent.' + source[:12] + '.' + str(epoch) + '.json')
        try:
            _authority(repo, source, epoch)
            state = broker_request(repo, {'operation': 'controller_status'})
            if intent.exists() or intent.is_symlink():
                _reconcile(_read(intent), state, source, epoch)

            while True:
                _authority(repo, source, epoch)
                cycles = len(state.get('acknowledged', []))
                phase = state.get('state')
                if phase in {'completed', 'stopped', 'blocked'}:
                    return _write_status(root, phase, source, epoch, cycles, state.get('blocker'))
                if phase not in {'planning', 'waiting'}:
                    raise ExecutionBlocked('native controller lifecycle is unknown')
                _write_status(root, 'waiting_for_browser' if state.get('waiting_reason') else phase,
                              source, epoch, cycles, state.get('waiting_reason'))
                atomic_json(intent, {'source_sha': source, 'epoch': epoch,
                                     'acknowledged_cycles': cycles, 'state': 'tick_started'})
                try:
                    state = broker_request(repo, {'operation': 'controller_tick'})
                except ExecutionBlocked as exc:
                    if str(exc) != 'remote request outstanding before planning':
                        raise
                    # Authenticated root response, before probe/model reservation.
                    # Let the native worker settle the original request/receipt.
                    atomic_json(intent, {'source_sha': source, 'epoch': epoch,
                                         'acknowledged_cycles': cycles, 'state': 'tick_returned'})
                    _write_status(root, 'waiting_for_existing_receipt', source, epoch, cycles)
                    time.sleep(poll_seconds)
                    state = broker_request(repo, {'operation': 'controller_status'})
                    continue
                # Keep the record after completion. A killed process must never
                # infer safety merely from a missing mutable operator file.
                atomic_json(intent, {'source_sha': source, 'epoch': epoch,
                                     'acknowledged_cycles': cycles, 'state': 'tick_returned'})
                time.sleep(poll_seconds)
        except Exception as exc:
            # Semantic blockers exit successfully, so launchd won't repeatedly
            # retry a challenge or an uncertain browser/remote effect.
            return _write_status(root, 'blocked', source, epoch, cycles,
                                 type(exc).__name__ + ': ' + str(exc)[:300])
    finally:
        os.close(fd)


def start(repo):
    repo = _target(repo)
    root = state_directory()
    authority = broker_request(repo, {'operation': 'status'})
    source, epoch = authority.get('source_sha'), authority.get('epoch')
    if authority.get('controller_mode') != PERSISTENT_KIND:
        raise ExecutionBlocked('installed native persistent authority is absent')
    # Explicit start may resume only jobpipe. Staging and proof checks happen
    # before the root operator interface advances admission.
    if authority.get('operator_intent') == 'maintenance':
        if authority.get('unresolved_executions') or authority.get('inflight_request_ids'):
            raise ExecutionBlocked('native start requires drained root effect journals')
        before = broker_request(repo, {'operation': 'controller_status'})
        if before.get('state') in {'blocked', 'completed'}:
            raise ExecutionBlocked('native controller requires reconciliation or a new goal before resume')
        worker_service_request(repo, 'stage_worker')
        activated = operator_request(repo, 'active')
        epoch = activated['epoch']
        worker_service_request(repo, 'start_worker', epoch=epoch)
    _authority(repo, source, epoch)
    controller = broker_request(repo, {'operation': 'controller_status'})
    if controller.get('state') in {'blocked', 'completed'}:
        raise ExecutionBlocked('native controller requires explicit reconciliation or a new goal')
    if controller.get('state') == 'stopped':
        broker_request(repo, {'operation': 'controller_start'})
    definition = {
        'Label': LABEL,
        'ProgramArguments': [str(Path(sys.executable).resolve()), '-m', 'do_again.cli',
                             'controller', 'run-persistent', str(repo), '--source-sha', source,
                             '--epoch', str(epoch)],
        'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 30, 'ProcessType': 'Background',
        'WorkingDirectory': str(root),
        'EnvironmentVariables': {'HOME': str(Path.home()),
                                 'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
                                 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': str(root / 'runner.out.log'),
        'StandardErrorPath': str(root / 'runner.err.log'),
    }
    path = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    if path.is_symlink():
        raise ExecutionBlocked('native launch definition is aliased')
    if path.exists():
        if plistlib.loads(path.read_bytes()) != definition:
            # Explicit resume replaces only the operator launch definition,
            # retaining the old bytes. Root controller/effect journals stay put.
            previous = path.read_bytes()
            import hashlib
            retained = path.with_name(path.name + '.' + hashlib.sha256(previous).hexdigest()[:16] + '.retained')
            if retained.exists() and retained.read_bytes() != previous:
                raise ExecutionBlocked('retained native launch definition differs')
            subprocess.run(['launchctl', 'bootout', 'gui/'+str(os.getuid())+'/'+LABEL],
                           capture_output=True, timeout=20)
            if not retained.exists():
                with retained.open('xb') as handle:
                    handle.write(previous)
                retained.chmod(0o600)
            temporary = path.with_suffix('.pending')
            with temporary.open('xb') as handle:
                handle.write(plistlib.dumps(definition))
            temporary.chmod(0o600)
            os.replace(temporary, path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as handle:
            handle.write(plistlib.dumps(definition))
        path.chmod(0o600)
    domain = 'gui/' + str(os.getuid())
    loaded = subprocess.run(['launchctl', 'print', domain+'/'+LABEL], capture_output=True, timeout=20)
    command = ['launchctl', 'kickstart', domain+'/'+LABEL] if loaded.returncode == 0 else ['launchctl', 'bootstrap', domain, str(path)]
    result = subprocess.run(command, capture_output=True, timeout=20)
    if result.returncode:
        raise ExecutionBlocked('native background service launch failed; all journals retained')
    return {'state': 'launched', 'source_sha': source, 'epoch': epoch,
            'autonomous_acceptance': False, 'status_file': str(root/'status.json')}


def status(repo):
    _target(repo)
    root = state_directory()
    path = root / 'status.json'
    if not path.exists():
        return {'state': 'not_started', 'autonomous_acceptance': False}
    record = _read(path)
    # A last-written heartbeat is not proof that a process is still alive.
    loaded = subprocess.run(['launchctl', 'print', 'gui/'+str(os.getuid())+'/'+LABEL],
                            capture_output=True, timeout=20)
    record['service_loaded'] = loaded.returncode == 0
    record['last_written_state'] = record.get('state')
    authority = broker_request(Path(repo).resolve(), {'operation': 'status'})
    controller = broker_request(Path(repo).resolve(), {'operation': 'controller_status'})
    record['state'] = controller['state']
    record['operator_intent'] = authority.get('operator_intent', authority.get('intent'))
    record['live_source_sha'] = authority.get('source_sha')
    record['live_epoch'] = authority.get('epoch')
    record['acknowledged_cycles'] = len(controller.get('acknowledged', []))
    record['blocker'] = controller.get('blocker')
    record['autonomous_acceptance'] = False
    return record


def stop(repo):
    repo = _target(repo)
    # Root stop preserves the outstanding reservation. Bootout prevents launchd
    # restart and does not cancel an in-flight native effect.
    controller = broker_request(repo, {'operation': 'controller_stop'})
    withdrawal_error = None
    try:
        worker_service_request(repo, 'withdraw_worker')
    except ExecutionBlocked as exc:
        # Admission is already stopped. Always unload the operator runner;
        # preserve an uncertain worker withdrawal for read-only reconciliation.
        withdrawal_error = str(exc)
    result = subprocess.run(['launchctl', 'bootout', 'gui/'+str(os.getuid())+'/'+LABEL],
                            capture_output=True, timeout=20)
    return {'state': 'worker_withdrawal_uncertain' if withdrawal_error else 'stopped',
            'controller': controller['state'], 'withdrawal_error': withdrawal_error,
            'service_unloaded': result.returncode == 0, 'journals_retained': True}
