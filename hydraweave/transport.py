"""Select the configured transport for a machine."""
from __future__ import annotations

from pathlib import Path


def for_machine(machine, config_path=None, *, timeout=None, known_hosts=None):
    if machine.access == "agent":
        from .onboarding import Client
        if config_path is None:
            from .cli import config_path as default_config_path
            config_path = default_config_path()
        return Client(machine, Path(config_path))
    if machine.access == "ssh":
        from . import remote
        if known_hosts is None:
            from . import secrets
            known_hosts = secrets._directory().parent / "known_hosts"
        return remote.Remote(machine, known_hosts)
    raise ValueError(f"{machine.id}: unsupported transport {machine.access!r}")
