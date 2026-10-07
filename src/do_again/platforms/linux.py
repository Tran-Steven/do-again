from __future__ import annotations

from pathlib import Path


def service_definition_path(name: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"
