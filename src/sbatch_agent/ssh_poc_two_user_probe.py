"""M10-A2 fixed remote operations. No generic commands, paths or job IDs API."""
import errno
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import sys
import time
from uuid import UUID, uuid4

from . import ssh_poc_probe as base

DIRECTORY = 'easysbatch-m10a2-poc'
OPERATIONS = {'identity', 'setup', 'cross_fs', 'slurm_readonly', 'submit', 'cross_cancel', 'cleanup'}
F = base.ProbeFailure


def valid_user(value):
    if not isinstance(value, str) or value == 'root' or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value):
        raise F('POC_INPUT_INVALID')


def exclusive_json(fd, name, value):
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    with os.fdopen(out, 'w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())


def read_marker(fd, name, uid):
    raw = os.open(name, base.FILE_FLAGS, dir_fd=fd)
    with os.fdopen(raw, 'rb') as stream:
        info = os.fstat(stream.fileno())
        base.owned(info, uid)
        if info.st_size > 1024:
            raise F('POC_OWNERSHIP_GUARD')
        return json.loads(stream.read(1025))


def receipt(ident, experiment, parent, private):
    return dict(username=ident['username'], uid=ident['uid'], home=ident['home'], experiment=experiment,
                base_dev=os.fstat(parent).st_dev, base_ino=os.fstat(parent).st_ino,
                private_dev=os.fstat(private).st_dev, private_ino=os.fstat(private).st_ino)


def setup(ident, experiment):
    home = base.open_directory(ident['home'])
    parent = private = None
    try:
        # Refuse any preexisting directory; never chmod/adopt another run.
        os.mkdir(DIRECTORY, mode=0o700, dir_fd=home)
        parent = os.open(DIRECTORY, base.DIR_FLAGS, dir_fd=home)
        os.mkdir('private-test', mode=0o700, dir_fd=parent)
        private = os.open('private-test', base.DIR_FLAGS, dir_fd=parent)
        for fd in (parent, private):
            base.owned(os.fstat(fd), ident['uid'], directory=True)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise F('FILESYSTEM_POLICY_FINDING')
        marker = dict(experiment=experiment, uid=ident['uid'])
        exclusive_json(parent, 'experiment.json', marker)
        os.listdir(private)  # newly created empty directory only
        name = '.easysbatch-m10a2-' + str(uuid4())
        out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=private)
        try:
            os.write(out, b'M10-A2\n')
            info = os.fstat(out)
            base.owned(info, ident['uid'])
            current = os.stat(name, dir_fd=private, follow_symlinks=False)
            if (current.st_ino, current.st_dev, current.st_size) != (info.st_ino, info.st_dev, 7):
                raise F('POC_OWNERSHIP_GUARD')
        finally:
            os.close(out)
        os.unlink(name, dir_fd=private)
        return dict(workspace=receipt(ident, experiment, parent, private), own_access='ALLOW',
                    exclusive_create=True, stat=True, temp_deleted=True, mode='0700')
    finally:
        for fd in (private, parent, home):
            if fd is not None:
                os.close(fd)


def open_workspace(ident, experiment, expected):
    if not isinstance(expected, dict) or expected.get('username') != ident['username'] or expected.get('uid') != ident['uid'] or expected.get('home') != ident['home'] or expected.get('experiment') != experiment:
        raise F('POC_OWNERSHIP_GUARD')
    home = base.open_directory(ident['home'])
    parent = private = None
    try:
        parent = os.open(DIRECTORY, base.DIR_FLAGS, dir_fd=home)
        private = os.open('private-test', base.DIR_FLAGS, dir_fd=parent)
        for fd in (parent, private):
            base.owned(os.fstat(fd), ident['uid'], directory=True)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise F('POC_OWNERSHIP_GUARD')
        if receipt(ident, experiment, parent, private) != expected or read_marker(parent, 'experiment.json', ident['uid']) != dict(experiment=experiment, uid=ident['uid']):
            raise F('POC_OWNERSHIP_GUARD')
        return home, parent, private
    except BaseException:
        for fd in (private, parent, home):
            if fd is not None:
                os.close(fd)
        raise


def open_cross_directory(path):
    # Ancestors need search/traversal permission, not directory-list permission.
    # O_RDONLY on HOME could manufacture a DENY for an otherwise accessible
    # dedicated child. O_PATH + no-follow preserves Linux traversal semantics.
    parts = Path(path).parts
    if not Path(path).is_absolute() or '..' in parts:
        raise F('POC_INPUT_INVALID')
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for component in parts[1:-1]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], base.DIR_FLAGS, dir_fd=fd)
    finally:
        os.close(fd)


def cross_fs(ident, experiment, other):
    valid_user(other['username'])
    account = pwd.getpwnam(other['username'])
    if (other['username'] == ident['username'] or other['uid'] == ident['uid'] or
            account.pw_uid != other['uid'] or account.pw_dir != other['home'] or other['experiment'] != experiment):
        raise F('POC_OWNERSHIP_GUARD')
    path = Path(account.pw_dir) / DIRECTORY / 'private-test'
    # OS performs the actual access check. No application-generated DENY.
    try:
        fd = open_cross_directory(str(path))
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EPERM):
            return dict(access='DENY', errno=exc.errno, category='REMOTE_PERMISSION_DENIED')
        raise F('REMOTE_COMMAND_FAILED') from None
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_uid) != (other['private_dev'], other['private_ino'], other['uid']):
            raise F('POC_OWNERSHIP_GUARD')
        # The peer created this dedicated empty directory; no contents read.
        with os.scandir(fd) as entries:
            next(entries, None)
        return dict(access='ALLOW', errno=0, category='FILESYSTEM_POLICY_FINDING',
                    mode=format(stat.S_IMODE(info.st_mode), '04o'), uid=info.st_uid, gid=info.st_gid)
    finally:
        os.close(fd)


def valid_job(job, experiment, username):
    if (not isinstance(job, dict) or set(job) != {'job_id', 'username', 'job_name', 'experiment'} or
            not isinstance(job['job_id'], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', job['job_id']) or
            job['job_name'] != 'easysbatch-m10a2-' + experiment or
            job['experiment'] != experiment or job['username'] != username):
        raise F('POC_OWNERSHIP_GUARD')
    valid_user(username)


def job_owner(job, experiment):
    valid_job(job, experiment, job['username'])
    return base.owner(job['job_id'], job['username'], job['job_name'])


def submit(ident, experiment, parent, result):
    exclusive_json(parent, 'attempt.json', {'experiment':experiment})
    path = str(Path(ident['home']) / DIRECTORY)
    name = 'easysbatch-m10a2-' + experiment
    script = '#!/bin/sh\nset -eu\n/usr/bin/hostname\n/usr/bin/whoami\n/usr/bin/id\n/usr/bin/sleep 1\n'
    p = base.call(['/usr/bin/sbatch', '--parsable', '--job-name=' + name,
                   '--nodes=1', '--ntasks=1', '--cpus-per-task=1', '--mem=32M', '--time=00:01:00',
                   '--export=NIL', '--chdir=' + path, '--output=' + path + '/smoke-%j.out',
                   '--error=' + path + '/smoke-%j.err'], input=script)
    if p.returncode or not re.fullmatch(r'[1-9][0-9]{0,19}\n?', p.stdout):
        raise F('SLURM_SUBMISSION_UNKNOWN')
    job = dict(job_id=p.stdout.strip(), username=ident['username'], job_name=name, experiment=experiment)
    result['job'] = job  # immediately retain own receipt, including on later error
    exclusive_json(parent, 'job.json', job)
    result['owner'] = job_owner(job, experiment)


def cross_cancel(ident, experiment, other):
    valid_job(other, experiment, other['username'])
    if other['username'] == ident['username']:
        raise F('POC_OWNERSHIP_GUARD')
    observed = job_owner(other, experiment)
    if observed['state'].split()[0] in base.TERMINAL:
        return dict(result='NOT_TESTED', reason='ALREADY_TERMINAL', returncode=None)
    p = base.call(['/usr/bin/scancel', other['job_id']])
    if p.returncode == 0:
        return dict(result='FAIL', category='SLURM_AUTHORIZATION_FINDING', returncode=0)
    # Nonzero alone does not prove Slurm denied authorization (job may finish).
    denied = any(s in p.stderr.lower() for s in ('access/permission denied', 'permission denied', 'access denied', 'user does not have permission'))
    if not denied:
        return dict(result='NOT_TESTED', reason='DENIAL_UNCONFIRMED', returncode=p.returncode)
    after = job_owner(other, experiment)
    return dict(result='PASS', category='REMOTE_PERMISSION_DENIED', returncode=p.returncode,
                owner_unchanged=after['reported_user'] == other['username'])


def cleanup(ident, experiment, descriptors, job):
    home, parent, private = descriptors
    result = {'filesystem':'NOT_REMOVED', 'job':'NONE'}
    names = set(os.listdir(parent))
    if 'attempt.json' in names and job is None:
        raise F('POC_CLEANUP_UNCONFIRMED')  # unknown submission is never retried
    allowed = {'experiment.json', 'private-test'}
    if job is not None:
        valid_job(job, experiment, ident['username'])
        if read_marker(parent, 'job.json', ident['uid']) != job:
            raise F('POC_OWNERSHIP_GUARD')
        observed = job_owner(job, experiment)
        if observed['state'].split()[0] not in base.TERMINAL:
            p = base.call(['/usr/bin/scancel', job['job_id']])
            if p.returncode:
                raise F('POC_CLEANUP_FAILED')
            for _ in range(3):
                time.sleep(2)
                observed = job_owner(job, experiment)
                if observed['state'].split()[0] in base.TERMINAL:
                    break
        if observed['state'].split()[0] not in base.TERMINAL:
            raise F('POC_CLEANUP_UNCONFIRMED')
        result['job'] = observed['state']
        allowed |= {'attempt.json', 'job.json', 'smoke-' + job['job_id'] + '.out', 'smoke-' + job['job_id'] + '.err'}
    if not names <= allowed or os.listdir(private):
        raise F('POC_OWNERSHIP_GUARD')
    for name in names - {'private-test'}:
        base.owned(os.stat(name, dir_fd=parent, follow_symlinks=False), ident['uid'])
    # No recursive delete. Recheck directory inode, basename and owner.
    for name, directory, fd in ((DIRECTORY,home,parent), ('private-test',parent,private)):
        actual = os.stat(name, dir_fd=directory, follow_symlinks=False)
        expected = os.fstat(fd)
        if (actual.st_dev, actual.st_ino, actual.st_uid) != (expected.st_dev, expected.st_ino, ident['uid']):
            raise F('POC_OWNERSHIP_GUARD')
    for name in names - {'private-test'}:
        os.unlink(name, dir_fd=parent)
    os.rmdir('private-test', dir_fd=parent)
    os.rmdir(DIRECTORY, dir_fd=home)
    result['filesystem'] = 'REMOVED'
    return result


def execute(request):
    report = dict(operation=request.get('operation'), experiment=request.get('experiment'), data={})
    descriptors = None
    try:
        if set(request) != {'operation','experiment','username','expected_uid','arguments'} or request['operation'] not in OPERATIONS:
            raise F('POC_INPUT_INVALID')
        valid_user(request['username'])
        experiment = request['experiment']
        if str(UUID(experiment)) != experiment or type(request['expected_uid']) is not int or request['expected_uid'] <= 0:
            raise F('POC_INPUT_INVALID')
        report['identity'] = base.identity(request['username'], request['expected_uid'])
        ident, op, args = report['identity'], request['operation'], request['arguments']
        keys = {'identity':set(), 'setup':set(), 'slurm_readonly':set(), 'cross_fs':{'other'},
                'submit':{'workspace'}, 'cross_cancel':{'other_job'}, 'cleanup':{'workspace','job'}}
        if not isinstance(args, dict) or set(args) != keys[op]:
            raise F('POC_INPUT_INVALID')
        if op == 'setup':
            report['data'] = setup(ident, experiment)
        elif op == 'cross_fs':
            report['data'] = cross_fs(ident, experiment, args['other'])
        elif op == 'slurm_readonly':
            report['data'] = base.slurm_readonly(ident['username'])
        elif op in {'submit','cleanup'}:
            descriptors = open_workspace(ident, experiment, args['workspace'])
            if op == 'submit':
                submit(ident, experiment, descriptors[1], report['data'])
            else:
                report['data'] = cleanup(ident, experiment, descriptors, args['job'])
        elif op == 'cross_cancel':
            report['data'] = cross_cancel(ident, experiment, args['other_job'])
        report['result'] = 'PASS'
    except F as exc:
        report.update(result='FAIL', error_code=exc.code)
    except PermissionError:
        report.update(result='FAIL', error_code='REMOTE_PERMISSION_DENIED')
    except FileExistsError:
        report.update(result='FAIL', error_code='POC_ALREADY_EXISTS')
    except (OSError, KeyError, ValueError, TypeError):
        report.update(result='FAIL', error_code='REMOTE_COMMAND_FAILED')
    finally:
        if descriptors:
            for fd in descriptors:
                os.close(fd)
    return report


def main():
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError
        report = execute(json.loads(raw))
    except BaseException:
        report = dict(result='FAIL', error_code='POC_INPUT_INVALID')
    print(json.dumps(report), flush=True)


def main_session():
    """One experiment, one SSH login, at most eight fixed operations; then exit."""
    connection = os.environ.get('SSH_CONNECTION', '')
    binding = None
    counts = {}
    deadline = time.monotonic() + 600
    def expired(*unused):
        raise TimeoutError
    signal.signal(signal.SIGALRM, expired)
    try:
        for _ in range(8):
            # No idle server, background worker, reconnect or persistent session.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            signal.alarm(max(1, min(90, int(remaining))))
            raw = sys.stdin.buffer.readline(8193)
            if not raw:
                break
            request = json.loads(raw)
            if len(raw) > 8192 or not raw.endswith(b'\n') or not isinstance(request, dict):
                raise ValueError
            current = (request.get('username'), request.get('expected_uid'), request.get('experiment'))
            if binding is None:
                binding = current
            operation = request.get('operation')
            if (current != binding or operation not in OPERATIONS or
                    counts.get(operation, 0) >= (2 if operation == 'identity' else 1)):
                raise ValueError
            counts[operation] = counts.get(operation, 0) + 1
            # identity() strips the environment after verification. Preserve only
            # this original SSH session's nonsecret connection metadata between
            # probes; UID/HOME/capabilities are actually rechecked every time.
            if connection:
                os.environ['SSH_CONNECTION'] = connection
            report = execute(request)
            print(json.dumps(report), flush=True)
            if report['result'] != 'PASS' or operation == 'cleanup':
                break
    except (ValueError, TypeError, TimeoutError, OSError):
        print(json.dumps(dict(result='FAIL', error_code='POC_INPUT_INVALID')), flush=True)
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    main()
