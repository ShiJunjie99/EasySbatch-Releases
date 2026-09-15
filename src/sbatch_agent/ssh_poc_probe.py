"""Fixed M10-A probes, sent over SSH. Not a worker or a production execution API.

Standard library only. Never import this from application services. Output is a
small structured report; command output and exception text are never forwarded.
"""
import errno
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import time
import uuid

OPERATIONS = frozenset({'identity', 'filesystem', 'slurm_readonly', 'smoke'})
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
TERMINAL = frozenset({'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL', 'BOOT_FAIL', 'DEADLINE'})


class ProbeFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def secret_like(name):
    upper = name.upper()
    return (upper.startswith(('SBATCH_AGENT_', 'DEEPSEEK_', 'OPENAI_')) or
            any(part in upper for part in ('TOKEN', 'SECRET', 'PASSWORD', 'PASSWD', 'PRIVATE_KEY', 'API_KEY', 'CREDENTIAL')))


def call(argv, *, input=None):
    try:
        p = subprocess.run(argv, input=input, capture_output=True, text=True,
                           encoding='utf-8', errors='strict', timeout=8, check=False,
                           env=dict(os.environ))
    except subprocess.TimeoutExpired:
        raise ProbeFailure('REMOTE_TIMEOUT') from None
    except (OSError, UnicodeError):
        raise ProbeFailure('REMOTE_COMMAND_FAILED') from None
    if len(p.stdout) + len(p.stderr) > 65536:
        raise ProbeFailure('REMOTE_OUTPUT_INVALID')
    return p


def required(argv):
    p = call(argv)
    if p.returncode:
        raise ProbeFailure('REMOTE_COMMAND_FAILED')
    return p.stdout.strip()


def identity(username, expected_uid):
    try:
        if any(secret_like(k) for k in os.environ):
            raise ProbeFailure('SECRET_ENVIRONMENT_PRESENT')
        account = pwd.getpwnam(username)
        name = required(['/usr/bin/whoami'])
        uid = int(required(['/usr/bin/id', '-u']))
        gid = int(required(['/usr/bin/id', '-g']))
        groups = required(['/usr/bin/id', '-Gn']).split()
        home = os.environ.get('HOME', '')
        working = os.getcwd()
        host = required(['/usr/bin/hostname', '-f'])
        if not os.environ.get('SSH_CONNECTION'):
            raise ProbeFailure('SSH_SESSION_REQUIRED')
        capabilities = next(line.split(':', 1)[1].strip() for line in
                            Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
        if int(capabilities, 16) != 0:
            raise ProbeFailure('UNEXPECTED_FILESYSTEM_ACCESS')
        if (uid == 0 or os.getuid() != os.geteuid() or name != username or
                uid != expected_uid or uid != account.pw_uid or os.geteuid() != uid or
                gid != account.pw_gid or os.getgid() != gid or os.getegid() != gid or
                home != account.pw_dir or Path(home).stat().st_uid != uid):
            raise ProbeFailure('SSH_IDENTITY_MISMATCH')
        # Check before sanitizing, so a leak cannot be hidden by deleting it.
        result = dict(username=name, uid=uid, gid=gid, groups=groups, home=home,
                      pwd=working, hostname=host, account_uid=account.pw_uid,
                      account_home=account.pw_dir, home_owner_uid=uid,
                      secret_like_variables_absent=True, ssh_session=True)
    except (KeyError, ValueError, OSError):
        raise ProbeFailure('SSH_IDENTITY_MISMATCH') from None
    # Slurm and helper children need none of the application/login environment.
    os.environ.clear()
    os.environ.update(PATH='/usr/bin:/bin', HOME=home, USER=name, LOGNAME=name, LC_ALL='C')
    return result


def open_directory(path):
    p = Path(path)
    if not p.is_absolute() or '..' in p.parts:
        raise ProbeFailure('REMOTE_PERMISSION_DENIED')
    fd = os.open('/', DIR_FLAGS)
    try:
        for component in p.parts[1:]:
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def owned(info, uid, *, directory=False):
    if (info.st_uid != uid or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))):
        raise ProbeFailure('UNEXPECTED_FILESYSTEM_ACCESS')


def filesystem(identity_result, experiment):
    """Only enumerate the dedicated PoC directory, never HOME or a project."""
    home_fd = open_directory(identity_result['home'])
    base_fd = run_fd = None
    parent_created = run_created = False
    run_name = '.easysbatch-m10a-poc-' + experiment
    name = '.easysbatch-m10a-poc-' + str(uuid.uuid4())
    created = False
    try:
        try:
            os.mkdir('easysbatch-poc', mode=0o700, dir_fd=home_fd)
            parent_created = True
        except FileExistsError:
            pass
        base_fd = os.open('easysbatch-poc', DIR_FLAGS, dir_fd=home_fd)
        owned(os.fstat(base_fd), identity_result['uid'], directory=True)
        # Do not chmod existing paths. An unexpected writable shared directory
        # is a finding, not something to silently repair for a green test.
        if os.fstat(base_fd).st_mode & 0o077:
            raise ProbeFailure('UNEXPECTED_FILESYSTEM_ACCESS')
        os.mkdir(run_name, mode=0o700, dir_fd=base_fd)  # no adoption/retry of old runs
        run_created = True
        run_fd = os.open(run_name, DIR_FLAGS, dir_fd=base_fd)
        owned(os.fstat(run_fd), identity_result['uid'], directory=True)
        os.listdir(run_fd)  # only this newly created, empty directory
        for _ in range(3):
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=run_fd)
                break
            except FileExistsError:
                name = '.easysbatch-m10a-poc-' + str(uuid.uuid4())
        else:
            raise ProbeFailure('REMOTE_COMMAND_FAILED')
        created = True
        try:
            os.write(fd, b'M10-A PoC\n')
            info = os.fstat(fd)
            owned(info, identity_result['uid'])
            if info.st_size != 10:
                raise ProbeFailure('REMOTE_COMMAND_FAILED')
        finally:
            os.close(fd)
        current = os.stat(name, dir_fd=run_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ProbeFailure('UNEXPECTED_FILESYSTEM_ACCESS')
        os.unlink(name, dir_fd=run_fd)
        created = False
        result = dict(directory_exists=True, can_list=True, exclusive_create=True,
                      file_owner_uid=info.st_uid, write_test=True, can_stat=True,
                      temp_file_deleted=True)
        path = str(Path(identity_result['home']) / 'easysbatch-poc' / run_name)
        return result, (path, home_fd, base_fd, run_fd, parent_created)
    except BaseException as failure:
        # Remove only the file we created; never remove/recurse into old runs.
        stop = isinstance(failure, ProbeFailure) and failure.code == 'UNEXPECTED_FILESYSTEM_ACCESS'
        if created and run_fd is not None and not stop:
            os.unlink(name, dir_fd=run_fd)
        if run_fd is not None:
            os.close(run_fd)
        if run_created and not stop:
            os.rmdir(run_name, dir_fd=base_fd)
        if base_fd is not None:
            os.close(base_fd)
        if parent_created and not stop:
            os.rmdir('easysbatch-poc', dir_fd=home_fd)
        os.close(home_fd)
        raise


def slurm_readonly(username):
    import shutil
    paths = {name: shutil.which(name) for name in ('sbatch', 'squeue', 'sacct', 'scancel', 'sacctmgr')}
    if any(paths[k] != '/usr/bin/' + k for k in ('sbatch', 'squeue', 'sacct', 'scancel')):
        raise ProbeFailure('SLURM_UNAVAILABLE')
    output = required(['/usr/bin/squeue', '--local', '--noheader', '--user=' + username, '--format=%i|%u|%T'])
    rows = [line.split('|') for line in output.splitlines() if line.strip()]
    if any(len(row) != 3 or row[1] != username for row in rows):
        raise ProbeFailure('SLURM_OWNER_MISMATCH')
    assoc = 'NOT_AVAILABLE'
    if paths['sacctmgr']:
        p = call(['/usr/bin/sacctmgr', '-n', '-P', 'show', 'assoc', 'where', 'user=' + username,
                  'format=User,Account,Cluster,QOS'])
        assoc = 'QUERY_SUCCEEDED' if p.returncode == 0 else 'NOT_AVAILABLE'
    return dict(commands_available=True, current_user_queue_query=True,
                own_queue_count=len(rows), association=assoc)


def owner(job_id, username, job_name):
    # An exact PoC id + name is required; callers never accept a CLI --job-id.
    queue = call(['/usr/bin/squeue', '--local', '--noheader', '--jobs=' + job_id,
                  '--format=%i|%u|%T|%j'])
    missing = queue.returncode == 1 and not queue.stdout.strip() and queue.stderr.strip() == 'slurm_load_jobs error: Invalid job id specified'
    if queue.returncode and not missing:
        raise ProbeFailure('REMOTE_COMMAND_FAILED')
    rows = queue.stdout.splitlines()
    source = 'squeue'
    if not rows:
        p = call(['/usr/bin/sacct', '--local', '-n', '-P', '-X', '--jobs=' + job_id,
                  '--format=JobIDRaw,User,State%80,JobName%80'])
        if p.returncode:
            raise ProbeFailure('REMOTE_COMMAND_FAILED')
        rows = p.stdout.splitlines()
        source = 'sacct'
    parsed = [line.strip().split('|') for line in rows if line.strip()]
    if any(len(row) != 4 for row in parsed):
        raise ProbeFailure('REMOTE_OUTPUT_INVALID')
    found = [r for r in parsed if r[0] == job_id]
    if len(found) != 1:
        raise ProbeFailure('SLURM_OWNER_UNCONFIRMED')
    row = found[0]
    if row[1] != username or row[3] != job_name:
        raise ProbeFailure('SLURM_OWNER_MISMATCH')
    if not re.fullmatch(r'[A-Z_]+(?: by [0-9]+)?', row[2]):
        raise ProbeFailure('REMOTE_OUTPUT_INVALID')
    return dict(job_id=job_id, reported_user=row[1], state=row[2], source=source, job_name=job_name)


def smoke(identity_result, workspace, experiment, report):
    path, _, _, fd, _ = workspace
    job_name = 'easysbatch-m10a-' + experiment
    # Reservation before sbatch: never retry this submission, even on timeout.
    attempt = os.open('submission-attempted', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    os.close(attempt)
    report['submission_attempted'] = True
    script = '#!/bin/sh\nset -eu\n/usr/bin/hostname -f\n/usr/bin/whoami\n/usr/bin/id -u\n/usr/bin/sleep 1\n'
    p = call(['/usr/bin/sbatch', '--parsable', '--job-name=' + job_name,
              '--nodes=1', '--ntasks=1', '--cpus-per-task=1', '--mem=32M', '--time=00:01:00',
              '--export=NIL', '--chdir=' + path, '--output=' + path + '/smoke-%j.out',
              '--error=' + path + '/smoke-%j.err'], input=script)
    # A failed or malformed acknowledgement does NOT establish no job exists.
    if p.returncode or not re.fullmatch(r'[1-9][0-9]*\n?', p.stdout):
        raise ProbeFailure('SLURM_SUBMISSION_UNKNOWN')
    job_id = p.stdout.strip()
    report['job_id'] = job_id
    receipt = os.open('receipt.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(receipt, 'w') as f:
        json.dump(dict(job_id=job_id, username=identity_result['username'], job_name=job_name), f)
        f.flush()
        os.fsync(f.fileno())
    observed = owner(job_id, identity_result['username'], job_name)
    report['slurm_identity'] = observed
    # Bounded observation; never wait indefinitely for scheduling.
    if observed['state'].split()[0] not in TERMINAL:
        time.sleep(5)
        observed = owner(job_id, identity_result['username'], job_name)
        report['slurm_identity'] = observed
    if observed['state'].split()[0] not in TERMINAL:
        # Same freshly asserted SSH/Linux identity; only this call's receipt.
        p = call(['/usr/bin/scancel', job_id])
        if p.returncode:
            raise ProbeFailure('POC_CLEANUP_FAILED')
        report['job_cleanup'] = 'CANCEL_REQUESTED_BY_OWNER'
        for _ in range(3):
            time.sleep(2)
            observed = owner(job_id, identity_result['username'], job_name)
            if observed['state'].split()[0] in TERMINAL:
                break
    if observed['state'].split()[0] not in TERMINAL:
        raise ProbeFailure('POC_CLEANUP_UNCONFIRMED')
    report['final_slurm_identity'] = observed
    report['job_cleanup'] = 'TERMINAL_' + observed['state'].split()[0]
    # Only known output names in our new run; no recursive or project cleanup.
    for name in ('smoke-' + job_id + '.out', 'smoke-' + job_id + '.err'):
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        owned(info, identity_result['uid'])
        os.unlink(name, dir_fd=fd)
    for name in ('receipt.json', 'submission-attempted'):
        os.unlink(name, dir_fd=fd)


def execute(request):
    if (not isinstance(request, dict) or set(request) != {'operation', 'username', 'expected_uid', 'experiment'} or
            request['operation'] not in OPERATIONS or
            not isinstance(request['username'], str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', request['username']) or
            type(request['expected_uid']) is not int or request['expected_uid'] <= 0 or
            not isinstance(request['experiment'], str) or str(uuid.UUID(request['experiment'])) != request['experiment']):
        raise ProbeFailure('POC_INPUT_INVALID')
    report = dict(operation=request['operation'], experiment=request['experiment'])
    workspace = None
    try:
        report['identity'] = identity(request['username'], request['expected_uid'])
        if request['operation'] != 'identity':
            report['filesystem'], workspace = filesystem(report['identity'], request['experiment'])
        if request['operation'] in {'slurm_readonly', 'smoke'}:
            report['slurm_availability'] = slurm_readonly(request['username'])
        if request['operation'] == 'smoke':
            smoke(report['identity'], workspace, request['experiment'], report)
        report['result'] = 'PASS'
    except ProbeFailure as exc:
        report.update(result='FAIL', error_code=exc.code)
    except PermissionError:
        report.update(result='FAIL', error_code='REMOTE_PERMISSION_DENIED')
    except FileExistsError:
        report.update(result='FAIL', error_code='POC_ALREADY_EXISTS')
    except (OSError, ValueError):
        report.update(result='FAIL', error_code='REMOTE_COMMAND_FAILED')
    finally:
        if workspace:
            path, home_fd, base_fd, fd, parent_created = workspace
            try:
                # On any failure keep only our PoC artifacts for reconciliation;
                # a timeout is never permission to cancel an unknown job.
                if report.get('result') == 'PASS':
                    os.rmdir(Path(path).name, dir_fd=base_fd)
                    if parent_created:
                        os.rmdir('easysbatch-poc', dir_fd=home_fd)
                    report['filesystem_cleanup'] = 'REMOVED'
                else:
                    report['filesystem_cleanup'] = 'RETAINED_FOR_REVIEW'
            except OSError:
                report.update(result='FAIL', error_code='POC_CLEANUP_FAILED',
                              filesystem_cleanup='RETAINED_FOR_REVIEW')
            finally:
                for descriptor in (fd, base_fd, home_fd):
                    os.close(descriptor)
    return report


def main():
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ProbeFailure('POC_INPUT_INVALID')
        report = execute(json.loads(raw))
    except (ValueError, TypeError, KeyError, ProbeFailure):
        report = dict(result='FAIL', error_code='POC_INPUT_INVALID')
    except BaseException:
        report = dict(result='FAIL', error_code='REMOTE_COMMAND_FAILED')
    print(json.dumps(report, separators=(',', ':')), flush=True)


def main_web_session():
    """Authenticate once, then accept only bounded identity assertions."""
    connection = os.environ.get('SSH_CONNECTION', '')
    try:
        signal.alarm(3600)
        raw = sys.stdin.buffer.readline(1025)
        if len(raw) > 1024 or not raw.endswith(b'\n'):
            raise ProbeFailure('POC_INPUT_INVALID')
        bootstrap = json.loads(raw)
        if (not isinstance(bootstrap, dict) or set(bootstrap) != {'username'} or
                not isinstance(bootstrap['username'], str) or
                not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', bootstrap['username'])):
            raise ProbeFailure('POC_INPUT_INVALID')
        account = pwd.getpwnam(bootstrap['username'])
        first = identity(bootstrap['username'], account.pw_uid)
        print(json.dumps({'result': 'PASS', 'identity': first}, separators=(',', ':')), flush=True)

        # identity() intentionally sanitizes the process environment. Preserve
        # only the proof that this process remains inside the SSH session.
        os.environ['SSH_CONNECTION'] = connection
        for _ in range(128):
            signal.alarm(1800)
            raw = sys.stdin.buffer.readline(8193)
            if not raw:
                return
            if len(raw) > 8192 or not raw.endswith(b'\n'):
                raise ProbeFailure('POC_INPUT_INVALID')
            request = json.loads(raw)
            if (not isinstance(request, dict) or request.get('operation') != 'identity' or
                    request.get('username') != first['username'] or
                    request.get('expected_uid') != first['uid']):
                raise ProbeFailure('POC_INPUT_INVALID')
            report = execute(request)
            print(json.dumps(report, separators=(',', ':')), flush=True)
            if report.get('result') != 'PASS':
                return
            os.environ['SSH_CONNECTION'] = connection
        raise ProbeFailure('POC_INPUT_INVALID')
    except (ValueError, TypeError, KeyError, OSError, ProbeFailure) as exc:
        code = exc.code if isinstance(exc, ProbeFailure) else 'POC_INPUT_INVALID'
        print(json.dumps({'result': 'FAIL', 'error_code': code}, separators=(',', ':')), flush=True)
    except BaseException:
        print(json.dumps({'result': 'FAIL', 'error_code': 'REMOTE_COMMAND_FAILED'}, separators=(',', ':')), flush=True)
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    main()
