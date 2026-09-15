"""M10-A2 only: one bounded SSH process per user during one CLI experiment.

No password enters Python. No pool, reconnect, credential storage, Web route or
general remote shell API. The maintained helper accepts at most eight probes.
"""
import json
import math
import os
import select
import subprocess
import time

from .ssh_poc import (ProbeProgram, SSHExecutionTarget, SSHProbeError,
                      classify_transport, require_controlling_terminal,
                      sanitized_environment, ssh_arguments)


class SSHExperimentTransport:
    def __init__(self, target, experiment):
        from .ssh_poc_deployment import SSH_HOST, SSH_PORT
        from uuid import UUID
        if (not isinstance(target, SSHExecutionTarget) or
                (target.host, target.port) != (SSH_HOST, SSH_PORT) or
                str(UUID(experiment)) != experiment):
            raise ValueError('Invalid fixed experiment target')
        self._target = target
        self._experiment = experiment
        self._process = None
        self._closed = False
        self._count = 0
        self._stdout = bytearray()
        self._stderr = bytearray()

    def exchange(self, target, payload, *, timeout, interactive):
        if self._closed:
            raise SSHProbeError('SSH_CONTEXT_CLOSED')
        try:
            if (target != self._target or interactive is not True or
                    isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                    not math.isfinite(timeout) or not 0 < timeout <= 75 or self._count >= 8):
                raise ValueError
            request = json.loads(payload)
            if (not isinstance(request, dict) or request.get('experiment') != self._experiment or
                    request.get('username') != target.username or request.get('expected_uid') != target.expected_uid or
                    request.get('operation') not in {'identity','setup','cross_fs','slurm_readonly','submit','cross_cancel','cleanup'}):
                raise ValueError
            frame = (payload + '\n').encode('utf-8')
            if len(frame) > 8192 or '\n' in payload:
                raise ValueError
            self._count += 1
            deadline = time.monotonic() + timeout
            if self._process is None:
                require_controlling_terminal()
                environment = sanitized_environment(os.environ)
                environment.pop('SSH_AUTH_SOCK', None)
                argv = ssh_arguments(target, interactive=True, program=ProbeProgram.TWO_USER,
                                     password_prompt=True, experiment_session=True)
                self._process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.PIPE, env=environment, bufsize=0)
                for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                    os.set_blocking(stream.fileno(), False)
            self._send(frame, deadline)
            return self._receive(deadline)
        except SSHProbeError:
            self.close()
            raise
        except (ValueError, TypeError, UnicodeError):
            self.close()
            raise SSHProbeError('POC_INPUT_INVALID') from None
        except OSError:
            self.close()
            raise SSHProbeError('SSH_CONNECTION_FAILED') from None

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SSHProbeError('SSH_TIMEOUT')
        return remaining

    def _send(self, frame, deadline):
        fd = self._process.stdin.fileno()
        sent = 0
        while sent < len(frame):
            _, writable, _ = select.select([], [fd], [], self._remaining(deadline))
            if not writable:
                raise SSHProbeError('SSH_TIMEOUT')
            try:
                sent += os.write(fd, frame[sent:])
            except BlockingIOError:
                continue

    def _receive(self, deadline):
        output = self._process.stdout.fileno()
        error = self._process.stderr.fileno()
        readers = {output, error}
        while True:
            readable, _, _ = select.select(list(readers), [], [], self._remaining(deadline))
            if not readable:
                raise SSHProbeError('SSH_TIMEOUT')
            for fd in readable:
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    continue
                if not chunk:
                    readers.discard(fd)
                    continue
                buffer = self._stdout if fd == output else self._stderr
                buffer.extend(chunk)
                if len(buffer) > 32768:
                    raise SSHProbeError('REMOTE_OUTPUT_INVALID')
            if b'\n' in self._stdout:
                line, separator, tail = self._stdout.partition(b'\n')
                if tail:
                    raise SSHProbeError('REMOTE_OUTPUT_INVALID')
                self._stdout.clear()
                self._stderr.clear()
                return line.decode('utf-8')
            if not readers:
                try:
                    code = self._process.wait(timeout=min(1, self._remaining(deadline)))
                except subprocess.TimeoutExpired:
                    raise SSHProbeError('SSH_TIMEOUT') from None
                detail = self._stderr.decode('utf-8', errors='replace')
                raise SSHProbeError(classify_transport(code, detail))

    def close(self):
        self._closed = True
        process = self._process
        try:
            if process is not None:
                process.stdin.close()  # EOF makes the fixed remote helper exit
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            self._process = None
            self._stdout.clear()
            self._stderr.clear()
