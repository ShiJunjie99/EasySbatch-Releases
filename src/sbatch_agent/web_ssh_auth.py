"""M10-B1 password authentication bound to the verified SSH PoC context.

The browser password is used once through OpenSSH's controlling terminal.  It
is never placed in argv, the child environment, a file, or the resulting Web
session.  Python strings cannot promise reliable memory zeroization; callers
must drop their references immediately after this method returns.
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import termios
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from .ssh_poc import (
    IdentityProbeResult,
    Operation,
    ProbeProgram,
    SSHExecutionContext,
    SSHExecutionTarget,
    SSHLoginTarget,
    SSHProbeError,
    checked_identity,
    classify_transport,
    sanitized_environment,
    ssh_arguments,
    username as validate_username,
)
from .ssh_poc_deployment import SSH_HOST, SSH_PORT


class LoginCredentials(BaseModel):
    """A deliberately short-lived request value with a redacted repr."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    username: str
    password: SecretStr

    @field_validator('username')
    @classmethod
    def valid_username(cls, value):
        return validate_username(value)

    @field_validator('password')
    @classmethod
    def bounded_password(cls, value):
        raw = value.get_secret_value()
        if not raw or len(raw) > 1024 or any(character in raw for character in ('\n', '\r', '\x00')):
            del raw
            raise ValueError('Invalid SSH credential')
        del raw
        return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class PersistentOpenSSHTransport:
    """One authenticated OpenSSH process; only fixed identity probes are sent."""

    def __init__(self, target: SSHLoginTarget, password: SecretStr, *, timeout: float = 12):
        if not isinstance(target, SSHLoginTarget) or not isinstance(password, SecretStr):
            raise ValueError('Validated login target and credential required')
        if type(timeout) not in (int, float) or not 1 <= timeout <= 30:
            raise ValueError('Invalid finite authentication timeout')
        self._login_target = target
        self._process = None
        self._master_fd = None
        self._stdout_fd = None
        self._stdout_buffer = bytearray()
        self._diagnostic = bytearray()
        self._closed = False
        self._start(password, timeout=float(timeout))

    @property
    def connected(self):
        return not self._closed and self._process is not None and self._process.poll() is None

    def _start(self, password, *, timeout):
        master_fd = slave_fd = None
        process = None
        try:
            master_fd, slave_fd = pty.openpty()
            attributes = termios.tcgetattr(slave_fd)
            attributes[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
            argv = ssh_arguments(self._login_target, interactive=True,
                                 program=ProbeProgram.WEB_SESSION, password_prompt=True)
            environment = sanitized_environment(os.environ)
            environment.pop('SSH_AUTH_SOCK', None)
            process = subprocess.Popen(
                ['/usr/bin/setsid', '--fork', '--wait', '--ctty', *argv],
                stdin=slave_fd, stdout=subprocess.PIPE, stderr=slave_fd,
                close_fds=True, env=environment,
            )
            os.close(slave_fd)
            slave_fd = None
            self._process = process
            self._master_fd = master_fd
            self._stdout_fd = process.stdout.fileno()
            master_fd = None
            deadline = time.monotonic() + timeout
            self._wait_for_password_prompt(deadline)

            raw = password.get_secret_value()
            encoded = bytearray(raw.encode('utf-8', errors='strict'))
            del raw
            try:
                os.write(self._master_fd, encoded)
                os.write(self._master_fd, b'\n')
            finally:
                for index in range(len(encoded)):
                    encoded[index] = 0
                del encoded
            self._write_line(json.dumps({'username': self._login_target.username}, separators=(',', ':')))
        except SSHProbeError:
            self.close()
            raise
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
            if process is not None:
                self._process = process
            if master_fd is not None:
                os.close(master_fd)
            if slave_fd is not None:
                os.close(slave_fd)
            self.close()
            raise SSHProbeError('SSH_CONNECTION_FAILED') from None

    def _remember_diagnostic(self, data):
        remaining = 32768 - len(self._diagnostic)
        if remaining > 0:
            self._diagnostic.extend(data[:remaining])

    def _wait_for_password_prompt(self, deadline):
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                code = classify_transport(self._process.returncode,
                                          self._diagnostic.decode('utf-8', errors='replace'))
                raise SSHProbeError(code)
            ready, _, _ = select.select([self._master_fd], [], [], min(0.2, deadline - time.monotonic()))
            if not ready:
                continue
            try:
                data = os.read(self._master_fd, 4096)
            except OSError:
                data = b''
            if not data:
                continue
            self._remember_diagnostic(data)
            if b'password:' in self._diagnostic.lower():
                return
        raise SSHProbeError('SSH_TIMEOUT')

    def _write_line(self, text):
        if not self.connected or not isinstance(text, str) or len(text.encode()) > 8192:
            raise SSHProbeError('SSH_CONTEXT_CLOSED')
        try:
            os.write(self._master_fd, text.encode('utf-8', errors='strict') + b'\n')
        except (OSError, UnicodeError):
            raise SSHProbeError('SSH_CONNECTION_FAILED') from None

    def read_report(self, *, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            newline = self._stdout_buffer.find(b'\n')
            if newline >= 0:
                raw = bytes(self._stdout_buffer[:newline])
                del self._stdout_buffer[:newline + 1]
                if not raw or len(raw) > 32768:
                    raise SSHProbeError('REMOTE_OUTPUT_INVALID')
                try:
                    return raw.decode('utf-8', errors='strict')
                except UnicodeError:
                    raise SSHProbeError('REMOTE_OUTPUT_INVALID') from None
            if len(self._stdout_buffer) > 32768:
                raise SSHProbeError('REMOTE_OUTPUT_INVALID')
            descriptors = [self._stdout_fd]
            if self._master_fd is not None:
                descriptors.append(self._master_fd)
            wait = max(0, min(0.2, deadline - time.monotonic()))
            ready, _, _ = select.select(descriptors, [], [], wait)
            for descriptor in ready:
                try:
                    data = os.read(descriptor, 4096)
                except OSError:
                    data = b''
                if descriptor == self._stdout_fd:
                    if data:
                        self._stdout_buffer.extend(data)
                elif data:
                    self._remember_diagnostic(data)
            if self._process.poll() is not None and b'\n' not in self._stdout_buffer:
                code = classify_transport(self._process.returncode,
                                          self._diagnostic.decode('utf-8', errors='replace'))
                raise SSHProbeError(code)
        raise SSHProbeError('SSH_TIMEOUT')

    def exchange(self, target, payload, *, timeout, interactive):
        if (not isinstance(target, SSHExecutionTarget) or
                (target.host, target.port, target.username) !=
                (self._login_target.host, self._login_target.port, self._login_target.username) or
                interactive is not False):
            raise SSHProbeError('SSH_IDENTITY_MISMATCH')
        self._write_line(payload)
        return self.read_report(timeout=timeout)

    def close(self):
        if self._closed:
            return
        self._closed = True
        master_fd, process = self._master_fd, self._process
        self._master_fd = None
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=.2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        if process is not None and process.stdout is not None:
            process.stdout.close()


@dataclass
class BoundSSHExecutionContext:
    """One verified Linux identity and its one live SSH process."""

    execution: SSHExecutionContext
    transport: PersistentOpenSSHTransport
    identity: IdentityProbeResult

    @property
    def connected(self):
        return self.transport.connected

    def verify_identity(self):
        report = self.execution.run(Operation.IDENTITY)
        current = checked_identity(report['identity'], self.execution.target)
        if current != self.identity:
            self.close()
            raise SSHProbeError('SSH_IDENTITY_MISMATCH')
        return current

    def close(self):
        self.transport.close()


class SSHPasswordAuthenticator:
    """One POST maps to exactly one OpenSSH authentication attempt."""

    def __init__(self, *, host, port, known_hosts=None, timeout=12):
        self._host = host
        self._port = port
        self._known_hosts = known_hosts
        self._timeout = timeout
        # Validate deployment configuration before serving requests.
        SSHLoginTarget(host, port, 'validation_user', known_hosts)

    def authenticate(self, credentials: LoginCredentials):
        if not isinstance(credentials, LoginCredentials):
            raise ValueError('LoginCredentials required')
        target = SSHLoginTarget(self._host, self._port, credentials.username, self._known_hosts)
        transport = PersistentOpenSSHTransport(target, credentials.password, timeout=self._timeout)
        try:
            line = transport.read_report(timeout=self._timeout)
            try:
                report = json.loads(line, object_pairs_hook=_unique_pairs)
            except (ValueError, TypeError, RecursionError):
                raise SSHProbeError('REMOTE_OUTPUT_INVALID') from None
            raw = report.get('identity') if isinstance(report, dict) else None
            if (not isinstance(report, dict) or set(report) != {'result', 'identity'} or
                    report.get('result') != 'PASS' or not isinstance(raw, dict) or
                    type(raw.get('uid')) is not int or raw['uid'] <= 0):
                if isinstance(report, dict) and report.get('result') == 'FAIL':
                    raise SSHProbeError(report.get('error_code', 'REMOTE_COMMAND_FAILED'))
                raise SSHProbeError('REMOTE_OUTPUT_INVALID')
            verified_target = SSHExecutionTarget(target.host, target.port, target.username,
                                                 raw['uid'], target.known_hosts)
            identity = checked_identity(raw, verified_target)
            execution = SSHExecutionContext(verified_target, transport=transport,
                                            timeout=min(30, self._timeout), interactive=False)
            return BoundSSHExecutionContext(execution, transport, identity)
        except Exception:
            transport.close()
            raise
