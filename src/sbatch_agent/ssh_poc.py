"""M10-A only: fixed SSH probes with immutable per-user targets.

No Web routes, connection pool, key/password storage, local fallback, generic
shell input, or integration with production services. Authentication belongs to
OpenSSH (an already loaded agent or its native interactive terminal prompt).
"""
from dataclasses import dataclass
from enum import StrEnum
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import termios
import time
import tomllib
from uuid import UUID, uuid4


class Operation(StrEnum):
    IDENTITY = 'identity'
    FILESYSTEM = 'filesystem'
    SLURM_READONLY = 'slurm_readonly'
    SMOKE = 'smoke'


class ProbeProgram(StrEnum):
    SINGLE_USER = 'single_user'
    TWO_USER = 'two_user'
    WEB_SESSION = 'web_session'


ERROR_CODES = frozenset({
    'POC_INPUT_INVALID', 'POC_ALREADY_EXISTS', 'SSH_AUTH_FAILED', 'SSH_CONNECTION_FAILED',
    'SSH_HOST_KEY_FAILED', 'SSH_TIMEOUT', 'SSH_IDENTITY_MISMATCH', 'SSH_SESSION_REQUIRED',
    'REMOTE_PERMISSION_DENIED', 'REMOTE_COMMAND_FAILED', 'REMOTE_OUTPUT_INVALID',
    'REMOTE_TIMEOUT', 'SECRET_ENVIRONMENT_PRESENT', 'UNEXPECTED_FILESYSTEM_ACCESS',
    'SLURM_UNAVAILABLE', 'SLURM_OWNER_MISMATCH', 'SLURM_OWNER_UNCONFIRMED',
    'SLURM_SUBMISSION_UNKNOWN', 'POC_CLEANUP_FAILED', 'POC_CLEANUP_UNCONFIRMED',
    'AUTHENTICATION_FLOW_BLOCKED', 'SSH_CONTEXT_CLOSED', 'POC_OWNERSHIP_GUARD',
    'FILESYSTEM_POLICY_FINDING', 'SLURM_AUTHORIZATION_FINDING',
})
LOG = logging.getLogger('sbatch_agent.ssh_poc')


class SSHProbeError(RuntimeError):
    """Only an allowlisted category is printable; no transport output/cause."""
    def __init__(self, code, *, evidence=None):
        self.code = code if code in ERROR_CODES else 'REMOTE_COMMAND_FAILED'
        self.evidence = evidence or {}
        super().__init__(self.code)


def username(value):
    if (not isinstance(value, str) or value == 'root' or
            re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value) is None):
        raise ValueError('Invalid PoC username')
    return value


def host(value):
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError('Invalid PoC host')
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    if not value or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                        for part in value.split('.')):
        raise ValueError('Invalid PoC host')
    return value


@dataclass(frozen=True)
class SSHExecutionTarget:
    host: str
    port: int
    username: str
    expected_uid: int
    known_hosts: str | None = None

    def __post_init__(self):
        host(self.host)
        username(self.username)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('Invalid SSH port')
        if type(self.expected_uid) is not int or self.expected_uid <= 0:
            raise ValueError('A verified non-root UID is required')
        if self.known_hosts is not None:
            value = self.known_hosts
            if (not isinstance(value, str) or not value.startswith('/') or '..' in Path(value).parts or
                    not re.fullmatch(r'/[A-Za-z0-9_./-]+', value) or value == '/dev/null'):
                raise ValueError('Invalid known_hosts path')

    @classmethod
    def load(cls, path):
        """Trusted local deployment config only, never request/browser input."""
        try:
            with Path(path).open('rb') as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError
            data = tomllib.loads(raw.decode())
            if set(data) != {'target'}:
                raise ValueError
            return cls(**data['target'])
        except (OSError, ValueError, TypeError):
            raise ValueError('Invalid SSH PoC deployment configuration') from None


@dataclass(frozen=True)
class SSHLoginTarget:
    """Deployment-selected SSH endpoint before the remote UID is known."""
    host: str
    port: int
    username: str
    known_hosts: str | None = None

    def __post_init__(self):
        # Reuse all target validation, with a temporary non-root UID that is
        # never used for an identity assertion.
        SSHExecutionTarget(self.host, self.port, self.username, 1, self.known_hosts)


@dataclass(frozen=True)
class IdentityProbeResult:
    username: str
    uid: int
    gid: int
    groups: tuple[str, ...]
    home: str
    pwd: str
    hostname: str


def sanitized_environment(environment):
    # Positive allowlist; not only the application's known credential prefix.
    # The socket reference is passed to OpenSSH only, never logged or sent to
    # the remote environment. No identity file is loaded by this transport.
    account = pwd.getpwuid(os.getuid())
    result = {'PATH': '/usr/bin:/bin', 'HOME': account.pw_dir, 'LC_ALL': 'C'}
    if environment.get('SSH_AUTH_SOCK'):
        result['SSH_AUTH_SOCK'] = environment['SSH_AUTH_SOCK']
    return result


def require_controlling_terminal():
    """Check the native password prompt can use a terminal, without reading it."""
    fd = None
    try:
        fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        if not os.isatty(fd):
            raise OSError
        termios.tcgetattr(fd)
    except (OSError, termios.error):
        raise SSHProbeError('AUTHENTICATION_FLOW_BLOCKED') from None
    finally:
        if fd is not None:
            os.close(fd)


def ssh_arguments(target, *, interactive=False, program=ProbeProgram.SINGLE_USER,
                  password_prompt=False, experiment_session=False):
    if (not isinstance(target, (SSHExecutionTarget, SSHLoginTarget)) or type(interactive) is not bool or
            type(password_prompt) is not bool or type(experiment_session) is not bool or
            (experiment_session and not password_prompt) or
            (password_prompt and (not interactive or program not in {ProbeProgram.TWO_USER,
                                                                     ProbeProgram.WEB_SESSION})) or
            (isinstance(target, SSHLoginTarget) and program != ProbeProgram.WEB_SESSION)):
        raise ValueError('Invalid SSH PoC target or authentication mode')
    options = ['StrictHostKeyChecking=yes', 'UpdateHostKeys=no', 'CheckHostIP=no',
               'ControlMaster=no', 'ControlPath=none', 'ControlPersist=no',
               'IdentityFile=none', 'AddKeysToAgent=no', 'ForwardAgent=no',
               'ForwardX11=no', 'ClearAllForwardings=yes', 'SendEnv=-*',
               'PermitLocalCommand=no', 'ConnectTimeout=8', 'ConnectionAttempts=1',
               'ServerAliveInterval=5', 'ServerAliveCountMax=1',
               'BatchMode=no' if interactive else 'BatchMode=yes', 'NumberOfPasswordPrompts=1']
    if password_prompt:
        # OpenSSH reads /dev/tty with echo disabled. The Python process never
        # reads, forwards, caches or serializes the password. No agent fallback.
        options.extend(['PreferredAuthentications=password', 'PasswordAuthentication=yes',
                        'PubkeyAuthentication=no', 'KbdInteractiveAuthentication=no',
                        'GSSAPIAuthentication=no', 'HostbasedAuthentication=no',
                        'IdentityAgent=none'])
    if target.known_hosts:
        options.extend(['UserKnownHostsFile=' + target.known_hosts, 'GlobalKnownHostsFile=/dev/null'])
    argv = ['/usr/bin/ssh', '-F', '/dev/null', '-T', '-p', str(target.port)]
    for option in options:
        argv.extend(['-o', option])
    # Remote source is maintained code, not a template interpolating user input.
    source = Path(__file__).with_name('ssh_poc_probe.py').read_text(encoding='utf-8')
    if not isinstance(program, ProbeProgram):
        raise ValueError('Only maintained PoC programs are allowed')
    if program == ProbeProgram.TWO_USER:
        from .ssh_poc_deployment import SSH_HOST, SSH_PORT
        if (target.host, target.port) != (SSH_HOST, SSH_PORT):
            raise ValueError('M10-A2 only permits its fixed deployment target')
        # Fixed two-module payload, no remotely installed package or mutable
        # shared worker. Input never becomes source code.
        second = Path(__file__).with_name('ssh_poc_two_user_probe.py').read_text(encoding='utf-8')
        source = ('import sys, types\n'
                  'package = types.ModuleType("easysbatch_poc_payload")\n'
                  'package.__path__ = []\n'
                  'sys.modules[package.__name__] = package\n'
                  f'for name, code in {[("ssh_poc_probe", source), ("ssh_poc_two_user_probe", second)]!r}:\n'
                  '    module = types.ModuleType(package.__name__ + "." + name)\n'
                  '    module.__package__ = package.__name__\n'
                  '    sys.modules[module.__name__] = module\n'
                  '    exec(compile(code, name + ".py", "exec"), module.__dict__)\n'
                  + ('module.main_session()\n' if experiment_session else 'module.main()\n'))
    elif program == ProbeProgram.WEB_SESSION:
        # Endpoint is selected by trusted deployment config, never by the
        # browser login form.  The remote identity probe remains fixed code.
        source = ('import types\n'
                  'module = types.ModuleType("easysbatch_web_identity_probe")\n'
                  f'code = {source!r}\n'
                  'exec(compile(code, "ssh_poc_probe.py", "exec"), module.__dict__)\n'
                  'module.main_web_session()\n')
    argv.extend(['-l', target.username, '--', target.host,
                 '/usr/bin/python3 -I -c ' + shlex.quote(source)])
    return argv


def classify_transport(returncode, stderr):
    # OpenSSH has exit 255 for different transport failures. Classify in memory;
    # discard raw text, even when it contains a malicious banner or secret.
    text = stderr.lower()
    if 'host key verification failed' in text or 'remote host identification has changed' in text:
        return 'SSH_HOST_KEY_FAILED'
    if 'permission denied' in text or 'authentication failed' in text or 'too many authentication failures' in text:
        return 'SSH_AUTH_FAILED'
    return 'SSH_CONNECTION_FAILED' if returncode == 255 else 'REMOTE_COMMAND_FAILED'


class OpenSSHTransport:
    def __init__(self, *, program=ProbeProgram.SINGLE_USER, password_prompt=False):
        if (not isinstance(program, ProbeProgram) or type(password_prompt) is not bool or
                (password_prompt and program != ProbeProgram.TWO_USER)):
            raise ValueError('Only maintained PoC programs are allowed')
        self._program = program
        self._password_prompt = password_prompt

    def exchange(self, target, payload, *, timeout, interactive):
        argv = ssh_arguments(target, interactive=interactive, program=self._program,
                             password_prompt=self._password_prompt)
        environment = sanitized_environment(os.environ)
        if self._password_prompt:
            require_controlling_terminal()
            environment.pop('SSH_AUTH_SOCK', None)
        try:
            p = subprocess.run(argv, input=payload, capture_output=True, text=True,
                               encoding='utf-8', errors='strict', timeout=timeout,
                               env=environment, check=False)
        except subprocess.TimeoutExpired:
            # The remote outcome may be unknown; the caller must not retry smoke.
            raise SSHProbeError('SSH_TIMEOUT') from None
        except (OSError, UnicodeError):
            raise SSHProbeError('SSH_CONNECTION_FAILED') from None
        if p.returncode:
            raise SSHProbeError(classify_transport(p.returncode, p.stderr)) from None
        if len(p.stdout.encode()) > 32768:
            raise SSHProbeError('REMOTE_OUTPUT_INVALID')
        return p.stdout


def checked_identity(raw, target):
    try:
        expected_keys = {'username', 'uid', 'gid', 'groups', 'home', 'pwd', 'hostname',
                         'account_uid', 'account_home', 'home_owner_uid',
                         'secret_like_variables_absent', 'ssh_session'}
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise ValueError
        if (raw['username'] != target.username or type(raw['uid']) is not int or
                raw['uid'] != target.expected_uid or raw['account_uid'] != target.expected_uid or
                raw['home_owner_uid'] != target.expected_uid or raw['home'] != raw['account_home'] or
                type(raw['gid']) is not int or raw['gid'] <= 0):
            raise SSHProbeError('SSH_IDENTITY_MISMATCH')
        if raw['secret_like_variables_absent'] is not True:
            raise SSHProbeError('SECRET_ENVIRONMENT_PRESENT')
        if raw['ssh_session'] is not True:
            raise SSHProbeError('SSH_SESSION_REQUIRED')
        for field in ('home', 'pwd'):
            value = raw[field]
            if (not isinstance(value, str) or not value.startswith('/') or not value.isprintable() or
                    len(value) > 2048 or '..' in Path(value).parts):
                raise ValueError
        host(raw['hostname'])
        groups = raw['groups']
        if not isinstance(groups, list) or not 1 <= len(groups) <= 128:
            raise ValueError
        if any(not isinstance(g, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', g) for g in groups):
            raise ValueError
        return IdentityProbeResult(raw['username'], raw['uid'], raw['gid'], tuple(groups),
                                   raw['home'], raw['pwd'], raw['hostname'])
    except (ValueError, TypeError, KeyError):
        raise SSHProbeError('REMOTE_OUTPUT_INVALID') from None


def decode_report(output, target, operation, experiment):
    def unique_pairs(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError
            result[k] = v
        return result
    try:
        report = json.loads(output, object_pairs_hook=unique_pairs)
        if (not isinstance(report, dict) or report.get('operation') != operation.value or
                report.get('experiment') != experiment):
            raise ValueError
        # Failures may include completed safe phases but are not returned/logged
        # wholesale. No remote free text is an error message or a log field.
        if report.get('result') == 'FAIL':
            safe = {'experiment': experiment, 'operation': operation.value}
            if type(report.get('submission_attempted')) is bool:
                safe['submission_attempted'] = report['submission_attempted']
            job_id = report.get('job_id')
            if isinstance(job_id, str) and re.fullmatch(r'[1-9][0-9]{0,19}', job_id):
                safe['job_id'] = job_id  # for reconciliation, never cancellation authority
            if report.get('filesystem_cleanup') in {'REMOVED', 'RETAINED_FOR_REVIEW'}:
                safe['filesystem_cleanup'] = report['filesystem_cleanup']
            raise SSHProbeError(report.get('error_code', 'REMOTE_COMMAND_FAILED'), evidence=safe)
        if report.get('result') != 'PASS':
            raise ValueError
        checked_identity(report.get('identity'), target)
        allowed = {'operation', 'experiment', 'identity', 'result'}
        if operation != Operation.IDENTITY:
            allowed |= {'filesystem', 'filesystem_cleanup'}
            fs = report['filesystem']
            if (set(fs) != {'directory_exists','can_list','exclusive_create','file_owner_uid','write_test','can_stat','temp_file_deleted'} or
                    fs['file_owner_uid'] != target.expected_uid or
                    any(v is not True for k,v in fs.items() if k != 'file_owner_uid') or
                    report['filesystem_cleanup'] != 'REMOVED'):
                raise ValueError
        if operation in {Operation.SLURM_READONLY, Operation.SMOKE}:
            allowed.add('slurm_availability')
            availability = report['slurm_availability']
            if (set(availability) != {'commands_available','current_user_queue_query','own_queue_count','association'} or
                    availability['commands_available'] is not True or availability['current_user_queue_query'] is not True or
                    type(availability['own_queue_count']) is not int or availability['own_queue_count'] < 0 or
                    availability['association'] not in {'QUERY_SUCCEEDED','NOT_AVAILABLE'}):
                raise ValueError
        if operation == Operation.SMOKE:
            allowed |= {'submission_attempted','job_id','slurm_identity','final_slurm_identity','job_cleanup'}
            if report['submission_attempted'] is not True or not re.fullmatch(r'[1-9][0-9]*', report['job_id']):
                raise ValueError
            for key in ('slurm_identity', 'final_slurm_identity'):
                row = report[key]
                if set(row) != {'job_id','reported_user','state','source','job_name'}:
                    raise ValueError
                if (row['reported_user'] != target.username or row['job_id'] != report['job_id'] or
                        row['job_name'] != 'easysbatch-m10a-' + experiment):
                    raise SSHProbeError('SLURM_OWNER_MISMATCH')
                if row['source'] not in {'squeue','sacct'} or not re.fullmatch(r'[A-Z_]+(?: by [0-9]+)?', row['state']):
                    raise ValueError
            final = report['final_slurm_identity']['state'].split()[0]
            if final not in {'COMPLETED','CANCELLED','FAILED','TIMEOUT','OUT_OF_MEMORY','NODE_FAIL','BOOT_FAIL','DEADLINE'} or report['job_cleanup'] != 'TERMINAL_' + final:
                raise ValueError
        if set(report) != allowed:
            raise ValueError
        return report
    except (ValueError, KeyError, TypeError, RecursionError):
        raise SSHProbeError('REMOTE_OUTPUT_INVALID') from None


class SSHExecutionContext:
    """A target-bound short invocation. Independent contexts share no identity.

    run accepts only Operation, not shell/argv/path input. Each call opens a new
    OpenSSH connection; no automatic authentication fallback, pooling or retry.
    """
    def __init__(self, target, *, transport=None, timeout=75, interactive=False):
        if not isinstance(target, SSHExecutionTarget):
            raise ValueError('SSHExecutionTarget required')
        if (type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0 < timeout <= 120 or
                type(interactive) is not bool):
            raise ValueError('Invalid finite timeout/authentication mode')
        self._target = target
        self._transport = transport if transport is not None else OpenSSHTransport()
        self._timeout = timeout
        self._interactive = interactive
        self._submission_attempted = False

    @property
    def target(self):
        return self._target

    def run(self, operation, *, experiment=None, authorize_smoke=False):
        if not isinstance(operation, Operation) or type(authorize_smoke) is not bool:
            raise ValueError('Only fixed PoC operations are allowed')
        experiment = str(uuid4()) if experiment is None else experiment
        if not isinstance(experiment, str) or str(UUID(experiment)) != experiment:
            raise ValueError('Invalid experiment UUID')
        if operation == Operation.SMOKE:
            if not authorize_smoke or self._submission_attempted:
                raise ValueError('A single explicitly authorized PoC submission is required')
            self._submission_attempted = True  # also consumed on timeout/failure
        payload = json.dumps(dict(operation=operation.value, username=self.target.username,
                                  expected_uid=self.target.expected_uid, experiment=experiment))
        started = time.monotonic()
        code = None
        success = False
        try:
            output = self._transport.exchange(self.target, payload, timeout=self._timeout, interactive=self._interactive)
            result = decode_report(output, self.target, operation, experiment)
            success = True
            return result
        except SSHProbeError as exc:
            code = exc.code
            raise
        except Exception:
            code = 'REMOTE_COMMAND_FAILED'
            raise SSHProbeError(code) from None
        finally:
            LOG.info(json.dumps(dict(correlation_id=experiment, host=self.target.host, username=self.target.username,
                                     operation=operation.value, duration_ms=round((time.monotonic()-started)*1000),
                                     error_code=code, success=success)))
