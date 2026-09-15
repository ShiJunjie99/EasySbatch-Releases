"""Cross-platform, credential-free helpers used by the packaged Launcher."""

from __future__ import annotations

import ipaddress
import os
import re


def validate_username(value):
    if (not isinstance(value, str) or value == "root" or
            re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value) is None):
        raise ValueError("Invalid Linux username")
    return value


def validate_host(value):
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError("Invalid SSH host")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not value or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
        for part in value.split(".")
    ):
        raise ValueError("Invalid SSH host")
    return value


def sanitized_environment(environment):
    """Return the minimum client environment needed by system OpenSSH."""
    allowed = {
        "PATH", "HOME", "LC_ALL", "SSH_AUTH_SOCK",
        # Windows OpenSSH and process creation require these OS paths.
        "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    }
    result = {key: value for key, value in environment.items()
              if key in allowed and isinstance(value, str) and value}
    result.setdefault("LC_ALL", "C")
    if "PATH" not in result:
        result["PATH"] = os.defpath
    return result


def classify_transport(returncode, stderr):
    text = stderr.lower()
    if "host key verification failed" in text or "remote host identification has changed" in text:
        return "SSH_HOST_KEY_FAILED"
    if any(marker in text for marker in (
        "permission denied", "authentication failed", "too many authentication failures",
    )):
        return "SSH_AUTH_FAILED"
    return "SSH_CONNECTION_FAILED" if returncode == 255 else "WORKER_START_FAILED"


def ssh_install_hint():
    if os.name == "nt":
        return "请在 Windows 可选功能中安装 OpenSSH 客户端。"
    return "请安装系统 OpenSSH 客户端（通常为 openssh-client）。"
