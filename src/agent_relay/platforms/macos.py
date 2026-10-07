from __future__ import annotations

from pathlib import Path


def service_definition_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
