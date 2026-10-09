from __future__ import annotations

import platform

from .base import PlatformInfo


def detect_platform() -> PlatformInfo:
    name = platform.system().lower()
    if name == "darwin":
        return PlatformInfo(name="macos", service_manager="launchd", supported=True)
    if name == "linux":
        return PlatformInfo(name="linux", service_manager="systemd", supported=True)
    if name == "windows":
        return PlatformInfo(name="windows", service_manager="task-scheduler", supported=True)
    return PlatformInfo(name=name or "unknown", service_manager="unknown", supported=False)
