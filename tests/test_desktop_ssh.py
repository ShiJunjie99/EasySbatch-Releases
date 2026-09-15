from pathlib import Path
from types import SimpleNamespace
import base64
import hashlib
import json

import pytest

from sbatch_agent.cluster import QUERIES
from sbatch_agent.cluster_profile import ClusterProfile
from sbatch_agent.desktop_ssh import DesktopSSHSlurmRunner
from sbatch_agent.desktop_ssh import DesktopSSHDirectoryError
from sbatch_agent.desktop_ssh import DesktopSSHPasswordRunner
from sbatch_agent.desktop_ssh import SSHHostKey
from sbatch_agent.desktop_ssh import _PROJECT_SCAN_SCRIPT
from sbatch_agent.desktop_ssh import inspect_ssh_host_key
from pydantic import SecretStr


def runner(tmp_path, calls):
    executable = tmp_path / "ssh"
    executable.write_text("synthetic", encoding="utf-8")

    def process_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"123\n", stderr=b"")

    return DesktopSSHSlurmRunner(
        profile=ClusterProfile("test", "Test cluster", "cluster.example.edu", 2222),
        username="student",
        ssh_executable=str(executable),
        process_run=process_run,
    )


def test_fixed_cluster_query_is_shell_quoted_and_uses_system_ssh(tmp_path):
    calls = []
    transport = runner(tmp_path, calls)
    result = transport.run(QUERIES["queue"], timeout=10)
    assert result.stdout == "123\n"
    argv, kwargs = calls[0]
    assert argv[0] == str(tmp_path / "ssh")
    assert "BatchMode=yes" in argv
    assert argv[-2] == "cluster.example.edu"
    assert "'--format=%i|%T|%P|%u|%r'" in argv[-1]
    assert kwargs["input"] is None
    assert kwargs["env"]["LC_ALL"]


def test_submission_sends_immutable_script_over_stdin_without_remote_path(tmp_path):
    calls = []
    transport = runner(tmp_path, calls)
    script = tmp_path / "submit.sh"
    script.write_bytes(b"#!/usr/bin/env bash\n#SBATCH --partition=cpu\n")
    result = transport.run(("sbatch", "--parsable", str(script)), timeout=30)
    assert result.returncode == 0
    argv, kwargs = calls[0]
    assert argv[-1] == "sbatch --parsable"
    assert str(script) not in " ".join(argv)
    assert kwargs["input"] == script.read_bytes()


def test_non_allowlisted_remote_command_is_refused_before_process_start(tmp_path):
    calls = []
    transport = runner(tmp_path, calls)
    with pytest.raises(ValueError, match="non-allowlisted"):
        transport.run(("bash", "-lc", "echo unsafe"), timeout=10)
    assert calls == []


def test_remote_directory_browser_is_bounded_read_only_and_quotes_path(tmp_path):
    calls = []
    executable = tmp_path / "ssh"
    executable.write_text("synthetic", encoding="utf-8")

    def process_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                b'{"path":"/home/student/project with space","entries":['
                b'{"name":"input.dat","kind":"file","size":12,"modified_ns":7},'
                b'{"name":"results","kind":"directory","size":null,"modified_ns":8}'
                b'],"truncated":false}'
            ),
            stderr=b"",
        )

    transport = DesktopSSHSlurmRunner(
        profile=ClusterProfile("test", "Test cluster", "cluster.example.edu", 2222),
        username="student", ssh_executable=str(executable), process_run=process_run,
    )
    result = transport.list_directory("/home/student/project with space")
    assert result["entries"][0]["name"] == "input.dat"
    argv, kwargs = calls[0]
    assert argv[-1].startswith("python3 -c ")
    assert "'/home/student/project with space'" in argv[-1]
    assert kwargs["input"] is None


@pytest.mark.parametrize("path", ["/", "relative", "/home/student/../root", "/home//student"])
def test_remote_directory_browser_refuses_unsafe_paths_before_ssh(tmp_path, path):
    calls = []
    transport = runner(tmp_path, calls)
    with pytest.raises(ValueError, match="remote directory"):
        transport.list_directory(path)
    assert calls == []


def test_remote_directory_browser_rejects_untrusted_response(tmp_path):
    calls = []
    transport = runner(tmp_path, calls)
    with pytest.raises(DesktopSSHDirectoryError, match="invalid"):
        transport.list_directory("/home/student")


def test_remote_project_scan_is_fixed_bounded_and_decodes_text_snapshot(tmp_path):
    compile(_PROJECT_SCAN_SCRIPT, "<remote-project-scan>", "exec")
    calls = []
    executable = tmp_path / "ssh"
    executable.write_text("synthetic", encoding="utf-8")
    content = b"python train.py\n"
    payload = {
        "path": "/home/student/project",
        "files": [{
            "path": "run.sbatch", "size": len(content), "status": "read",
            "reason": None, "mode": 0o100644,
            "data": base64.b64encode(content).decode("ascii"),
        }],
        "skipped_directories": [], "git_present": False,
        "bytes_read": len(content), "warnings": [], "limits_reached": [],
    }

    def process_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(payload).encode("utf-8"), stderr=b"",
        )

    transport = DesktopSSHSlurmRunner(
        profile=ClusterProfile("test", "Test cluster", "cluster.example.edu", 2222),
        username="student", ssh_executable=str(executable), process_run=process_run,
    )
    result = transport.scan_project("/home/student/project")
    assert result["files"][0]["data"] == content
    argv, kwargs = calls[0]
    assert argv[-1].startswith("python3 -c ")
    assert argv[-1].endswith(" /home/student/project")
    assert kwargs["input"] is None


def test_remote_project_scan_rejects_path_traversal_before_ssh(tmp_path):
    calls = []
    transport = runner(tmp_path, calls)
    with pytest.raises(ValueError, match="remote directory"):
        transport.scan_project("/home/student/../root")
    assert calls == []


def test_host_key_is_inspected_before_authentication(monkeypatch):
    raw = b"synthetic-ed25519-host-key"

    class Key:
        def get_name(self):
            return "ssh-ed25519"

        def asbytes(self):
            return raw

        def get_base64(self):
            return base64.b64encode(raw).decode("ascii")

    class Transport:
        closed = False

        def get_remote_server_key(self):
            return Key()

        def close(self):
            self.closed = True

    transport = Transport()
    monkeypatch.setattr(
        "sbatch_agent.desktop_ssh._start_ssh_transport",
        lambda host, port, timeout: transport,
    )
    result = inspect_ssh_host_key("10.158.132.77", 3088)
    assert result["host"] == "10.158.132.77"
    assert result["ssh_port"] == 3088
    assert result["host_key"]["algorithm"] == "ssh-ed25519"
    assert result["host_key"]["fingerprint"].startswith("SHA256:")
    assert transport.closed is True


def test_password_runner_keeps_password_out_of_command_and_reuses_transport(monkeypatch):
    raw = b"synthetic-ed25519-host-key"
    host_key = SSHHostKey.from_mapping({
        "algorithm": "ssh-ed25519",
        "public_key": base64.b64encode(raw).decode("ascii"),
        "fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("="),
    })
    authentication = []
    commands = []

    class Key:
        def get_name(self): return "ssh-ed25519"
        def asbytes(self): return raw
        def get_base64(self): return base64.b64encode(raw).decode("ascii")

    class Channel:
        def __init__(self):
            self.output = bytearray(b"123\n")

        def settimeout(self, _timeout): pass
        def exec_command(self, command): commands.append(command)
        def shutdown_write(self): pass
        def recv_ready(self): return bool(self.output)
        def recv(self, _size):
            result = bytes(self.output)
            self.output.clear()
            return result
        def recv_stderr_ready(self): return False
        def exit_status_ready(self): return not self.output
        def recv_exit_status(self): return 0
        def close(self): pass

    class Transport:
        authenticated = False

        def is_active(self): return True
        def is_authenticated(self): return self.authenticated
        def get_remote_server_key(self): return Key()
        def auth_password(self, username, password, **_kwargs):
            authentication.append((username, password))
            self.authenticated = True
        def set_keepalive(self, _seconds): pass
        def open_session(self, **_kwargs): return Channel()
        def close(self): pass

    transport = Transport()
    monkeypatch.setattr(
        "sbatch_agent.desktop_ssh._start_ssh_transport",
        lambda host, port, timeout: transport,
    )
    password = "SYNTHETIC_PASSWORD_NOT_FOR_LOGS"
    client = DesktopSSHPasswordRunner(
        profile=ClusterProfile("test", "Test", "10.158.132.77", 3088),
        username="student", password=SecretStr(password), host_key=host_key,
    )
    result = client.run(QUERIES["queue"], timeout=10)
    assert result.stdout == "123\n"
    assert authentication == [("student", password)]
    assert password not in commands[0]
    assert "squeue" in commands[0]
