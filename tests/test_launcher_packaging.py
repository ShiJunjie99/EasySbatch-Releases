"""M10-B4A packaging metadata and release-tool regressions."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

import sbatch_agent
from sbatch_agent import launcher_client
from sbatch_agent.launcher_version import (
    AGENT_LIFECYCLE_VERSION, LAUNCHER_VERSION, PROTOCOL_VERSION,
)
from sbatch_agent import user_worker
from sbatch_agent import worker_broker


ROOT = Path(__file__).resolve().parents[1]


def run_script(name, *arguments):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / name), *map(str, arguments)],
        text=True, capture_output=True, check=False,
    )


def test_launcher_release_version_and_worker_protocol_are_aligned():
    assert LAUNCHER_VERSION == "0.1.0-alpha.5"
    assert AGENT_LIFECYCLE_VERSION == 1
    assert PROTOCOL_VERSION == worker_broker.PROTOCOL_VERSION == user_worker.PROTOCOL_VERSION
    assert worker_broker.WORKER_FILENAME == f"user-worker-v{PROTOCOL_VERSION}.py"
    assert worker_broker.LEGACY_WORKER_FILENAME == "user-worker-v1.py"
    assert b"EASYSBATCH_WORKER_READY_V2" in worker_broker.LEGACY_WORKER_GUARD_SOURCE


def test_public_server_api_remains_available_through_lazy_imports():
    assert sbatch_agent.JobSpec.__name__ == "JobSpec"
    assert callable(sbatch_agent.render_job_script)
    assert "JobRepository" in dir(sbatch_agent)


def test_cross_platform_client_environment_keeps_os_paths_but_strips_secrets():
    secret = "M10B4A_TEST_CREDENTIAL_DO_NOT_PACKAGE"
    environment = {
        "PATH": "path", "HOME": "home", "SYSTEMROOT": "system",
        "WINDIR": "windows", "USERPROFILE": "profile", "TEMP": "temp",
        "SSH_AUTH_SOCK": "agent", "SSH_PASSWORD": secret,
        "DEEPSEEK_API_KEY": secret,
    }
    sanitized = launcher_client.sanitized_environment(environment)
    assert sanitized["SYSTEMROOT"] == "system"
    assert sanitized["SSH_AUTH_SOCK"] == "agent"
    assert secret not in repr(sanitized)
    assert "SSH_PASSWORD" not in sanitized and "DEEPSEEK_API_KEY" not in sanitized


def test_platform_open_ssh_hints_are_safe(monkeypatch):
    monkeypatch.setattr(launcher_client.os, "name", "nt")
    assert "Windows" in launcher_client.ssh_install_hint()
    monkeypatch.setattr(launcher_client.os, "name", "posix")
    assert "openssh-client" in launcher_client.ssh_install_hint()


def test_build_stamp_accepts_only_public_git_revision(tmp_path):
    output = tmp_path / "build.json"
    passed = run_script("stamp_launcher_build.py", "0123456789abcdef", "--output", output)
    assert passed.returncode == 0
    assert json.loads(output.read_text()) == {"commit": "0123456789abcdef"}

    rejected = run_script("stamp_launcher_build.py", "$(credential)", "--output", output)
    assert rejected.returncode != 0
    assert "credential" not in output.read_text()


def test_checksum_script_uses_only_approved_release_asset_names(tmp_path):
    approved = tmp_path / "EasySbatch-Linux-x86_64"
    approved.write_bytes(b"launcher")
    (tmp_path / "unrelated.log").write_text("ignore me")
    result = run_script("write_launcher_checksums.py", tmp_path)
    assert result.returncode == 0
    expected = hashlib.sha256(b"launcher").hexdigest()
    assert (tmp_path / "SHA256SUMS").read_text() == (
        f"{expected}  EasySbatch-Linux-x86_64\n"
    )


def test_artifact_secret_scan_rejects_key_material(tmp_path):
    clean = tmp_path / "EasySbatch-Linux-x86_64"
    clean.write_bytes(b"example-cluster cluster.example.edu 22")
    assert run_script("scan_launcher_artifact.py", clean).returncode == 0
    clean.write_bytes(b"BEGIN OPENSSH PRIVATE KEY")
    failed = run_script("scan_launcher_artifact.py", clean)
    assert failed.returncode != 0
    assert "PRIVATE KEY" not in failed.stdout


def test_macos_assets_are_a_real_application_bundle_recipe():
    plist = (ROOT / "packaging/macos/Info.plist").read_text()
    wrapper = ROOT / "packaging/macos/EasySbatch"
    script = (ROOT / "packaging/macos/launch.applescript").read_text()
    assert "CFBundlePackageType" in plist and "APPL" in plist
    assert "CFBundleExecutable" in plist and "EasySbatch" in plist
    assert wrapper.read_text().startswith("#!/bin/sh")
    assert "/usr/bin/osascript" in wrapper.read_text()
    assert 'application "Terminal"' in script and "quoted form" in script
    assert "password" not in (wrapper.read_text() + script).lower()


def test_release_workflow_uses_native_runners_and_never_needs_credentials():
    workflow = (ROOT / ".github/workflows/build-launcher.yml").read_text()
    assert "ubuntu-22.04" in workflow
    assert "windows-latest" in workflow
    assert "macos-15" in workflow and "architecture: arm64" in workflow
    assert "macos-15-intel" in workflow and "architecture: x86_64" in workflow
    assert "--self-test" in workflow and "SHA256SUMS" in workflow
    assert "unexpected Agent status smoke result" in workflow
    assert "$global:LASTEXITCODE = 0" in workflow
    assert "workflow_dispatch" in workflow
    assert '"launcher-build/**"' in workflow
    forbidden = ("SSH_PASSWORD", "DEEPSEEK_API_KEY", "StrictHostKeyChecking=no", "sshpass")
    assert all(value not in workflow for value in forbidden)


@pytest.mark.parametrize("name", [
    "EasySbatch-Windows-x86_64.exe",
    "EasySbatch-macOS-arm64.zip",
    "EasySbatch-macOS-arm64.dmg",
    "EasySbatch-macOS-x86_64.zip",
    "EasySbatch-macOS-x86_64.dmg",
    "EasySbatch-Linux-x86_64",
])
def test_release_asset_names_are_accepted(name, tmp_path):
    path = tmp_path / name
    path.write_bytes(name.encode("ascii"))
    assert run_script("write_launcher_checksums.py", tmp_path).returncode == 0
