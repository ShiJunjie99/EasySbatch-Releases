"""M10-A2 coordinator: trusted CLI only, per-context identity and run-local receipts."""
from enum import StrEnum
import json
import logging
import re
import threading
import time
from uuid import UUID, uuid4

from .ssh_poc import SSHExecutionTarget, SSHProbeError, OpenSSHTransport, ProbeProgram, checked_identity
from .ssh_poc_deployment import SSH_HOST, SSH_PORT

LOG = logging.getLogger('sbatch_agent.ssh_poc_two_user')


class Phase(StrEnum):
    IDENTITY = 'identity'
    SETUP = 'setup'
    CROSS_FS = 'cross_fs'
    SLURM_READONLY = 'slurm_readonly'
    SUBMIT = 'submit'
    CROSS_CANCEL = 'cross_cancel'
    CLEANUP = 'cleanup'


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise SSHProbeError('REMOTE_OUTPUT_INVALID')


def valid_workspace(value, target, experiment, identity):
    exact(value, 'username uid home experiment base_dev base_ino private_dev private_ino')
    if (value['username'] != target.username or value['uid'] != target.expected_uid or
            value['home'] != identity['home'] or value['experiment'] != experiment):
        raise SSHProbeError('POC_OWNERSHIP_GUARD')
    if any(type(value[k]) is not int or value[k] < 0 for k in ('base_dev','base_ino','private_dev','private_ino')):
        raise SSHProbeError('REMOTE_OUTPUT_INVALID')


def valid_job(value, target, experiment):
    exact(value, 'job_id username job_name experiment')
    if (value['username'] != target.username or value['experiment'] != experiment or
            value['job_name'] != 'easysbatch-m10a2-' + experiment or
            not isinstance(value['job_id'], str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value['job_id'])):
        raise SSHProbeError('POC_OWNERSHIP_GUARD')


def valid_data(data, phase, target, experiment, identity):
    if phase == Phase.IDENTITY:
        exact(data, '')
    elif phase == Phase.SETUP:
        exact(data, 'workspace own_access exclusive_create stat temp_deleted mode')
        valid_workspace(data['workspace'], target, experiment, identity)
        if data['own_access'] != 'ALLOW' or data['mode'] != '0700' or any(data[k] is not True for k in ('exclusive_create','stat','temp_deleted')):
            raise SSHProbeError('REMOTE_OUTPUT_INVALID')
    elif phase == Phase.CROSS_FS:
        if data.get('access') == 'DENY':
            exact(data, 'access errno category')
            if type(data['errno']) is not int or data['errno'] not in (1,13) or data['category'] != 'REMOTE_PERMISSION_DENIED':
                raise SSHProbeError('REMOTE_OUTPUT_INVALID')
        elif data.get('access') == 'ALLOW':
            exact(data, 'access errno category mode uid gid')
            if data['errno'] != 0 or data['category'] != 'FILESYSTEM_POLICY_FINDING' or not re.fullmatch(r'[0-7]{4}', data['mode']) or any(type(data[k]) is not int or data[k] < 0 for k in ('uid','gid')):
                raise SSHProbeError('REMOTE_OUTPUT_INVALID')
        else:
            raise SSHProbeError('REMOTE_OUTPUT_INVALID')
    elif phase == Phase.SLURM_READONLY:
        exact(data, 'commands_available current_user_queue_query own_queue_count association')
        if (data['commands_available'] is not True or data['current_user_queue_query'] is not True or
                type(data['own_queue_count']) is not int or data['own_queue_count'] < 0 or
                data['association'] not in ('QUERY_SUCCEEDED','NOT_AVAILABLE')):
            raise SSHProbeError('REMOTE_OUTPUT_INVALID')
    elif phase == Phase.SUBMIT:
        exact(data, 'job owner')
        valid_job(data['job'], target, experiment)
        row = data['owner']
        exact(row, 'job_id reported_user state source job_name')
        if (row['job_id'] != data['job']['job_id'] or row['job_name'] != data['job']['job_name'] or row['reported_user'] != target.username):
            raise SSHProbeError('SLURM_OWNER_MISMATCH')
        if row['source'] not in ('squeue','sacct') or not re.fullmatch(r'[A-Z_]+(?: by [0-9]+)?', row['state']):
            raise SSHProbeError('REMOTE_OUTPUT_INVALID')
    elif phase == Phase.CROSS_CANCEL:
        outcome = data.get('result')
        if outcome == 'PASS':
            exact(data, 'result category returncode owner_unchanged')
            if data['category'] != 'REMOTE_PERMISSION_DENIED' or data['owner_unchanged'] is not True or type(data['returncode']) is not int or not 1 <= data['returncode'] <= 255:
                raise SSHProbeError('REMOTE_OUTPUT_INVALID')
        elif outcome == 'FAIL':
            exact(data, 'result category returncode')
            if data['category'] != 'SLURM_AUTHORIZATION_FINDING' or data['returncode'] != 0:
                raise SSHProbeError('REMOTE_OUTPUT_INVALID')
        elif outcome == 'NOT_TESTED':
            exact(data, 'result reason returncode')
            if data['reason'] not in ('ALREADY_TERMINAL','DENIAL_UNCONFIRMED') or (data['returncode'] is not None and (type(data['returncode']) is not int or not 0 <= data['returncode'] <= 255)):
                raise SSHProbeError('REMOTE_OUTPUT_INVALID')
        else:
            raise SSHProbeError('REMOTE_OUTPUT_INVALID')
    elif phase == Phase.CLEANUP:
        exact(data, 'filesystem job')
        if data['filesystem'] != 'REMOVED' or not re.fullmatch(r'(?:NONE|COMPLETED|CANCELLED|FAILED|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|BOOT_FAIL|DEADLINE)(?: by [0-9]+)?', data['job']):
            raise SSHProbeError('REMOTE_OUTPUT_INVALID')


class TwoUserContext:
    def __init__(self, target, *, experiment, transport=None, interactive=False):
        if not isinstance(target, SSHExecutionTarget) or (target.host,target.port) != (SSH_HOST,SSH_PORT):
            raise ValueError('M10-A2 only permits its fixed deployment target')
        if str(UUID(experiment)) != experiment or type(interactive) is not bool:
            raise ValueError('Invalid experiment/authentication mode')
        self._target = target
        self._experiment = experiment
        self._transport = transport if transport is not None else OpenSSHTransport(program=ProbeProgram.TWO_USER)
        self._interactive = interactive
        self._lock = threading.RLock()
        self._closed = False
        self._attempted = False
        self._setup_attempted = False
        self.identity = self.workspace = self.job = None

    @property
    def target(self):
        return self._target

    def run(self, phase, *, peer=None, authorize_submit=False):
        with self._lock:
            if self._closed:
                raise SSHProbeError('SSH_CONTEXT_CLOSED')
            if not isinstance(phase, Phase) or type(authorize_submit) is not bool:
                raise ValueError('Only fixed M10-A2 phases are allowed')
            arguments = {}
            if phase in (Phase.CROSS_FS, Phase.CROSS_CANCEL):
                if not isinstance(peer, TwoUserContext) or peer is self or peer.target.username == self.target.username or peer.target.expected_uid == self.target.expected_uid or peer._experiment != self._experiment:
                    raise ValueError('An independent authorized peer context is required')
                if phase == Phase.CROSS_FS:
                    if peer.workspace is None or self.workspace is None:
                        raise ValueError('Both dedicated workspaces must be verified')
                    arguments['other'] = dict(peer.workspace)
                else:
                    if peer.job is None or self.job is None:
                        raise ValueError('Both current-run job receipts must be verified')
                    valid_job(peer.job, peer.target, self._experiment)
                    arguments['other_job'] = dict(peer.job)
            if phase in (Phase.SUBMIT, Phase.CLEANUP):
                if self.workspace is None or self.identity is None:
                    raise ValueError('Verified own workspace required')
                valid_workspace(self.workspace, self.target, self._experiment, self.identity)
                arguments['workspace'] = dict(self.workspace)
                if phase == Phase.CLEANUP:
                    if self.job is not None:
                        valid_job(self.job, self.target, self._experiment)
                    arguments['job'] = self.job
            if phase == Phase.SUBMIT:
                if not authorize_submit or self._attempted:
                    raise ValueError('At most one explicitly authorized submission per context')
                self._attempted = True
            if phase == Phase.SETUP:
                self._setup_attempted = True
            started = time.monotonic()
            code = None
            try:
                payload = json.dumps(dict(operation=phase.value, experiment=self._experiment,
                                          username=self.target.username, expected_uid=self.target.expected_uid,
                                          arguments=arguments))
                raw = self._transport.exchange(self.target, payload, timeout=75, interactive=self._interactive)
                def pairs(items):
                    result = {}
                    for k,v in items:
                        if k in result:
                            raise ValueError
                        result[k] = v
                    return result
                result = json.loads(raw, object_pairs_hook=pairs)
                if not isinstance(result, dict) or result.get('operation') != phase.value or result.get('experiment') != self._experiment:
                    raise ValueError
                if result.get('result') == 'FAIL' and 'identity' not in result:
                    raise SSHProbeError(result.get('error_code'))
                checked_identity(result.get('identity'), self.target)
                if self.identity is not None and result['identity'] != self.identity:
                    raise SSHProbeError('SSH_IDENTITY_MISMATCH')
                self.identity = result['identity']
                data = result.get('data')
                if not isinstance(data, dict):
                    raise ValueError
                # Retain only a strictly validated own receipt on downstream failure.
                if phase == Phase.SUBMIT and 'job' in data:
                    valid_job(data['job'], self.target, self._experiment)
                    self.job = dict(data['job'])
                if result.get('result') == 'FAIL':
                    raise SSHProbeError(result.get('error_code'))
                exact(result, 'operation experiment identity data result')
                if result['result'] != 'PASS':
                    raise ValueError
                valid_data(data, phase, self.target, self._experiment, self.identity)
                if phase == Phase.SETUP:
                    self.workspace = dict(data['workspace'])
                elif phase == Phase.CLEANUP:
                    self.workspace = None
                return data
            except SSHProbeError as exc:
                code = exc.code
                self._closed = True  # auth/identity failures never fall back/retry
                raise
            except Exception:
                code = 'REMOTE_OUTPUT_INVALID'
                self._closed = True
                raise SSHProbeError(code) from None
            finally:
                if code is not None:
                    close = getattr(self._transport, 'close', None)
                    if close is not None:
                        close()
                LOG.info(json.dumps(dict(correlation_id=self._experiment, host=self.target.host,
                                         username=self.target.username, operation=phase.value,
                                         duration_ms=round((time.monotonic()-started)*1000), error_code=code)))


class TwoUserExperiment:
    def __init__(self, a, b):
        if (not isinstance(a,TwoUserContext) or not isinstance(b,TwoUserContext) or a is b or
                a.target.username == b.target.username or a.target.expected_uid == b.target.expected_uid or a._experiment != b._experiment):
            raise ValueError('Two independent real identities are required')
        self.contexts = (a,b)
        self.used = False

    def run(self, *, authorize_two_smokes=False, identity_only=False):
        if self.used:
            raise ValueError('No experiment replay')
        self.used = True
        a,b = self.contexts
        results = [dict(username=c.target.username) for c in self.contexts]
        report = dict(experiment=a._experiment, target=f'{SSH_HOST}:{SSH_PORT}', users=results,
                      context_isolation='NOT_TESTED', cross_user_slurm='NOT_TESTED',
                      credential_persistence='NONE', overall='PARTIAL')
        stop_all = False
        try:
            sequence = []
            for c in (a,b,a,b):
                c.run(Phase.IDENTITY)
                sequence.append(c.target.username)
            report['identity_sequence'] = sequence
            report['context_isolation'] = 'PASS'
            for r,c in zip(results,self.contexts):
                r['identity'] = dict(c.identity)
            if identity_only:
                return report
            for r,c in zip(results,self.contexts):
                r['filesystem'] = c.run(Phase.SETUP)
            for r,c,peer in zip(results,self.contexts,(b,a)):
                r['cross_filesystem'] = c.run(Phase.CROSS_FS,peer=peer)
            if any(r['cross_filesystem']['access'] != 'DENY' for r in results):
                report['finding'] = 'FILESYSTEM_POLICY_FINDING'
                return report
            for r,c in zip(results,self.contexts):
                r['slurm_available'] = c.run(Phase.SLURM_READONLY)
            if not authorize_two_smokes:
                return report
            for r,c in zip(results,self.contexts):
                r['submission'] = c.run(Phase.SUBMIT,authorize_submit=True)
            for r,c,peer in zip(results,self.contexts,(b,a)):
                r['cross_cancel'] = c.run(Phase.CROSS_CANCEL,peer=peer)
                if r['cross_cancel']['result'] == 'FAIL':
                    report['finding'] = 'SLURM_AUTHORIZATION_FINDING'
                    report['cross_user_slurm'] = 'FAIL'
                    return report
            if all(r['cross_cancel']['result'] == 'PASS' for r in results):
                report['cross_user_slurm'] = 'PASS'
            report['overall'] = 'PASS'
        except SSHProbeError as exc:
            report['error_code'] = exc.code
            stop_all = exc.code in ('SSH_IDENTITY_MISMATCH','SLURM_OWNER_MISMATCH','SECRET_ENVIRONMENT_PRESENT','UNEXPECTED_FILESYSTEM_ACCESS')
            report['overall'] = 'FAIL' if stop_all or exc.code == 'SSH_AUTH_FAILED' else 'PARTIAL'
            if exc.code == 'SSH_IDENTITY_MISMATCH':
                report['context_isolation'] = 'FAIL'
        finally:
            try:
                for r,c in zip(results,self.contexts):
                    if c.identity is not None:
                        r['identity'] = dict(c.identity)
                    if c.job is not None:
                        r['job_receipt'] = dict(c.job)
                    if c._setup_attempted and c._closed and c.workspace is None:
                        r['cleanup'] = {'result':'MANUAL_REVIEW_REQUIRED', 'reason':'SETUP_OUTCOME_UNCONFIRMED'}
                    if c.workspace is not None:
                        if stop_all or c._closed:
                            r['cleanup'] = {'result':'MANUAL_REVIEW_REQUIRED'}
                        else:
                            try:
                                r['cleanup'] = c.run(Phase.CLEANUP)
                            except SSHProbeError as exc:
                                r['cleanup'] = {'result':'MANUAL_REVIEW_REQUIRED', 'error_code':exc.code}
                        if r['cleanup'].get('filesystem') != 'REMOVED' and report['overall'] == 'PASS':
                            report['overall'] = 'PARTIAL'
            finally:
                for context in self.contexts:
                    close = getattr(context._transport, 'close', None)
                    if close is not None:
                        close()
        return report
