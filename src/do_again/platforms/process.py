from __future__ import annotations

import os


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel.CloseHandle(handle)


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True
