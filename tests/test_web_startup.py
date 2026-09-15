"""Private config/launcher tests use temp files and fake server/model only."""

import importlib.util
import os
from pathlib import Path
import signal
import socket
import sys

import pytest

from sbatch_agent.model_client import ModelConfig, OpenAICompatibleClient
from sbatch_agent.scanner import ProjectScanner


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("web_startup", ROOT / "scripts/start_web.py")
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Launcher tests must not access network, stop a real process or call a model")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    monkeypatch.setattr(OpenAICompatibleClient, "generate_structured", forbidden)
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.chdir(ROOT)  # Restore cwd after main() changes it.


@pytest.fixture
def configuration(tmp_path):
    path = tmp_path / "deepseek.toml"
    path.write_text((ROOT / "config/deepseek.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_template_loads_explicit_config_without_key_and_no_network(configuration, tmp_path):
    values, port = startup.load_settings(configuration, tmp_path)
    os.environ.update(values)
    config = ModelConfig.from_env()
    assert config.provider == "deepseek" and config.endpoint == "https://api.deepseek.com/chat/completions"
    assert OpenAICompatibleClient(config).availability().state == "credential_missing"
    assert port == 8000 and values["SBATCH_AGENT_DATABASE_PATH"] == str(tmp_path / ".sbatch-agent/jobs.sqlite3")


def test_config_values_are_literal_and_secret_stays_in_backend(configuration, tmp_path, monkeypatch, capsys):
    text = configuration.read_text().replace("<your-key>", "offline-secret-$(never-run)")
    configuration.write_text(text)
    seen = []
    monkeypatch.setattr(startup, "ROOT", tmp_path)
    monkeypatch.setattr(startup, "serve", lambda root, port, restart: seen.append((root, port, restart)))
    assert startup.main(["--config", str(configuration)]) == 0
    config = ModelConfig.from_env()
    assert os.environ[config.api_key_env] == "offline-secret-$(never-run)"
    assert "offline-secret" not in capsys.readouterr().out
    assert seen == [(tmp_path, 8000, False)]
    assert not (tmp_path / "never-run").exists()


def test_missing_credential_still_starts_manual_web(configuration, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(startup, "serve", lambda *args: calls.append(args))
    assert startup.main(["--config", str(configuration)]) == 0
    assert calls and "手工表单" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    '[ai]\napi_key = "DO-NOT-PRINT',
    'password = "DO-NOT-PRINT"',
    '[ai]\napi_key = 123',
])
def test_bad_configuration_redacts_parse_errors(configuration, bad, capsys):
    configuration.write_text(bad)
    assert startup.main(["--config", str(configuration)]) == 2
    output = capsys.readouterr().out
    assert "DO-NOT-PRINT" not in output and "配置" in output and "Traceback" not in output


def test_private_permissions_required(configuration):
    configuration.chmod(0o644)
    with pytest.raises(startup.StartupError, match="600"):
        startup.load_settings(configuration, ROOT)


def test_large_config_rejected_without_leaking_content(configuration):
    configuration.write_text("PRIVATE" * startup.CONFIG_LIMIT)
    with pytest.raises(startup.StartupError, match="过大"):
        startup.load_settings(configuration, ROOT)


def test_unknown_or_public_host_configuration_rejected(configuration):
    configuration.write_text(configuration.read_text() + '\nhost = "0.0.0.0"\n')
    with pytest.raises(startup.StartupError):
        startup.load_settings(configuration, ROOT)


def test_explicit_profiles_and_paths_preserved(configuration, tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text("{}")
    configuration.write_text(configuration.read_text().replace('profiles_path = ""', 'profiles_path = "profiles.json"'))
    values, _ = startup.load_settings(configuration, tmp_path)
    assert values["SBATCH_AGENT_PROFILES_PATH"] == str(profiles)
    assert not (tmp_path / ".sbatch-agent/jobs.sqlite3").exists()


def test_missing_profiles_does_not_replace_with_empty_profile(configuration):
    configuration.write_text(configuration.read_text().replace('profiles_path = ""', 'profiles_path = "missing.json"'))
    with pytest.raises(startup.StartupError, match="profiles_path"):
        startup.load_settings(configuration, ROOT)


def test_optional_ca_bundle_only_changes_backend_environment(configuration, tmp_path):
    ca_file = tmp_path / "system-ca.pem"
    ca_file.write_text("offline-fixture")
    configuration.write_text(configuration.read_text().replace('ca_bundle = ""', 'ca_bundle = "system-ca.pem"'))
    values, _ = startup.load_settings(configuration, tmp_path)
    assert values["SSL_CERT_FILE"] == str(ca_file)
    assert ca_file.read_text() == "offline-fixture"


def test_missing_ca_bundle_file_fails_without_silently_disabling_tls(configuration, tmp_path):
    configuration.write_text(configuration.read_text().replace('ca_bundle = ""', 'ca_bundle = "missing-ca.pem"'))
    with pytest.raises(startup.StartupError, match="ca_bundle"):
        startup.load_settings(configuration, tmp_path)


def test_old_config_without_ca_bundle_preserves_existing_ssl_environment(configuration, tmp_path, monkeypatch):
    configuration.write_text('\n'.join(line for line in configuration.read_text().splitlines() if not line.startswith('ca_bundle')))
    monkeypatch.setenv("SSL_CERT_FILE", "previous-user-ca.pem")
    values, _ = startup.load_settings(configuration, tmp_path)
    assert "SSL_CERT_FILE" not in values
    assert os.environ["SSL_CERT_FILE"] == "previous-user-ca.pem"


def test_explicit_system_ca_keeps_hostname_and_certificate_verification(configuration):
    import ssl
    configuration.write_text(configuration.read_text().replace('ca_bundle = ""', 'ca_bundle = "system-ca.pem"'))
    # No network: test the actual standard-library TLS defaults with this
    # machine's preinstalled public CA bundle, if present.
    system_ca = Path('/etc/ssl/cert.pem')
    if not system_ca.is_file():
        pytest.skip('No system CA fixture on this platform')
    (configuration.parent / 'system-ca.pem').write_bytes(system_ca.read_bytes())
    values, _ = startup.load_settings(configuration, configuration.parent)
    os.environ.update(values)
    # The fixture isolates os.environ as a Python dict; pass the selected path
    # explicitly here because OpenSSL reads the real C process environment.
    context = ssl.create_default_context(cafile=values['SSL_CERT_FILE'])
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()['x509_ca'] > 0


def test_check_ai_loads_same_environment_without_starting_web(configuration, monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setitem(sys.modules, "smoke_ai_provider", SimpleNamespace(main=lambda argv: calls.append((argv, ModelConfig.from_env())) or 2))
    monkeypatch.setattr(startup, "serve", lambda *a: pytest.fail("Check must not start Web"))
    assert startup.main(["--config", str(configuration), "--check-ai"]) == 2
    assert len(calls) == 1 and calls[0][0] == [] and calls[0][1].provider == "deepseek"


def test_scanner_skips_private_config_directory(tmp_path, configuration):
    state = tmp_path / ".sbatch-agent"
    state.mkdir()
    (state / "deepseek.toml").write_text(configuration.read_text().replace("<your-key>", "PRIVATE"))
    evidence = ProjectScanner().scan(tmp_path)
    assert ".sbatch-agent" in evidence.skipped_directories
    assert "PRIVATE" not in evidence.model_dump_json()


def test_restart_refuses_pid_of_unrelated_process(tmp_path):
    pid_file = tmp_path / "web.pid"
    pid_file.write_text(str(os.getppid()))
    with pytest.raises(startup.StartupError, match="核实"):
        startup.restart_preview(pid_file, tmp_path, 8000)


def test_no_pid_requires_no_stop(tmp_path):
    startup.restart_preview(tmp_path / "missing.pid", tmp_path, 8000)


@pytest.mark.parametrize("command", [
    ["python", "-m", "uvicorn", "sbatch_agent.web:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000"],
    ["python", "scripts/start_web.py", "--restart"],
])
def test_restart_stops_only_verified_preview_with_sigterm(tmp_path, monkeypatch, command):
    proc_root = tmp_path / "proc"
    proc = proc_root / "1234"
    proc.mkdir(parents=True)
    (proc / "cwd").symlink_to(tmp_path, target_is_directory=True)
    (proc / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in command))
    (proc / "stat").write_text("1234 (python) S")
    pid_file = tmp_path / "web.pid"
    pid_file.write_text("1234")
    monkeypatch.setattr(startup, "Path", lambda value: proc_root if value == "/proc" else Path(value))
    calls = []
    def stop(pid, sig):
        calls.append((pid, sig))
        (proc / "stat").unlink()  # Simulate successful graceful exit.
    monkeypatch.setattr(os, "kill", stop)
    startup.restart_preview(pid_file, tmp_path, 8000)
    assert calls == [(1234, signal.SIGTERM)]


def test_occupied_port_does_not_overwrite_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / ".sbatch-agent/web.pid"
    pid_file.parent.mkdir()
    pid_file.write_text("previous-pid")
    monkeypatch.setattr(socket.socket, "bind", lambda *args: (_ for _ in ()).throw(OSError("occupied")))
    with pytest.raises(startup.StartupError, match="端口"):
        startup.serve(tmp_path, 8000, False)
    assert pid_file.read_text() == "previous-pid"


def test_server_uses_loopback_existing_factory_and_cleans_pid(tmp_path, monkeypatch):
    import uvicorn
    bound = []
    monkeypatch.setattr(socket.socket, "bind", lambda sock, address: bound.append(address))
    class FakeServer:
        def __init__(self, config):
            assert config.app == "sbatch_agent.web:create_app" and config.factory
            assert config.host == "127.0.0.1" and not config.reload
            assert not config.proxy_headers and config.forwarded_allow_ips == ''
        def run(self, sockets):
            assert len(sockets) == 1
            assert (tmp_path / ".sbatch-agent/web-8123.pid").read_text() == str(os.getpid())
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    state = tmp_path / ".sbatch-agent"
    state.mkdir()
    (state / "web.pid").write_text("existing-8000-pid")
    startup.serve(tmp_path, 8123, False)
    assert bound == [("127.0.0.1", 8123)]
    assert not (state / "web-8123.pid").exists()
    assert (state / "web.pid").read_text() == "existing-8000-pid"


def test_workspace_config_is_deployment_environment_only(configuration, tmp_path):
    configuration.write_text(configuration.read_text() + '\nworkspace_root = "workspace"\nmax_entries_per_directory = 42\n')
    values, _ = startup.load_settings(configuration, tmp_path)
    assert values["SBATCH_AGENT_WORKSPACE_ROOT"] == str(tmp_path / "workspace")
    assert values["SBATCH_AGENT_FOLDER_MAX_ENTRIES"] == "42"
    assert not (tmp_path / "workspace").exists()  # no discovery or creation


def test_m10b1_authentication_settings_are_fixed_deployment_only(configuration, tmp_path):
    text = configuration.read_text().replace('authentication_enabled = false', 'authentication_enabled = true')
    configuration.write_text(text)
    values, _ = startup.load_settings(configuration, tmp_path)
    assert values['SBATCH_AGENT_WEB_AUTH_ENABLED'] == '1'
    assert values['SBATCH_AGENT_SSH_HOST'] == 'cluster.example.edu'
    assert values['SBATCH_AGENT_SSH_PORT'] == '22'
    assert values['SBATCH_AGENT_SESSION_IDLE_SECONDS'] == '1800'
    assert values['SBATCH_AGENT_SESSION_COOKIE_SECURE'] == '0'
    assert values['SBATCH_AGENT_DEPLOYMENT_MODE'] == 'loopback_legacy'
    assert values['SBATCH_AGENT_PUBLIC_BASE_URL'] == ''


def test_m10b3_ssh_first_settings_enable_fixed_local_broker(configuration, tmp_path):
    text = configuration.read_text()
    text = text.replace('authentication_enabled = false', 'authentication_enabled = true')
    text = text.replace('deployment_mode = "loopback_legacy"', 'deployment_mode = "ssh_first"')
    text += '\nworker_broker_socket = "/tmp/easysbatch-1000/broker.sock"\n'
    configuration.write_text(text)
    values, _ = startup.load_settings(configuration, tmp_path)
    assert values['SBATCH_AGENT_DEPLOYMENT_MODE'] == 'ssh_first'
    assert values['SBATCH_AGENT_WORKER_BROKER_SOCKET'] == (
        '/tmp/easysbatch-1000/broker.sock'
    )
    assert values['SBATCH_AGENT_WEB_AUTH_ENABLED'] == '1'
    assert values['SBATCH_AGENT_SESSION_COOKIE_SECURE'] == '0'
    assert values['SBATCH_AGENT_PUBLIC_BASE_URL'] == ''


def test_m10b3_command_mode_is_reversible_and_does_not_rewrite_private_config(
        configuration, tmp_path, monkeypatch):
    before = configuration.read_bytes()
    seen = []
    monkeypatch.setattr(startup, "ROOT", tmp_path)
    monkeypatch.setattr(startup, "serve", lambda root, port, restart: seen.append(
        (root, port, restart, dict(os.environ))
    ))
    assert startup.main([
        "--config", str(configuration), "--restart", "--ssh-first",
    ]) == 0
    environment = seen[0][3]
    assert environment["SBATCH_AGENT_DEPLOYMENT_MODE"] == "ssh_first"
    assert environment["SBATCH_AGENT_WEB_AUTH_ENABLED"] == "1"
    assert environment["SBATCH_AGENT_SESSION_COOKIE_SECURE"] == "0"
    assert environment["SBATCH_AGENT_PUBLIC_BASE_URL"] == ""
    assert environment["SBATCH_AGENT_WORKER_BROKER_SOCKET"] == (
        f"/tmp/easysbatch-{os.geteuid()}/broker.sock"
    )
    assert configuration.read_bytes() == before


@pytest.mark.parametrize('change', [
    ('authentication_enabled = true', 'authentication_enabled = false'),
    ('session_cookie_secure = false', 'session_cookie_secure = true'),
    ('worker_broker_socket = "/tmp/easysbatch-1000/broker.sock"',
     'worker_broker_socket = "/tmp/arbitrary.sock"'),
])
def test_invalid_m10b3_ssh_first_settings_fail_closed(configuration, tmp_path, change):
    text = configuration.read_text()
    text = text.replace('deployment_mode = "loopback_legacy"', 'deployment_mode = "ssh_first"')
    text = text.replace('authentication_enabled = false', 'authentication_enabled = true')
    text += '\nworker_broker_socket = "/tmp/easysbatch-1000/broker.sock"\n'
    text = text.replace(*change)
    configuration.write_text(text)
    with pytest.raises(startup.StartupError):
        startup.load_settings(configuration, tmp_path)


def test_m10b2_lan_https_settings_require_secure_authenticated_fixed_origin(configuration, tmp_path):
    text = configuration.read_text()
    text = text.replace('authentication_enabled = false', 'authentication_enabled = true')
    text = text.replace('session_cookie_secure = false', 'session_cookie_secure = true')
    text = text.replace('deployment_mode = "loopback_legacy"', 'deployment_mode = "lan_https"')
    text = text.replace('public_base_url = ""',
                        'public_base_url = "https://cluster.example.edu:18443"')
    configuration.write_text(text)
    values, _ = startup.load_settings(configuration, tmp_path)
    assert values['SBATCH_AGENT_DEPLOYMENT_MODE'] == 'lan_https'
    assert values['SBATCH_AGENT_PUBLIC_BASE_URL'] == 'https://cluster.example.edu:18443'
    assert values['SBATCH_AGENT_SESSION_COOKIE_SECURE'] == '1'


@pytest.mark.parametrize('replacement', [
    ('session_cookie_secure = false', 'session_cookie_secure = false'),
    ('authentication_enabled = false', 'authentication_enabled = false'),
    ('public_base_url = ""', 'public_base_url = "http://cluster.example.edu:18443"'),
    ('public_base_url = ""', 'public_base_url = "https://other.example.edu:18443"'),
    ('public_base_url = ""', 'public_base_url = "https://cluster.example.edu:18443/login"'),
])
def test_invalid_m10b2_lan_https_settings_fail_closed(configuration, tmp_path, replacement):
    text = configuration.read_text()
    text = text.replace('deployment_mode = "loopback_legacy"', 'deployment_mode = "lan_https"')
    text = text.replace(*replacement)
    if replacement[0] != 'authentication_enabled = false':
        text = text.replace('authentication_enabled = false', 'authentication_enabled = true')
    if replacement[0] != 'session_cookie_secure = false':
        text = text.replace('session_cookie_secure = false', 'session_cookie_secure = true')
    if replacement[0] != 'public_base_url = ""':
        text = text.replace('public_base_url = ""',
                            'public_base_url = "https://cluster.example.edu:18443"')
    configuration.write_text(text)
    with pytest.raises(startup.StartupError):
        startup.load_settings(configuration, tmp_path)


def test_lan_https_uvicorn_stays_loopback_and_only_trusts_loopback_proxy(
        tmp_path, monkeypatch, capsys):
    import uvicorn
    captured = []
    monkeypatch.setenv('SBATCH_AGENT_DEPLOYMENT_MODE', 'lan_https')
    monkeypatch.setenv('SBATCH_AGENT_PUBLIC_BASE_URL', 'https://cluster.example.edu:18443')
    monkeypatch.setenv('SBATCH_AGENT_WEB_AUTH_ENABLED', '1')
    monkeypatch.setattr(socket.socket, 'bind', lambda sock, address: captured.append(('bind', address)))

    class FakeServer:
        def __init__(self, config):
            captured.append(('config', config))

        def run(self, sockets):
            captured.append(('run', len(sockets)))

    monkeypatch.setattr(uvicorn, 'Server', FakeServer)
    startup.serve(tmp_path, 8124, False)
    config = next(item[1] for item in captured if item[0] == 'config')
    assert config.host == '127.0.0.1' and config.workers == 1
    assert config.proxy_headers is True and config.forwarded_allow_ips == '127.0.0.1'
    assert 'https://cluster.example.edu:18443/login' in capsys.readouterr().out


def test_ssh_first_uvicorn_is_loopback_single_worker_without_proxy_trust(
        tmp_path, monkeypatch, capsys):
    import uvicorn
    captured = []
    monkeypatch.setenv('SBATCH_AGENT_DEPLOYMENT_MODE', 'ssh_first')
    monkeypatch.setenv('SBATCH_AGENT_WEB_AUTH_ENABLED', '1')
    monkeypatch.setattr(socket.socket, 'bind', lambda sock, address: captured.append(('bind', address)))

    class FakeServer:
        def __init__(self, config):
            captured.append(('config', config))

        def run(self, sockets):
            captured.append(('run', len(sockets)))

    monkeypatch.setattr(uvicorn, 'Server', FakeServer)
    startup.serve(tmp_path, 8125, False)
    uvicorn_config = next(item[1] for item in captured if item[0] == 'config')
    assert uvicorn_config.host == '127.0.0.1' and uvicorn_config.workers == 1
    assert uvicorn_config.proxy_headers is False and uvicorn_config.forwarded_allow_ips == ''
    output = capsys.readouterr().out
    assert 'easysbatch-launcher' in output and 'http://127.0.0.1:8125' not in output


@pytest.mark.parametrize(('enabled', 'path'), [('0', '/new'), ('1', '/login')])
def test_startup_landing_path_preserves_legacy_mode(monkeypatch, enabled, path):
    monkeypatch.setenv('SBATCH_AGENT_WEB_AUTH_ENABLED', enabled)
    assert startup.landing_path() == path


@pytest.mark.parametrize('setting', [
    'ssh_host = "bad host"', 'ssh_port = 0',
    'session_idle_timeout_seconds = 10', 'session_cookie_secure = "false"',
])
def test_invalid_m10b1_authentication_settings_rejected(configuration, tmp_path, setting):
    field = setting.split(' =', 1)[0]
    lines = [line for line in configuration.read_text().splitlines()
             if not line.startswith(field + ' =')]
    configuration.write_text('\n'.join(lines) + '\n' + setting + '\n')
    with pytest.raises(startup.StartupError):
        startup.load_settings(configuration, tmp_path)


@pytest.mark.parametrize("setting", ['workspace_root = 12', 'workspace_root = ""',
                                   'max_entries_per_directory = 0', 'max_entries_per_directory = 501'])
def test_invalid_workspace_settings_rejected(configuration, tmp_path, setting):
    configuration.write_text(configuration.read_text() + '\n' + setting + '\n')
    with pytest.raises(startup.StartupError):
        startup.load_settings(configuration, tmp_path)
