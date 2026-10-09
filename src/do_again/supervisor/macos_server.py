"""Root-owned, project-bound Unix broker. Only the operator UID may connect."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import platform
import socket
import stat
import sys
import threading
from pathlib import Path

from .authority import AuthorityRegistry
from .macos_execution import (
    EXECUTION_ROOT, INSTALL_ROOT, SOCKET_ROOT, ExecutionBlocked, ExecutionLedger,
    capture, launch_spec, load_configuration, peer_uid, project_from_dict, receive_packet,
)
from ..core.schema import canonical_json


def machine_identity(config: dict) -> dict:
    return {'source_sha': config['source_sha'], 'os': platform.platform(),
            'manifest_sha256':hashlib.sha256((INSTALL_ROOT/'current/manifest.json').read_bytes()).hexdigest(),
            'python_sha256': hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
            'sandbox_sha256': hashlib.sha256(Path('/usr/bin/sandbox-exec').read_bytes()).hexdigest()}


def verify_installation(config: dict) -> None:
    from .macos_execution import private_root_file
    manifest = INSTALL_ROOT / 'current/manifest.json'
    private_root_file(manifest)
    expected = json.loads(manifest.read_text())
    if expected['source_sha'] != config['source_sha']:
        raise ExecutionBlocked('installed source attestation mismatch')
    for relative, digest in expected['files'].items():
        path = INSTALL_ROOT / 'current' / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ExecutionBlocked('invalid installed manifest path')
        private_root_file(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ExecutionBlocked('installed package drift; execution blocked')


def worktree_authority(worktree: Path) -> dict:
    """Read only Git identity files; never execute worktree Git configuration."""
    import re
    git = worktree / '.git'
    if not git.is_dir() or git.is_symlink():
        raise ExecutionBlocked('assigned Git directory cannot be attested')
    def read(relative: str) -> str:
        path = git / relative
        for item in (path, *path.parents):
            if item == git.parent: break
            if item.is_symlink(): raise ExecutionBlocked('Git identity contains an alias')
        if path.stat().st_nlink != 1: raise ExecutionBlocked('Git identity contains a hardlink')
        if path.stat().st_size > 1024 * 1024: raise ExecutionBlocked('Git identity file exceeds limit')
        return path.read_text().strip()
    head = read('HEAD')
    branch = None
    if head.startswith('ref: '):
        ref = head[5:]
        if not re.fullmatch(r'refs/heads/[A-Za-z0-9._/-]+', ref) or '..' in ref or '//' in ref:
            raise ExecutionBlocked('invalid assigned HEAD reference')
        branch = ref[len('refs/heads/'):]
        try: head = read(ref)
        except FileNotFoundError:
            head = ''
            for line in read('packed-refs').splitlines():
                fields = line.split()
                if len(fields) == 2 and fields[1] == ref: head = fields[0]
    if not re.fullmatch('[0-9a-f]{40}', head):
        raise ExecutionBlocked('assigned HEAD cannot be attested')
    return {'repo_head': head, 'repo_branch': branch}


class ProjectBroker:
    def __init__(self, config: dict, project: dict):
        self.config = config
        self.project = project_from_dict(project)
        self.project.validate(config['operator_uid'])
        self.registry = AuthorityRegistry(Path(config['authority_path']), owner_uid=0)
        self.state = INSTALL_ROOT / 'state' / self.project.key
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger = ExecutionLedger(Path(config['authority_path']))
        self.lock = threading.Lock()

    def dispatch(self, packet: dict, uid: int) -> dict:
        if uid != self.config['operator_uid']:
            raise ExecutionBlocked('peer is not the trusted operator identity')
        if packet == {'operation': 'status'}:
            status = self.registry.status(self.project.repo)
            return {'operator_intent': status['intent'], 'epoch': status['epoch'],
                    'source_sha': self.config['source_sha'], 'uid': self.project.uid,
                    'worktree': str(self.project.worktree),
                    'production_ready': self.config.get('production_ready', False),
                    'executables': [str(p) for p in self.project.executables],
                    'authority': worktree_authority(self.project.worktree),
                    'goal_revision': status['goal_revision'],
                    'enforcement_verified': self._verified(),
                    'enforcement_blocker': self._probe_blocker(),
                    'unresolved_executions': self.ledger.pending(self.project.key)}
        if packet == {'operation': 'probe'}:
            if self.registry.status(self.project.repo)['intent'] != 'maintenance':
                raise ExecutionBlocked('installation probes require maintenance intent')
            with self.lock:
                verify_installation(self.config)
                (self.state / 'enforcement.json').unlink(missing_ok=True)
                from .macos_probe import verify_dedicated_boundary
                from ..core.schema import atomic_json
                try:
                    result = verify_dedicated_boundary(self.config, self.project, start_guard=self.probe_admission)
                except Exception as exc:
                    atomic_json(self.state / 'enforcement-blocker.json',
                                {'reason':str(exc),'evidence':getattr(exc,'evidence',{}),
                                 'identity':machine_identity(self.config)})
                    raise
                (self.state / 'enforcement-blocker.json').unlink(missing_ok=True)
                atomic_json(self.state / 'enforcement.json', {'identity': machine_identity(self.config), 'result': result})
                return result
        if packet == {'operation':'qualify_capabilities'}:
            from .capability_probe import qualify_capabilities
            return qualify_capabilities(self)
        if packet.get('operation') == 'git_commit':
            from .git_broker import commit_via_broker
            return commit_via_broker(self, packet)
        if packet.get('operation') == 'dependency_install':
            from .dependencies import install_via_broker
            return install_via_broker(self, packet)
        if packet.get('operation') == 'git_publish':
            from .publication import publish_via_broker
            return publish_via_broker(self, packet)
        if packet.get('operation') == 'git_publication_reconcile':
            from .publication import reconcile_publication
            return reconcile_publication(self, packet)
        if packet.get('operation') != 'execute':
            raise ExecutionBlocked('no administrative operations are exposed')
        with self.lock:
            if self.registry.status(self.project.repo)['intent'] != 'active':
                from .authority import AuthorityDenied
                raise AuthorityDenied('operator intent blocks new execution')
            verify_installation(self.config)
            if not self._verified():
                raise ExecutionBlocked('native enforcement has not been verified for this OS/runtime')
            if 'request_fingerprint' in packet:
                import re
                if not isinstance(packet['request_fingerprint'], str) or not re.fullmatch('[0-9a-f]{64}', packet['request_fingerprint']):
                    raise ExecutionBlocked('invalid original request fingerprint')
            request_id = packet.get('request_id', '')
            from .macos_execution import REQUEST_ID
            if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
                raise ExecutionBlocked('invalid request identity')
            request_root = EXECUTION_ROOT / self.project.key / 'requests' / request_id
            scratch, cache = request_root / 'scratch', request_root / 'cache'
            for path in (scratch, cache):
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chown(path, self.project.uid, self.project.gid)
            for ancestor in (request_root, request_root.parent):
                os.chmod(ancestor, 0o711)
            spec = launch_spec(self.project, packet, scratch, cache)
            fingerprint = hashlib.sha256(canonical_json(packet)).hexdigest()
            # Pause and spawn share the same project admission fence.
            # Existing execution may drain; the lock is not held during capture.
            with self.admission():
                if self.registry.status(self.project.repo)['intent'] != 'active':
                    raise ExecutionBlocked('operator authority changed before launch')
                if not self.config.get('production_ready', False):
                    raise ExecutionBlocked('production entrypoint migration is incomplete')
                if 'expected_head' in packet or 'expected_authority' in packet:
                    actual = worktree_authority(self.project.worktree)
                    expected = packet.get('expected_authority', {})
                    if not isinstance(expected, dict) or any(key not in actual or actual[key] != value for key, value in expected.items()):
                        raise ExecutionBlocked('assigned worktree authority changed before launch')
                    if 'expected_head' in packet and packet['expected_head'] != actual['repo_head']:
                        raise ExecutionBlocked('assigned worktree head changed before launch')
                recovered = self.ledger.reserve(self.project.key, request_id, fingerprint)
                if recovered is not None:
                    return recovered
                from .macos_execution import MacOSProcesses
                processes = MacOSProcesses()
                if processes.owned(self.project.uid):
                    raise ExecutionBlocked('unclassified dedicated processes already exist')
                import subprocess
                # Started marker is durable before this effect. Operator pause uses
                # the same file fence and cannot slip between final check and spawn.
                proc = subprocess.Popen(**spec)
            result = capture(spec, packet['timeout'], processes=processes, process=proc)
            result.update({'execution_uid': self.project.uid, 'source_sha': self.config['source_sha']})
            self.ledger.finish(self.project.key, request_id, result)
            return result

    @contextmanager
    def admission(self):
        import fcntl
        with (self.state / 'admission.lock').open('a') as fence:
            fcntl.flock(fence, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fence, fcntl.LOCK_UN)

    @contextmanager
    def probe_admission(self):
        with self.admission():
            if self.registry.status(self.project.repo)['intent'] != 'maintenance':
                raise ExecutionBlocked('operator intent changed before installation probe')
            yield

    def operator_intent(self, intent: str) -> dict:
        if intent not in {'active','paused','maintenance','stopped'}:
            raise ExecutionBlocked('invalid operator intent')
        with self.admission():
            if intent == 'active' and (not self.config.get('production_ready',False) or not self._verified()):
                raise ExecutionBlocked('resume requires migrated entrypoints and verified native enforcement')
            status = self.registry.status(self.project.repo)
            epoch = self.registry.set_intent(self.project.repo, intent, goal_revision=status['goal_revision'])
            return {'intent':intent,'epoch':epoch}

    def operator_github_token(self, token: str) -> dict:
        from .github import GitHubRepository
        project_config = next(p for p in self.config['projects'] if p['key']==self.project.key)
        GitHubRepository(project_config.get('github_repository'),token)
        with self.admission():
            if self.registry.status(self.project.repo)['intent'] != 'maintenance':
                raise ExecutionBlocked('credential enrollment requires maintenance intent')
            target = self.state / 'github-token'
            if target.is_symlink() or (target.exists() and target.stat().st_nlink != 1):
                raise ExecutionBlocked('credential path is aliased')
            temporary = self.state / 'github-token.pending'
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    stream.write(token);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,target)
                from .git_broker import sync_directory
                sync_directory(self.state)
            finally:
                temporary.unlink(missing_ok=True)
            return {'registered':True,'repository':project_config['github_repository']}

    def _probe_blocker(self) -> dict | None:
        try:
            value=json.loads((self.state / 'enforcement-blocker.json').read_text())
            if value['identity']==machine_identity(self.config):return value
        except (OSError,ValueError,KeyError):pass
        return None

    def _verified(self) -> bool:
        try:
            proof = json.loads((self.state / 'enforcement.json').read_text())
            return proof['identity'] == machine_identity(self.config) and proof['result']['verified'] is True
        except (OSError, ValueError, KeyError):
            return False


def serve_project(config: dict, project: dict, broker: ProjectBroker) -> None:
    path = SOCKET_ROOT / f'{broker.project.key[:24]}.sock'
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not stat.S_ISSOCK(path.lstat().st_mode) or path.lstat().st_uid != config['operator_uid']:
            raise ExecutionBlocked('unexpected socket path; refusing replacement')
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
        else:
            raise ExecutionBlocked('existing broker is live')
        finally:
            probe.close()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        os.chown(path, config['operator_uid'], config['operator_gid'])
        os.chmod(path, 0o600)
        listener.listen(8)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    uid = peer_uid(connection)
                    if uid != config['operator_uid']:
                        raise ExecutionBlocked('untrusted broker peer')
                    result = broker.dispatch(receive_packet(connection), uid)
                    response = {'ok': True, 'result': result}
                except Exception as exc:
                    response = {'ok': False, 'blocked': True, 'error': str(exc)}
                try:
                    connection.sendall(canonical_json(response) + b'\n')
                except OSError:
                    pass  # Durable execution ledger survives a lost client.


def serve_operator(config: dict, brokers: dict) -> None:
    path = SOCKET_ROOT / 'operator.sock'
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not stat.S_ISSOCK(path.lstat().st_mode) or path.lstat().st_uid != config['operator_uid']:
            raise ExecutionBlocked('unexpected operator socket path; refusing replacement')
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            path.unlink()
        else:
            raise ExecutionBlocked('existing operator broker is live')
        finally:
            probe.close()
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path));os.chown(path,config['operator_uid'],config['operator_gid']);os.chmod(path,0o600)
        listener.listen(4)
        while True:
            connection,_ = listener.accept()
            with connection:
                try:
                    if peer_uid(connection) != config['operator_uid']:
                        raise ExecutionBlocked('untrusted administrative peer')
                    packet=receive_packet(connection)
                    broker=brokers.get(packet['project'])
                    if broker is None:raise ExecutionBlocked('project is excluded or unregistered')
                    if set(packet)=={'operation','project','intent'} and packet['operation']=='set_intent':
                        result=broker.operator_intent(packet['intent'])
                    elif set(packet)=={'operation','project','token'} and packet['operation']=='set_github_token':
                        result=broker.operator_github_token(packet['token'])
                    else:raise ExecutionBlocked('unknown operator operation')
                    response={'ok':True,'result':result}
                except Exception as exc:
                    response={'ok':False,'blocked':True,'error':str(exc)}
                try:connection.sendall(canonical_json(response)+b'\n')
                except OSError:pass


def main() -> None:
    os.umask(0o077)
    config = load_configuration(INSTALL_ROOT / 'current/config.json')
    verify_installation(config)
    if SOCKET_ROOT.is_symlink() or (SOCKET_ROOT.exists() and SOCKET_ROOT.stat().st_uid != 0):
        raise ExecutionBlocked('unexpected supervisor socket directory')
    SOCKET_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chmod(SOCKET_ROOT, 0o755)
    brokers={project['key']:ProjectBroker(config,project) for project in config['projects']}
    threads = [threading.Thread(target=serve_project, args=(config,project,brokers[project['key']]))
               for project in config['projects']]
    threads.append(threading.Thread(target=serve_operator,args=(config,brokers)))
    for thread in threads:thread.start()
    for thread in threads:thread.join()


if __name__ == '__main__':main()
