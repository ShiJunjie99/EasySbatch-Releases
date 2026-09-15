"""Password-mode boundaries; synthetic values only, no SSH or credential input."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from sbatch_agent import ssh_poc as ssh
from sbatch_agent.ssh_poc_deployment import SSH_HOST, SSH_PORT
from sbatch_agent.ssh_poc_two_user import TwoUserContext, Phase

CHECK_TERMINAL = ssh.require_controlling_terminal


@pytest.fixture(autouse=True)
def no_real_ssh(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Unexpected SSH command or terminal password input')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(ssh, 'require_controlling_terminal', forbidden)


def target():
    return ssh.SSHExecutionTarget(SSH_HOST, SSH_PORT, 'alice', 1001)


def transport():
    return ssh.OpenSSHTransport(program=ssh.ProbeProgram.TWO_USER, password_prompt=True)


def cli():
    spec = importlib.util.spec_from_file_location(
        'password_cli', Path(__file__).parents[1] / 'scripts/ssh_two_user_poc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ARGS = ['--uid-a', '1001', '--user-b', 'bob', '--uid-b', '1002']


def test_password_only_has_no_agent_or_method_fallback():
    argv = ssh.ssh_arguments(target(), interactive=True,
                             program=ssh.ProbeProgram.TWO_USER, password_prompt=True)
    for option in ('PreferredAuthentications=password', 'PasswordAuthentication=yes',
                   'PubkeyAuthentication=no', 'KbdInteractiveAuthentication=no',
                   'HostbasedAuthentication=no', 'GSSAPIAuthentication=no',
                   'IdentityAgent=none', 'IdentityFile=none', 'BatchMode=no',
                   'NumberOfPasswordPrompts=1', 'StrictHostKeyChecking=yes',
                   'ControlPath=none', 'ControlPersist=no'):
        assert option in argv
    assert not any('sshpass' in item for item in argv)
    assert argv[-5:-1] == ['-l', 'alice', '--', SSH_HOST]


@pytest.mark.parametrize('mode', [None, 1, 'synthetic-sensitive-value'])
def test_password_mode_must_be_boolean(mode):
    with pytest.raises(ValueError):
        ssh.OpenSSHTransport(program=ssh.ProbeProgram.TWO_USER, password_prompt=mode)


def test_password_mode_cannot_change_original_single_user_contract():
    with pytest.raises(ValueError):
        ssh.OpenSSHTransport(password_prompt=True)
    with pytest.raises(ValueError):
        ssh.ssh_arguments(target(), interactive=True, password_prompt=True)
    assert 'PreferredAuthentications=password' not in ssh.ssh_arguments(target(), interactive=True)


def test_password_mode_requires_interactive_and_fixed_target():
    with pytest.raises(ValueError):
        ssh.ssh_arguments(target(), program=ssh.ProbeProgram.TWO_USER, password_prompt=True)
    other = ssh.SSHExecutionTarget('other.example.org', 22, 'alice', 1001)
    with pytest.raises(ValueError):
        ssh.ssh_arguments(other, interactive=True, program=ssh.ProbeProgram.TWO_USER,
                          password_prompt=True)


def test_no_terminal_fails_before_ssh(monkeypatch):
    def blocked():
        raise ssh.SSHProbeError('AUTHENTICATION_FLOW_BLOCKED')
    monkeypatch.setattr(ssh, 'require_controlling_terminal', blocked)
    context = TwoUserContext(target(), experiment=str(uuid4()), transport=transport(), interactive=True)
    with pytest.raises(ssh.SSHProbeError, match='AUTHENTICATION_FLOW_BLOCKED'):
        context.run(Phase.IDENTITY)
    with pytest.raises(ssh.SSHProbeError, match='SSH_CONTEXT_CLOSED'):
        context.run(Phase.IDENTITY)


def test_secret_environment_and_agent_removed_before_ssh(monkeypatch):
    monkeypatch.setattr(ssh, 'require_controlling_terminal', lambda: None)
    for key in ('PASSWORD', 'SSH_PASSWORD', 'SSH_AUTH_SOCK', 'SSH_ASKPASS',
                'SSH_ASKPASS_REQUIRE', 'DEEPSEEK_API_KEY', 'RELAY_TOKEN'):
        monkeypatch.setenv(key, 'synthetic-sensitive-value')
    observed = []
    def fake(argv, **kwargs):
        observed.append((argv, kwargs))
        assert set(kwargs['env']) == {'PATH', 'HOME', 'LC_ALL'}
        assert kwargs['input'] == '{"operation":"identity"}'
        assert kwargs['timeout'] == 75 and not kwargs.get('shell', False)
        assert 'synthetic-sensitive-value' not in repr((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, '{}', '')
    monkeypatch.setattr(subprocess, 'run', fake)
    client = transport()
    for _ in range(2):
        assert client.exchange(target(), '{"operation":"identity"}', timeout=75, interactive=True) == '{}'
    assert len(observed) == 2  # fresh process per connection; no credential cache
    assert set(vars(client)) == {'_program', '_password_prompt'}


@pytest.mark.parametrize('detail,code', [
    ('Permission denied (password)', 'SSH_AUTH_FAILED'),
    ('Host key verification failed', 'SSH_HOST_KEY_FAILED'),
    ('Connection refused', 'SSH_CONNECTION_FAILED'),
])
def test_password_auth_error_closed_and_redacted(monkeypatch, caplog, detail, code):
    monkeypatch.setattr(ssh, 'require_controlling_terminal', lambda: None)
    calls = []
    def fail(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 255, '', detail + ' synthetic-sensitive-value')
    monkeypatch.setattr(subprocess, 'run', fail)
    context = TwoUserContext(target(), experiment=str(uuid4()), transport=transport(), interactive=True)
    caplog.set_level('INFO', logger='sbatch_agent.ssh_poc_two_user')
    with pytest.raises(ssh.SSHProbeError, match=code) as exc:
        context.run(Phase.IDENTITY)
    with pytest.raises(ssh.SSHProbeError, match='SSH_CONTEXT_CLOSED'):
        context.run(Phase.SETUP)
    assert len(calls) == 1
    assert 'synthetic-sensitive-value' not in str(exc.value) + caplog.text


def test_password_prompt_timeout_closes_context(monkeypatch):
    monkeypatch.setattr(ssh, 'require_controlling_terminal', lambda: None)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('ssh', 75, stderr='synthetic-sensitive-value')
    monkeypatch.setattr(subprocess, 'run', timeout)
    context = TwoUserContext(target(), experiment=str(uuid4()), transport=transport(), interactive=True)
    with pytest.raises(ssh.SSHProbeError, match='SSH_TIMEOUT'):
        context.run(Phase.IDENTITY)
    with pytest.raises(ssh.SSHProbeError, match='SSH_CONTEXT_CLOSED'):
        context.run(Phase.IDENTITY)


def test_cli_password_mode_without_tty_does_not_connect(monkeypatch, capsys):
    module = cli()
    monkeypatch.setattr(module.sys.stdin, 'isatty', lambda: False)
    assert module.main(ARGS + ['--password-prompt', '--identity-only']) == 2
    assert json.loads(capsys.readouterr().out)['error_code'] == 'AUTHENTICATION_FLOW_BLOCKED'


@pytest.mark.parametrize('option', ['--password', '--ssh-password', '--passphrase', '--password-prompt=synthetic-sensitive-value'])
def test_no_cli_secret_argument_or_echo(option, capsys):
    with pytest.raises(SystemExit):
        cli().main(ARGS + [option, 'synthetic-sensitive-value'])
    assert 'synthetic-sensitive-value' not in capsys.readouterr().err


def test_cli_mutually_exclusive_authentication_modes(capsys):
    with pytest.raises(SystemExit):
        cli().main(ARGS + ['--password-prompt', '--interactive'])
    assert 'POC_INPUT_INVALID' in capsys.readouterr().err


def test_cli_independent_password_transports_and_original_experiment(monkeypatch, capsys):
    module = cli()
    monkeypatch.setattr(module.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(module.resource, 'setrlimit', lambda *args: None)
    captured = []
    class FakeExperiment:
        def __init__(self, a, b):
            captured.extend((a, b))
        def run(self, **kwargs):
            assert kwargs == {'authorize_two_smokes': True, 'identity_only': False}
            return {'overall': 'PASS'}
    monkeypatch.setattr(module, 'TwoUserExperiment', FakeExperiment)
    assert module.main(ARGS + ['--password-prompt', '--authorize-two-smokes']) == 0
    a, b = captured
    assert a._transport is not b._transport and a._interactive and b._interactive
    assert isinstance(a._transport, module.SSHExperimentTransport)
    assert isinstance(b._transport, module.SSHExperimentTransport)
    assert a.target.username == 'alice' and b.target.username == 'bob'
    assert json.loads(capsys.readouterr().out)['authentication_method'] == 'native-terminal-password'


@pytest.mark.parametrize('terminal_state', ['missing', 'not_tty', 'invalid_termios', 'valid'])
def test_controlling_terminal_check_reads_no_password(monkeypatch, terminal_state):
    closed = []
    def open_(path, flags):
        assert path == '/dev/tty' and flags & os.O_NOCTTY
        if terminal_state == 'missing':
            raise OSError('synthetic-sensitive-value')
        return 123
    def attributes(fd):
        if terminal_state == 'invalid_termios':
            raise ssh.termios.error('synthetic-sensitive-value')
        return []
    monkeypatch.setattr(ssh.os, 'open', open_)
    monkeypatch.setattr(ssh.os, 'isatty', lambda fd: terminal_state != 'not_tty')
    monkeypatch.setattr(ssh.os, 'close', closed.append)
    monkeypatch.setattr(ssh.termios, 'tcgetattr', attributes)
    if terminal_state == 'valid':
        CHECK_TERMINAL()
    else:
        with pytest.raises(ssh.SSHProbeError, match='AUTHENTICATION_FLOW_BLOCKED'):
            CHECK_TERMINAL()
    assert closed == ([] if terminal_state == 'missing' else [123])
