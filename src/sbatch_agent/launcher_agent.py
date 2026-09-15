"""Persistent local Agent for the SSH-first Launcher.

The Controller is intentionally short lived.  This module owns the one system
OpenSSH process, the Web tunnel, Worker protocol and optional AI byte stream.
The local protocol is deliberately tiny and never accepts a command to run.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import getpass
import hashlib
import json
import os
from pathlib import Path
import select
import secrets
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import webbrowser

from .launcher_client import sanitized_environment, validate_username
from .launcher_version import AGENT_LIFECYCLE_VERSION
from .credential_store import CredentialStoreError, create_ai_credential_manager, validate_api_key


AGENT_PROTOCOL_VERSION = 1
MAX_CONTROL_MESSAGE = 8192
CONTROL_OPERATIONS = frozenset({"STATUS", "OPEN_BROWSER", "STOP", "AUTH"})
AI_CONTROL_OPERATIONS = frozenset({"AI_STATUS", "AI_CONFIGURE", "AI_DELETE", "AI_TEST"})
ALL_CONTROL_OPERATIONS = CONTROL_OPERATIONS | AI_CONTROL_OPERATIONS
AGENT_STATES = frozenset({
    "STARTING", "AUTHENTICATING", "CONNECTING", "READY", "DEGRADED",
    "DISCONNECTING", "STOPPED", "ERROR",
})


class AgentError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class AgentVersionError(AgentError):
    pass


@dataclass(frozen=True)
class RuntimePaths:
    directory: Path
    endpoint: str
    lock_file: Path


def runtime_paths(*, environment=None, uid=None, platform=None):
    """Return a deterministic, per-local-user Agent endpoint."""
    environment = os.environ if environment is None else environment
    platform = os.name if platform is None else platform
    if platform == "nt":
        base = Path(environment.get("LOCALAPPDATA") or
                    environment.get("TEMP") or Path.home())
        directory = base / "EasySbatch" / "runtime"
        identity = (environment.get("USERPROFILE") or getpass.getuser()).encode(
            "utf-8", errors="replace",
        )
        suffix = hashlib.sha256(identity).hexdigest()[:16]
        return RuntimePaths(
            directory=directory,
            endpoint=str(directory / f"agent-v1-{suffix}.json"),
            lock_file=directory / "agent-v1.lock",
        )

    local_uid = os.getuid() if uid is None else uid
    xdg = environment.get("XDG_RUNTIME_DIR")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path("/tmp")
    name = "easysbatch" if xdg else f"easysbatch-agent-{local_uid}"
    directory = base / name
    return RuntimePaths(
        directory=directory,
        endpoint=str(directory / "agent-v1.sock"),
        lock_file=directory / "agent-v1.lock",
    )


def ensure_private_runtime(paths, *, uid=None):
    paths.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        local_uid = os.getuid() if uid is None else uid
        info = paths.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                info.st_uid != local_uid):
            raise AgentError("AGENT_RUNTIME_UNSAFE")
        if stat.S_IMODE(info.st_mode) != 0o700:
            # This changes only EasySbatch's own leaf runtime directory.
            paths.directory.chmod(0o700)
    return paths


def user_ssh_config(environment=None, *, platform=None, uid=None):
    """Select only the current user's OpenSSH config, or OpenSSH's `none`.

    Supplying ``-F`` also prevents an unrelated unsafe system include from
    aborting this fixed-target Launcher before authentication.  The user's
    known_hosts, default identities and ssh-agent behavior remain OpenSSH's.
    """
    environment = os.environ if environment is None else environment
    platform = os.name if platform is None else platform
    home = environment.get("USERPROFILE" if platform == "nt" else "HOME")
    if not isinstance(home, str) or not home:
        return "none"
    candidate = Path(home) / ".ssh" / "config"
    if not candidate.exists():
        return "none"
    if platform != "nt":
        local_uid = os.getuid() if uid is None else uid
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                info.st_uid != local_uid or stat.S_IMODE(info.st_mode) & 0o022):
            raise AgentError("SSH_USER_CONFIG_UNSAFE")
    return candidate


def _encode_message(message):
    if not isinstance(message, dict):
        raise AgentError("AGENT_PROTOCOL_INVALID")
    raw = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if not raw or len(raw) > MAX_CONTROL_MESSAGE:
        raise AgentError("AGENT_PROTOCOL_INVALID")
    return raw


def _decode_message(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CONTROL_MESSAGE:
        raise AgentError("AGENT_PROTOCOL_INVALID")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AgentError("AGENT_PROTOCOL_INVALID") from None
    if not isinstance(message, dict):
        raise AgentError("AGENT_PROTOCOL_INVALID")
    return message


def _recv_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.recv(size - len(chunks))
        if not chunk:
            raise AgentError("AGENT_PROTOCOL_INVALID")
        chunks.extend(chunk)
    return bytes(chunks)


class SocketChannel:
    def __init__(self, connection):
        self.connection = connection

    def send(self, message):
        raw = _encode_message(message)
        self.connection.sendall(struct.pack("!I", len(raw)) + raw)

    def receive(self):
        length = struct.unpack("!I", _recv_exact(self.connection, 4))[0]
        if not 0 < length <= MAX_CONTROL_MESSAGE:
            raise AgentError("AGENT_PROTOCOL_INVALID")
        return _decode_message(_recv_exact(self.connection, length))

    def close(self):
        self.connection.close()


class PosixControlServer:
    def __init__(self, paths, *, uid=None):
        self.paths = paths
        self.uid = os.getuid() if uid is None else uid
        endpoint = Path(paths.endpoint)
        if endpoint.exists() or endpoint.is_socket():
            info = endpoint.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.uid or
                    stat.S_ISLNK(info.st_mode)):
                raise AgentError("AGENT_RUNTIME_UNSAFE")
            endpoint.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(paths.endpoint)
        os.chmod(paths.endpoint, 0o600)
        self.listener.listen(4)
        self.listener.settimeout(0.25)

    def accept(self):
        try:
            connection, _ = self.listener.accept()
        except socket.timeout:
            return None
        if hasattr(socket, "SO_PEERCRED"):
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"),
            )
            _, peer_uid, _ = struct.unpack("3i", credentials)
            if peer_uid != self.uid:
                connection.close()
                return None
        connection.settimeout(30)
        return SocketChannel(connection)

    def close(self):
        self.listener.close()
        endpoint = Path(self.paths.endpoint)
        try:
            info = endpoint.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == self.uid:
                endpoint.unlink()
        except FileNotFoundError:
            pass


class WindowsControlServer:
    def __init__(self, paths):
        self.paths = paths
        self.credential = secrets.token_urlsafe(32)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(0.25)
        state = {
            "version": AGENT_PROTOCOL_VERSION,
            "port": self.listener.getsockname()[1],
            "credential": self.credential,
        }
        endpoint = Path(paths.endpoint)
        endpoint.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        try:
            endpoint.chmod(0o600)
        except OSError:
            pass

    def accept(self):
        try:
            connection, peer = self.listener.accept()
        except socket.timeout:
            return None
        if peer[0] != "127.0.0.1":
            connection.close()
            return None
        connection.settimeout(2)
        channel = SocketChannel(connection)
        try:
            authentication = channel.receive()
            if (set(authentication) != {"credential"} or
                    not secrets.compare_digest(
                        str(authentication["credential"]), self.credential,
                    )):
                channel.close()
                return None
        except (AgentError, OSError):
            channel.close()
            return None
        connection.settimeout(30)
        return channel

    def close(self):
        self.listener.close()
        self.credential = None
        try:
            Path(self.paths.endpoint).unlink()
        except FileNotFoundError:
            pass


def connect_control(paths, *, timeout=1.0):
    if os.name == "nt":
        try:
            raw = Path(paths.endpoint).read_bytes()
            if len(raw) > MAX_CONTROL_MESSAGE:
                raise ValueError
            state = json.loads(raw.decode("utf-8"))
            if (set(state) != {"version", "port", "credential"} or
                    state["version"] != AGENT_PROTOCOL_VERSION or
                    type(state["port"]) is not int or not 1024 <= state["port"] <= 65535 or
                    not isinstance(state["credential"], str) or
                    len(state["credential"]) < 43):
                raise ValueError
            connection = socket.create_connection(("127.0.0.1", state["port"]), timeout)
            channel = SocketChannel(connection)
            channel.send({"credential": state["credential"]})
            state.clear()
            return channel
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise AgentError("AGENT_NOT_RUNNING") from None
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(paths.endpoint)
        return SocketChannel(connection)
    except OSError:
        connection.close()
        raise AgentError("AGENT_NOT_RUNNING") from None


class PosixForkProcess:
    """Small Popen-compatible wrapper around a pre-thread ``fork/exec``."""

    def __init__(self, argv, *, env):
        import fcntl
        import pty
        import termios

        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        error_read, error_write = os.pipe()
        master_fd, slave_fd = pty.openpty()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - exercised by packaged integration
            try:
                os.close(input_write)
                os.close(output_read)
                os.close(error_read)
                os.close(master_fd)
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
                # Keep one slave reference across exec.  Closing the last
                # slave before OpenSSH opens /dev/tty would hang up the
                # pseudoterminal and lose the authentication bridge.
                os.set_inheritable(slave_fd, True)
                os.dup2(input_read, 0)
                os.dup2(output_write, 1)
                os.dup2(error_write, 2)
                for descriptor in {input_read, output_write, error_write}:
                    if descriptor > 2:
                        os.close(descriptor)
                os.execve(argv[0], argv, env)
            except BaseException:
                os._exit(127)
        os.close(input_read)
        os.close(output_write)
        os.close(error_write)
        os.close(slave_fd)
        self.pid = pid
        self.master_fd = master_fd
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = os.fdopen(output_read, "rb", buffering=0)
        self.stderr = os.fdopen(error_read, "rb", buffering=0)
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        try:
            pid, status_value = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return self.returncode
        if pid:
            self.returncode = os.waitstatus_to_exitcode(status_value)
        return self.returncode

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("ssh", timeout)
            time.sleep(0.02)
        return self.returncode

    def terminate(self):
        if self.poll() is None:
            os.kill(self.pid, signal.SIGTERM)

    def kill(self):
        if self.poll() is None:
            os.kill(self.pid, signal.SIGKILL)

    def close_terminal(self):
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class PosixSSHFactory:
    def __init__(self):
        self.process = None

    def __call__(self, argv, **kwargs):
        if self.process is not None:
            raise OSError(errno.EBUSY, "SSH process already created")
        self.process = PosixForkProcess(argv, env=kwargs["env"])
        return self.process

    @property
    def terminal_fd(self):
        return self.process.master_fd if self.process is not None else None

    def close(self):
        if self.process is not None:
            self.process.close_terminal()


def _windows_process_factory(argv, **kwargs):
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(argv, creationflags=flags, **kwargs)


class AgentRunner:
    def __init__(self, config, username, *, paths=None, launcher_factory=None,
                 ssh_executable=None, ssh_control_path=None,
                 clock=time.monotonic):
        from .launcher import Launcher

        self.config = config
        self.username = validate_username(username)
        self.paths = paths or runtime_paths()
        self.launcher_factory = launcher_factory or Launcher
        self.ssh_executable = ssh_executable
        self.ssh_control_path = ssh_control_path
        self.clock = clock
        self.state = "STARTING"
        self.error_code = None
        self.connection = None
        self.ssh_process = None
        self.local_port = None
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._auth_complete = threading.Event()
        self._auth_claimed = False
        self._lock_stream = None
        self._server = None
        self._ssh_factory = None
        self._connection_thread = None
        self._credential_manager = create_ai_credential_manager()

    def _set_state(self, state, *, error_code=None):
        if state not in AGENT_STATES:
            raise ValueError("Invalid Agent state")
        with self._state_lock:
            self.state = state
            self.error_code = error_code

    def _acquire_single_instance(self):
        ensure_private_runtime(self.paths)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.paths.lock_file, flags, 0o600)
        except OSError:
            raise AgentError("AGENT_RUNTIME_UNSAFE") from None
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise AgentError("AGENT_RUNTIME_UNSAFE")
        self._lock_stream = os.fdopen(descriptor, "r+b", buffering=0)
        if os.name != "nt":
            import fcntl

            os.fchmod(self._lock_stream.fileno(), 0o600)
            try:
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AgentError("AGENT_ALREADY_RUNNING") from None
        else:
            import msvcrt

            if info.st_size == 0:
                self._lock_stream.write(b"\0")
            self._lock_stream.seek(0)
            try:
                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AgentError("AGENT_ALREADY_RUNNING") from None

    def _prepare(self):
        from .launcher import SSHLauncherProcess, choose_local_port, ssh_arguments

        self._acquire_single_instance()
        if os.name == "nt":
            expected_control = windows_master_path(self.paths)
            if (self.ssh_control_path is None or
                    Path(self.ssh_control_path) != expected_control):
                raise AgentError("AGENT_RUNTIME_UNSAFE")
            self._server = WindowsControlServer(self.paths)
            process_factory = _windows_process_factory
        else:
            if self.ssh_control_path is not None:
                raise AgentError("AGENT_RUNTIME_UNSAFE")
            self._server = PosixControlServer(self.paths)
            self._ssh_factory = PosixSSHFactory()
            process_factory = self._ssh_factory
        self.local_port = choose_local_port()
        executable = self.ssh_executable
        if not executable:
            import shutil
            executable = shutil.which("ssh")
        if not executable:
            raise AgentError("SSH_CLIENT_UNAVAILABLE")
        self._set_state("AUTHENTICATING")
        ssh_config = user_ssh_config()
        self.ssh_process = SSHLauncherProcess(
            ssh_arguments(
                self.config, self.username, self.local_port,
                ssh_executable=executable, control_path=self.ssh_control_path,
                ssh_config_path=ssh_config,
            ),
            process_factory=process_factory,
        )

    def _progress(self, message, **_):
        if message.startswith("✓ SSH"):
            self._set_state("CONNECTING")

    def _establish(self):
        try:
            launcher = self.launcher_factory(
                self.config, ssh_executable=self.ssh_executable,
            )
            connection = launcher.connect(
                self.username, open_browser=False, progress=self._progress,
                local_port=self.local_port, process=self.ssh_process,
            )
            self.connection = connection
            self._set_state("READY" if connection.ai_available else "DEGRADED")
            self._auth_complete.set()
            connection.wait()
            if not self._stop.is_set():
                self._set_state("ERROR", error_code="SSH_CONNECTION_FAILED")
                self._stop.set()
        except Exception as exc:
            code = getattr(exc, "code", "LAUNCHER_INTERNAL_ERROR")
            self._set_state("ERROR", error_code=code)
            self._auth_complete.set()

    def status(self):
        with self._state_lock:
            state, error_code = self.state, self.error_code
        web_url = (f"http://127.0.0.1:{self.local_port}/session"
                   if self.local_port and state in {"READY", "DEGRADED"} else None)
        provider_status = None
        if self.connection is not None and getattr(self.connection, "egress_agent", None) is not None:
            try:
                provider_status = self.connection.egress_agent.provider_client.status()
            except Exception:
                provider_status = {"provider": "deepseek", "configured": False,
                                   "backend": "Unavailable", "availability": "unavailable"}
        return {
            "version": AGENT_PROTOCOL_VERSION,
            "ok": True,
            "state": state,
            "username": self.username,
            "cluster": self.config.name,
            "web_url": web_url,
            "ai": ("available" if state == "READY" else
                   "unavailable" if state == "DEGRADED" else "unknown"),
            "error_code": error_code,
            "ai_provider": provider_status,
        }

    def _browser_url(self):
        if self.connection is None or self.state not in {"READY", "DEGRADED"}:
            raise AgentError("AGENT_NOT_READY")
        handoff = self.connection.handoff
        if handoff is not None and not handoff.served:
            return handoff.url
        return f"http://127.0.0.1:{self.local_port}/session"

    def _close_consumed_handoff(self):
        if self.connection is None:
            return
        handoff = self.connection.handoff
        if handoff is not None and handoff.served:
            handoff.close()
            self.connection.handoff = None

    def _relay_auth(self, channel):
        if os.name == "nt" or self._ssh_factory is None:
            channel.send({
                "version": AGENT_PROTOCOL_VERSION, "ok": False,
                "error_code": "AGENT_AUTH_BRIDGE_UNAVAILABLE",
            })
            return
        with self._state_lock:
            if self._auth_complete.is_set():
                channel.send({
                    "version": AGENT_PROTOCOL_VERSION, "ok": True,
                    "auth_complete": True,
                })
                return
            if self._auth_claimed:
                channel.send({
                    "version": AGENT_PROTOCOL_VERSION, "ok": False,
                    "error_code": "AGENT_AUTH_BUSY",
                })
                return
            self._auth_claimed = True
        connection = channel.connection
        connection.settimeout(None)
        channel.send({
            "version": AGENT_PROTOCOL_VERSION, "ok": True, "raw": True,
        })
        terminal_fd = self._ssh_factory.terminal_fd
        try:
            while not self._auth_complete.is_set():
                readable, _, _ = select.select([connection, terminal_fd], [], [], 0.2)
                if terminal_fd in readable:
                    data = os.read(terminal_fd, 4096)
                    if not data:
                        break
                    connection.sendall(data)
                if connection in readable:
                    data = connection.recv(4096)
                    if not data:
                        break
                    os.write(terminal_fd, data)
        except OSError:
            pass
        finally:
            with self._state_lock:
                self._auth_claimed = False

    def _handle(self, channel):
        try:
            request = channel.receive()
            if (not isinstance(request, dict) or
                    not {"version", "operation"} <= set(request)):
                raise AgentError("AGENT_PROTOCOL_INVALID")
            if request["version"] != AGENT_PROTOCOL_VERSION:
                raise AgentVersionError("AGENT_PROTOCOL_UNSUPPORTED")
            operation = request["operation"]
            if operation not in ALL_CONTROL_OPERATIONS:
                raise AgentError("AGENT_OPERATION_REJECTED")
            if operation in CONTROL_OPERATIONS and set(request) != {"version", "operation"}:
                raise AgentError("AGENT_PROTOCOL_INVALID")
            if operation == "STATUS":
                channel.send(self.status())
            elif operation == "OPEN_BROWSER":
                channel.send({
                    "version": AGENT_PROTOCOL_VERSION, "ok": True,
                    "browser_url": self._browser_url(),
                })
            elif operation == "STOP":
                channel.send({"version": AGENT_PROTOCOL_VERSION, "ok": True})
                self._set_state("DISCONNECTING")
                self._stop.set()
            elif operation == "AI_STATUS":
                if self.connection is not None and self.connection.egress_agent is not None:
                    self.connection.egress_agent.refresh_provider_status()
                channel.send({"version": AGENT_PROTOCOL_VERSION, "ok": True,
                              "ai_provider": self.status().get("ai_provider") or {
                                  "provider": "deepseek", "configured": False,
                                  "backend": "Unavailable", "availability": "unavailable",
                              }})
            elif operation == "AI_CONFIGURE":
                if (set(request) != {"version", "operation", "secret", "session_only"} or
                        type(request["session_only"]) is not bool):
                    raise AgentError("AGENT_PROTOCOL_INVALID")
                try:
                    validate_api_key(request["secret"])
                    backend = self._credential_manager.set(
                        request["secret"], session_only=request["session_only"],
                    )
                except CredentialStoreError as exc:
                    raise AgentError(exc.code) from None
                if self.connection is not None and self.connection.egress_agent is not None:
                    self.connection.egress_agent.refresh_provider_status()
                channel.send({"version": AGENT_PROTOCOL_VERSION, "ok": True,
                              "backend": backend.value})
            elif operation == "AI_DELETE":
                self._credential_manager.delete()
                if self.connection is not None and self.connection.egress_agent is not None:
                    self.connection.egress_agent.refresh_provider_status()
                channel.send({"version": AGENT_PROTOCOL_VERSION, "ok": True})
            elif operation == "AI_TEST":
                agent = self.connection.egress_agent if self.connection is not None else None
                if agent is None:
                    raise AgentError("AGENT_NOT_READY")
                result = agent.provider_client.test_connection()
                channel.send({"version": AGENT_PROTOCOL_VERSION, "ok": True,
                              "result": {"status": result.get("status"),
                                          "error_category": result.get("error_category")}})
            else:
                self._relay_auth(channel)
        except AgentError as exc:
            try:
                channel.send({
                    "version": AGENT_PROTOCOL_VERSION, "ok": False,
                    "error_code": exc.code,
                })
            except (AgentError, OSError):
                pass
        except OSError:
            pass
        finally:
            channel.close()

    def run(self):
        try:
            self._prepare()
            self._connection_thread = threading.Thread(
                target=self._establish, name="easysbatch-agent-connect", daemon=True,
            )
            self._connection_thread.start()
            error_since = None
            while not self._stop.is_set():
                self._close_consumed_handoff()
                if self.state == "ERROR":
                    error_since = error_since or self.clock()
                    if self.clock() - error_since >= 5:
                        break
                channel = self._server.accept()
                if channel is not None:
                    self._handle(channel)
            return 0 if self.state != "ERROR" else 2
        except AgentError as exc:
            return 0 if exc.code == "AGENT_ALREADY_RUNNING" else 2
        finally:
            self._cleanup()

    def _cleanup(self):
        self._stop.set()
        if self.connection is not None:
            self.connection.close()
        elif self.ssh_process is not None:
            self.ssh_process.close()
        self._auth_complete.set()
        if self._connection_thread is not None and self._connection_thread is not threading.current_thread():
            self._connection_thread.join(timeout=6)
        if self._server is not None:
            self._server.close()
        if self._ssh_factory is not None:
            self._ssh_factory.close()
        if os.name == "nt" and self.ssh_control_path is not None:
            close_windows_master(
                self.config, self.ssh_control_path,
                ssh_executable=self.ssh_executable,
            )
        self._set_state("STOPPED")
        if self._lock_stream is not None:
            self._lock_stream.close()


class AgentClient:
    def __init__(self, *, paths=None, connect=connect_control,
                 browser_open=webbrowser.open):
        self.paths = paths or runtime_paths()
        self.connect = connect
        self.browser_open = browser_open

    def request(self, operation):
        return self.request_payload(operation)

    def request_payload(self, operation, **payload):
        if operation not in ALL_CONTROL_OPERATIONS:
            raise AgentError("AGENT_OPERATION_REJECTED")
        message = {"version": AGENT_PROTOCOL_VERSION, "operation": operation, **payload}
        channel = self.connect(self.paths)
        try:
            channel.send(message)
            response = channel.receive()
        finally:
            channel.close()
        if response.get("version") != AGENT_PROTOCOL_VERSION:
            raise AgentVersionError("AGENT_PROTOCOL_UNSUPPORTED")
        if not response.get("ok"):
            code = response.get("error_code")
            if code == "AGENT_PROTOCOL_UNSUPPORTED":
                raise AgentVersionError(code)
            raise AgentError(code if isinstance(code, str) else "AGENT_PROTOCOL_INVALID")
        return response

    def status(self):
        return self.request("STATUS")

    def open_browser(self):
        response = self.request("OPEN_BROWSER")
        try:
            return bool(self.browser_open(response["browser_url"])), response["browser_url"]
        except (KeyError, OSError, webbrowser.Error):
            return False, response.get("browser_url")

    def stop(self):
        self.request("STOP")

    def ai_status(self):
        return self.request("AI_STATUS").get("ai_provider")

    def ai_configure(self, secret, *, session_only=False):
        return self.request_payload(
            "AI_CONFIGURE", secret=secret, session_only=session_only,
        )

    def ai_delete(self):
        return self.request("AI_DELETE")

    def ai_test(self):
        return self.request("AI_TEST").get("result", {})

    def bridge_authentication(self, *, terminal_path="/dev/tty"):
        if os.name == "nt":
            # Agent-owned key/ssh-agent authentication needs no bridge.  A
            # real interactive Windows OpenSSH/ConPTY validation is pending.
            return
        import termios
        import tty

        channel = self.connect(self.paths, timeout=5)
        connection = channel.connection
        channel.send({"version": AGENT_PROTOCOL_VERSION, "operation": "AUTH"})
        response = channel.receive()
        if not response.get("ok"):
            channel.close()
            raise AgentError(response.get("error_code", "AGENT_PROTOCOL_INVALID"))
        if response.get("auth_complete"):
            channel.close()
            return
        terminal_fd = os.open(terminal_path, os.O_RDWR)
        previous = termios.tcgetattr(terminal_fd)
        try:
            tty.setraw(terminal_fd)
            connection.settimeout(None)
            while True:
                readable, _, _ = select.select([connection, terminal_fd], [], [])
                if connection in readable:
                    data = connection.recv(4096)
                    if not data:
                        break
                    os.write(terminal_fd, data)
                if terminal_fd in readable:
                    data = os.read(terminal_fd, 4096)
                    if not data:
                        break
                    connection.sendall(data)
        finally:
            termios.tcsetattr(terminal_fd, termios.TCSADRAIN, previous)
            os.close(terminal_fd)
            channel.close()


def agent_environment(environment=None):
    environment = os.environ if environment is None else environment
    result = sanitized_environment(environment)
    for name in ("XDG_RUNTIME_DIR", "LOCALAPPDATA"):
        value = environment.get(name)
        if isinstance(value, str) and value:
            result[name] = value
    return result


def agent_command(*, username, config_path=None, ssh_control_path=None):
    validate_username(username)
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--agent", "--username", username]
    else:
        command = [sys.executable, "-m", "sbatch_agent.launcher", "--agent",
                   "--username", username]
    if config_path is not None:
        command.extend(["--config", str(config_path)])
    if ssh_control_path is not None:
        command.extend(["--ssh-control-path", str(ssh_control_path)])
    return command


def spawn_agent(*, username, config_path=None, ssh_control_path=None,
                process_factory=subprocess.Popen):
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": agent_environment(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0) |
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return process_factory(
        agent_command(
            username=username, config_path=config_path,
            ssh_control_path=ssh_control_path,
        ), **kwargs,
    )


def wait_for_agent(client, *, timeout, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    last = None
    while clock() < deadline:
        try:
            status_value = client.status()
            last = status_value
            if status_value["state"] in {"READY", "DEGRADED", "ERROR"}:
                return status_value
        except AgentError:
            pass
        sleep(0.1)
    if last is not None:
        return last
    raise AgentError("AGENT_START_TIMEOUT")


def windows_master_path(paths):
    candidate = paths.directory / "ssh-master-v1.sock"
    try:
        candidate.relative_to(paths.directory)
    except ValueError:
        raise AgentError("AGENT_RUNTIME_UNSAFE") from None
    return candidate


def windows_master_arguments(config, username, control_path, *, ssh_executable,
                             ssh_config_path=None):
    validate_username(username)
    path = Path(control_path)
    if not path.is_absolute() or "\x00" in str(path):
        raise AgentError("AGENT_RUNTIME_UNSAFE")
    arguments = [ssh_executable]
    if ssh_config_path is not None:
        arguments.extend(["-F", str(ssh_config_path)])
    arguments.extend([
        "-T", "-M", "-S", str(path), "-fN",
        "-p", str(config.port),
        "-o", "Hostname=" + config.host,
        "-o", "User=" + username,
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no",
        "-o", "CheckHostIP=yes",
        "-o", "ControlPersist=no",
        "-o", "AddKeysToAgent=no",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "PermitLocalCommand=no",
        "-o", "SendEnv=-*",
        "-o", "GatewayPorts=no",
        "-o", "ConnectTimeout=" + str(int(config.connect_timeout_seconds)),
        "-o", "ConnectionAttempts=1",
        "-o", "BatchMode=no",
        "-o", "NumberOfPasswordPrompts=1",
        "--", config.host,
    ])
    return arguments


def create_windows_master(config, username, paths, *, ssh_executable,
                          run=subprocess.run):
    ensure_private_runtime(paths)
    control_path = windows_master_path(paths)
    # If a previous Agent crashed, only address the private, exact control
    # socket.  Never enumerate or terminate unrelated ssh.exe processes.
    close_windows_master(
        config, control_path, ssh_executable=ssh_executable, run=run,
    )
    result = run(
        windows_master_arguments(
            config, username, control_path, ssh_executable=ssh_executable,
            ssh_config_path=user_ssh_config(),
        ),
        stdin=None, stdout=subprocess.DEVNULL, stderr=None,
        env=sanitized_environment(os.environ), check=False,
    )
    if result.returncode != 0:
        raise AgentError("SSH_AUTH_FAILED" if result.returncode == 255
                         else "SSH_CONNECTION_FAILED")
    return control_path


def close_windows_master(config, control_path, *, ssh_executable=None,
                         run=subprocess.run):
    import shutil

    executable = ssh_executable or shutil.which("ssh")
    if not executable or control_path is None:
        return False
    try:
        result = run(
            [executable, "-T", "-S", str(control_path), "-O", "exit",
             "-p", str(config.port), "--", config.host],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=sanitized_environment(os.environ),
            check=False, timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_agent(config, username, *, ssh_control_path=None):
    return AgentRunner(
        config, username, ssh_control_path=ssh_control_path,
    ).run()
