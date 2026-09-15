"""Public Launcher build and protocol metadata; contains no runtime secret."""

from __future__ import annotations

from importlib import resources
import json
import re


LAUNCHER_VERSION = "0.1.0-alpha.5"
PROTOCOL_VERSION = 2
AGENT_LIFECYCLE_VERSION = 1


def build_commit():
    try:
        raw = resources.files("sbatch_agent").joinpath("launcher_build.json").read_bytes()
        if len(raw) > 1024:
            raise ValueError
        value = json.loads(raw.decode("ascii"))
        commit = value["commit"]
        if set(value) != {"commit"} or not isinstance(commit, str) or not re.fullmatch(
            r"(?:source|[0-9a-f]{7,40})", commit,
        ):
            raise ValueError
        return commit
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return "unknown"


def version_text():
    return (f"EasySbatch Launcher {LAUNCHER_VERSION}\n"
            f"protocol {PROTOCOL_VERSION}\n"
            f"agent lifecycle {AGENT_LIFECYCLE_VERSION}\n"
            f"commit {build_commit()}")
