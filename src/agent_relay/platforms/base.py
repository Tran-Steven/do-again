from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformInfo:
    name: str
    service_manager: str
    supported: bool
