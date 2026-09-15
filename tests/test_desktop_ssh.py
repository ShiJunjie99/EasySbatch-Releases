from pathlib import Path
from types import SimpleNamespace

import pytest

from sbatch_agent.cluster import QUERIES
from sbatch_agent.cluster_profile import ClusterProfile
from sbatch_agent.desktop_ssh import DesktopSSHSlurmRunner
from sbatch_agent.desktop_ssh import DesktopSSHDirectoryError


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
