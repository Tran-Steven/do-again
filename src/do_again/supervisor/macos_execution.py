"""Dedicated-identity macOS execution boundary. No administrative script API."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import threading
import math
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .authority import AuthorityDenied, AuthorityRegistry, project_identity
from ..core.schema import OperatorError, canonical_json

INSTALL_ROOT = Path('/Library/Application Support/DoAgainSupervisor')
EXECUTION_ROOT = Path('/private/var/do-again-execution')
SOCKET_ROOT = Path('/private/var/run/do-again-supervisor')
SERVICE_LABEL = 'io.github.tran-steven.do-again.supervisor'
REQUEST_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{7,159}$')
MAX_PACKET = 1024 * 1024
MAX_OUTPUT = 256 * 1024


class ExecutionBlocked(OperatorError):
    pass


@dataclass(frozen=True)
class ProjectExecution:
    repo: Path
    uid: int
    gid: int
    account: str
    worktree: Path
    executables: tuple[Path, ...]

    @property
    def key(self) -> str:
        return project_identity(self.repo)

    def validate(self, operator_uid: int) -> None:
        if not 400 <= self.uid < 500 or self.uid == operator_uid or self.gid != self.uid:
            raise ExecutionBlocked('execution requires a distinct non-root identity')
        if not re.fullmatch(r'_doagain_[a-z0-9]{2,20}', self.account):
            raise ExecutionBlocked('invalid dedicated execution account')
        if self.worktree != EXECUTION_ROOT / self.key / 'worktree':
            raise ExecutionBlocked('worktree is not supervisor allocated')
        if not self.executables or any(not p.is_absolute() for p in self.executables):
            raise ExecutionBlocked('executables must be supervisor resolved')


def peer_uid(connection: socket.socket) -> int:
    if sys.platform != 'darwin':
        raise ExecutionBlocked('authenticated macOS peer credentials unavailable')
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    getpeereid = library.getpeereid
    getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    getpeereid.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise ExecutionBlocked('cannot authenticate Unix-socket peer')
    return uid.value


def private_root_file(path: Path) -> None:
    """Validate installed code/config; reject aliases and writable ancestors."""
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ExecutionBlocked('installed path contains a symlink')
        info = ancestor.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ExecutionBlocked('installed path is not immutable root-owned state')
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ExecutionBlocked('installed file is not an unaliased regular file')


def load_configuration(path: Path) -> dict[str, Any]:
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ExecutionBlocked('root macOS helper is required; no fallback')
    private_root_file(path)
    config = json.loads(path.read_text())
    if config.get('schema_version') != 1 or config.get('operator_uid', 0) <= 0:
        raise ExecutionBlocked('invalid sealed supervisor configuration')
    projects = config.get('projects', [])
    if not projects or len({p['uid'] for p in projects}) != len(projects):
        raise ExecutionBlocked('each project requires its own execution identity')
    approvals = config.get('dependency_artifacts', {})
    from .dependencies import approved_artifact
    if not isinstance(approvals, dict) or set(approvals) - {p['key'] for p in projects}:
        raise ExecutionBlocked('dependency approval includes an excluded project')
    for key, entries in approvals.items():
        if not isinstance(entries, list) or len(entries) > 64:
            raise ExecutionBlocked('dependency approval exceeds project limits')
        for item in entries:
            approved_artifact(config, key, item['id'])
    import pwd, grp
    operator = pwd.getpwuid(config['operator_uid'])
    if operator.pw_gid != config['operator_gid'] or operator.pw_dir != config['operator_home']:
        raise ExecutionBlocked('operator identity changed')
    for item in projects:
        project = project_from_dict(item)
        project.validate(config['operator_uid'])
        account = pwd.getpwuid(project.uid)
        if (account.pw_name, account.pw_gid, account.pw_dir, account.pw_shell) != (project.account, project.gid, '/var/empty', '/usr/bin/false'):
            raise ExecutionBlocked('dedicated account identity changed')
        if grp.getgrgid(project.gid).gr_name != project.account:
            raise ExecutionBlocked('dedicated account group changed')
    return config


def project_from_dict(item: dict[str, Any]) -> ProjectExecution:
    return ProjectExecution(Path(item['repo']), int(item['uid']), int(item['gid']),
                            item['account'], Path(item['worktree']),
                            tuple(Path(p) for p in item['executables']))


def profile(project: ProjectExecution, scratch: Path, cache: Path) -> str:
    # Imported OS rules supply loader/secinit support. Restrict their IPC and
    # network grants explicitly. UID separation protects the operator's launchd
    # domain even where Seatbelt alone does not check existing service mutations.
    roots = (project.worktree.resolve(), scratch.resolve(), cache.resolve())
    if any(p == Path(p.anchor) or p.is_symlink() for p in (project.worktree, scratch, cache)):
        raise ExecutionBlocked('invalid execution roots')
    for root in roots:
        for directory, dirs, files in os.walk(root, followlinks=False):
            if Path(directory) == project.worktree:
                dirs[:] = [d for d in dirs if d != '.git']
            for name in dirs + files:
                p = Path(directory) / name
                if p.is_symlink():
                    # Read-only interpreter links are not write-root aliases.
                    if not any(p.resolve().is_relative_to(r) for r in roots):
                        raise ExecutionBlocked('external write-root symlink')
                elif p.is_file() and p.stat().st_nlink != 1:
                    raise ExecutionBlocked('hardlink alias in write root')
    reads = (*roots, Path('/System'), Path('/usr/lib'), Path('/usr/share'),
             Path('/bin'), Path('/usr/bin'), INSTALL_ROOT / 'current/runtimes',
             INSTALL_ROOT / 'current/package')
    subpaths = lambda paths: ' '.join(f'(subpath {json.dumps(str(p))})' for p in paths)
    runner = INSTALL_ROOT / 'current/package/do_again/supervisor/execution_runner.py'
    protected_git = json.dumps(str(project.worktree / '.git'))
    return (
        '(version 1)(deny default)(import "/System/Library/Sandbox/Profiles/system.sb")'
        '(deny network*)(deny signal)(deny job-creation)(deny mach-bootstrap)(deny ipc-posix-shm*)'
        '(deny mach-lookup (require-not (require-any (global-name "com.apple.secinitd")'
        '(global-name "com.apple.logd")(global-name "com.apple.logd.events"))))'
        '(allow process-fork)(allow process-exec)(allow file-read-metadata)'
        f'(allow file-read* {subpaths(reads)} (literal {json.dumps(str(runner))}) (literal "/dev/null")(literal "/dev/urandom"))'
        f'(allow file-map-executable {subpaths(reads)})'
        f'(allow file-write* {subpaths(roots)} (literal "/dev/null"))'
        f'(deny file-write* (subpath {protected_git}))'
    )


def launch_spec(project: ProjectExecution, packet: dict[str, Any], scratch: Path, cache: Path) -> dict[str, Any]:
    fields = {'operation', 'request_id', 'argv', 'cwd', 'timeout'}
    optional = {'request_fingerprint', 'expected_head', 'expected_authority'}
    if not fields <= set(packet) or set(packet) - fields - optional or packet['operation'] != 'execute':
        raise ExecutionBlocked('unknown broker operation or administrative fields')
    request_id = packet['request_id']
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise ExecutionBlocked('invalid request identity')
    argv = packet['argv']
    if (not isinstance(argv, list) or not argv or len(argv) > 256
            or any(not isinstance(arg, str) or '\x00' in arg for arg in argv)
            or Path(argv[0]) not in project.executables):
        raise ExecutionBlocked('command does not use an admitted executable')
    cwd_value = packet['cwd']
    if not isinstance(cwd_value, str) or Path(cwd_value).is_absolute():
        raise ExecutionBlocked('cwd must be relative to the assigned worktree')
    try:
        cwd = (project.worktree / cwd_value).resolve(strict=True)
    except OSError as exc:
        raise ExecutionBlocked('execution cwd is unavailable') from exc
    if not cwd.is_relative_to(project.worktree) or not cwd.is_dir():
        raise ExecutionBlocked('cwd escapes assigned worktree')
    timeout = packet['timeout']
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise ExecutionBlocked('invalid execution timeout')
    return {
        'args': ['/usr/bin/sandbox-exec', '-p', profile(project, scratch, cache), sys.executable, '-I', '-S', '-B',
                 str(INSTALL_ROOT / 'current/package/do_again/supervisor/execution_runner.py'),
                 str(project.uid), str(math.ceil(timeout)), *argv],
        'cwd': str(cwd), 'user': project.uid, 'group': project.gid, 'extra_groups': [],
        'umask': 0o077, 'close_fds': True, 'start_new_session': True,
        'env': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(scratch), 'TMPDIR': str(scratch),
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                'GIT_TERMINAL_PROMPT': '0'},
        'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE,
    }


class ExecutionLedger:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS execution (project TEXT, request TEXT, fingerprint TEXT, '
                       'state TEXT, result TEXT, PRIMARY KEY(project,request))')
            db.execute('CREATE TABLE IF NOT EXISTS capability_intent (project TEXT, request TEXT, '
                       'payload TEXT NOT NULL, PRIMARY KEY(project,request))')

    def intent(self, project: str, request: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT payload FROM capability_intent WHERE project=? AND request=?',
                             (project,request)).fetchone()
        return None if row is None else json.loads(row[0])

    def evidence(self, project: str) -> dict[str, Any]:
        """Redacted, transaction-consistent proof that effects survived cutover."""
        digest=hashlib.sha256();counts={}
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            for table,fields in (('execution','request,fingerprint,state,result'),
                                 ('capability_intent','request,payload')):
                count=0;digest.update(canonical_json({'table':table,'project':project}))
                for row in db.execute('SELECT '+fields+' FROM '+table+' WHERE project=? ORDER BY request',(project,)):
                    digest.update(canonical_json(list(row)));digest.update(b'\n');count+=1
                counts[table]=count
        return {'schema_version':1,'sha256':digest.hexdigest(),'counts':counts}

    def pending(self, project: str) -> list[dict[str, str]]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT request,fingerprint FROM execution WHERE project=? AND state='started'",
                              (project,)).fetchall()
        return [{'request_id':row[0],'fingerprint':row[1],'state':'started'} for row in rows]

    def lookup(self, project: str, request: str, fingerprint: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT fingerprint,state,result FROM execution WHERE project=? AND request=?',
                             (project,request)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise ExecutionBlocked('request fingerprint conflict')
        if row[1] != 'terminal':
            raise ExecutionBlocked('ambiguous started execution cannot replay')
        return json.loads(row[2])

    def reserve(self, project: str, request: str, fingerprint: str, *, intent: dict | None = None) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint,state,result FROM execution WHERE project=? AND request=?',
                             (project, request)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ExecutionBlocked('request fingerprint conflict')
                if row[1] == 'terminal':
                    return json.loads(row[2])
                raise ExecutionBlocked('ambiguous started execution cannot replay')
            if db.execute("SELECT 1 FROM execution WHERE project=? AND state='started'", (project,)).fetchone():
                raise ExecutionBlocked('project already has an unresolved execution')
            db.execute('INSERT INTO execution VALUES(?,?,?,?,NULL)', (project, request, fingerprint, 'started'))
            if intent is not None:
                db.execute('INSERT INTO capability_intent VALUES(?,?,?)',(project,request,json.dumps(intent)))
        return None

    def finish(self, project: str, request: str, result: dict[str, Any]) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE execution SET state=?,result=? WHERE project=? AND request=? AND state=?',
                       ('terminal', json.dumps(result), project, request, 'started'))


def receive_packet(connection: socket.socket) -> dict[str, Any]:
    connection.settimeout(10)
    data = bytearray()
    while b'\n' not in data:
        part = connection.recv(min(65536, MAX_PACKET + 1 - len(data)))
        if not part:
            raise ExecutionBlocked('incomplete broker packet')
        data.extend(part)
        if len(data) > MAX_PACKET:
            raise ExecutionBlocked('broker packet exceeds limit')
    head, tail = bytes(data).split(b'\n', 1)
    if tail:
        raise ExecutionBlocked('one request per authenticated connection')
    packet = json.loads(head)
    if not isinstance(packet, dict):
        raise ExecutionBlocked('broker request must be an object')
    return packet


def capture(spec: dict[str, Any], timeout: float, *, processes: Any = None, process: Any = None, start_guard: Any = None) -> dict[str, Any]:
    processes = processes or MacOSProcesses()
    if process is None:
        with start_guard() if start_guard else nullcontext():
            if processes.owned(spec['user']):
                raise ExecutionBlocked('execution identity has pre-existing processes')
            proc = subprocess.Popen(**spec)
    else:
        proc = process
    output: dict[str, tuple[bytes, bool]] = {}
    def read(name, pipe):
        kept = bytearray()
        total = 0
        try:
            while part := pipe.read(65536):
                total += len(part)
                kept.extend(part[:max(0, MAX_OUTPUT - len(kept))])
        finally:
            pipe.close()
            output[name] = (bytes(kept), total > MAX_OUTPUT)
    threads = [threading.Thread(target=read, args=(name, pipe), daemon=True)
               for name, pipe in (('stdout', proc.stdout), ('stderr', proc.stderr))]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        # The identity is reserved exclusively for this project execution.
        # Drain detached descendants too before recording a terminal receipt.
        processes.drain(spec['user'])
        proc.wait(timeout=10)
        for thread in threads:
            thread.join(10)
        if any(thread.is_alive() for thread in threads):
            raise ExecutionBlocked('execution streams remain ambiguous')
    except BaseException:
        processes.drain(spec['user'])
        raise
    return {'returncode': proc.returncode, 'timed_out': timed_out,
            **{name: value[0].decode('utf-8', errors='replace') for name, value in output.items()},
            **{name + '_truncated': value[1] for name, value in output.items()}}


class _BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid',
        'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles','pgid','jobc','tdev','tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]


class MacOSProcesses:
    """Kernel UID and birth identity, including detached descendants."""
    def __init__(self):
        if sys.platform != 'darwin':
            raise ExecutionBlocked('native process ownership unavailable')
        self.lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self.lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_listpids.restype = ctypes.c_int

    def identity(self, pid: int) -> tuple[int, int, int, int] | None:
        info = _BSDInfo()
        ctypes.set_errno(0)
        count = self.lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if count == 0 and ctypes.get_errno() in (0, 3):
            return None
        if count != ctypes.sizeof(info) or info.pid != pid:
            raise ExecutionBlocked('process ownership is unclassifiable')
        return info.uid, info.start_seconds, info.start_microseconds, info.status

    def owned(self, uid: int) -> dict[int, tuple[int, int, int, int]]:
        count = self.lib.proc_listpids(4, uid, None, 0)
        if count <= 0:
            raise ExecutionBlocked('cannot enumerate dedicated execution identity')
        buffer = (ctypes.c_int * (count // 4 + 128))()
        ctypes.set_errno(0)
        size = self.lib.proc_listpids(4, uid, buffer, ctypes.sizeof(buffer))
        if size == 0 and ctypes.get_errno() == 0:
            return {}
        if size <= 0 or size >= ctypes.sizeof(buffer):
            raise ExecutionBlocked('incomplete execution process inventory')
        result = {}
        for pid in list(buffer)[:size // 4]:
            if pid <= 0:
                continue
            identity = self.identity(pid)
            if identity and identity[0] == uid and identity[3] != 5:  # SZOMB
                result[pid] = identity
        return result

    def drain(self, uid: int, *, seconds: float = 5) -> None:
        deadline = time.monotonic() + seconds
        while True:
            owned = self.owned(uid)
            if not owned:
                return
            for pid, identity in owned.items():
                # Recheck birth identity immediately before a signal: a reused
                # PID or changed owner is never treated as our child.
                if self.identity(pid) == identity:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if time.monotonic() >= deadline:
                raise ExecutionBlocked('dedicated descendants did not quiesce')
            time.sleep(.05)
