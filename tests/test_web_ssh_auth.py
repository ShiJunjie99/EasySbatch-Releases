"""Offline validation of the M10-B1 fixed SSH authenticator."""

import json

from pydantic import SecretStr, ValidationError
import pytest

from sbatch_agent import web_ssh_auth as auth
from sbatch_agent.ssh_poc import ProbeProgram, SSHLoginTarget, SSHProbeError, ssh_arguments


SECRET = 'M10B1_TRANSPORT_SECRET_DO_NOT_LOG'
RealPersistentOpenSSHTransport = auth.PersistentOpenSSHTransport


def identity(username='alice', uid=1001):
    return {
        'username': username, 'uid': uid, 'gid': uid, 'groups': [username],
        'home': '/home/' + username, 'pwd': '/home/' + username,
        'hostname': 'example-cluster', 'account_uid': uid,
        'account_home': '/home/' + username, 'home_owner_uid': uid,
        'secret_like_variables_absent': True, 'ssh_session': True,
    }


class FakePersistentTransport:
    bootstrap = None
    instances = []

    def __init__(self, target, password, *, timeout):
        assert isinstance(password, SecretStr)
        assert password.get_secret_value() == SECRET
        self.target = target
        self.connected = True
        self.closed = False
        self.instances.append(self)

    def read_report(self, *, timeout):
        return self.bootstrap or json.dumps({'result': 'PASS', 'identity': identity()})

    def exchange(self, target, payload, *, timeout, interactive):
        request = json.loads(payload)
        return json.dumps({
            'operation': 'identity', 'experiment': request['experiment'],
            'identity': identity(target.username, target.expected_uid), 'result': 'PASS',
        })

    def close(self):
        self.closed = True
        self.connected = False


@pytest.fixture(autouse=True)
def fake_transport(monkeypatch):
    FakePersistentTransport.bootstrap = None
    FakePersistentTransport.instances.clear()
    monkeypatch.setattr(auth, 'PersistentOpenSSHTransport', FakePersistentTransport)


def credentials(username='alice'):
    return auth.LoginCredentials(username=username, password=SECRET)


def test_credentials_repr_redacts_secret_and_rejects_unsafe_values():
    value = credentials()
    assert SECRET not in repr(value) and '**********' in repr(value)
    for username in ('root', 'a;id', 'Alice', ''):
        with pytest.raises(ValidationError):
            auth.LoginCredentials(username=username, password=SECRET)
    for password in ('', 'line\nnext', 'x' * 1025):
        with pytest.raises(ValidationError):
            auth.LoginCredentials(username='alice', password=password)


def test_authenticator_binds_bootstrap_identity_and_fixed_verify():
    authenticator = auth.SSHPasswordAuthenticator(host='cluster.example.edu', port=22)
    context = authenticator.authenticate(credentials())
    assert context.identity.username == 'alice' and context.identity.uid == 1001
    assert context.verify_identity().username == 'alice'
    context.close()
    assert FakePersistentTransport.instances[0].closed


@pytest.mark.parametrize('bootstrap,code', [
    ('not-json', 'REMOTE_OUTPUT_INVALID'),
    ('{"result":"PASS"}', 'REMOTE_OUTPUT_INVALID'),
    (json.dumps({'result': 'FAIL', 'error_code': 'SSH_AUTH_FAILED'}), 'SSH_AUTH_FAILED'),
    (json.dumps({'result': 'PASS', 'identity': identity('bob', 1001)}), 'SSH_IDENTITY_MISMATCH'),
])
def test_bootstrap_failure_closes_transport(bootstrap, code):
    FakePersistentTransport.bootstrap = bootstrap
    authenticator = auth.SSHPasswordAuthenticator(host='cluster.example.edu', port=22)
    with pytest.raises(SSHProbeError) as exc:
        authenticator.authenticate(credentials())
    assert exc.value.code == code
    assert FakePersistentTransport.instances[0].closed
    assert SECRET not in str(exc.value)


def test_authenticator_accepts_configured_target_and_rejects_invalid_host():
    auth.SSHPasswordAuthenticator(host='other.example.edu', port=2222)
    with pytest.raises(ValueError):
        auth.SSHPasswordAuthenticator(host='bad host', port=22)


def test_web_ssh_command_is_fixed_password_only_and_has_no_browser_command():
    target = SSHLoginTarget('cluster.example.edu', 22, 'alice')
    argv = ssh_arguments(
        target, interactive=True, program=ProbeProgram.WEB_SESSION,
        password_prompt=True,
    )
    assert argv[-5:-1] == ['-l', 'alice', '--', 'cluster.example.edu']
    for option in (
        'StrictHostKeyChecking=yes', 'NumberOfPasswordPrompts=1',
        'PreferredAuthentications=password', 'PubkeyAuthentication=no',
        'KbdInteractiveAuthentication=no', 'IdentityAgent=none',
    ):
        assert option in argv
    assert 'main_web_session' in argv[-1]
    assert SECRET not in ' '.join(argv)
    with pytest.raises(ValueError):
        ssh_arguments(
            SSHLoginTarget('bad host', 22, 'alice'), interactive=True,
            program=ProbeProgram.WEB_SESSION, password_prompt=True,
        )


class FinishedProcess:
    def __init__(self, returncode):
        self.returncode = returncode

    def poll(self):
        return self.returncode


@pytest.mark.parametrize('diagnostic,code', [
    (b'Permission denied (publickey,password).', 'SSH_AUTH_FAILED'),
    (b'Host key verification failed.', 'SSH_HOST_KEY_FAILED'),
    (b'Connection refused.', 'SSH_CONNECTION_FAILED'),
])
def test_finished_persistent_transport_classifies_without_retry(monkeypatch, diagnostic, code):
    transport = object.__new__(RealPersistentOpenSSHTransport)
    transport._process = FinishedProcess(255)
    transport._master_fd = 10
    transport._stdout_fd = 11
    transport._stdout_buffer = bytearray()
    transport._diagnostic = bytearray(diagnostic)
    transport._closed = False
    monkeypatch.setattr(auth.select, 'select', lambda *args: ([], [], []))
    with pytest.raises(SSHProbeError) as exc:
        transport.read_report(timeout=1)
    assert exc.value.code == code


def test_persistent_report_timeout_is_finite(monkeypatch):
    transport = object.__new__(RealPersistentOpenSSHTransport)
    transport._process = FinishedProcess(None)
    transport._master_fd = 10
    transport._stdout_fd = 11
    transport._stdout_buffer = bytearray()
    transport._diagnostic = bytearray()
    transport._closed = False
    moments = iter((0.0, 2.0))
    monkeypatch.setattr(auth.time, 'monotonic', lambda: next(moments))
    with pytest.raises(SSHProbeError) as exc:
        transport.read_report(timeout=1)
    assert exc.value.code == 'SSH_TIMEOUT'


def test_password_uses_tty_once_and_never_argv_or_environment(monkeypatch):
    launched = []
    writes = []

    class Stdout:
        def fileno(self):
            return 12

        def close(self):
            pass

    class RunningProcess:
        returncode = None
        stdout = Stdout()

        def poll(self):
            return None

    def popen(argv, **kwargs):
        launched.append((argv, kwargs))
        return RunningProcess()

    monkeypatch.setattr(auth.pty, 'openpty', lambda: (10, 11))
    monkeypatch.setattr(auth.termios, 'tcgetattr', lambda fd: [0, 0, 0, 0, 0, 0, []])
    monkeypatch.setattr(auth.termios, 'tcsetattr', lambda *args: None)
    monkeypatch.setattr(auth.subprocess, 'Popen', popen)
    monkeypatch.setattr(auth.os, 'close', lambda fd: None)
    monkeypatch.setattr(auth.os, 'write', lambda fd, data: writes.append((fd, bytes(data))) or len(data))
    monkeypatch.setattr(auth, 'sanitized_environment', lambda environment: {
        'PATH': '/usr/bin:/bin', 'HOME': '/home/test', 'LC_ALL': 'C',
    })
    monkeypatch.setattr(RealPersistentOpenSSHTransport, '_wait_for_password_prompt',
                        lambda self, deadline: None)
    monkeypatch.setattr(RealPersistentOpenSSHTransport, '_write_line',
                        lambda self, text: writes.append(('bootstrap', text.encode())))

    RealPersistentOpenSSHTransport(
        SSHLoginTarget('cluster.example.edu', 22, 'alice'), SecretStr(SECRET), timeout=12,
    )
    assert len(launched) == 1
    argv, kwargs = launched[0]
    assert SECRET not in ' '.join(argv) and SECRET not in repr(kwargs['env'])
    assert 'SSH_AUTH_SOCK' not in kwargs['env']
    assert writes[:2] == [(10, SECRET.encode()), (10, b'\n')]
    assert json.loads(writes[2][1]) == {'username': 'alice'}
