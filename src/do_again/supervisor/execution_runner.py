"""Immutable child shim, executed only after root helper drops UID/groups."""
from __future__ import annotations
import os
import ctypes
import resource
import sys


def kernel_groups() -> set[int]:
    """Darwin's legacy POSIX symbol reads the effective kernel credential.

    Modern Python os.getgroups uses a directory-backed user access list on
    macOS and is not an attestation of setgroups. No directory IPC is granted.
    """
    if sys.platform != 'darwin':
        raise SystemExit('native group attestation unavailable')
    library=ctypes.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True)
    query=library.getgroups
    query.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_uint)]
    query.restype=ctypes.c_int
    buffer=(ctypes.c_uint*128)()
    count=query(len(buffer),buffer)
    if count<0 or count>len(buffer):
        raise SystemExit('kernel group attestation failed')
    return set(buffer[:count])


def main(argv: list[str]) -> int:
    uid, cpu_seconds = int(argv[0]), int(argv[1])
    if uid <= 0 or os.getuid() != uid or os.geteuid() != uid or kernel_groups() - {os.getgid()}:
        raise SystemExit('execution identity or supplementary groups are unsafe')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))
    command = argv[2:]
    if not command or not os.path.isabs(command[0]):
        raise SystemExit('resolved executable required')
    os.execve(command[0], command, os.environ)
    return 1


if __name__ == '__main__':
    main(sys.argv[1:])
