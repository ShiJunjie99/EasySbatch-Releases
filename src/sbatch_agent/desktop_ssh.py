"""Credential-free system-OpenSSH adapter for the desktop Beta.

The adapter accepts only the fixed Slurm argv emitted by ``ClusterService``
and ``SlurmClient``.  It does not expose a general remote-command API.  SSH
keys, agents, host verification, and authentication prompts remain owned by
the operating-system OpenSSH client.
"""

from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess

from .cluster import QUERIES
from .cluster_profile import ClusterProfile
from .launcher_client import sanitized_environment, validate_username
from .runner import CommandResult, SlurmCommandError, _validate_timeout


MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024


class DesktopSSHUnavailableError(RuntimeError):
    """The configured credential-free desktop SSH transport is unavailable."""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _read_submission_script(path_value: str) -> bytes:
    path = Path(path_value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("submission script path must be absolute")
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise OSError("submission script cannot be opened") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError("submission script must be a regular non-link file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("submission script changed while being opened")
        payload = bytearray()
        while len(payload) <= MAX_SCRIPT_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_SCRIPT_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        closed = os.fstat(descriptor)
        if len(payload) > MAX_SCRIPT_BYTES:
            raise OSError("submission script exceeds the desktop size limit")
        if len(payload) != opened.st_size or (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
        ) != (
            closed.st_dev, closed.st_ino, closed.st_size, closed.st_mtime_ns
        ):
            raise OSError("submission script changed while being read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _allowed_remote_command(argv: tuple[str, ...]) -> tuple[tuple[str, ...], bytes | None]:
    """Map an existing Slurm adapter argv to one fixed remote command."""
    if argv in set(QUERIES.values()):
        return argv, None
    if len(argv) == 3 and argv[:2] == ("sbatch", "--parsable"):
        return ("sbatch", "--parsable"), _read_submission_script(argv[2])
    if (
        len(argv) == 6
        and argv[:4] == ("squeue", "--local", "--noheader", "--states=all")
        and argv[4].startswith("--jobs=")
        and argv[4][7:].isdigit()
        and argv[5] == "--format=%i|%T|%r|%P|%u"
    ):
        return argv, None
    if (
        len(argv) == 7
        and argv[:4] == ("sacct", "--local", "--noheader", "--parsable2")
        and argv[4] == "--allocations"
        and argv[5].startswith("--jobs=")
        and argv[5][7:].isdigit()
        and argv[6] == "--format=JobID,State%80,ExitCode,Start,End"
    ):
        return argv, None
    raise ValueError("desktop SSH adapter refused a non-allowlisted Slurm command")


class DesktopSSHSlurmRunner:
    """Run fixed Slurm commands through system OpenSSH without storing credentials.

    Batch mode is deliberate: the Electron backend has no trustworthy terminal
    prompt surface.  Users authenticate with a normal key or ssh-agent and can
    verify the host once using the operating-system ``ssh`` client.
    """

    def __init__(
        self, *, profile: ClusterProfile, username: str,
        ssh_executable: str | None = None, process_run=subprocess.run,
    ):
        if not isinstance(profile, ClusterProfile):
            raise ValueError("profile must be a ClusterProfile")
        self.profile = profile
        self.username = validate_username(username)
        executable = ssh_executable or shutil.which("ssh")
        if not executable or not Path(executable).is_absolute():
            raise DesktopSSHUnavailableError("System OpenSSH client is unavailable")
        self.ssh_executable = str(Path(executable))
        self._process_run = process_run

    def _ssh_argv(self, command: tuple[str, ...]) -> tuple[str, ...]:
        # OpenSSH sends one command string to the remote login shell.  Every
        # token is shell-quoted, and ``command`` has already passed the strict
        # allowlist above; no user-provided command fragment reaches this path.
        remote = shlex.join(command)
        return (
            self.ssh_executable,
            "-T", "-p", str(self.profile.ssh_port),
            "-o", f"Hostname={self.profile.host}",
            "-o", f"User={self.username}",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no",
            "-o", "CheckHostIP=yes",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ControlPersist=no",
            "-o", "AddKeysToAgent=no",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "PermitLocalCommand=no",
            "-o", "SendEnv=-*",
            "-o", "ConnectTimeout=12",
            "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "--", self.profile.host, remote,
        )

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        _validate_timeout(timeout)
        if isinstance(argv, (str, bytes)) or not argv or any(
            not isinstance(item, str) or "\x00" in item for item in argv
        ):
            raise ValueError("argv must be a nonempty string sequence without NUL")
        original = tuple(argv)
        command, stdin = _allowed_remote_command(original)
        try:
            completed = self._process_run(
                list(self._ssh_argv(command)),
                input=stdin,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=sanitized_environment(os.environ),
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                original, None,
                _decode(exc.stdout or b"") if isinstance(exc.stdout, bytes) else exc.stdout or "",
                _decode(exc.stderr or b"") if isinstance(exc.stderr, bytes) else exc.stderr or "",
            )
            raise SlurmCommandError(
                f"remote {original[0]} timed out after {timeout}s; submission outcome may be unknown",
                result,
            ) from exc
        except OSError as exc:
            result = CommandResult(original, None, "", "")
            raise SlurmCommandError("System OpenSSH could not be started", result) from exc
        stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout or "").encode()
        stderr = completed.stderr if isinstance(completed.stderr, bytes) else str(completed.stderr or "").encode()
        if len(stdout) > MAX_COMMAND_OUTPUT_BYTES or len(stderr) > MAX_COMMAND_OUTPUT_BYTES:
            result = CommandResult(original, completed.returncode, "", "")
            raise SlurmCommandError("Remote Slurm response exceeded the desktop size limit", result)
        return CommandResult(original, completed.returncode, _decode(stdout), _decode(stderr))
