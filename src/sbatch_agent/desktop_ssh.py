"""Restricted SSH adapters for the desktop Beta.

Both the legacy system-OpenSSH path and the in-app password path accept only
the fixed Slurm argv emitted by ``ClusterService`` and ``SlurmClient``. Neither
adapter exposes a general remote-command API. New desktop connections use an
in-memory Paramiko transport after the user approves the server public key.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
from pathlib import PurePosixPath
import json
import shlex
import shutil
import socket
import stat
import subprocess
import time

from pydantic import SecretStr

from .cluster import QUERIES
from .cluster_profile import ClusterProfile
from .launcher_client import sanitized_environment, validate_host, validate_username
from .runner import CommandResult, SlurmCommandError, _validate_timeout


MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 500
MAX_REMOTE_SCAN_TEXT_BYTES = 384 * 1024
MAX_REMOTE_SCAN_FILES = 500
SUPPORTED_HOST_KEY_TYPES = frozenset({
    "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521", "ssh-rsa",
})

_DIRECTORY_SCRIPT = r'''import json, os, stat, sys
p = sys.argv[1]
s = os.lstat(p)
if stat.S_ISLNK(s.st_mode) or not stat.S_ISDIR(s.st_mode):
    raise SystemExit(41)
rows = []
truncated = False
with os.scandir(p) as stream:
    for entry in stream:
        if len(rows) >= 500:
            truncated = True
            break
        try:
            info = entry.stat(follow_symlinks=False)
            mode = info.st_mode
            kind = "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "link" if stat.S_ISLNK(mode) else "other"
            rows.append({"name": entry.name, "kind": kind, "size": info.st_size if kind == "file" else None, "modified_ns": info.st_mtime_ns})
        except OSError:
            rows.append({"name": entry.name, "kind": "unavailable", "size": None, "modified_ns": None})
rows.sort(key=lambda row: (row["kind"] != "directory", row["name"].casefold(), row["name"]))
print(json.dumps({"path": p, "entries": rows, "truncated": truncated}, ensure_ascii=False, separators=(",", ":")))'''


# This program is sent as one fixed ``python3 -c`` argument.  The only dynamic
# argument is a separately quoted, UI-selected absolute directory.  It never
# imports project code, executes a project command, follows a symlink, or writes
# to the cluster.  The lower text budget leaves room for base64 and metadata
# inside the transport's fixed two-MiB response limit.
_PROJECT_SCAN_SCRIPT = r'''import base64, json, os, stat, sys
root = sys.argv[1]
IGNORE = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist", ".cache", ".pytest_cache", ".idea", ".vscode", ".sbatch-agent", "trajectory", "trajectories", "output", "outputs", "results", "checkpoints"}
BINARY = {".xtc", ".trr", ".dcd", ".tng", ".nc", ".h5", ".hdf5", ".npy", ".npz", ".pt", ".pth", ".ckpt", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".so", ".o", ".a", ".bin", ".db", ".sqlite", ".sqlite3", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".tpr", ".exe"}
TEXT = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cu", ".sh", ".bash", ".sbatch", ".slurm", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".in", ".dat", ".txt", ".gro", ".top", ".mdp", ".data", ".xyz", ".pdb", ".itp", ".py"}
SPECIAL = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "environment.yml", "environment.yaml", "pipfile", "poetry.lock", "cmakelists.txt", "makefile"}
MAX_FILES, MAX_DIRS, MAX_ENTRIES, MAX_DEPTH, MAX_FILE, MAX_TEXT = 500, 160, 1000, 6, 524288, 393216
flags_d = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
flags_f = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
parts = root.split("/")[1:]
fd = os.open("/", flags_d)
try:
    for part in parts:
        child = os.open(part, flags_d, dir_fd=fd)
        os.close(fd); fd = child
except Exception:
    os.close(fd); raise
files, skipped_dirs, warnings, limits = [], [], set(), set()
total = 0
directories = 0
stopped = False
git_present = False
def add(path, size, status, reason=None, mode=None, data=None):
    row = {"path": path, "size": size, "status": status, "reason": reason, "mode": mode, "data": None}
    if data is not None: row["data"] = base64.b64encode(data).decode("ascii")
    files.append(row)
def walk(current, rel="", depth=0):
    global directories, stopped, total, git_present
    if stopped: return
    if directories >= MAX_DIRS:
        limits.add("max_directories"); skipped_dirs.append(rel or "."); return
    directories += 1
    names = []
    try:
        with os.scandir(current) as stream:
            for entry in stream:
                if len(names) >= MAX_ENTRIES:
                    limits.add("max_directory_entries"); skipped_dirs.append(rel or "."); return
                names.append(entry.name)
    except OSError:
        warnings.add((rel or ".") + ": cannot list directory"); skipped_dirs.append(rel or "."); return
    for name in sorted(names, key=lambda value: (value.casefold(), value)):
        if len(files) >= MAX_FILES:
            limits.add("max_files"); stopped = True; return
        path = (rel + "/" + name).lstrip("/")
        try:
            name.encode("utf-8")
            info = os.stat(name, dir_fd=current, follow_symlinks=False)
        except (OSError, UnicodeError):
            add(path, None, "skipped", "metadata unavailable"); continue
        mode = info.st_mode
        if not rel and name == ".git": git_present = True
        if stat.S_ISDIR(mode):
            if name in IGNORE: skipped_dirs.append(path)
            elif depth >= MAX_DEPTH: limits.add("max_depth"); skipped_dirs.append(path)
            else:
                try:
                    child = os.open(name, flags_d, dir_fd=current)
                except OSError:
                    warnings.add(path + ": cannot safely open directory"); skipped_dirs.append(path); continue
                try: walk(child, path, depth + 1)
                finally: os.close(child)
            continue
        if stat.S_ISLNK(mode): add(path, info.st_size, "skipped", "symlink"); continue
        if not stat.S_ISREG(mode): add(path, info.st_size, "skipped", "not a regular file"); continue
        lower = name.lower(); suffix = os.path.splitext(lower)[1]
        relevant = lower.startswith(("readme", "usage", "install")) or lower in SPECIAL or suffix in TEXT or not suffix
        if suffix in BINARY or ".so." in lower: add(path, info.st_size, "skipped", "binary/archive/data extension", mode); continue
        if not relevant: add(path, info.st_size, "metadata_only", "non-target text type", mode); continue
        if info.st_size > MAX_FILE:
            limits.add("max_file_size"); add(path, info.st_size, "skipped", "file size limit", mode); continue
        if info.st_size > MAX_TEXT - total:
            limits.add("max_total_text_bytes"); add(path, info.st_size, "skipped", "remaining text budget", mode); continue
        try:
            handle = os.open(name, flags_f, dir_fd=current)
            before = os.fstat(handle)
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns): raise OSError()
            chunks, count = [], 0
            while count < before.st_size:
                chunk = os.read(handle, min(65536, before.st_size - count))
                if not chunk: break
                chunks.append(chunk); count += len(chunk)
            after = os.fstat(handle); os.close(handle)
            if count != before.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns): raise OSError()
            data = b"".join(chunks)
        except OSError:
            try: os.close(handle)
            except Exception: pass
            add(path, info.st_size, "skipped", "changed or unreadable", mode); continue
        if b"\x00" in data:
            add(path, len(data), "skipped", "binary content", mode); continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            add(path, len(data), "skipped", "not UTF-8 text", mode); continue
        if any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
            add(path, len(data), "skipped", "binary control bytes", mode); continue
        total += len(data); add(path, len(data), "read", None, mode, data)
try: walk(fd)
finally: os.close(fd)
for value in sorted(limits): warnings.add("scan limit reached: " + value)
print(json.dumps({"path": root, "files": files, "skipped_directories": sorted(set(skipped_dirs)), "git_present": git_present, "bytes_read": total, "warnings": sorted(warnings), "limits_reached": sorted(limits)}, ensure_ascii=False, separators=(",", ":")))'''


class DesktopSSHUnavailableError(RuntimeError):
    """The configured credential-free desktop SSH transport is unavailable."""


class DesktopSSHDirectoryError(RuntimeError):
    """A bounded, user-requested remote directory could not be listed."""


class DesktopSSHProjectScanError(RuntimeError):
    """A bounded, user-authorized remote project scan could not be completed."""


class DesktopSSHAuthenticationError(RuntimeError):
    """A password-only SSH login failed without exposing server diagnostics."""


@dataclass(frozen=True)
class SSHHostKey:
    """One public SSH server identity approved by the desktop user."""

    algorithm: str
    public_key: str
    fingerprint: str

    @classmethod
    def from_key(cls, key: object) -> "SSHHostKey":
        try:
            algorithm = key.get_name()  # type: ignore[attr-defined]
            raw = key.asbytes()  # type: ignore[attr-defined]
            public_key = key.get_base64()  # type: ignore[attr-defined]
        except Exception:
            raise ValueError("invalid SSH host key") from None
        if (
            algorithm not in SUPPORTED_HOST_KEY_TYPES
            or not isinstance(raw, bytes) or not 16 <= len(raw) <= 16 * 1024
            or not isinstance(public_key, str) or len(public_key) > 32 * 1024
        ):
            raise ValueError("unsupported SSH host key")
        fingerprint = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
        return cls(algorithm, public_key, f"SHA256:{fingerprint}")

    @classmethod
    def from_mapping(cls, value: object) -> "SSHHostKey":
        if not isinstance(value, dict) or set(value) != {"algorithm", "public_key", "fingerprint"}:
            raise ValueError("invalid SSH host key")
        algorithm, public_key, fingerprint = (
            value["algorithm"], value["public_key"], value["fingerprint"],
        )
        if (
            algorithm not in SUPPORTED_HOST_KEY_TYPES
            or not isinstance(public_key, str) or not isinstance(fingerprint, str)
            or len(public_key) > 32 * 1024 or len(fingerprint) > 128
        ):
            raise ValueError("invalid SSH host key")
        try:
            raw = base64.b64decode(public_key, validate=True)
        except ValueError:
            raise ValueError("invalid SSH host key") from None
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
        if not hmac.compare_digest(fingerprint, expected):
            raise ValueError("SSH host key fingerprint mismatch")
        return cls(algorithm, public_key, fingerprint)

    def to_mapping(self) -> dict[str, str]:
        return {
            "algorithm": self.algorithm,
            "public_key": self.public_key,
            "fingerprint": self.fingerprint,
        }


def _start_ssh_transport(host: str, port: int, *, timeout: float):
    """Start Paramiko only after input validation; import remains desktop-only."""
    validate_host(host)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid SSH port")
    connection = transport = None
    try:
        import paramiko

        connection = socket.create_connection((host, port), timeout=timeout)
        transport = paramiko.Transport(connection)
        transport.start_client(timeout=timeout)
        if not transport.is_active():
            raise OSError
        return transport
    except (ImportError, OSError, EOFError, socket.timeout):
        if transport is not None:
            transport.close()
        elif connection is not None:
            connection.close()
        raise DesktopSSHUnavailableError("SSH server is unavailable") from None
    except Exception:
        if transport is not None:
            transport.close()
        elif connection is not None:
            connection.close()
        raise DesktopSSHUnavailableError("SSH handshake failed") from None


def inspect_ssh_host_key(host: str, port: int, *, timeout: float = 12) -> dict[str, object]:
    """Read a server public key before any username or password is transmitted."""
    transport = _start_ssh_transport(host, port, timeout=timeout)
    try:
        key = SSHHostKey.from_key(transport.get_remote_server_key())
        return {"host": host, "ssh_port": port, "host_key": key.to_mapping()}
    finally:
        transport.close()


def _remote_directory_path(value: str) -> str:
    if (
        not isinstance(value, str) or len(value) > 1024 or not value.isprintable()
        or "\x00" in value or "\\" in value or value == "/"
    ):
        raise ValueError("remote directory must be a printable non-root absolute POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("remote directory must be a normalized absolute POSIX path")
    return value


def _directory_result(raw: bytes, expected_path: str) -> dict[str, object]:
    try:
        data = json.loads(_decode(raw))
    except (UnicodeError, json.JSONDecodeError):
        raise DesktopSSHDirectoryError("Remote directory response was invalid") from None
    if not isinstance(data, dict) or set(data) != {"path", "entries", "truncated"}:
        raise DesktopSSHDirectoryError("Remote directory response was invalid")
    rows = data["entries"]
    if data["path"] != expected_path or not isinstance(data["truncated"], bool) or not isinstance(rows, list):
        raise DesktopSSHDirectoryError("Remote directory response was invalid")
    if len(rows) > MAX_DIRECTORY_ENTRIES:
        raise DesktopSSHDirectoryError("Remote directory response exceeded the entry limit")
    clean = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "kind", "size", "modified_ns"}:
            raise DesktopSSHDirectoryError("Remote directory response was invalid")
        name, kind = row["name"], row["kind"]
        if (
            not isinstance(name, str) or not name or name in {".", ".."}
            or "/" in name or "\x00" in name or not name.isprintable()
            or kind not in {"directory", "file", "link", "other", "unavailable"}
        ):
            raise DesktopSSHDirectoryError("Remote directory response was invalid")
        for field in ("size", "modified_ns"):
            value = row[field]
            if value is not None and (type(value) is not int or value < 0):
                raise DesktopSSHDirectoryError("Remote directory response was invalid")
        clean.append(dict(row))
    return {"path": expected_path, "entries": clean, "truncated": data["truncated"]}


def _project_scan_result(raw: bytes, expected_path: str) -> dict[str, object]:
    try:
        data = json.loads(_decode(raw))
    except (UnicodeError, json.JSONDecodeError):
        raise DesktopSSHProjectScanError("Remote project scan response was invalid") from None
    expected = {
        "path", "files", "skipped_directories", "git_present", "bytes_read",
        "warnings", "limits_reached",
    }
    if not isinstance(data, dict) or set(data) != expected or data["path"] != expected_path:
        raise DesktopSSHProjectScanError("Remote project scan response was invalid")
    if (
        not isinstance(data["files"], list) or len(data["files"]) > MAX_REMOTE_SCAN_FILES
        or type(data["bytes_read"]) is not int
        or not 0 <= data["bytes_read"] <= MAX_REMOTE_SCAN_TEXT_BYTES
        or type(data["git_present"]) is not bool
    ):
        raise DesktopSSHProjectScanError("Remote project scan response exceeded its limits")
    for field, limit in (("skipped_directories", 256), ("warnings", 256), ("limits_reached", 16)):
        values = data[field]
        if not isinstance(values, list) or len(values) > limit or any(
            not isinstance(value, str) or len(value) > 2048 or not value.isprintable()
            for value in values
        ):
            raise DesktopSSHProjectScanError("Remote project scan response was invalid")
    total = 0
    clean = []
    for row in data["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "size", "status", "reason", "mode", "data"}:
            raise DesktopSSHProjectScanError("Remote project scan response was invalid")
        relative = row["path"]
        path = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            path is None or path.is_absolute() or str(path) != relative or ".." in path.parts
            or not relative or len(relative) > 2048 or not relative.isprintable()
            or row["status"] not in {"read", "skipped", "metadata_only"}
        ):
            raise DesktopSSHProjectScanError("Remote project scan response was invalid")
        for field in ("size", "mode"):
            value = row[field]
            if value is not None and (type(value) is not int or value < 0):
                raise DesktopSSHProjectScanError("Remote project scan response was invalid")
        reason = row["reason"]
        if reason is not None and (not isinstance(reason, str) or len(reason) > 256 or not reason.isprintable()):
            raise DesktopSSHProjectScanError("Remote project scan response was invalid")
        encoded = row["data"]
        if row["status"] == "read":
            if not isinstance(encoded, str):
                raise DesktopSSHProjectScanError("Remote project scan response was invalid")
            try:
                content = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise DesktopSSHProjectScanError("Remote project scan response was invalid") from None
            if len(content) != row["size"]:
                raise DesktopSSHProjectScanError("Remote project scan response was invalid")
            total += len(content)
        elif encoded is not None:
            raise DesktopSSHProjectScanError("Remote project scan response was invalid")
        else:
            content = None
        clean.append({**row, "data": content})
    if total != data["bytes_read"] or total > MAX_REMOTE_SCAN_TEXT_BYTES:
        raise DesktopSSHProjectScanError("Remote project scan response was invalid")
    return {**data, "files": clean}


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

    def list_directory(self, path: str, *, timeout: float = 15) -> dict[str, object]:
        """List one user-selected remote directory without reading file contents."""
        _validate_timeout(timeout)
        path = _remote_directory_path(path)
        command = ("python3", "-c", _DIRECTORY_SCRIPT, path)
        try:
            completed = self._process_run(
                list(self._ssh_argv(command)),
                input=None,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=sanitized_environment(os.environ),
            )
        except subprocess.TimeoutExpired:
            raise DesktopSSHDirectoryError("Remote directory request timed out") from None
        except OSError:
            raise DesktopSSHDirectoryError("System OpenSSH could not be started") from None
        stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout or "").encode()
        stderr = completed.stderr if isinstance(completed.stderr, bytes) else str(completed.stderr or "").encode()
        if len(stdout) > MAX_COMMAND_OUTPUT_BYTES or len(stderr) > MAX_COMMAND_OUTPUT_BYTES:
            raise DesktopSSHDirectoryError("Remote directory response exceeded the size limit")
        if completed.returncode != 0:
            raise DesktopSSHDirectoryError("Remote directory is unavailable or is not a regular directory")
        return _directory_result(stdout, path)

    def scan_project(self, path: str, *, timeout: float = 30) -> dict[str, object]:
        """Read a bounded text snapshot of one UI-selected remote directory."""
        _validate_timeout(timeout)
        path = _remote_directory_path(path)
        command = ("python3", "-c", _PROJECT_SCAN_SCRIPT, path)
        try:
            completed = self._process_run(
                list(self._ssh_argv(command)), input=None, capture_output=True,
                timeout=timeout, check=False, env=sanitized_environment(os.environ),
            )
        except subprocess.TimeoutExpired:
            raise DesktopSSHProjectScanError("Remote project scan timed out") from None
        except OSError:
            raise DesktopSSHProjectScanError("System OpenSSH could not be started") from None
        stdout = completed.stdout if isinstance(completed.stdout, bytes) else str(completed.stdout or "").encode()
        stderr = completed.stderr if isinstance(completed.stderr, bytes) else str(completed.stderr or "").encode()
        if len(stdout) > MAX_COMMAND_OUTPUT_BYTES or len(stderr) > MAX_COMMAND_OUTPUT_BYTES:
            raise DesktopSSHProjectScanError("Remote project scan response exceeded the size limit")
        if completed.returncode != 0:
            raise DesktopSSHProjectScanError("Remote project is unavailable or cannot be scanned safely")
        return _project_scan_result(stdout, path)


class DesktopSSHPasswordRunner:
    """Password-only SSH adapter backed by one in-memory Paramiko transport.

    The password is supplied through the local stdio protocol for the current
    desktop session. It is never written to configuration, argv, environment,
    logs, or the server preset.
    """

    def __init__(
        self, *, profile: ClusterProfile, username: str, password: SecretStr,
        host_key: SSHHostKey,
    ):
        if not isinstance(profile, ClusterProfile) or not isinstance(password, SecretStr):
            raise ValueError("validated cluster profile and password are required")
        if not isinstance(host_key, SSHHostKey):
            raise ValueError("an approved SSH host key is required")
        self.profile = profile
        self.username = validate_username(username)
        self._password = password
        self.host_key = host_key
        self._transport = None

    def connect(self, *, timeout: float = 12) -> None:
        if self._transport is not None and self._transport.is_active() and self._transport.is_authenticated():
            return
        transport = _start_ssh_transport(self.profile.host, self.profile.ssh_port, timeout=timeout)
        try:
            actual = SSHHostKey.from_key(transport.get_remote_server_key())
            if (
                actual.algorithm != self.host_key.algorithm
                or not hmac.compare_digest(actual.public_key, self.host_key.public_key)
            ):
                raise DesktopSSHAuthenticationError("SSH server identity changed")
            raw_password = self._password.get_secret_value()
            try:
                transport.auth_password(
                    self.username, raw_password, event=None, fallback=True,
                )
            finally:
                del raw_password
            if not transport.is_authenticated():
                raise DesktopSSHAuthenticationError("SSH username or password is incorrect")
            transport.set_keepalive(5)
            self._transport = transport
        except DesktopSSHAuthenticationError:
            transport.close()
            raise
        except Exception:
            transport.close()
            raise DesktopSSHAuthenticationError("SSH username or password is incorrect") from None

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _execute(
        self, command: tuple[str, ...], *, stdin: bytes | None, timeout: float,
    ) -> tuple[int, bytes, bytes]:
        _validate_timeout(timeout)
        self.connect(timeout=min(12, timeout))
        deadline = time.monotonic() + timeout
        channel = None
        try:
            channel = self._transport.open_session(timeout=min(12, timeout))
            channel.settimeout(min(12, timeout))
            channel.exec_command(shlex.join(command))
            if stdin is not None:
                channel.sendall(stdin)
            channel.shutdown_write()
            stdout = bytearray()
            stderr = bytearray()
            while True:
                progressed = False
                while channel.recv_ready():
                    stdout.extend(channel.recv(65536))
                    progressed = True
                    if len(stdout) > MAX_COMMAND_OUTPUT_BYTES:
                        raise OSError("SSH output exceeded the size limit")
                while channel.recv_stderr_ready():
                    stderr.extend(channel.recv_stderr(65536))
                    progressed = True
                    if len(stderr) > MAX_COMMAND_OUTPUT_BYTES:
                        raise OSError("SSH output exceeded the size limit")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    return channel.recv_exit_status(), bytes(stdout), bytes(stderr)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                if not progressed:
                    time.sleep(0.01)
        finally:
            if channel is not None:
                channel.close()

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        _validate_timeout(timeout)
        if isinstance(argv, (str, bytes)) or not argv or any(
            not isinstance(item, str) or "\x00" in item for item in argv
        ):
            raise ValueError("argv must be a nonempty string sequence without NUL")
        original = tuple(argv)
        command, stdin = _allowed_remote_command(original)
        try:
            returncode, stdout, stderr = self._execute(command, stdin=stdin, timeout=timeout)
        except TimeoutError:
            result = CommandResult(original, None, "", "")
            raise SlurmCommandError(
                f"remote {original[0]} timed out after {timeout}s; submission outcome may be unknown",
                result,
            ) from None
        except (DesktopSSHAuthenticationError, DesktopSSHUnavailableError, OSError):
            result = CommandResult(original, None, "", "")
            raise SlurmCommandError("Password SSH connection failed", result) from None
        return CommandResult(original, returncode, _decode(stdout), _decode(stderr))

    def list_directory(self, path: str, *, timeout: float = 15) -> dict[str, object]:
        path = _remote_directory_path(path)
        try:
            returncode, stdout, _ = self._execute(
                ("python3", "-c", _DIRECTORY_SCRIPT, path), stdin=None, timeout=timeout,
            )
        except TimeoutError:
            raise DesktopSSHDirectoryError("Remote directory request timed out") from None
        except (DesktopSSHAuthenticationError, DesktopSSHUnavailableError, OSError):
            raise DesktopSSHDirectoryError("Password SSH connection failed") from None
        if returncode != 0:
            raise DesktopSSHDirectoryError("Remote directory is unavailable or is not a regular directory")
        return _directory_result(stdout, path)

    def scan_project(self, path: str, *, timeout: float = 30) -> dict[str, object]:
        path = _remote_directory_path(path)
        try:
            returncode, stdout, _ = self._execute(
                ("python3", "-c", _PROJECT_SCAN_SCRIPT, path), stdin=None, timeout=timeout,
            )
        except TimeoutError:
            raise DesktopSSHProjectScanError("Remote project scan timed out") from None
        except (DesktopSSHAuthenticationError, DesktopSSHUnavailableError, OSError):
            raise DesktopSSHProjectScanError("Password SSH connection failed") from None
        if returncode != 0:
            raise DesktopSSHProjectScanError("Remote project is unavailable or cannot be scanned safely")
        return _project_scan_result(stdout, path)
