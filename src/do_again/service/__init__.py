"""Public service entrypoints, imported lazily for sealed installer preflight.

The root installer uses the system Python to validate a verified source bundle.
Importing the unrelated service runtime here would eagerly load tomllib and
browser dependencies before the bundled Python runtime is selected.
"""
from __future__ import annotations

__all__ = [
    "ServiceError", "install_service", "restart_service", "run_foreground",
    "service_status", "stop_service", "uninstall_service",
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import runtime
    return getattr(runtime, name)
