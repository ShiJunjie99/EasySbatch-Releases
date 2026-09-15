"""M10-B3 Launcher tests use fake OpenSSH, HTTP and browser transports."""

from io import BytesIO
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

import sbatch_agent.launcher as launcher_module
from sbatch_agent.launcher_version import PROTOCOL_VERSION, version_text
from sbatch_agent.launcher import (
    Launcher,
    LauncherConfig,
    LauncherConnection,
    LauncherError,
    SSHLauncherProcess,
    bootstrap_session,
    ssh_arguments,
    wait_for_tunnel,
)


SECRET = "M10B3_BOOTSTRAP_SECRET_DO_NOT_LOG_0123456789"


def config():
    return LauncherConfig(
        name="example-cluster",
        host="cluster.example.edu", port=22, remote_web_port=8000,
        broker_socket="/tmp/easysbatch-1000/broker.sock",
        worker_entrypoint="/tmp/easysbatch-1000/user-worker-v2.py",
    )


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=None):
        self.stdout = BytesIO(stdout)
        self.stdin = BytesIO()
        self.stderr = BytesIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            if timeout is not None:
                self.returncode = -15
            else:
                self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class Response:
    def __init__(self, status, *, headers=(), body=b""):
        self.status = status
        self._headers = list(headers)
        self._body = body

    def read(self, limit):
        return self._body[:limit]

    def getheaders(self):
        return list(self._headers)

    def getheader(self, name):
        for key, value in self._headers:
            if key.lower() == name.lower():
                return value
        return None


class HTTP:
    def __init__(self, response, capture):
        self.response = response
        self.capture = capture

    def request(self, method, path, body=None, headers=None):
        self.capture.append((method, path, body, headers or {}))

    def getresponse(self):
        return self.response

    def close(self):
        pass


def factory_for(response, capture):
    return lambda host, port, timeout: HTTP(response, capture)


def ready_line(username="alice", token=SECRET):
    return launcher_module.WORKER_READY_PREFIX + json.dumps({
        "version": PROTOCOL_VERSION, "event": "ready", "worker_id": str(uuid4()),
        "bootstrap_token": token, "username": username, "uid": 1001,
        "ttl_seconds": 45,
    }, separators=(",", ":")).encode() + b"\n"


def test_public_launcher_config_is_fixed_and_contains_no_credential(tmp_path):
    source = Path("config/ssh_first.alpha.toml")
    loaded = LauncherConfig.load(source)
    assert (loaded.host, loaded.port, loaded.remote_web_port) == (
        "cluster.example.edu", 22, 8000,
    )
    assert loaded.ready_timeout_seconds == 60
    assert loaded.name == "example-cluster"
    assert loaded.worker_entrypoint.endswith("/user-worker-v2.py")
    bundled = LauncherConfig.bundled()
    assert vars(bundled) == vars(loaded)
    text = source.read_text(encoding="utf-8").lower()
    assert "password" not in text and "private key" not in text and "token" not in text

    bad = tmp_path / "launcher.toml"
    bad.write_text(source.read_text().replace("cluster.example.edu", "bad host"))
    with pytest.raises(LauncherError, match="LAUNCHER_CONFIG_INVALID"):
        LauncherConfig.load(bad)


def test_openssh_argv_uses_one_connection_fixed_tunnel_and_fixed_worker():
    argv = ssh_arguments(
        config(), "alice", 51234, ssh_executable="/usr/bin/ssh",
    )
    assert argv[0] == "/usr/bin/ssh" and argv.count("/usr/bin/ssh") == 1
    assert "-R" not in argv
    forward = argv.index("-L")
    assert argv[forward:forward + 2] == ["-L", "127.0.0.1:51234:127.0.0.1:8000"]
    assert "Hostname=cluster.example.edu" in argv and "User=alice" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert not any("StrictHostKeyChecking=no" in value for value in argv)
    assert SECRET not in repr(argv) and "sshpass" not in repr(argv).lower()
    assert "NumberOfPasswordPrompts=1" in argv
    assert argv[-2] == "cluster.example.edu"
    assert argv[-1] == (
        "/usr/bin/python3 -I /tmp/easysbatch-1000/user-worker-v2.py "
        "--socket /tmp/easysbatch-1000/broker.sock"
    )
    assert " -c " not in argv[-1]
    assert "127.0.0.1:8000" not in argv[-1]
    with pytest.raises(ValueError):
        ssh_arguments(config(), "alice;id", 51234, ssh_executable="/usr/bin/ssh")


def test_openssh_process_strips_application_secrets_from_child_environment(monkeypatch):
    captured = {}

    def create(argv, **kwargs):
        captured.update(kwargs)
        return FakeProcess(returncode=0)

    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("SBATCH_AGENT_LOCAL_AI_KEY", SECRET)
    process = SSHLauncherProcess(["/usr/bin/ssh"], process_factory=create)
    process._stderr_thread.join(1)
    assert SECRET not in repr(captured["env"])
    assert set(captured["env"]) <= {
        "PATH", "HOME", "LC_ALL", "SSH_AUTH_SOCK", "USERPROFILE", "SYSTEMROOT",
        "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    }
    assert captured["stdin"] is subprocess.PIPE


@pytest.mark.parametrize(("stderr", "returncode", "code"), [
    (b"Permission denied (publickey,password).", 255, "SSH_AUTH_FAILED"),
    (b"Host key verification failed.", 255, "SSH_HOST_KEY_FAILED"),
    (b"Connection timed out", 255, "SSH_CONNECTION_FAILED"),
    (b"remote worker exited", 2, "WORKER_START_FAILED"),
])
def test_ssh_auth_host_key_connection_and_worker_failures(stderr, returncode, code):
    process = SSHLauncherProcess(
        ["/usr/bin/ssh"],
        process_factory=lambda *a, **k: FakeProcess(stderr=stderr, returncode=returncode),
    )
    process._stderr_thread.join(1)
    with pytest.raises(LauncherError, match=code):
        process.ready("alice", timeout=0.2)


def test_remote_stderr_is_bounded_and_never_mirrored_to_launcher_output(capsys):
    process = SSHLauncherProcess(
        ["/usr/bin/ssh"], process_factory=lambda *a, **k:
        FakeProcess(stderr=SECRET.encode(), returncode=255),
    )
    process._stderr_thread.join(1)
    with pytest.raises(LauncherError) as caught:
        process.ready("alice", timeout=0.2)
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""
    assert SECRET not in repr(caught.value)


def test_ready_report_rejects_identity_mismatch_and_malformed_output():
    mismatch = SSHLauncherProcess(
        ["/usr/bin/ssh"], process_factory=lambda *a, **k:
        FakeProcess(stdout=ready_line("mallory"), returncode=None),
    )
    with pytest.raises(LauncherError, match="WORKER_IDENTITY_MISMATCH"):
        mismatch.ready("alice", timeout=0.2)
    mismatch.close()

    for output in (
            launcher_module.WORKER_READY_PREFIX + b"not-json\n",
            launcher_module.WORKER_READY_PREFIX + b"{}",
    ):
        process = SSHLauncherProcess(
            ["/usr/bin/ssh"], process_factory=lambda *a, output=output, **k:
            FakeProcess(stdout=output, returncode=None),
        )
        with pytest.raises(LauncherError, match="LAUNCHER_PROTOCOL_INVALID"):
            process.ready("alice", timeout=0.2)
        process.close()


def test_ready_rejects_unsupported_worker_protocol_with_friendly_category():
    for output in (
        launcher_module.WORKER_READY_PREFIX_ROOT + b"1 {}\n",
        launcher_module.WORKER_READY_PREFIX + json.dumps({
            "version": 1, "event": "ready", "worker_id": str(uuid4()),
            "bootstrap_token": SECRET, "username": "alice", "uid": 1001,
            "ttl_seconds": 45,
        }).encode() + b"\n",
    ):
        process = SSHLauncherProcess(
            ["/usr/bin/ssh"], process_factory=lambda *a, output=output, **k:
            FakeProcess(stdout=output, returncode=None),
        )
        with pytest.raises(LauncherError, match="LAUNCHER_PROTOCOL_UNSUPPORTED"):
            process.ready("alice", timeout=0.2)
        process.close()


def test_ready_ignores_bounded_remote_shell_banner():
    process = SSHLauncherProcess(
        ["/usr/bin/ssh"], process_factory=lambda *a, **k:
        FakeProcess(stdout=b"module banner\n" + ready_line(), returncode=None),
    )
    assert process.ready("alice", timeout=0.2)["username"] == "alice"
    process.close()


def test_tunnel_readiness_uses_http_and_returns_anonymous_cookie():
    capture = []
    anonymous = "A" * 43
    response = Response(200, headers=[(
        "Set-Cookie",
        f"easysbatch_ssh_first_session={anonymous}; Path=/; HttpOnly; SameSite=Strict",
    )])
    process = type("Process", (), {"connected": True})()
    result = wait_for_tunnel(
        51234, process, timeout=1, clock=lambda: 0,
        sleep=lambda value: None, connection_factory=factory_for(response, capture),
    )
    assert result == anonymous
    assert capture == [("GET", "/login", None, {"Host": "127.0.0.1:51234"})]


def test_tunnel_process_exit_fails_closed_without_fallback():
    process = type("Process", (), {
        "connected": False,
        "_failure_code": lambda self: "TUNNEL_START_FAILED",
    })()
    with pytest.raises(LauncherError, match="TUNNEL_START_FAILED"):
        wait_for_tunnel(
            51234, process, timeout=1, clock=lambda: 0,
            sleep=lambda value: None, connection_factory=lambda *a, **k: None,
        )


def test_bootstrap_posts_token_not_url_rotates_cookie_and_has_no_token_response():
    capture = []
    session_token = "S" * 43
    response = Response(303, headers=[
        ("Location", "/session"),
        ("Set-Cookie", f"easysbatch_ssh_first_session={session_token}; Path=/; HttpOnly; SameSite=Strict"),
    ])
    worker_id = str(uuid4())
    result = bootstrap_session(
        51234, worker_id, SECRET, anonymous_token="A" * 43,
        connection_factory=factory_for(response, capture),
    )
    assert result == session_token
    method, path, body, headers = capture[0]
    assert method == "POST" and path == "/auth/ssh-bootstrap"
    assert SECRET.encode() in body and SECRET not in path
    assert headers["Cookie"].endswith("=" + "A" * 43)
    assert headers["Host"] == "127.0.0.1:51234"
    assert headers["Origin"] == "http://127.0.0.1:51234"


def test_ai_egress_credential_uses_bootstrap_body_not_ssh_argv_or_url():
    capture = []
    session_token = "S" * 43
    response = Response(303, headers=[
        ("Location", "/session"),
        ("X-EasySbatch-AI-Status", "available"),
        ("Set-Cookie", f"easysbatch_ssh_first_session={session_token}; Path=/; HttpOnly; SameSite=Strict"),
    ])
    egress_secret = "E" * 43
    status = []
    result = bootstrap_session(
        51234, str(uuid4()), SECRET, anonymous_token="A" * 43,
        ai_egress_port=54321, ai_egress_credential=egress_secret,
        status_result=status, connection_factory=factory_for(response, capture),
    )
    assert result == session_token and status == ["available"]
    method, path, body, _ = capture[0]
    assert method == "POST" and path == "/auth/ssh-bootstrap"
    assert egress_secret.encode() in body
    assert egress_secret not in path
    argv = ssh_arguments(
        config(), "alice", 51234, ssh_executable="/usr/bin/ssh",
        ai_egress_local_port=52345,
    )
    assert egress_secret not in repr(argv)


def test_bootstrap_failure_is_closed_and_secret_not_in_exception():
    with pytest.raises(LauncherError) as caught:
        bootstrap_session(
            51234, str(uuid4()), SECRET, anonymous_token="A" * 43,
            connection_factory=factory_for(Response(401, body=SECRET.encode()), []),
        )
    assert caught.value.code == "BOOTSTRAP_FAILED"
    assert SECRET not in repr(caught.value)


def test_launcher_success_browser_open_and_worker_lifecycle(monkeypatch):
    created = []

    class Process:
        connected = True

        def __init__(self, *args, **kwargs):
            self.closed = False
            created.append(self)

        def ready(self, username, timeout):
            return {
                "worker_id": str(uuid4()), "bootstrap_token": SECRET,
                "username": username,
            }

        def wait(self):
            return 0

        def close(self):
            self.closed = True

    class Handoff:
        url = "http://127.0.0.1:54321/"

        def __init__(self, token, port):
            assert token == "S" * 43 and port == 51234
            self.closed = False

        def start(self):
            pass

        def wait(self, timeout):
            return True

        def close(self):
            self.closed = True

    monkeypatch.setattr(launcher_module, "choose_local_port", lambda: 51234)
    monkeypatch.setattr(launcher_module, "SSHLauncherProcess", Process)
    monkeypatch.setattr(launcher_module, "wait_for_tunnel", lambda *a, **k: "A" * 43)
    monkeypatch.setattr(launcher_module, "bootstrap_session", lambda *a, **k: "S" * 43)
    opened = []
    connection = Launcher(
        config(), browser_open=lambda url: opened.append(url) or True,
        handoff_factory=Handoff, ssh_executable="/usr/bin/ssh",
    ).connect("alice")
    assert connection.username == "alice" and connection.local_port == 51234
    assert opened == [Handoff.url] and not created[0].closed
    connection.close()
    assert created[0].closed


def test_browser_failure_keeps_manual_handoff_and_connection_alive(monkeypatch, capsys):
    class Process:
        connected = True
        instance = None

        def __init__(self, *a, **k):
            self.closed = False
            self.__class__.instance = self

        def ready(self, username, timeout):
            return {"worker_id": str(uuid4()), "bootstrap_token": SECRET}

        def wait(self):
            return 0

        def close(self):
            self.closed = True

    class Handoff:
        url = "http://127.0.0.1:54321/"
        instance = None
        def __init__(self, *a):
            self.closed = False
            self.__class__.instance = self
        def start(self): pass
        def wait(self, timeout): return True
        def close(self): self.closed = True

    monkeypatch.setattr(launcher_module, "choose_local_port", lambda: 51234)
    monkeypatch.setattr(launcher_module, "SSHLauncherProcess", Process)
    monkeypatch.setattr(launcher_module, "wait_for_tunnel", lambda *a, **k: "A" * 43)
    monkeypatch.setattr(launcher_module, "bootstrap_session", lambda *a, **k: "S" * 43)
    connection = Launcher(
        config(), browser_open=lambda url: False, handoff_factory=Handoff,
        ssh_executable="/usr/bin/ssh",
    ).connect("alice")
    assert not Process.instance.closed and connection.handoff is Handoff.instance
    assert Handoff.url in capsys.readouterr().out
    connection.close()
    assert Process.instance.closed and Handoff.instance.closed


def test_ctrl_c_and_remote_exit_cleanup(capsys):
    class Interrupting:
        def __init__(self): self.closed = False
        def wait(self): raise KeyboardInterrupt
        def close(self): self.closed = True
    process = Interrupting()
    assert LauncherConnection(process, "alice", 51234).wait() == 0
    assert process.closed

    class Exited:
        def wait(self): return 2
        def close(self): pytest.fail("already-exited process should not require kill")
    assert LauncherConnection(Exited(), "alice", 51234).wait() == 2
    assert "连接已断开" in capsys.readouterr().out


def test_version_output_is_stable_and_matches_worker_protocol(capsys):
    with pytest.raises(SystemExit) as caught:
        launcher_module.main(["--version"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert output == version_text() + "\n"
    assert f"protocol {PROTOCOL_VERSION}" in output


def test_missing_ssh_has_friendly_platform_hint(monkeypatch, capsys):
    monkeypatch.setattr(launcher_module.shutil, "which", lambda command: None)
    assert launcher_module.main(["--username", "alice"]) == 2
    output = capsys.readouterr().out
    assert "未检测到 OpenSSH 客户端" in output
    assert "openssh-client" in output
    assert "Traceback" not in output


def test_network_failure_says_authentication_has_not_started(monkeypatch, capsys):
    monkeypatch.setattr(
        launcher_module, "_controller_start",
        lambda config, args: (_ for _ in ()).throw(LauncherError("SSH_CONNECTION_FAILED")),
    )
    assert launcher_module.main(["--username", "alice"]) == 2
    output = capsys.readouterr().out
    assert "密码验证尚未开始" in output
    assert "校园网或学校 VPN" in output
    assert "配置无效" not in output


def test_invalid_username_has_own_safe_error_instead_of_config_error(capsys):
    assert launcher_module.main(["--username", "Alice;id"]) == 2
    output = capsys.readouterr().out
    assert "用户名格式无效" in output
    assert "配置无效" not in output


def test_unexpected_runtime_failure_is_not_misreported_as_config(monkeypatch, capsys):
    monkeypatch.setattr(
        launcher_module, "_controller_start",
        lambda config, args: (_ for _ in ()).throw(
            ValueError("runtime detail must stay private")
        ),
    )
    assert launcher_module.main(["--username", "alice", "--verbose"]) == 2
    output = capsys.readouterr().out
    assert "LAUNCHER_INTERNAL_ERROR" in output
    assert "配置无效" not in output
    assert "runtime detail" not in output


def test_packaged_windows_error_pause_requires_an_interactive_console(monkeypatch):
    class Stream:
        def __init__(self, interactive):
            self.interactive = interactive

        def isatty(self):
            return self.interactive

    prompts = []
    monkeypatch.setattr(launcher_module.os, "name", "nt")
    monkeypatch.setattr(launcher_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    launcher_module._pause_after_windows_error(
        input_stream=Stream(False), output_stream=Stream(True),
    )
    assert prompts == []
    launcher_module._pause_after_windows_error(
        input_stream=Stream(True), output_stream=Stream(True),
    )
    assert prompts == ["按 Enter 键退出。"]


def test_packaging_self_test_loads_bundled_resource_ssh_and_local_port(monkeypatch, capsys):
    monkeypatch.setattr(launcher_module.shutil, "which", lambda command: "/usr/bin/ssh")
    monkeypatch.setattr(launcher_module, "choose_local_port", lambda: 54321)
    assert launcher_module.main(["--self-test"]) == 0
    output = capsys.readouterr().out
    assert "Launcher 自检通过" in output
    assert "集群配置：example-cluster" in output
    assert "系统 OpenSSH：可用" in output
    assert "本地端口：可用" in output


def test_local_port_allocation_failure_is_bounded():
    def denied(*args):
        raise PermissionError

    with pytest.raises(LauncherError, match="TUNNEL_START_FAILED"):
        launcher_module.choose_local_port(bind=denied, attempts=2)


def test_console_output_reconfigures_utf8_without_exposing_traceback():
    class Stream:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    stdout, stderr = Stream(), Stream()
    original_stdout, original_stderr = launcher_module.sys.stdout, launcher_module.sys.stderr
    try:
        launcher_module.sys.stdout = stdout
        launcher_module.sys.stderr = stderr
        launcher_module.configure_console_output()
    finally:
        launcher_module.sys.stdout = original_stdout
        launcher_module.sys.stderr = original_stderr
    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]
