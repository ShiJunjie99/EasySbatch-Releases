"""EasySbatch SSH-first CLI Launcher.

One system OpenSSH process performs the user's normal authentication, creates
the local Web forward, and runs the fixed per-user Worker.  SSH credentials are
never read or persisted by EasySbatch; M10-B7's separate DeepSeek key flow is
explicitly local OS credential-store storage on the user's device.
"""

from __future__ import annotations

import argparse
import getpass
import os
from http.client import HTTPConnection
from http.cookies import CookieError, SimpleCookie
from importlib import resources
import json
from pathlib import Path
import queue
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from urllib.parse import urlencode
from uuid import UUID
import webbrowser

from .launcher_client import (
    classify_transport, sanitized_environment, ssh_install_hint,
    validate_host, validate_username,
)
from .launcher_version import PROTOCOL_VERSION, version_text
from .cluster_profile import ClusterProfile
from .launcher_ai_stream import LauncherAIEgressAgent
from .credential_store import CredentialStoreError, create_ai_credential_manager, validate_api_key
from .local_ai_provider import LocalDeepSeekProviderClient


SSH_FIRST_COOKIE_NAME = "easysbatch_ssh_first_session"
WORKER_READY_PREFIX_ROOT = b"EASYSBATCH_WORKER_READY_V"
WORKER_READY_PREFIX = WORKER_READY_PREFIX_ROOT + str(PROTOCOL_VERSION).encode("ascii") + b" "
MAX_STARTUP_PREAMBLE_BYTES = 128 * 1024
_PROTOCOL_UNSUPPORTED = object()
LAUNCHER_ERRORS = frozenset({
    "LAUNCHER_CONFIG_INVALID", "SSH_CLIENT_UNAVAILABLE", "SSH_AUTH_FAILED",
    "SSH_HOST_KEY_FAILED", "SSH_CONNECTION_FAILED", "TUNNEL_START_FAILED",
    "WORKER_START_FAILED", "WORKER_IDENTITY_MISMATCH", "LAUNCHER_PROTOCOL_INVALID",
    "BOOTSTRAP_FAILED", "HANDOFF_FAILED", "LAUNCHER_PROTOCOL_UNSUPPORTED",
    "USERNAME_INVALID", "LAUNCHER_INTERNAL_ERROR",
    "AI_CREDENTIAL_INVALID", "AI_LOCAL_CREDENTIAL_UNAVAILABLE",
    "AI_SESSION_ONLY_AGENT_REQUIRED", "AI_COMMAND_INVALID",
})


class LauncherError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in LAUNCHER_ERRORS else "SSH_CONNECTION_FAILED"
        super().__init__(self.code)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "启动参数无效；请使用 --help 查看支持的参数。\n")


class VersionAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS,
                 help=None):
        super().__init__(option_strings=option_strings, dest=dest, nargs=0,
                         default=default, required=False, help=help)

    def __call__(self, parser, namespace, values, option_string=None):
        parser._print_message(version_text() + "\n", sys.stdout)
        parser.exit(0)


def configure_console_output():
    """Use UTF-8 where supported and never crash on an older console codec."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


class LauncherConfig:
    def __init__(self, *, name, host, port, remote_web_port, broker_socket,
                 worker_entrypoint,
                 connect_timeout_seconds=12, ready_timeout_seconds=20,
                 handoff_timeout_seconds=30, display_name=None, username=None):
        validate_host(host)
        profile = ClusterProfile(id=name, display_name=display_name or name, host=host, ssh_port=port)
        if username is not None:
            validate_username(username)
        worker_path = Path(worker_entrypoint) if isinstance(worker_entrypoint, str) else None
        broker_path = Path(broker_socket) if isinstance(broker_socket, str) else None
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or
                type(remote_web_port) is not int or not 1024 <= remote_web_port <= 65535 or
                not isinstance(broker_socket, str) or
                re.fullmatch(r"/tmp/easysbatch-[0-9]+/broker\.sock", broker_socket) is None or
                worker_path is None or broker_path is None or
                worker_path.parent != broker_path.parent or
                worker_path.name != f"user-worker-v{PROTOCOL_VERSION}.py" or
                type(connect_timeout_seconds) not in (int, float) or
                not 5 <= connect_timeout_seconds <= 30 or
                type(ready_timeout_seconds) not in (int, float) or
                not 5 <= ready_timeout_seconds <= 60 or
                type(handoff_timeout_seconds) not in (int, float) or
                not 10 <= handoff_timeout_seconds <= 120):
            raise ValueError("Invalid SSH-first deployment configuration")
        self.id = profile.id
        self.name = profile.display_name
        self.username = username
        self.host = host
        self.port = port
        self.remote_web_port = remote_web_port
        self.broker_socket = broker_socket
        self.worker_entrypoint = worker_entrypoint
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.ready_timeout_seconds = float(ready_timeout_seconds)
        self.handoff_timeout_seconds = float(handoff_timeout_seconds)

    @classmethod
    def _decode(cls, raw):
        if len(raw) > 8192:
            raise ValueError
        data = tomllib.loads(raw.decode("utf-8"))
        if not {"target", "launcher"} <= set(data) or set(data) - {"target", "launcher", "user"} or not isinstance(data["target"], dict) or not isinstance(data["launcher"], dict):
            raise ValueError
        if set(data["target"]) - {"display_name"} != {
            "name", "host", "port", "remote_web_port", "broker_socket", "worker_entrypoint",
        }:
            raise ValueError
        if set(data["launcher"]) != {"connect_timeout_seconds", "ready_timeout_seconds", "handoff_timeout_seconds"}:
            raise ValueError
        user = data.get("user", {})
        if not isinstance(user, dict) or set(user) - {"username"}:
            raise ValueError
        return cls(**data["target"], **data["launcher"], **user)

    @classmethod
    def load(cls, path):
        try:
            return cls._decode(Path(path).read_bytes())
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, tomllib.TOMLDecodeError):
            raise LauncherError("LAUNCHER_CONFIG_INVALID") from None

    @classmethod
    def bundled(cls):
        try:
            raw = resources.files("sbatch_agent").joinpath("launcher_alpha.toml").read_bytes()
            return cls._decode(raw)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, tomllib.TOMLDecodeError):
            raise LauncherError("LAUNCHER_CONFIG_INVALID") from None


def choose_local_port(*, bind=socket.socket, attempts=32):
    for _ in range(attempts):
        port = 49152 + secrets.randbelow(65535 - 49152 + 1)
        candidate = None
        try:
            candidate = bind(socket.AF_INET, socket.SOCK_STREAM)
            candidate.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            if candidate is not None:
                candidate.close()
        return port
    raise LauncherError("TUNNEL_START_FAILED")


def ssh_arguments(config, username, local_port, *, ssh_executable=None,
                  ai_egress_local_port=None, control_path=None,
                  ssh_config_path=None):
    if not isinstance(config, LauncherConfig):
        raise ValueError("LauncherConfig required")
    username = validate_username(username)
    if type(local_port) is not int or not 49152 <= local_port <= 65535:
        raise ValueError("Invalid loopback port")
    if (ai_egress_local_port is not None and
            (type(ai_egress_local_port) is not int or
             not 1024 <= ai_egress_local_port <= 65535)):
        raise ValueError("Invalid AI egress loopback port")
    executable = ssh_executable or shutil.which("ssh")
    if not executable or not Path(executable).is_absolute():
        raise LauncherError("SSH_CLIENT_UNAVAILABLE")
    if control_path is not None:
        control_path = Path(control_path)
        if not control_path.is_absolute() or "\x00" in str(control_path):
            raise ValueError("Invalid SSH control path")
    if ssh_config_path is not None:
        if ssh_config_path != "none":
            ssh_config_path = Path(ssh_config_path)
            if not ssh_config_path.is_absolute() or "\x00" in str(ssh_config_path):
                raise ValueError("Invalid SSH configuration path")
    remote_command = (
        "/usr/bin/python3 -I " + shlex.quote(config.worker_entrypoint) +
        " --socket " + shlex.quote(config.broker_socket)
    )
    arguments = [executable]
    if ssh_config_path is not None:
        arguments.extend(["-F", str(ssh_config_path)])
    arguments.extend([
        "-T", "-p", str(config.port),
        "-o", "Hostname=" + config.host,
        "-o", "User=" + username,
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no",
        "-o", "CheckHostIP=yes",
        "-o", "ControlMaster=no",
        "-o", ("ControlPath=" + str(control_path) if control_path is not None
               else "ControlPath=none"),
        "-o", "ControlPersist=no",
        "-o", "AddKeysToAgent=no",
        "-o", "ForwardAgent=no",
        "-o", "ForwardX11=no",
        "-o", "PermitLocalCommand=no",
        "-o", "SendEnv=-*",
        "-o", "GatewayPorts=no",
        # The Web/Worker path remains independently verified below.  A denied
        # remote forward degrades only AI instead of killing the SSH session.
        "-o", "ExitOnForwardFailure=no",
        "-o", "ConnectTimeout=" + str(int(config.connect_timeout_seconds)),
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "-o", ("BatchMode=yes" if control_path is not None else "BatchMode=no"),
        "-o", ("NumberOfPasswordPrompts=0" if control_path is not None
               else "NumberOfPasswordPrompts=1"),
        "-L", f"127.0.0.1:{local_port}:127.0.0.1:{config.remote_web_port}",
    ])
    if ai_egress_local_port is not None:
        arguments.extend([
            "-R", f"127.0.0.1:0:127.0.0.1:{ai_egress_local_port}",
        ])
    arguments.extend(["--", config.host, remote_command])
    return arguments


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class SSHLauncherProcess:
    def __init__(self, argv, *, process_factory=subprocess.Popen):
        try:
            self.process = process_factory(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True, env=sanitized_environment(os.environ),
            )
        except (OSError, subprocess.SubprocessError):
            raise LauncherError("SSH_CLIENT_UNAVAILABLE") from None
        self._stderr = bytearray()
        self._remote_forward_event = threading.Event()
        self._remote_forward_port = None
        self.ai_egress_supported = False
        self.ai_egress_agent = None
        self._stderr_thread = threading.Thread(target=self._pump_stderr,
                                               name="easysbatch-ssh-stderr", daemon=True)
        self._stderr_thread.start()

    def _pump_stderr(self):
        stream = self.process.stderr
        if stream is None:
            return
        while True:
            chunk = stream.read(1024)
            if not chunk:
                return
            remaining = 32 * 1024 - len(self._stderr)
            if remaining > 0:
                self._stderr.extend(chunk[:remaining])
            match = re.search(
                rb"Allocated port ([0-9]{1,5}) for remote forward(?:ing)?\b",
                self._stderr,
            )
            if match is not None:
                port = int(match.group(1))
                if 1024 <= port <= 65535:
                    self._remote_forward_port = port
                    self._remote_forward_event.set()
            lowered = bytes(self._stderr).lower()
            if (b"remote port forwarding failed" in lowered or
                    b"remote forwarding failed" in lowered):
                self._remote_forward_event.set()
            # Remote login shells may print module banners, paths or other
            # user-specific text. Keep only a bounded in-memory diagnostic for
            # error classification and never mirror it into Launcher output.

    def _failure_code(self):
        self.process.poll()
        if self.process.returncode is not None:
            self._stderr_thread.join(timeout=0.2)
        diagnostic = self._stderr.decode("utf-8", errors="replace")
        lowered = diagnostic.lower()
        if any(marker in lowered for marker in (
                "cannot listen to port", "address already in use", "port forwarding failed")):
            return "TUNNEL_START_FAILED"
        if "worker_identity_mismatch" in lowered:
            return "WORKER_IDENTITY_MISMATCH"
        classified = classify_transport(self.process.returncode or 255, diagnostic)
        if classified in {"SSH_AUTH_FAILED", "SSH_HOST_KEY_FAILED", "SSH_CONNECTION_FAILED"}:
            return classified
        return "WORKER_START_FAILED"

    def ready(self, expected_username, *, timeout):
        output = queue.Queue(maxsize=1)

        def read_once():
            try:
                consumed = 0
                while consumed <= MAX_STARTUP_PREAMBLE_BYTES:
                    raw = self.process.stdout.readline(32 * 1024 + 2)
                    if not raw:
                        output.put_nowait(b"")
                        return
                    consumed += len(raw)
                    if (raw.startswith(WORKER_READY_PREFIX_ROOT) and
                            not raw.startswith(WORKER_READY_PREFIX)):
                        output.put_nowait(_PROTOCOL_UNSUPPORTED)
                        return
                    if raw.startswith(WORKER_READY_PREFIX):
                        output.put_nowait(raw[len(WORKER_READY_PREFIX):])
                        return
                output.put_nowait(None)
            except (OSError, queue.Full):
                pass

        threading.Thread(target=read_once, name="easysbatch-worker-ready", daemon=True).start()
        try:
            raw = output.get(timeout=timeout)
        except queue.Empty:
            self.close()
            raise LauncherError("WORKER_START_FAILED") from None
        if raw is _PROTOCOL_UNSUPPORTED:
            raise LauncherError("LAUNCHER_PROTOCOL_UNSUPPORTED")
        if raw is None:
            raise LauncherError("LAUNCHER_PROTOCOL_INVALID")
        if not raw:
            raise LauncherError(self._failure_code())
        if len(raw) > 32 * 1024 + 1 or not raw.endswith(b"\n"):
            raise LauncherError("LAUNCHER_PROTOCOL_INVALID")
        try:
            report = json.loads(raw[:-1].decode("utf-8", errors="strict"),
                                object_pairs_hook=_unique_object)
            raw = b""
            expected = {"version", "event", "worker_id", "bootstrap_token",
                        "username", "uid", "ttl_seconds"}
            if isinstance(report, dict) and report.get("version") != PROTOCOL_VERSION:
                report.clear()
                raise LauncherError("LAUNCHER_PROTOCOL_UNSUPPORTED")
            if (not isinstance(report, dict) or set(report) != expected or
                    report.get("version") != PROTOCOL_VERSION or report.get("event") != "ready" or
                    type(report.get("uid")) is not int or report["uid"] <= 0 or
                    type(report.get("ttl_seconds")) is not int or
                    not 30 <= report["ttl_seconds"] <= 60 or
                    not isinstance(report.get("bootstrap_token"), str) or
                    re.fullmatch(r"[A-Za-z0-9_-]{43,256}", report["bootstrap_token"]) is None):
                raise ValueError
            UUID(report["worker_id"])
            if report.get("username") != expected_username:
                report.clear()
                raise LauncherError("WORKER_IDENTITY_MISMATCH")
            return report
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            raise LauncherError("LAUNCHER_PROTOCOL_INVALID") from None

    @property
    def connected(self):
        return self.process.poll() is None

    def wait_remote_forward(self, *, timeout=5):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not 0 < timeout <= 30):
            raise ValueError("Invalid remote forward timeout")
        self._remote_forward_event.wait(timeout)
        if not self.connected:
            return None
        return self._remote_forward_port

    def start_ai_egress(self, *, agent_factory=LauncherAIEgressAgent, timeout=5):
        """Start B5B over the already-authenticated SSH command stdio."""
        if self.ai_egress_agent is not None:
            return self.ai_egress_agent.ready
        try:
            agent = agent_factory(self.process.stdout, self.process.stdin)
            agent.start()
            self.ai_egress_agent = agent
            return agent.wait_ready(timeout)
        except (OSError, ValueError):
            return False

    def wait(self):
        return self.process.wait()

    def close(self):
        if self.ai_egress_agent is not None:
            self.ai_egress_agent.close()
            self.ai_egress_agent = None
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


def wait_for_tunnel(port, process, *, timeout, clock=time.monotonic,
                    sleep=time.sleep, connection_factory=HTTPConnection):
    deadline = clock() + timeout
    while clock() < deadline:
        if not process.connected:
            raise LauncherError(process._failure_code())
        connection = connection_factory("127.0.0.1", port, timeout=1)
        try:
            connection.request(
                "GET", "/login", headers={"Host": f"127.0.0.1:{port}"},
            )
            response = connection.getresponse()
            response.read(4096)
            if response.status == 200:
                process.ai_egress_supported = (
                    response.getheader("X-EasySbatch-AI-Egress") == "v1"
                )
                cookies = SimpleCookie()
                for name, value in response.getheaders():
                    if name.lower() == "set-cookie":
                        cookies.load(value)
                if SSH_FIRST_COOKIE_NAME in cookies:
                    token = cookies[SSH_FIRST_COOKIE_NAME].value
                    if re.fullmatch(r"[A-Za-z0-9_-]{43,256}", token):
                        return token
        except OSError:
            pass
        finally:
            connection.close()
        sleep(0.1)
    raise LauncherError("TUNNEL_START_FAILED")


def bootstrap_session(port, worker_id, token, *, anonymous_token,
                      ai_egress_port=None, ai_egress_credential=None,
                      status_result=None, connection_factory=HTTPConnection):
    fields = {"worker_id": worker_id, "bootstrap_token": token}
    if ai_egress_port is not None or ai_egress_credential is not None:
        if (type(ai_egress_port) is not int or not 1024 <= ai_egress_port <= 65535 or
                not isinstance(ai_egress_credential, str) or
                re.fullmatch(r"[A-Za-z0-9_-]{43,128}", ai_egress_credential) is None):
            raise ValueError("Invalid AI egress bootstrap state")
        fields.update({
            "ai_egress_port": str(ai_egress_port),
            "ai_egress_credential": ai_egress_credential,
        })
    body = urlencode(fields).encode("ascii")
    fields.clear()
    # Server-side TLS health is bounded at eight seconds. Keep the bootstrap
    # socket budget larger so an AI timeout can return "unavailable" without
    # being mistaken for a failed cluster/Web bootstrap.
    connection = connection_factory("127.0.0.1", port, timeout=15)
    try:
        connection.request(
            "POST", "/auth/ssh-bootstrap", body=body,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Content-Length": str(len(body)),
                "Cookie": f"{SSH_FIRST_COOKIE_NAME}={anonymous_token}",
            },
        )
        response = connection.getresponse()
        payload = response.read(4096)
        if response.status != 303 or response.getheader("Location") != "/session":
            raise LauncherError("BOOTSTRAP_FAILED")
        cookies = SimpleCookie()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                cookies.load(value)
        if SSH_FIRST_COOKIE_NAME not in cookies:
            raise LauncherError("BOOTSTRAP_FAILED")
        session_token = cookies[SSH_FIRST_COOKIE_NAME].value
        if (not isinstance(session_token, str) or
                re.fullmatch(r"[A-Za-z0-9_-]{43,256}", session_token) is None or
                token.encode("ascii") in payload or
                (ai_egress_credential is not None and
                 ai_egress_credential.encode("ascii") in payload)):
            raise LauncherError("BOOTSTRAP_FAILED")
        ai_status = response.getheader("X-EasySbatch-AI-Status")
        if status_result is not None and ai_status in {"available", "unavailable"}:
            status_result.append(ai_status)
        return session_token
    except (OSError, CookieError, ValueError):
        raise LauncherError("BOOTSTRAP_FAILED") from None
    finally:
        body = b""
        token = None
        ai_egress_credential = None
        anonymous_token = None
        connection.close()


class LocalHandoff:
    """One-shot loopback cookie handoff; its URL contains no credential."""

    def __init__(self, session_token, target_port):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        if (not isinstance(session_token, str) or
                re.fullmatch(r"[A-Za-z0-9_-]{43,256}", session_token) is None or
                type(target_port) is not int or not 49152 <= target_port <= 65535):
            raise ValueError("Invalid handoff state")
        state = {"token": session_token, "served": False, "closed": False}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/" or state["served"]:
                    self.send_error(404)
                    return
                state["served"] = True
                self.send_response(303)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/session")
                self.send_header(
                    "Set-Cookie",
                    f"{SSH_FIRST_COOKIE_NAME}={state['token']}; Path=/; HttpOnly; SameSite=Strict",
                )
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", "0")
                self.end_headers()
                state["token"] = None

            def log_message(self, format, *args):
                return

        try:
            self._server = HTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=True)
        except OSError:
            raise LauncherError("HANDOFF_FAILED") from None
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self._server.timeout = 0.25
        self._state = state
        self._thread = None

    def start(self):
        def serve_one():
            while not self._state["served"] and not self._state["closed"]:
                self._server.handle_request()

        self._thread = threading.Thread(target=serve_one,
                                        name="easysbatch-browser-handoff", daemon=True)
        self._thread.start()

    def wait(self, timeout):
        self._thread.join(timeout)
        return self._state["served"]

    @property
    def served(self):
        return self._state["served"]

    def close(self):
        self._state["closed"] = True
        self._state["token"] = None
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=0.5)
        self._server.server_close()


class LauncherConnection:
    def __init__(self, process, username, local_port, *, handoff=None,
                 egress_agent=None, ai_available=False):
        self.process = process
        self.username = username
        self.local_port = local_port
        self.handoff = handoff
        self.egress_agent = egress_agent
        self.ai_available = bool(ai_available)

    def wait(self):
        try:
            result = self.process.wait()
            if result != 0:
                print("EasySbatch 连接已断开，请重新运行 Launcher。", flush=True)
                return 2
            return 0
        except KeyboardInterrupt:
            self.process.close()
            return 0
        finally:
            if self.handoff is not None:
                self.handoff.close()
                self.handoff = None
            if self.egress_agent is not None:
                self.egress_agent.close()
                self.egress_agent = None

    def close(self):
        self.process.close()
        if self.handoff is not None:
            self.handoff.close()
            self.handoff = None
        if self.egress_agent is not None:
            self.egress_agent.close()
            self.egress_agent = None


class Launcher:
    def __init__(self, config, *, process_factory=subprocess.Popen,
                 browser_open=webbrowser.open, connection_factory=HTTPConnection,
                 handoff_factory=LocalHandoff, ssh_executable=None,
                 egress_agent_factory=LauncherAIEgressAgent):
        self.config = config
        self.process_factory = process_factory
        self.browser_open = browser_open
        self.connection_factory = connection_factory
        self.handoff_factory = handoff_factory
        self.ssh_executable = ssh_executable
        self.egress_agent_factory = egress_agent_factory

    def connect(self, username, *, open_browser=True, progress=print,
                local_port=None, process=None):
        """Establish one SSH-first session.

        ``open_browser=False`` is the persistent-Agent entrypoint.  It keeps
        the one-shot cookie handoff alive in memory so a short-lived
        Controller can request it over the protected local control channel.
        The historical foreground path remains available for focused tests
        and compatibility, but the packaged CLI uses the Agent.
        """
        username = validate_username(username)
        ssh_executable = self.ssh_executable or shutil.which("ssh")
        if not ssh_executable:
            raise LauncherError("SSH_CLIENT_UNAVAILABLE")
        local_port = local_port or choose_local_port()
        egress_agent = None
        if process is None:
            process = SSHLauncherProcess(
                ssh_arguments(
                    self.config, username, local_port,
                    ssh_executable=ssh_executable,
                ),
                process_factory=self.process_factory,
            )
        report = None
        handoff = None
        anonymous_token = None
        try:
            report = process.ready(username, timeout=self.config.ready_timeout_seconds)
            progress("✓ SSH 已连接", flush=True)
            progress(f"✓ 集群身份：{username}", flush=True)
            progress("✓ EasySbatch Worker 已启动", flush=True)
            anonymous_token = wait_for_tunnel(
                local_port, process, timeout=self.config.connect_timeout_seconds,
                connection_factory=self.connection_factory,
            )
            progress("✓ 本地通道已建立", flush=True)
            progress("✓ 集群已连接", flush=True)
            progress("正在检查 AI 网络…", flush=True)
            ai_channel_ready = False
            starter = getattr(process, "start_ai_egress", None)
            if callable(starter):
                ai_channel_ready = starter(
                    agent_factory=self.egress_agent_factory, timeout=5,
                )
                egress_agent = getattr(process, "ai_egress_agent", None)
            token = report.pop("bootstrap_token")
            ai_status = []
            session_token = bootstrap_session(
                local_port, report["worker_id"], token,
                anonymous_token=anonymous_token,
                status_result=ai_status,
                connection_factory=self.connection_factory,
            )
            anonymous_token = None
            token = None
            ai_available = ai_channel_ready and ai_status == ["available"]
            if ai_available:
                progress("✓ AI 服务可用", flush=True)
            else:
                progress("○ AI 服务当前不可用", flush=True)
            handoff = self.handoff_factory(session_token, local_port)
            session_token = None
            handoff.start()
            if open_browser:
                progress("正在打开浏览器…", flush=True)
                try:
                    opened = bool(self.browser_open(handoff.url))
                except (OSError, webbrowser.Error):
                    opened = False
                served = handoff.wait(self.config.handoff_timeout_seconds) if opened else False
                if not served:
                    progress("浏览器未能自动打开。", flush=True)
                    progress(f"请手动访问：{handoff.url}", flush=True)
                else:
                    handoff.close()
                    handoff = None
                    progress(f"浏览器：http://127.0.0.1:{local_port}/session", flush=True)
                progress("按 Ctrl+C 断开连接。", flush=True)
            result = LauncherConnection(
                process, username, local_port, handoff=handoff,
                egress_agent=egress_agent, ai_available=ai_available,
            )
            handoff = None
            egress_agent = None
            return result
        except Exception:
            process.close()
            raise
        finally:
            if report is not None:
                report.clear()
            anonymous_token = None
            if handoff is not None:
                handoff.close()
            if egress_agent is not None:
                egress_agent.close()


ERROR_MESSAGES = {
    "LAUNCHER_CONFIG_INVALID": "Launcher 配置无效，请联系管理员。",
    "USERNAME_INVALID": "用户名格式无效，请输入 Linux 集群用户名。",
    "SSH_CLIENT_UNAVAILABLE": "未检测到 OpenSSH 客户端。",
    "SSH_AUTH_FAILED": "SSH 认证失败。",
    "SSH_HOST_KEY_FAILED": "无法验证服务器身份。",
    "SSH_CONNECTION_FAILED": "无法连接计算集群；密码验证尚未开始。请检查校园网或学校 VPN。",
    "TUNNEL_START_FAILED": "无法创建本地连接端口。",
    "WORKER_START_FAILED": "EasySbatch 服务启动失败。",
    "WORKER_IDENTITY_MISMATCH": "集群身份校验失败，连接已终止。",
    "LAUNCHER_PROTOCOL_INVALID": "EasySbatch 身份握手无效，连接已终止。",
    "LAUNCHER_PROTOCOL_UNSUPPORTED": "Launcher 与服务器版本不兼容，请更新 EasySbatch Launcher。",
    "BOOTSTRAP_FAILED": "无法建立浏览器会话。",
    "HANDOFF_FAILED": "浏览器会话交接未完成。",
    "LAUNCHER_INTERNAL_ERROR": "Launcher 运行失败，请重新下载最新版本。",
    "AI_COMMAND_INVALID": "AI 命令无效，请使用 configure、replace、delete、status 或 test。",
    "AI_LOCAL_CREDENTIAL_UNAVAILABLE": "未检测到可用的系统安全凭据存储。可使用 --session-only 仅本次运行保存。",
    "AI_SESSION_ONLY_AGENT_REQUIRED": "仅本次运行模式需要先启动 EasySbatch Launcher Agent。",
}


def _pause_after_windows_error(*, input_stream=None, output_stream=None):
    """Keep a double-clicked Windows console visible after a startup failure."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    if not (getattr(input_stream, "isatty", lambda: False)() and
            getattr(output_stream, "isatty", lambda: False)()):
        return
    try:
        input("按 Enter 键退出。")
    except (EOFError, KeyboardInterrupt):
        pass


def _print_agent_status(status):
    state = status.get("state")
    labels = {
        "STARTING": "正在启动", "AUTHENTICATING": "正在认证",
        "CONNECTING": "正在连接", "READY": "已连接",
        "DEGRADED": "已连接", "DISCONNECTING": "正在断开",
        "STOPPED": "已停止", "ERROR": "连接已断开",
    }
    print("EasySbatch Agent", flush=True)
    print(f"状态       {labels.get(state, state or '未知')}", flush=True)
    if status.get("username"):
        print(f"用户       {status['username']}", flush=True)
    if status.get("cluster"):
        print(f"集群       {status['cluster']}", flush=True)
    if status.get("web_url"):
        print(f"Web        {status['web_url']}", flush=True)
    if status.get("ai") in {"available", "unavailable"}:
        print(f"AI         {'可用' if status['ai'] == 'available' else '不可用'}", flush=True)
    provider = status.get("ai_provider")
    if isinstance(provider, dict):
        print(f"DeepSeek   {'已配置' if provider.get('configured') else '尚未配置'}", flush=True)
        print(f"凭据存储   {provider.get('backend', 'Unavailable')}", flush=True)


def _ai_credential_location(session_only):
    """Return truthful UX text for persistent versus memory-only credentials."""
    if session_only:
        return "保存位置：本次运行使用（仅存于当前 Launcher Agent 内存）。"
    return "保存位置：本机系统安全凭据管理器。"


def _ai_command(action, args):
    from .launcher_agent import AgentClient, AgentError

    if action not in {"configure", "replace", "delete", "status", "test"}:
        raise LauncherError("AI_COMMAND_INVALID")
    client = AgentClient()
    agent = _existing_agent(client)
    agent_ready = (isinstance(agent, dict) and
                   agent.get("state") in {"READY", "DEGRADED"})
    manager = create_ai_credential_manager()
    if action == "status":
        provider = None
        if agent_ready:
            try:
                provider = client.ai_status()
            except AgentError:
                provider = None
        provider = provider or LocalDeepSeekProviderClient(credentials=manager).status()
        print("EasySbatch AI 设置", flush=True)
        print("服务商     DeepSeek", flush=True)
        print(f"状态       {'已配置' if provider.get('configured') else '尚未配置'}", flush=True)
        print(f"凭据存储   {provider.get('backend', 'Unavailable')}", flush=True)
        return 0
    if action == "delete":
        manager.delete()
        if agent_ready:
            try:
                client.ai_delete()
            except AgentError:
                pass
        print("DeepSeek API Key 已删除。", flush=True)
        return 0
    if action in {"configure", "replace"}:
        try:
            secret = getpass.getpass("DeepSeek API Key: ")
            validate_api_key(secret)
        except (EOFError, KeyboardInterrupt):
            return 130
        except CredentialStoreError:
            raise LauncherError("AI_LOCAL_CREDENTIAL_UNAVAILABLE") from None
        if args.session_only:
            if not agent_ready:
                raise LauncherError("AI_SESSION_ONLY_AGENT_REQUIRED")
            try:
                client.ai_configure(secret, session_only=True)
            except AgentError as exc:
                raise LauncherError(exc.code) from None
        else:
            try:
                manager.set(secret)
            except CredentialStoreError:
                raise LauncherError("AI_LOCAL_CREDENTIAL_UNAVAILABLE") from None
        secret = None
        print("DeepSeek API Key 已保存。", flush=True)
        print(_ai_credential_location(args.session_only), flush=True)
        result = (client.ai_test() if agent_ready else
                  LocalDeepSeekProviderClient(credentials=manager).test_connection())
        if result.get("status") == "ok":
            print("✓ DeepSeek 连接测试通过。", flush=True)
            return 0
        category = result.get("error_category")
        if category == "AI_PROVIDER_AUTH_FAILED":
            print("DeepSeek API Key 无效或已失效，请重新配置。", flush=True)
        elif category == "AI_PROVIDER_RATE_LIMITED":
            print("AI 服务当前请求较多，请稍后重试。", flush=True)
        else:
            print("DeepSeek 连接测试失败；SSH 和手动模式仍可使用。", flush=True)
        return 2
    # test
    result = (client.ai_test() if agent_ready else
              LocalDeepSeekProviderClient(credentials=manager).test_connection())
    if result.get("status") == "ok":
        print("✓ DeepSeek 连接测试通过。", flush=True)
        return 0
    print("DeepSeek 连接测试失败；请检查本机网络和 API Key。", flush=True)
    return 2


def _existing_agent(client):
    from .launcher_agent import AgentError

    try:
        return client.status()
    except AgentError as exc:
        if exc.code == "AGENT_NOT_RUNNING":
            return None
        raise


def _controller_start(config, args):
    from .launcher_agent import (
        AgentClient, AgentError, create_windows_master, runtime_paths,
        close_windows_master, spawn_agent, wait_for_agent,
    )

    client = AgentClient()
    current = _existing_agent(client)
    if current is not None and current.get("state") in {"READY", "DEGRADED"}:
        print("EasySbatch 已连接。", flush=True)
        print("正在打开浏览器…", flush=True)
        opened, url = client.open_browser()
        if not opened:
            print("浏览器未能自动打开。", flush=True)
            print(f"请手动访问：{url}", flush=True)
        return 0
    if current is None:
        if not shutil.which("ssh"):
            raise LauncherError("SSH_CLIENT_UNAVAILABLE")
        username = args.username or config.username or input("用户名: ").strip()
        try:
            validate_username(username)
        except ValueError:
            raise LauncherError("USERNAME_INVALID") from None
        print(f"正在连接 {config.name}…", flush=True)
        ssh_control_path = None
        if os.name == "nt":
            ssh_control_path = create_windows_master(
                config, username, runtime_paths(),
                ssh_executable=shutil.which("ssh"),
            )
        try:
            spawn_agent(
                username=username, config_path=args.config,
                ssh_control_path=ssh_control_path,
            )
        except Exception:
            if ssh_control_path is not None:
                close_windows_master(
                    config, ssh_control_path, ssh_executable=shutil.which("ssh"),
                )
            raise
        deadline = time.monotonic() + 10
        while current is None and time.monotonic() < deadline:
            time.sleep(0.1)
            current = _existing_agent(client)
        if current is None:
            raise AgentError("AGENT_START_TIMEOUT")
    elif args.username and args.username != current.get("username"):
        raise AgentError("AGENT_IDENTITY_MISMATCH")

    if current.get("state") in {"STARTING", "AUTHENTICATING", "CONNECTING"}:
        client.bridge_authentication()
        current = wait_for_agent(
            client,
            timeout=(config.ready_timeout_seconds + config.connect_timeout_seconds + 20),
        )
    if current.get("state") == "ERROR":
        code = current.get("error_code")
        if code in LAUNCHER_ERRORS:
            raise LauncherError(code)
        raise AgentError("AGENT_START_FAILED")
    if current.get("state") not in {"READY", "DEGRADED"}:
        raise AgentError("AGENT_START_TIMEOUT")

    print("✓ SSH 已连接", flush=True)
    print(f"✓ 集群身份：{current['username']}", flush=True)
    print("✓ EasySbatch 已启动", flush=True)
    if current.get("ai") == "available":
        print("✓ AI 服务可用", flush=True)
    else:
        print("○ AI 服务当前不可用", flush=True)
    print("正在打开浏览器…", flush=True)
    opened, url = client.open_browser()
    if not opened:
        print("浏览器未能自动打开。", flush=True)
        print(f"请手动访问：{url}", flush=True)
    print("EasySbatch 已在后台运行。可以关闭此终端。", flush=True)
    return 0


def _controller_command(command):
    from .launcher_agent import AgentClient, AgentError

    client = AgentClient()
    if command == "status":
        status = _existing_agent(client)
        if status is None:
            print("EasySbatch Agent 未运行。", flush=True)
            return 1
        _print_agent_status(status)
        return 0
    if command == "open":
        status = _existing_agent(client)
        if status is None or status.get("state") not in {"READY", "DEGRADED"}:
            raise AgentError("AGENT_NOT_READY")
        opened, url = client.open_browser()
        if not opened:
            print(f"请手动访问：{url}", flush=True)
        return 0
    if command == "stop":
        status = _existing_agent(client)
        if status is None:
            print("EasySbatch Agent 未运行。", flush=True)
            return 0
        client.stop()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                client.status()
            except AgentError as exc:
                if exc.code == "AGENT_NOT_RUNNING":
                    print("EasySbatch 已断开。", flush=True)
                    return 0
            time.sleep(0.1)
        raise AgentError("AGENT_STOP_TIMEOUT")
    raise AgentError("AGENT_OPERATION_REJECTED")


def local_cluster_path():
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "EasySbatch" / "cluster.toml"


def configure_cluster(path):
    """Interactive non-secret local profile; never asks for authentication keys."""
    print("添加计算集群", flush=True)
    display_name = input("名称 [My Cluster]: ").strip() or "My Cluster"
    host = input("Host: ").strip()
    port = int(input("SSH Port [22]: ").strip() or "22")
    username = input("Username: ").strip()
    central_uid = input("EasySbatch 服务 UID（由管理员提供）: ").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", central_uid):
        raise LauncherError("LAUNCHER_CONFIG_INVALID")
    broker = f"/tmp/easysbatch-{central_uid}/broker.sock"
    worker = f"/tmp/easysbatch-{central_uid}/user-worker-v{PROTOCOL_VERSION}.py"
    config = LauncherConfig(name="my-cluster", display_name=display_name,
                            host=host, port=port, username=username,
                            remote_web_port=8000, broker_socket=broker,
                            worker_entrypoint=worker, ready_timeout_seconds=60)
    # JSON quoted strings are valid TOML basic strings for these validated
    # fields.  This file contains cluster metadata and a username only.
    lines = ["[target]", 'name = "my-cluster"',
             "display_name = " + json.dumps(config.name, ensure_ascii=False),
             "host = " + json.dumps(config.host), f"port = {config.port}",
             "remote_web_port = 8000", "broker_socket = " + json.dumps(broker),
             "worker_entrypoint = " + json.dumps(worker), "", "[launcher]",
             "connect_timeout_seconds = 12", "ready_timeout_seconds = 60",
             "handoff_timeout_seconds = 30", "", "[user]",
             "username = " + json.dumps(username), ""]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise LauncherError("LAUNCHER_CONFIG_INVALID")
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".cluster-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print("✓ 集群配置已保存", flush=True)
    return config


def main(argv=None):
    configure_console_output()
    parser = SafeParser(description="通过现有 SSH 服务启动 EasySbatch。")
    parser.add_argument("--version", action=VersionAction,
                        help="显示 Launcher、协议和构建版本。")
    parser.add_argument("--self-test", action="store_true",
                        help="检查内置配置、系统 OpenSSH 和本地端口，不连接集群。")
    parser.add_argument("--config", type=Path,
                        help="可选的管理员公开 Launcher 配置，不含 credential。")
    parser.add_argument("--configure-cluster", action="store_true",
                        help="添加或更换本机集群配置；不输入 SSH 或 AI credential。")
    parser.add_argument("--username", help="Linux 集群用户名；省略时交互输入。")
    parser.add_argument("--verbose", action="store_true",
                        help="显示受控错误分类，不输出 credential。")
    parser.add_argument("command", nargs="?", choices=("start", "status", "open", "stop", "ai"),
                        default="start", help="启动、查看、打开或停止本地 Agent。")
    parser.add_argument("ai_action", nargs="?", choices=("configure", "replace", "delete", "status", "test"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--session-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ssh-control-path", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.configure_cluster:
            configure_cluster(args.config or local_cluster_path())
            return 0
        if args.config is None and local_cluster_path().is_file():
            args.config = local_cluster_path()
        if (args.config is None and args.command == "start" and
                not args.self_test and not args.agent and
                getattr(sys.stdin, "isatty", lambda: False)()):
            args.config = local_cluster_path()
            configure_cluster(args.config)
        config = LauncherConfig.load(args.config) if args.config else LauncherConfig.bundled()
        if args.agent:
            if not args.username:
                return 2
            from .launcher_agent import run_agent
            return run_agent(
                config, args.username, ssh_control_path=args.ssh_control_path,
            )
        if args.self_test:
            if not shutil.which("ssh"):
                raise LauncherError("SSH_CLIENT_UNAVAILABLE")
            choose_local_port()
            print("Launcher 自检通过", flush=True)
            print(f"集群配置：{config.name}", flush=True)
            print("系统 OpenSSH：可用", flush=True)
            print("本地端口：可用", flush=True)
            return 0
        if args.command == "ai":
            return _ai_command(args.ai_action or "status", args)
        if args.command != "start":
            return _controller_command(args.command)
        print("EasySbatch", flush=True)
        print("─" * 28, flush=True)
        print(f"集群\n{config.name}\n", flush=True)
        return _controller_start(config, args)
    except (EOFError, KeyboardInterrupt):
        return 130
    except LauncherError as exc:
        code = exc.code
        print(ERROR_MESSAGES[code], flush=True)
        if code == "SSH_CLIENT_UNAVAILABLE":
            print(ssh_install_hint(), flush=True)
        if args.verbose:
            print(f"错误代码：{code}", flush=True)
        _pause_after_windows_error()
        return 2
    except Exception as exc:
        from .launcher_agent import AgentError, AgentVersionError

        if isinstance(exc, AgentVersionError):
            print("EasySbatch Launcher 与正在运行的 Agent 版本不兼容。", flush=True)
            print("请退出旧 Agent 后重新启动。", flush=True)
            if args.verbose:
                print(f"错误代码：{exc.code}", flush=True)
            _pause_after_windows_error()
            return 2
        if isinstance(exc, AgentError):
            messages = {
                "AGENT_NOT_RUNNING": "EasySbatch Agent 未运行。",
                "AGENT_NOT_READY": "EasySbatch 尚未连接，请先启动 Launcher。",
                "AGENT_AUTH_BUSY": "另一个 Launcher 正在完成 SSH 认证。",
                "AGENT_AUTH_BRIDGE_UNAVAILABLE": "当前平台无法安全显示 SSH 认证交互。",
                "AGENT_IDENTITY_MISMATCH": "当前 Agent 已绑定其他集群用户名。",
                "AGENT_RUNTIME_UNSAFE": "本地 Agent 运行目录不安全。",
                "SSH_USER_CONFIG_UNSAFE": "当前用户的 SSH config owner 或权限不安全。",
                "AGENT_START_TIMEOUT": "EasySbatch Agent 启动超时。",
                "AGENT_STOP_TIMEOUT": "EasySbatch Agent 未能及时停止。",
                "AGENT_START_FAILED": "EasySbatch Agent 启动失败。",
                "AI_LOCAL_CREDENTIAL_UNAVAILABLE": "未检测到可用的系统安全凭据存储。",
                "AI_CREDENTIAL_INVALID": "API Key 格式无效。",
            }
            print(messages.get(exc.code, "EasySbatch Agent 操作失败。"), flush=True)
            if args.verbose:
                print(f"错误代码：{exc.code}", flush=True)
            _pause_after_windows_error()
            return 2
        code = "LAUNCHER_INTERNAL_ERROR"
        print(ERROR_MESSAGES[code], flush=True)
        if args.verbose:
            print(f"错误代码：{code}", flush=True)
        _pause_after_windows_error()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
