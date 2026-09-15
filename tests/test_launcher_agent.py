"""M10-B6 persistent Controller/Agent lifecycle and security tests."""

from io import BytesIO
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

import pytest

import sbatch_agent.launcher as launcher_module
import sbatch_agent.launcher_agent as agent_module
from sbatch_agent.launcher import Launcher, LauncherConfig
from sbatch_agent.launcher_agent import (
    AGENT_PROTOCOL_VERSION,
    CONTROL_OPERATIONS,
    AgentClient,
    AgentError,
    AgentRunner,
    PosixControlServer,
    PosixForkProcess,
    RuntimePaths,
    SocketChannel,
    WindowsControlServer,
    agent_environment,
    ensure_private_runtime,
    runtime_paths,
    spawn_agent,
    user_ssh_config,
    windows_master_arguments,
)


SECRET = "M10B6_TEST_PASSWORD_DO_NOT_LOG"


def config():
    return LauncherConfig(
        name="example-cluster", host="cluster.example.edu", port=22,
        remote_web_port=8000,
        broker_socket="/tmp/easysbatch-1000/broker.sock",
        worker_entrypoint="/tmp/easysbatch-1000/user-worker-v2.py",
    )


def paths(tmp_path):
    directory = tmp_path / "runtime"
    return RuntimePaths(
        directory, str(directory / "agent-v1.sock"),
        directory / "agent-v1.lock",
    )


class MemoryChannel:
    def __init__(self, request):
        self.request = request
        self.sent = []
        self.closed = False

    def receive(self):
        return self.request

    def send(self, value):
        self.sent.append(value)

    def close(self):
        self.closed = True


def test_runtime_path_prefers_xdg_and_is_private(tmp_path):
    selected = runtime_paths(
        environment={"XDG_RUNTIME_DIR": str(tmp_path)}, uid=1234, platform="posix",
    )
    assert selected.directory == tmp_path / "easysbatch"
    assert selected.endpoint.endswith("/agent-v1.sock")
    ensure_private_runtime(selected, uid=os.getuid())
    assert stat.S_IMODE(selected.directory.stat().st_mode) == 0o700


def test_runtime_fallback_is_per_uid_and_windows_state_is_per_profile():
    linux = runtime_paths(environment={}, uid=4321, platform="posix")
    assert linux.directory == Path("/tmp/easysbatch-agent-4321")
    first = runtime_paths(
        environment={"LOCALAPPDATA": "C:/Users/A/AppData/Local", "USERPROFILE": "A"},
        platform="nt",
    )
    second = runtime_paths(
        environment={"LOCALAPPDATA": "C:/Users/B/AppData/Local", "USERPROFILE": "B"},
        platform="nt",
    )
    assert first.endpoint != second.endpoint
    assert first.endpoint.endswith(".json")


def test_runtime_rejects_symlink_instead_of_following_it(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    selected = RuntimePaths(
        tmp_path / "runtime", str(tmp_path / "runtime" / "agent-v1.sock"),
        tmp_path / "runtime" / "agent-v1.lock",
    )
    selected.directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(AgentError, match="AGENT_RUNTIME_UNSAFE"):
        ensure_private_runtime(selected)


def test_user_ssh_config_is_explicit_safe_and_skips_bad_system_include(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    candidate = ssh_dir / "config"
    candidate.write_text("Host school\n")
    candidate.chmod(0o600)
    selected = user_ssh_config(
        {"HOME": str(tmp_path)}, platform="posix", uid=os.getuid(),
    )
    assert selected == candidate
    argv = launcher_module.ssh_arguments(
        config(), "alice", 51234, ssh_executable="/usr/bin/ssh",
        ssh_config_path=selected,
    )
    assert argv[:3] == ["/usr/bin/ssh", "-F", str(candidate)]

    candidate.chmod(0o666)
    with pytest.raises(AgentError, match="SSH_USER_CONFIG_UNSAFE"):
        user_ssh_config(
            {"HOME": str(tmp_path)}, platform="posix", uid=os.getuid(),
        )
    candidate.unlink()
    assert user_ssh_config(
        {"HOME": str(tmp_path)}, platform="posix", uid=os.getuid(),
    ) == "none"


def test_socket_control_framing_round_trip_and_bound():
    left, right = socket.socketpair()
    try:
        sender, receiver = SocketChannel(left), SocketChannel(right)
        sender.send({"version": 1, "operation": "STATUS"})
        assert receiver.receive() == {"version": 1, "operation": "STATUS"}
        right.sendall(struct.pack("!I", agent_module.MAX_CONTROL_MESSAGE + 1))
        with pytest.raises(AgentError, match="AGENT_PROTOCOL_INVALID"):
            sender.receive()
    finally:
        left.close()
        right.close()


def test_control_surface_is_fixed_and_has_no_command_execution():
    assert CONTROL_OPERATIONS == {"STATUS", "OPEN_BROWSER", "STOP", "AUTH"}
    assert not ({"RUN_SHELL", "EXEC", "CONNECT", "PROXY"} & CONTROL_OPERATIONS)
    client = AgentClient(paths=RuntimePaths(Path("."), "unused", Path("lock")))
    with pytest.raises(AgentError, match="AGENT_OPERATION_REJECTED"):
        client.request("RUN_SHELL")


def test_posix_socket_is_owner_only_and_accepts_current_peer(tmp_path):
    selected = paths(tmp_path)
    ensure_private_runtime(selected)
    server = PosixControlServer(selected)
    accepted = []

    def connect():
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(selected.endpoint)
        accepted.append(connection)

    thread = threading.Thread(target=connect)
    thread.start()
    channel = None
    deadline = time.monotonic() + 2
    while channel is None and time.monotonic() < deadline:
        channel = server.accept()
    thread.join(1)
    try:
        assert channel is not None
        assert stat.S_IMODE(Path(selected.endpoint).stat().st_mode) == 0o600
    finally:
        if channel is not None:
            channel.close()
        for connection in accepted:
            connection.close()
        server.close()


def test_posix_socket_rejects_wrong_peer_uid():
    class Connection:
        def getsockopt(self, *args):
            return struct.pack("3i", 99, os.getuid() + 1, 99)

        def close(self):
            self.closed = True

    connection = Connection()
    server = object.__new__(PosixControlServer)
    server.uid = os.getuid()
    server.listener = type("Listener", (), {"accept": lambda self: (connection, None)})()
    assert server.accept() is None
    assert connection.closed


def test_windows_fallback_is_loopback_authenticated_and_ephemeral(tmp_path):
    selected = RuntimePaths(
        tmp_path, str(tmp_path / "agent-v1-state.json"), tmp_path / "agent.lock",
    )
    ensure_private_runtime(selected)
    server = WindowsControlServer(selected)
    state = json.loads(Path(selected.endpoint).read_text())
    assert state["port"] >= 1024 and len(state["credential"]) >= 43
    assert server.listener.getsockname()[0] == "127.0.0.1"

    wrong = socket.create_connection(("127.0.0.1", state["port"]))
    SocketChannel(wrong).send({"credential": "wrong"})
    assert server.accept() is None

    right = socket.create_connection(("127.0.0.1", state["port"]))
    SocketChannel(right).send({"credential": state["credential"]})
    accepted = server.accept()
    assert accepted is not None
    accepted.close()
    server.close()
    assert not Path(selected.endpoint).exists()
    assert SECRET not in json.dumps(state)


def test_windows_master_performs_one_interactive_auth_without_secret_argv(tmp_path):
    control_path = tmp_path / "ssh-master-v1.sock"
    argv = windows_master_arguments(
        config(), "alice", control_path, ssh_executable="C:/Windows/System32/OpenSSH/ssh.exe",
    )
    assert argv.count("C:/Windows/System32/OpenSSH/ssh.exe") == 1
    assert "-M" in argv and "-fN" in argv
    assert "BatchMode=no" in argv and "NumberOfPasswordPrompts=1" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert SECRET not in repr(argv) and "sshpass" not in repr(argv).lower()
    assert argv[-1] == "cluster.example.edu"


def test_agent_ssh_reuses_windows_master_without_second_authentication():
    control_path = Path("/tmp/private-runtime/ssh-master-v1.sock")
    argv = launcher_module.ssh_arguments(
        config(), "alice", 51234, ssh_executable="/usr/bin/ssh",
        control_path=control_path,
    )
    assert "ControlMaster=no" in argv
    assert "ControlPath=" + str(control_path) in argv
    assert "-M" not in argv
    assert "BatchMode=yes" in argv and "NumberOfPasswordPrompts=0" in argv
    assert "BatchMode=no" not in argv


def test_runner_rejects_bad_version_malformed_and_unknown_operation(tmp_path):
    runner = AgentRunner(config(), "alice", paths=paths(tmp_path))
    cases = [
        ({"version": 999, "operation": "STATUS"}, "AGENT_PROTOCOL_UNSUPPORTED"),
        ({"version": 1, "operation": "RUN_SHELL"}, "AGENT_OPERATION_REJECTED"),
        ({"version": 1, "operation": "STATUS", "extra": True},
         "AGENT_PROTOCOL_INVALID"),
    ]
    for request, code in cases:
        channel = MemoryChannel(request)
        runner._handle(channel)
        assert channel.closed and channel.sent[-1]["error_code"] == code


def test_agent_environment_strips_password_keys_and_session_secrets():
    environment = agent_environment({
        "PATH": "/usr/bin", "HOME": "/home/alice",
        "XDG_RUNTIME_DIR": "/run/user/1001",
        "PASSWORD": SECRET, "DEEPSEEK_API_KEY": SECRET,
        "BOOTSTRAP_TOKEN": SECRET, "SESSION_COOKIE": SECRET,
    })
    assert environment == {
        "PATH": "/usr/bin", "HOME": "/home/alice", "LC_ALL": "C",
        "XDG_RUNTIME_DIR": "/run/user/1001",
    }
    assert SECRET not in repr(environment)


def test_spawned_agent_is_detached_and_has_no_terminal_descriptors(monkeypatch):
    captured = {}

    def create(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agent_module.sys, "frozen", True, raising=False)
    spawn_agent(username="alice", process_factory=create)
    assert captured["command"] == [sys.executable, "--agent", "--username", "alice"]
    assert captured["start_new_session"] is True
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert SECRET not in repr(captured)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY behavior")
def test_agent_owned_pty_keeps_auth_separate_from_protocol_stdout():
    program = (
        "import os,sys; "
        "fd=os.open('/dev/tty',os.O_RDWR); "
        "os.write(fd,b'Password: '); value=os.read(fd,128); "
        "os.write(1,b'PROTOCOL-ONLY\\n'); os.close(fd)"
    )
    process = PosixForkProcess(
        [sys.executable, "-c", program],
        env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
    )
    try:
        readable, _, _ = select.select([process.master_fd], [], [], 2)
        assert readable and os.read(process.master_fd, 128) == b"Password: "
        os.write(process.master_fd, (SECRET + "\n").encode())
        assert process.stdout.readline() == b"PROTOCOL-ONLY\n"
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        process.close_terminal()


def test_single_instance_lock_does_not_use_pid_as_authority(tmp_path):
    selected = paths(tmp_path)
    first = AgentRunner(config(), "alice", paths=selected)
    second = AgentRunner(config(), "alice", paths=selected)
    first._acquire_single_instance()
    try:
        with pytest.raises(AgentError, match="AGENT_ALREADY_RUNNING"):
            second._acquire_single_instance()
    finally:
        first._cleanup()
        second._cleanup()
    assert selected.lock_file.read_bytes() in {b"", b"\0"}


def test_stale_owned_socket_is_replaced_but_regular_file_is_rejected(tmp_path):
    selected = paths(tmp_path)
    ensure_private_runtime(selected)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(selected.endpoint)
    stale.close()
    server = PosixControlServer(selected)
    server.close()
    Path(selected.endpoint).write_text("not a socket")
    with pytest.raises(AgentError, match="AGENT_RUNTIME_UNSAFE"):
        PosixControlServer(selected)


def test_background_launcher_keeps_handoff_and_does_not_open_browser(monkeypatch):
    class Process:
        connected = True

        def ready(self, username, timeout):
            return {"worker_id": "c7b9b081-9089-41bd-a32d-10ba3fc5e15a",
                    "bootstrap_token": "B" * 43}

        def start_ai_egress(self, **kwargs):
            return False

        def close(self):
            self.closed = True

    class Handoff:
        url = "http://127.0.0.1:54321/"
        served = False

        def __init__(self, token, port):
            self.token, self.port = token, port

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(launcher_module, "wait_for_tunnel", lambda *a, **k: "A" * 43)
    monkeypatch.setattr(launcher_module, "bootstrap_session", lambda *a, **k: "S" * 43)
    opened = []
    connection = Launcher(
        config(), ssh_executable="/usr/bin/ssh",
        browser_open=lambda url: opened.append(url), handoff_factory=Handoff,
    ).connect(
        "alice", open_browser=False, progress=lambda *a, **k: None,
        local_port=51234, process=Process(),
    )
    assert opened == []
    assert connection.handoff.url == Handoff.url
    assert connection.local_port == 51234 and not connection.ai_available


def test_agent_lifecycle_survives_controller_disconnect_reuses_and_stops(
        tmp_path, monkeypatch):
    selected = paths(tmp_path)
    stopped = threading.Event()

    class SSH:
        connected = True

        def close(self):
            stopped.set()

    class Handoff:
        url = "http://127.0.0.1:54545/"
        served = False

        def close(self):
            pass

    class Connection:
        username = "alice"
        local_port = 51234
        ai_available = False
        handoff = Handoff()

        def wait(self):
            stopped.wait(5)
            return 0

        def close(self):
            stopped.set()

    class FakeLauncher:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            return Connection()

    class FakeSSHLauncherProcess(SSH):
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(launcher_module, "SSHLauncherProcess", FakeSSHLauncherProcess)
    runner = AgentRunner(
        config(), "alice", paths=selected, launcher_factory=FakeLauncher,
        ssh_executable="/usr/bin/ssh",
    )
    thread = threading.Thread(target=runner.run)
    thread.start()
    client = AgentClient(paths=selected, browser_open=lambda url: True)
    deadline = time.monotonic() + 3
    status_value = None
    while time.monotonic() < deadline:
        try:
            status_value = client.status()
            if status_value["state"] == "DEGRADED":
                break
        except AgentError:
            pass
        time.sleep(0.02)
    assert status_value["state"] == "DEGRADED"
    assert thread.is_alive()  # The first Controller connection has ended.
    assert client.status()["username"] == "alice"  # A second Controller reuses it.
    opened, url = client.open_browser()
    assert opened and url == Handoff.url
    client.stop()
    thread.join(8)
    assert not thread.is_alive() and stopped.is_set()
    assert not Path(selected.endpoint).exists()


def test_controller_reuses_ready_agent_without_spawning_or_auth(monkeypatch):
    calls = []

    class Client:
        def status(self):
            return {"state": "READY", "username": "alice", "cluster": "example-cluster"}

        def open_browser(self):
            calls.append("open")
            return True, "http://127.0.0.1:51234/session"

    monkeypatch.setattr(agent_module, "AgentClient", Client)
    monkeypatch.setattr(agent_module, "spawn_agent", lambda **kwargs: calls.append("spawn"))
    args = type("Args", (), {"username": None, "config": None})()
    assert launcher_module._controller_start(config(), args) == 0
    assert calls == ["open"]


def test_stop_command_is_idempotent_when_agent_is_absent(monkeypatch, capsys):
    class Client:
        def status(self):
            raise AgentError("AGENT_NOT_RUNNING")

    monkeypatch.setattr(agent_module, "AgentClient", Client)
    assert launcher_module._controller_command("stop") == 0
    assert "未运行" in capsys.readouterr().out
