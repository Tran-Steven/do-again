"""Local Git transaction primitives for the dedicated-identity broker.

This module never starts Git. Its command plans must run through the native
execution boundary, after draining the project UID and reserving a durable
execution identity. Candidate metadata is not authoritative until guarded
promotion. Network and publication are deliberately absent from this capability.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .macos_execution import ExecutionBlocked

SHA = re.compile(r'[0-9a-f]{40}')
BRANCH = re.compile(r'do-again/[a-z0-9][a-z0-9-]{0,79}')
CONFIG = b'''[core]
 repositoryformatversion = 0
 bare = false
 hooksPath = /dev/null
 fsmonitor = false
 autocrlf = false
[gc]
 auto = 0
[maintenance]
 auto = false
[protocol]
 allow = never
[commit]
 gpgSign = false
[user]
 name = Do Again
 email = do-again@users.noreply.github.com
'''
# Only native Git data is imported. In particular: no config, hooks, alternates,
# grafts, replace refs, attributes, shallow state, extensions or worktree links.
PACK = re.compile(r'pack-[0-9a-f]{40}\.(pack|idx|rev)')
LOOSE = re.compile(r'[0-9a-f]{2}/[0-9a-f]{38}')
REF = re.compile(r'refs/(heads|tags)/[A-Za-z0-9._/-]+')


def _read(path: Path, limit: int) -> bytes:
    if any(ancestor.is_symlink() for ancestor in path.parents):
        raise ExecutionBlocked('Git metadata ancestor is aliased')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ExecutionBlocked('Git metadata is unavailable or aliased') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ExecutionBlocked('Git metadata is aliased, special or oversized')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ExecutionBlocked('Git metadata exceeds the transaction budget')
        return content
    finally:
        os.close(fd)


def _safe_relative(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and '\\' not in value
            and len(value.encode()) <= 4096
            and '\x00' not in value and not value.startswith('-')
            and not PurePosixPath(value).is_absolute()
            and all(part not in {'', '.', '..', '.git'} for part in value.split('/')))


def _data_file(relative: str) -> bool:
    if relative in {'HEAD', 'packed-refs', 'index'}:
        return True
    if relative.startswith('objects/'):
        name = relative[8:]
        return bool(LOOSE.fullmatch(name) or
                    (name.startswith('pack/') and PACK.fullmatch(name[5:])))
    return bool(REF.fullmatch(relative) and '..' not in relative and '//' not in relative)


def copy_git_data(source: Path, destination: Path, *, byte_budget: int = 512 * 1024 * 1024) -> None:
    """Copy inert Git data, without executing or retaining repository configuration.

    Caller holds the project writer lease; no project UID process may be alive.
    Source ancestors and destination must be supervisor-controlled. Never import
    a user worktree while another process can rename its directories.
    """
    if source.is_symlink() or not source.is_dir() or destination.exists() or destination.is_symlink():
        raise ExecutionBlocked('Git transaction requires an unaliased fresh directory')
    for ancestor in source.parents:
        if ancestor.is_symlink():
            raise ExecutionBlocked('Git source ancestor is aliased')
    destination.mkdir(mode=0o700)
    remaining = byte_budget
    try:
        for directory, directories, files in os.walk(source, followlinks=False):
            for name in directories + files:
                path = Path(directory) / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or (not stat.S_ISDIR(info.st_mode)
                        and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                    raise ExecutionBlocked('Git metadata contains an alias or special file')
            for name in files:
                path = Path(directory) / name
                relative = path.relative_to(source).as_posix()
                if not _data_file(relative):
                    continue
                content = _read(path, remaining)
                remaining -= len(content)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(content)
                target.chmod(0o600)
        (destination / 'config').write_bytes(CONFIG)
        for name in ('objects', 'refs'):
            (destination / name).mkdir(exist_ok=True, mode=0o700)
    except BaseException:
        # Preserve failed scratch evidence for diagnosis; never promote it.
        raise


@dataclass(frozen=True)
class GitTransaction:
    """A project-bound, local-only plan. Branch is trusted task-reservation input."""
    git: Path
    worktree: Path
    metadata: Path
    expected_head: str
    branch: str

    def __post_init__(self) -> None:
        if (not self.git.is_absolute() or not self.worktree.is_absolute()
                or not self.metadata.is_absolute() or self.metadata == self.worktree / '.git'
                or not isinstance(self.expected_head, str) or not SHA.fullmatch(self.expected_head)
                or not isinstance(self.branch, str) or not BRANCH.fullmatch(self.branch)):
            raise ExecutionBlocked('invalid trusted Git transaction binding')

    def _command(self, *arguments: str) -> list[str]:
        # Never inherit PATH, GIT_* or credential variables from scripts. The
        # native executor supplies the clean environment, UID and no-network rule.
        return [str(self.git), '--no-pager', '--no-replace-objects',
                '--literal-pathspecs', f'--git-dir={self.metadata}',
                f'--work-tree={self.worktree}', *arguments]

    def prepare(self, source: Path) -> None:
        copy_git_data(source, self.metadata)
        self.check_head(self.expected_head)
        if (self.metadata / 'config').read_bytes() != CONFIG:
            raise ExecutionBlocked('Git configuration was not sanitized')

    def check_head(self, expected: str) -> None:
        head = _read(self.metadata / 'HEAD', 4096).decode('ascii').strip()
        if head.startswith('ref: '):
            ref = head[5:]
            if not REF.fullmatch(ref) or '..' in ref or '//' in ref:
                raise ExecutionBlocked('Git HEAD reference is invalid')
            target = self.metadata / ref
            if target.exists():
                head = _read(target, 4096).decode('ascii').strip()
            else:
                head = ''
                packed = self.metadata / 'packed-refs'
                if packed.exists():
                    for line in _read(packed, 1024 * 1024).decode('ascii').splitlines():
                        fields = line.split()
                        if len(fields) == 2 and fields[1] == ref:
                            head = fields[0]
        if head != expected or not SHA.fullmatch(head):
            raise ExecutionBlocked('Git transaction head changed')

    def status_command(self) -> list[str]:
        return self._command('status', '--porcelain=v1', '-z', '--untracked-files=normal')

    def commit_commands(self, paths: list[str], message: str) -> list[list[str]]:
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 128
                or any(not _safe_relative(path) for path in paths)
                or len(set(paths)) != len(paths)):
            raise ExecutionBlocked('commit requires exact relative paths')
        if not isinstance(message, str) or not message.strip() or '\x00' in message or len(message.encode()) > 8192:
            raise ExecutionBlocked('invalid commit message')
        # Rebuild the index from the exact expected head. Imported staged edits
        # cannot silently join the commit. Do not checkout or modify source files.
        return [self._command('read-tree', self.expected_head),
                self._command('symbolic-ref', 'HEAD', f'refs/heads/{self.branch}'),
                self._command('update-ref', f'refs/heads/{self.branch}', self.expected_head),
                self._command('add', '--', *paths),
                self._command('commit', '--no-verify', '--no-gpg-sign', '-m', message),
                self._command('rev-parse', '--verify', 'HEAD')]

    def validate_candidate(self, head: str, sealed: Path) -> None:
        """Re-copy the candidate to a fresh supervisor-owned directory before promotion."""
        if not isinstance(head, str) or not SHA.fullmatch(head):
            raise ExecutionBlocked('Git did not produce an exact commit identity')
        self.check_head(head)
        if _read(self.metadata / 'config', 16384) != CONFIG:
            raise ExecutionBlocked('candidate Git configuration changed')
        if _read(self.metadata / 'HEAD', 4096).strip() != f'ref: refs/heads/{self.branch}'.encode():
            raise ExecutionBlocked('candidate changed the reserved branch')
        copy_git_data(self.metadata, sealed)
