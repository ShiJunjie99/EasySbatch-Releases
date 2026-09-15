"""Portable, non-secret configuration for one SSH/HPC cluster."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .launcher_client import validate_host


@dataclass(frozen=True, repr=False)
class ClusterProfile:
    """The public cluster boundary.

    Authentication material, usernames, known-host files and AI credentials
    deliberately live outside this object.  A profile can therefore be
    stored in a user-local config without turning into a secret container.
    """

    id: str
    display_name: str
    host: str
    ssh_port: int = 22

    def __post_init__(self):
        if (not isinstance(self.id, str) or
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.id) is None or
                not isinstance(self.display_name, str) or
                not 1 <= len(self.display_name) <= 128 or
                not self.display_name.isprintable()):
            raise ValueError("Invalid cluster profile identity")
        validate_host(self.host)
        if type(self.ssh_port) is not int or not 1 <= self.ssh_port <= 65535:
            raise ValueError("Invalid cluster SSH port")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Invalid cluster profile")
        if set(value) != {"id", "display_name", "host", "ssh_port"}:
            raise ValueError("Invalid cluster profile")
        return cls(**value)

    def to_mapping(self):
        return {
            "id": self.id,
            "display_name": self.display_name,
            "host": self.host,
            "ssh_port": self.ssh_port,
        }

    def __repr__(self):
        return (f"ClusterProfile(id={self.id!r}, display_name={self.display_name!r}, "
                f"host={self.host!r}, ssh_port={self.ssh_port!r})")
