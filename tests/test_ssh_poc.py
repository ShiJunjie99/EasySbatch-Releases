"""Offline only: fake SSH/Slurm, isolated temporary files, no credentials."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading
from uuid import uuid4

import pytest

from sbatch_agent import ssh_poc as p
from sbatch_agent import ssh_poc_probe as remote


@pytest.fixture(autouse=True)
def no_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Unexpected real SSH/Slurm subprocess')
    monkeypatch.setattr(subprocess, 'run', forbidden)


def target(user='alice', uid=1001):
    return p.SSHExecutionTarget('login.example.org', 22, user, uid)


def identity(t):
    return dict(username=t.username, uid=t.expected_uid, gid=t.expected_uid,
                groups=[t.username], home='/home/'+t.username, pwd='/home/'+t.username,
                hostname='login.example.org', account_uid=t.expected_uid,
                account_home='/home/'+t.username, home_owner_uid=t.expected_uid,
                secret_like_variables_absent=True, ssh_session=True)


def report(t, payload):
    request = json.loads(payload)
    result = dict(operation=request['operation'], experiment=request['experiment'],
                  identity=identity(t), result='PASS')
    if request['operation'] != 'identity':
        result.update(filesystem=dict(directory_exists=True, can_list=True, exclusive_create=True,
                                      file_owner_uid=t.expected_uid, write_test=True, can_stat=True,
                                      temp_file_deleted=True), filesystem_cleanup='REMOVED')
    if request['operation'] in {'slurm_readonly','smoke'}:
        result['slurm_availability'] = dict(commands_available=True, current_user_queue_query=True,
                                          own_queue_count=0, association='NOT_AVAILABLE')
    if request['operation'] == 'smoke':
        row = dict(job_id='123', reported_user=t.username, state='COMPLETED', source='sacct',
                   job_name='easysbatch-m10a-'+request['experiment'])
        result.update(submission_attempted=True, job_id='123', slurm_identity=row,
                      final_slurm_identity=dict(row), job_cleanup='TERMINAL_COMPLETED')
    return result


class FakeTransport:
    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate

    def exchange(self, t, payload, **kwargs):
        self.calls.append((t,payload,kwargs))
        result = report(t,payload)
        if self.mutate:
            self.mutate(result)
        return json.dumps(result)


@pytest.mark.parametrize('name', ['root','','Alice','a b','a;id','-oProxyCommand=id','a@host','a\n','a/../b',None,True,'a'*33])
def test_username_rejected(name):
    with pytest.raises(ValueError):
        target(name)


@pytest.mark.parametrize('host', ['', '-oProxyCommand=id', 'x;id', 'a@host', 'host/path', 'x\n', 'a..b', 'a_b', '*',None,'a'*64+'.org'])
def test_target_host_rejected(host):
    with pytest.raises(ValueError):
        p.SSHExecutionTarget(host,22,'alice',1001)


@pytest.mark.parametrize('port', [0,65536,True,'22',None,1.5])
def test_invalid_port(port):
    with pytest.raises(ValueError):
        p.SSHExecutionTarget('host',port,'alice',1001)


@pytest.mark.parametrize('uid', [0,-1,True,'1001',None])
def test_invalid_uid(uid):
    with pytest.raises(ValueError):
        target(uid=uid)


@pytest.mark.parametrize('path', ['/dev/null','relative','/tmp/a b','/tmp/../key','/tmp/$HOME/key','/tmp/%h','/tmp/known\n'])
def test_bad_known_hosts_path(path):
    with pytest.raises(ValueError):
        p.SSHExecutionTarget('host',22,'alice',1001,path)


@pytest.mark.parametrize('host', ['127.0.0.1','::1','example-cluster','example-cluster.cluster.local'])
def test_safe_hosts(host):
    assert p.SSHExecutionTarget(host,22,'alice',1001).host == host


def test_target_immutable_and_no_global_identity():
    t=target()
    with pytest.raises(FrozenInstanceError):
        t.username='bob'
    context=p.SSHExecutionContext(t,transport=FakeTransport())
    with pytest.raises(AttributeError):
        context.target=target('bob',1002)


def test_configuration_rejects_credential_or_unknown_fields_without_echo(tmp_path):
    path=tmp_path/'deployment.toml'
    path.write_text('[target]\nhost="host"\nport=22\nusername="alice"\nexpected_uid=1001\npassword="synthetic-sensitive-value"\n')
    with pytest.raises(ValueError) as exc:
        p.SSHExecutionTarget.load(path)
    assert 'synthetic-sensitive-value' not in str(exc.value)
    path.write_text('[target]\nhost="host"\nport=22\nusername="alice"\nexpected_uid=1001\n')
    assert p.SSHExecutionTarget.load(path).username=='alice'


def test_ssh_command_construction_has_fixed_remote_code_and_no_reuse():
    t=p.SSHExecutionTarget('127.0.0.1',22,'alice',1001,'/tmp/poc/known_hosts')
    argv=p.ssh_arguments(t)
    assert argv[:4]==['/usr/bin/ssh','-F','/dev/null','-T']
    for option in ('StrictHostKeyChecking=yes','ControlPath=none','ControlMaster=no','ControlPersist=no',
                   'IdentityFile=none','ForwardAgent=no','SendEnv=-*','BatchMode=yes','ConnectionAttempts=1'):
        assert option in argv
    assert argv[-5:-1]==['-l','alice','--','127.0.0.1']
    assert '/usr/bin/python3 -I -c ' in argv[-1]
    assert 'BatchMode=no' in p.ssh_arguments(t,interactive=True)
    assert 'StrictHostKeyChecking=no' not in argv


@pytest.mark.parametrize('operation', ['whoami','id; rm -rf /',['id'],None,1])
def test_only_operation_enum_allowed(operation):
    fake=FakeTransport()
    with pytest.raises(ValueError):
        p.SSHExecutionContext(target(),transport=fake).run(operation)
    assert not fake.calls


@pytest.mark.parametrize('op', list(p.Operation))
def test_all_fixed_operations_decode(op):
    result=p.SSHExecutionContext(target(),transport=FakeTransport()).run(op,authorize_smoke=op==p.Operation.SMOKE)
    assert result['identity']['uid']==1001


@pytest.mark.parametrize('field,value,code', [
    ('username','bob','SSH_IDENTITY_MISMATCH'),('uid',1002,'SSH_IDENTITY_MISMATCH'),
    ('uid',0,'SSH_IDENTITY_MISMATCH'),('uid',True,'SSH_IDENTITY_MISMATCH'),
    ('account_uid',1002,'SSH_IDENTITY_MISMATCH'),('home_owner_uid',1002,'SSH_IDENTITY_MISMATCH'),
    ('home','/home/bob','SSH_IDENTITY_MISMATCH'),('gid',0,'SSH_IDENTITY_MISMATCH'),
    ('secret_like_variables_absent',False,'SECRET_ENVIRONMENT_PRESENT'),
    ('ssh_session',False,'SSH_SESSION_REQUIRED'),('groups',[],'REMOTE_OUTPUT_INVALID')])
def test_identity_mismatch_and_environment_rejected(field,value,code):
    fake=FakeTransport(lambda result:result['identity'].update({field:value}))
    with pytest.raises(p.SSHProbeError) as exc:
        p.SSHExecutionContext(target(),transport=fake).run(p.Operation.IDENTITY)
    assert exc.value.code==code


def test_sequential_a_b_a_and_concurrent_isolation():
    fake=FakeTransport()
    a=p.SSHExecutionContext(target(),transport=fake)
    b=p.SSHExecutionContext(target('bob',1002),transport=fake)
    assert [c.run(p.Operation.IDENTITY)['identity']['username'] for c in (a,b,a)]==['alice','bob','alice']
    barrier=threading.Barrier(2)
    class ConcurrentFake(FakeTransport):
        def exchange(self,t,payload,**kwargs):
            barrier.wait(timeout=2)
            return super().exchange(t,payload,**kwargs)
    shared=ConcurrentFake()
    a=p.SSHExecutionContext(target(),transport=shared)
    b=p.SSHExecutionContext(target('bob',1002),transport=shared)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks=[pool.submit(c.run,p.Operation.IDENTITY) for c in (a,b)]
        assert [future.result()['identity']['username'] for future in tasks]==['alice','bob']


def test_environment_is_positive_allowlist_and_parent_unchanged():
    original=dict(PATH='/unsafe',HOME='/wrong',SSH_AUTH_SOCK='/synthetic/agent.sock',
                  SBATCH_AGENT_AI_RELAY_TOKEN='synthetic-a',DEEPSEEK_API_KEY='synthetic-b',
                  CUSTOM_DATABASE_SECRET='synthetic-c',UNRELATED_VALUE='synthetic-d',
                  BASH_ENV='/untrusted',PYTHONPATH='/untrusted',SSH_ASKPASS='/untrusted')
    before=dict(original)
    clean=p.sanitized_environment(original)
    assert set(clean)=={'PATH','HOME','LC_ALL','SSH_AUTH_SOCK'}
    assert clean['PATH']=='/usr/bin:/bin'
    assert not any(v in clean.values() for v in ('synthetic-a','synthetic-b','synthetic-c','synthetic-d'))
    assert original==before


@pytest.mark.parametrize('raw', ['not-json','[]','null','{"result":"PASS"}','{"result":1,"result":2}', '\x00'])
def test_malformed_output(raw):
    class Fake:
        def exchange(self,*args,**kwargs):return raw
    with pytest.raises(p.SSHProbeError) as exc:
        p.SSHExecutionContext(target(),transport=Fake()).run(p.Operation.IDENTITY)
    assert exc.value.code=='REMOTE_OUTPUT_INVALID'


@pytest.mark.parametrize('detail,code', [
    ('Permission denied (publickey,password).','SSH_AUTH_FAILED'),
    ('Too many authentication failures','SSH_AUTH_FAILED'),
    ('Host key verification failed.','SSH_HOST_KEY_FAILED'),
    ('WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!','SSH_HOST_KEY_FAILED'),
    ('Connection refused','SSH_CONNECTION_FAILED')])
def test_transport_error_categories_never_echo(monkeypatch,detail,code):
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,255,'',detail+' synthetic-sensitive-value'))
    with pytest.raises(p.SSHProbeError) as exc:
        p.SSHExecutionContext(target()).run(p.Operation.IDENTITY)
    assert exc.value.code==code
    assert 'synthetic-sensitive-value' not in str(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize('failure,code', [
    (subprocess.TimeoutExpired('ssh',1,output='synthetic-sensitive-value'),'SSH_TIMEOUT'),
    (FileNotFoundError('synthetic-sensitive-value'),'SSH_CONNECTION_FAILED')])
def test_timeout_and_os_error(monkeypatch,failure,code):
    def fail(*a,**k):raise failure
    monkeypatch.setattr(subprocess,'run',fail)
    with pytest.raises(p.SSHProbeError) as exc:
        p.SSHExecutionContext(target()).run(p.Operation.IDENTITY)
    assert exc.value.code==code
    assert 'synthetic-sensitive-value' not in str(exc.value)


def test_subprocess_argv_stdin_environment_and_finite_timeout(monkeypatch):
    def fake(argv,**kwargs):
        assert argv[0]=='/usr/bin/ssh'
        assert kwargs['timeout']==75
        assert not kwargs.get('shell',False)
        assert set(kwargs['env']) <= {'HOME','PATH','LC_ALL','SSH_AUTH_SOCK'}
        request=json.loads(kwargs['input'])
        assert set(request)=={'operation','username','expected_uid','experiment'}
        return subprocess.CompletedProcess(argv,0,json.dumps(report(target(),kwargs['input'])),'')
    monkeypatch.setattr(subprocess,'run',fake)
    assert p.SSHExecutionContext(target()).run(p.Operation.IDENTITY)['result']=='PASS'


@pytest.mark.parametrize('timeout', [0,-1,float('inf'),float('nan'),True,'10',121])
def test_timeouts_must_be_bounded(timeout):
    with pytest.raises(ValueError):p.SSHExecutionContext(target(),timeout=timeout)


def test_no_second_submit_even_after_unknown_outcome():
    class Broken(FakeTransport):
        def exchange(self,*args,**kwargs):raise p.SSHProbeError('SSH_TIMEOUT')
    context=p.SSHExecutionContext(target(),transport=Broken())
    with pytest.raises(ValueError):context.run(p.Operation.SMOKE)
    with pytest.raises(p.SSHProbeError):context.run(p.Operation.SMOKE,authorize_smoke=True)
    with pytest.raises(ValueError):context.run(p.Operation.SMOKE,authorize_smoke=True)


def test_owner_mismatch_fails_instead_of_ui_relabeling():
    fake=FakeTransport(lambda r:r['slurm_identity'].update(reported_user='bob'))
    with pytest.raises(p.SSHProbeError) as exc:
        p.SSHExecutionContext(target(),transport=fake).run(p.Operation.SMOKE,authorize_smoke=True)
    assert exc.value.code=='SLURM_OWNER_MISMATCH'


def test_error_evidence_keeps_job_receipt_without_raw_secret(caplog):
    def change(r):
        r.update(result='FAIL',error_code='SLURM_OWNER_MISMATCH',raw='synthetic-sensitive-value',
                 filesystem_cleanup='RETAINED_FOR_REVIEW')
    caplog.set_level('INFO',logger='sbatch_agent.ssh_poc')
    with pytest.raises(p.SSHProbeError) as exc:
        p.SSHExecutionContext(target(),transport=FakeTransport(change)).run(p.Operation.SMOKE,authorize_smoke=True)
    assert exc.value.evidence['job_id']=='123'
    assert 'synthetic-sensitive-value' not in json.dumps(exc.value.evidence)+caplog.text
    assert 'SSH_AUTH_SOCK' not in caplog.text


def local_identity(tmp_path):
    return dict(home=str(tmp_path),username='alice',uid=os.getuid())


def test_own_filesystem_exclusive_creation_stat_delete_and_cleanup(tmp_path):
    result,workspace=remote.filesystem(local_identity(tmp_path),str(uuid4()))
    assert result['exclusive_create'] and result['temp_file_deleted']
    path,home_fd,base_fd,fd,parent_created=workspace
    assert not os.listdir(fd)
    os.rmdir(Path(path).name,dir_fd=base_fd)
    os.rmdir('easysbatch-poc',dir_fd=home_fd)
    for item in (fd,base_fd,home_fd):os.close(item)
    assert not list(tmp_path.iterdir())


def test_existing_poc_directory_is_not_overwritten(tmp_path):
    base=tmp_path/'easysbatch-poc';base.mkdir(mode=0o700)
    experiment=str(uuid4());run=base/('.easysbatch-m10a-poc-'+experiment);run.mkdir(mode=0o700)
    sentinel=run/'sentinel';sentinel.write_text('keep')
    with pytest.raises(FileExistsError):remote.filesystem(local_identity(tmp_path),experiment)
    assert sentinel.read_text()=='keep'


def test_symlink_workspace_is_rejected_without_touching_target(tmp_path):
    elsewhere=tmp_path/'other';elsewhere.mkdir()
    (tmp_path/'easysbatch-poc').symlink_to(elsewhere,target_is_directory=True)
    with pytest.raises(OSError):remote.filesystem(local_identity(tmp_path),str(uuid4()))
    assert not list(elsewhere.iterdir())


def test_identity_failure_stops_before_filesystem_or_slurm(monkeypatch):
    def fail(*args):raise remote.ProbeFailure('SSH_IDENTITY_MISMATCH')
    def forbidden(*args):pytest.fail('Continued after identity mismatch')
    monkeypatch.setattr(remote,'identity',fail)
    monkeypatch.setattr(remote,'filesystem',forbidden)
    monkeypatch.setattr(remote,'slurm_readonly',forbidden)
    r=remote.execute(dict(operation='smoke',username='alice',expected_uid=1001,experiment=str(uuid4())))
    assert r['error_code']=='SSH_IDENTITY_MISMATCH'


def test_remote_secret_detection_before_any_command(monkeypatch):
    monkeypatch.setenv('SBATCH_AGENT_AI_RELAY_TOKEN','synthetic-sensitive-value')
    with pytest.raises(remote.ProbeFailure) as exc:remote.identity('alice',1001)
    assert exc.value.code=='SECRET_ENVIRONMENT_PRESENT'


def test_remote_owner_queries_exact_id_and_user_not_other_job(monkeypatch):
    commands=[]
    def fake(argv,**kwargs):
        commands.append(argv)
        if argv[0].endswith('squeue'):
            return subprocess.CompletedProcess(argv,0,'','')
        return subprocess.CompletedProcess(argv,0,'123|alice|COMPLETED|easysbatch-m10a-test\n','')
    monkeypatch.setattr(remote,'call',fake)
    r=remote.owner('123','alice','easysbatch-m10a-test')
    assert r['reported_user']=='alice' and r['source']=='sacct'
    assert all('--jobs=123' in c for c in commands)
    assert not any('547433' in str(c) for c in commands)


def test_remote_smoke_tiny_job_and_pending_owner_cleanup(tmp_path,monkeypatch):
    ident=local_identity(tmp_path);experiment=str(uuid4())
    _,workspace=remote.filesystem(ident,experiment);observations=[];commands=[]
    states=iter(['PENDING','PENDING','CANCELLED'])
    def observe(job,user,name):
        state=next(states);observations.append((job,user,name,state))
        return dict(job_id=job,reported_user=user,job_name=name,state=state,source='squeue')
    def fake(argv,**kwargs):
        commands.append(argv)
        if argv[0].endswith('sbatch'):
            assert '--export=NIL' in argv and '--mem=32M' in argv and '--time=00:01:00' in argv
            assert not any('--nodelist' in a or '--gres' in a or '--gpu' in a for a in argv)
            assert 'sleep 1' in kwargs['input']
            return subprocess.CompletedProcess(argv,0,'123\n','')
        assert argv==['/usr/bin/scancel','123']
        return subprocess.CompletedProcess(argv,0,'','')
    monkeypatch.setattr(remote,'call',fake);monkeypatch.setattr(remote,'owner',observe)
    monkeypatch.setattr(remote.time,'sleep',lambda _:None)
    r={};remote.smoke(ident,workspace,experiment,r)
    assert r['job_cleanup']=='TERMINAL_CANCELLED'
    assert len(commands)==2 and all(o[1]=='alice' for o in observations)
    path,home_fd,base_fd,fd,created=workspace
    assert not os.listdir(fd)
    for item in (fd,base_fd,home_fd):os.close(item)


def test_owner_mismatch_never_cancels_even_poc(tmp_path,monkeypatch):
    ident=local_identity(tmp_path);experiment=str(uuid4())
    _,workspace=remote.filesystem(ident,experiment);commands=[]
    def fake(argv,**kwargs):
        commands.append(argv);return subprocess.CompletedProcess(argv,0,'123\n','')
    def mismatch(*args):raise remote.ProbeFailure('SLURM_OWNER_MISMATCH')
    monkeypatch.setattr(remote,'call',fake);monkeypatch.setattr(remote,'owner',mismatch)
    r={}
    with pytest.raises(remote.ProbeFailure):remote.smoke(ident,workspace,experiment,r)
    assert len(commands)==1 and commands[0][0].endswith('sbatch')
    assert r['job_id']=='123'
    for item in workspace[1:4]:os.close(item)
