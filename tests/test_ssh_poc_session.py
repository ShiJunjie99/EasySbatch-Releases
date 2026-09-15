"""Bounded experiment connections with fake processes; no real SSH/passwords."""
import io
import json
import os
import subprocess
import sys
import threading
from uuid import uuid4

import pytest

from sbatch_agent import ssh_poc as ssh
from sbatch_agent import ssh_poc_session as session
from sbatch_agent import ssh_poc_two_user as client
from sbatch_agent import ssh_poc_two_user_probe as remote
from sbatch_agent.ssh_poc_deployment import SSH_HOST, SSH_PORT
from test_ssh_poc_two_user import response


def target(user='alice', uid=1001):
    return ssh.SSHExecutionTarget(SSH_HOST, SSH_PORT, user, uid)


class FakeProcess:
    def __init__(self, t, callback, exit_code):
        self.target = t
        self.requests = []
        self.done = threading.Event()
        self.returncode = exit_code
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        error_read, error_write = os.pipe()
        self.stdin = os.fdopen(input_write, 'wb', buffering=0)
        self.stdout = os.fdopen(output_read, 'rb', buffering=0)
        self.stderr = os.fdopen(error_read, 'rb', buffering=0)
        def run():
            try:
                with os.fdopen(input_read, 'rb') as source, os.fdopen(output_write, 'wb', buffering=0) as output, os.fdopen(error_write, 'wb', buffering=0) as error:
                    for line in source:
                        request = json.loads(line)
                        self.requests.append(request)
                        data = callback(t, request)
                        if isinstance(data, tuple):
                            out, err = data
                            output.write(out)
                            error.write(err)
                            break
                        output.write((json.dumps(data) + '\n').encode())
                        if request['operation'] == 'cleanup':
                            break
            finally:
                self.done.set()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, timeout):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('fake-ssh', timeout)
        return self.returncode

    def terminate(self):
        self.stdin.close()

    kill = terminate


@pytest.fixture
def factory(monkeypatch):
    processes = []
    options = {'callback': response, 'exit_code': 0}
    def start(argv, **kwargs):
        assert 'module.main_session()' in argv[-1]
        assert 'PreferredAuthentications=password' in argv
        assert set(kwargs['env']) == {'PATH', 'HOME', 'LC_ALL'}
        assert 'synthetic-sensitive-value' not in repr((argv, kwargs))
        user = argv[-4]
        t = target(user, 1001 if user == 'alice' else 1002)
        process = FakeProcess(t, options['callback'], options['exit_code'])
        processes.append(process)
        return process
    monkeypatch.setattr(session.subprocess, 'Popen', start)
    monkeypatch.setattr(session, 'require_controlling_terminal', lambda: None)
    yield processes, options
    for p in processes:
        p.stdin.close()
        assert p.done.wait(2)
        p.stdout.close()
        p.stderr.close()


def contexts():
    experiment = str(uuid4())
    return tuple(client.TwoUserContext(t, experiment=experiment, interactive=True,
                                      transport=session.SSHExperimentTransport(t, experiment))
                 for t in (target(), target('bob', 1002)))


def test_full_experiment_only_two_processes_then_closed(factory, monkeypatch):
    monkeypatch.setenv('SSH_AUTH_SOCK', 'synthetic-sensitive-value')
    monkeypatch.setenv('SSH_ASKPASS', 'synthetic-sensitive-value')
    a, b = contexts()
    report = client.TwoUserExperiment(a, b).run(authorize_two_smokes=True)
    assert report['overall'] == 'PASS'
    assert report['identity_sequence'] == ['alice','bob','alice','bob']
    assert len(factory[0]) == 2 and all(len(p.requests) == 8 for p in factory[0])
    for context in (a, b):
        assert context._transport._closed and context._transport._process is None
        assert not context._transport._stdout and not context._transport._stderr
        with pytest.raises(ssh.SSHProbeError, match='SSH_CONTEXT_CLOSED'):
            context.run(client.Phase.IDENTITY)


def test_identity_only_closes_both_connections(factory):
    a, b = contexts()
    report = client.TwoUserExperiment(a, b).run(identity_only=True)
    assert report['context_isolation'] == 'PASS'
    assert [len(p.requests) for p in factory[0]] == [2, 2]
    assert all(c._transport._closed for c in (a, b))


def test_interrupt_during_cleanup_still_closes_both(factory, monkeypatch):
    a,b=contexts()
    original=a.run
    def interrupted(phase, **kwargs):
        if phase==client.Phase.CLEANUP:
            raise KeyboardInterrupt
        return original(phase, **kwargs)
    monkeypatch.setattr(a,'run',interrupted)
    with pytest.raises(KeyboardInterrupt):
        client.TwoUserExperiment(a,b).run()
    assert len(factory[0])==2
    assert all(c._transport._closed and c._transport._process is None for c in (a,b))


def test_interleaved_concurrent_contexts_do_not_share_process(factory):
    from concurrent.futures import ThreadPoolExecutor
    a, b = contexts()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for _ in range(2):
                futures = [pool.submit(c.run, client.Phase.IDENTITY) for c in (a,b)]
                for future in futures:
                    future.result(timeout=3)
        assert a.identity['username'] == 'alice' and b.identity['username'] == 'bob'
        assert len(factory[0]) == 2
    finally:
        a._transport.close()
        b._transport.close()


@pytest.mark.parametrize('detail,code', [
    (b'Permission denied (password). synthetic-sensitive-value', 'SSH_AUTH_FAILED'),
    (b'Host key verification failed. synthetic-sensitive-value', 'SSH_HOST_KEY_FAILED'),
    (b'Connection reset. synthetic-sensitive-value', 'SSH_CONNECTION_FAILED'),
])
def test_failure_classification_and_no_reconnect(factory, detail, code, caplog):
    factory[1].update(callback=lambda t,r: (b'', detail), exit_code=255)
    a, b = contexts()
    caplog.set_level('INFO')
    report = client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['error_code'] == code and len(factory[0]) == 1
    assert 'synthetic-sensitive-value' not in json.dumps(report) + caplog.text
    assert a._transport._process is None and b._transport._process is None


def test_identity_mismatch_terminates_both_before_filesystem(factory):
    def swapped(t,r):
        result = response(t,r)
        if t.username == 'bob':
            result['identity']['username'] = 'alice'
        return result
    factory[1]['callback'] = swapped
    a,b = contexts()
    report = client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['overall'] == 'FAIL' and report['error_code'] == 'SSH_IDENTITY_MISMATCH'
    assert [len(p.requests) for p in factory[0]] == [1,1]
    assert all(c._transport._process is None for c in (a,b))


@pytest.mark.parametrize('raw', [b'not-json\n', b'{}\n{}\n', b'x' * 32769])
def test_malformed_or_large_output_closes(factory, raw):
    factory[1]['callback'] = lambda t,r: (raw, b'')
    a,b = contexts()
    report = client.TwoUserExperiment(a,b).run()
    assert report['overall'] == 'PARTIAL'
    assert report['error_code'] in ('REMOTE_OUTPUT_INVALID', 'POC_INPUT_INVALID')
    assert a._transport._process is None


@pytest.mark.parametrize('mutation', ['username','uid','experiment','operation','target','timeout'])
def test_request_binding_guard_before_process(factory, mutation):
    t=target();exp=str(uuid4());transport=session.SSHExperimentTransport(t,exp)
    request=dict(username=t.username,expected_uid=t.expected_uid,experiment=exp,operation='identity',arguments={})
    timeout=75
    if mutation=='username': request['username']='bob'
    elif mutation=='uid': request['expected_uid']=1002
    elif mutation=='experiment': request['experiment']=str(uuid4())
    elif mutation=='operation': request['operation']='arbitrary-shell'
    elif mutation=='target': t=target('bob',1002)
    else: timeout=float('inf')
    with pytest.raises(ssh.SSHProbeError,match='POC_INPUT_INVALID'):
        transport.exchange(t,json.dumps(request),timeout=timeout,interactive=True)
    assert not factory[0] and transport._closed


def test_timeout_terminates_process(factory, monkeypatch):
    a,b = contexts()
    def expired(deadline): raise ssh.SSHProbeError('SSH_TIMEOUT')
    monkeypatch.setattr(session.SSHExperimentTransport, '_remaining', staticmethod(expired))
    report=client.TwoUserExperiment(a,b).run()
    assert report['error_code']=='SSH_TIMEOUT' and a._transport._process is None


@pytest.fixture
def remote_stream(monkeypatch):
    monkeypatch.setattr(remote.signal,'signal',lambda *args:None)
    monkeypatch.setattr(remote.signal,'alarm',lambda *args:None)
    calls=[]
    def execute(request):
        calls.append(request)
        assert os.environ.get('SSH_CONNECTION')=='synthetic-session-metadata'
        os.environ.pop('SSH_CONNECTION')
        return dict(result='PASS',data={})
    monkeypatch.setenv('SSH_CONNECTION','synthetic-session-metadata')
    monkeypatch.setattr(remote,'execute',execute)
    def run(requests):
        data=b''.join(json.dumps(r).encode()+b'\n' for r in requests)
        monkeypatch.setattr(sys,'stdin',io.TextIOWrapper(io.BytesIO(data)))
        remote.main_session()
        return calls
    return run


def frame(operation='identity', **kwargs):
    return dict(operation=operation,username='alice',expected_uid=1001,experiment='fixed',arguments={},**kwargs)


def test_remote_restores_original_ssh_marker_and_exits_after_cleanup(remote_stream,capsys):
    operations=['identity','identity','setup','cross_fs','slurm_readonly','submit','cross_cancel','cleanup','identity']
    calls=remote_stream([frame(op) for op in operations])
    assert [r['operation'] for r in calls]==operations[:8]
    assert len(capsys.readouterr().out.splitlines())==8


@pytest.mark.parametrize('field,value', [('username','bob'),('expected_uid',1002),('experiment','other')])
def test_remote_cannot_switch_identity_or_experiment(remote_stream,capsys,field,value):
    second=frame();second[field]=value
    assert len(remote_stream([frame(),second]))==1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['error_code']=='POC_INPUT_INVALID'


def test_remote_phase_budget_prevents_repeated_submission(remote_stream,capsys):
    assert len(remote_stream([frame('submit'),frame('submit')]))==1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['error_code']=='POC_INPUT_INVALID'


def test_remote_failure_stops_request_loop(remote_stream,monkeypatch,capsys):
    calls=[]
    def fail(r):
        calls.append(r)
        return {'result':'FAIL','error_code':'SSH_IDENTITY_MISMATCH'}
    monkeypatch.setattr(remote,'execute',fail)
    remote_stream([frame(),frame('setup')])
    assert len(calls)==1 and len(capsys.readouterr().out.splitlines())==1
