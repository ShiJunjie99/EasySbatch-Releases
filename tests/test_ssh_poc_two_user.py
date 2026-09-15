"""A2 behavior regression. Fake DENY is not real Linux permission evidence."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import subprocess
import threading
from uuid import uuid4

import pytest

from sbatch_agent import ssh_poc as original
from sbatch_agent import ssh_poc_two_user as client
from sbatch_agent import ssh_poc_two_user_probe as remote
from sbatch_agent.ssh_poc_deployment import SSH_HOST, SSH_PORT


@pytest.fixture(autouse=True)
def no_commands(monkeypatch):
    def fail(*a, **k):
        pytest.fail('Unexpected real SSH/Slurm command')
    monkeypatch.setattr(subprocess, 'run', fail)


def target(user='alice', uid=1001):
    return original.SSHExecutionTarget(SSH_HOST, SSH_PORT, user, uid)


def identity(t):
    return dict(username=t.username, uid=t.expected_uid, gid=t.expected_uid,
                groups=[t.username], home='/test-homes/' + t.username, pwd='/test-homes/' + t.username,
                hostname='example-cluster.example.org', account_uid=t.expected_uid,
                account_home='/test-homes/' + t.username, home_owner_uid=t.expected_uid,
                secret_like_variables_absent=True, ssh_session=True)


def workspace(t, experiment):
    return dict(username=t.username, uid=t.expected_uid, home=identity(t)['home'], experiment=experiment,
                base_dev=1,base_ino=t.expected_uid,private_dev=1,private_ino=t.expected_uid+100)


def job(t, experiment):
    return dict(job_id=str(t.expected_uid),username=t.username,experiment=experiment,
                job_name='easysbatch-m10a2-'+experiment)


def response(t, request):
    operation=request['operation']; experiment=request['experiment']
    data={}
    if operation=='setup':
        data=dict(workspace=workspace(t,experiment),own_access='ALLOW',exclusive_create=True,stat=True,temp_deleted=True,mode='0700')
    elif operation=='cross_fs':
        data=dict(access='DENY',errno=13,category='REMOTE_PERMISSION_DENIED')
    elif operation=='slurm_readonly':
        data=dict(commands_available=True,current_user_queue_query=True,own_queue_count=0,association='NOT_AVAILABLE')
    elif operation=='submit':
        receipt=job(t,experiment)
        data=dict(job=receipt,owner=dict(job_id=receipt['job_id'],reported_user=t.username,job_name=receipt['job_name'],state='PENDING',source='squeue'))
    elif operation=='cross_cancel':
        data=dict(result='PASS',category='REMOTE_PERMISSION_DENIED',returncode=1,owner_unchanged=True)
    elif operation=='cleanup':
        data=dict(filesystem='REMOVED',job='CANCELLED' if request['arguments']['job'] else 'NONE')
    return dict(operation=operation,experiment=experiment,identity=identity(t),data=data,result='PASS')


class Fake:
    def __init__(self, mutate=None):
        self.calls=[];self.mutate=mutate
    def exchange(self,t,payload,**kwargs):
        r=json.loads(payload);self.calls.append((t,r,kwargs))
        result=response(t,r)
        if self.mutate:
            self.mutate(t,r,result)
        return json.dumps(result)


def contexts(fake=None):
    fake=fake or Fake(); experiment=str(uuid4())
    return (client.TwoUserContext(target(),experiment=experiment,transport=fake),
            client.TwoUserContext(target('bob',1002),experiment=experiment,transport=fake),fake)


@pytest.mark.parametrize('host,port', [('127.0.0.1',22),('example.org',22),(SSH_HOST,23),(SSH_HOST,2222)])
def test_only_fixed_deployment_host_port(host,port):
    with pytest.raises(ValueError):
        client.TwoUserContext(original.SSHExecutionTarget(host,port,'alice',1001),experiment=str(uuid4()))


@pytest.mark.parametrize('user', ['root','a;id','a@host','-oProxyCommand=id','',None])
def test_username_rejected(user):
    with pytest.raises(ValueError):target(user)


@pytest.mark.parametrize('port', [0,65536,True,'22'])
def test_port_validation(port):
    with pytest.raises(ValueError):original.SSHExecutionTarget(SSH_HOST,port,'alice',1001)


def test_alternating_a_b_a_b_real_context_objects():
    a,b,fake=contexts()
    for c in (a,b,a,b): c.run(client.Phase.IDENTITY)
    assert [t.username for t,r,k in fake.calls]==['alice','bob','alice','bob']
    assert a.identity['uid']==1001 and b.identity['uid']==1002
    with pytest.raises(AttributeError):a.target=b.target


def test_concurrent_interleaved_contexts():
    barrier=threading.Barrier(2)
    class Concurrent(Fake):
        def exchange(self,*a,**k):
            barrier.wait(timeout=2)
            return super().exchange(*a,**k)
    a,b,f=contexts(Concurrent())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(lambda c:[c.run(client.Phase.IDENTITY) for _ in range(3)],c) for c in (a,b)]
        for future in futures: future.result(timeout=3)
    assert a.identity['username']=='alice' and b.identity['username']=='bob'
    assert len(f.calls)==6


def test_complete_fake_matrix_and_cleanup():
    a,b,f=contexts();report=client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['overall']=='PASS' and report['identity_sequence']==['alice','bob','alice','bob']
    assert report['cross_user_slurm']=='PASS'
    assert len([r for t,r,k in f.calls if r['operation']=='submit'])==2
    assert all(r['cleanup']['filesystem']=='REMOVED' for r in report['users'])
    assert all(k['timeout']==75 for t,r,k in f.calls)


def test_no_submit_without_authorization_and_no_experiment_replay():
    a,b,f=contexts();experiment=client.TwoUserExperiment(a,b)
    assert experiment.run()['overall']=='PARTIAL'
    assert not any(r['operation']=='submit' for t,r,k in f.calls)
    with pytest.raises(ValueError):experiment.run(authorize_two_smokes=True)


@pytest.mark.parametrize('code', ['SSH_AUTH_FAILED','SSH_IDENTITY_MISMATCH','SSH_HOST_KEY_FAILED','SSH_CONNECTION_FAILED'])
def test_failure_closed_no_a_fallback_and_no_further_operations(code):
    class Broken(Fake):
        def exchange(self,t,payload,**kwargs):
            if t.username=='bob':
                self.calls.append((t,json.loads(payload),kwargs))
                raise original.SSHProbeError(code)
            return super().exchange(t,payload,**kwargs)
    a,b,f=contexts(Broken());report=client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['error_code']==code
    assert [t.username for t,r,k in f.calls]==['alice','bob']
    assert all(r['operation']=='identity' for t,r,k in f.calls)
    with pytest.raises(original.SSHProbeError,match='SSH_CONTEXT_CLOSED'):b.run(client.Phase.IDENTITY)


def test_remote_identity_failure_classified_before_missing_identity():
    def mutate(t,r,result):
        result.pop('identity');result.update(result='FAIL',error_code='SSH_IDENTITY_MISMATCH')
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run()
    assert report['overall']=='FAIL' and report['context_isolation']=='FAIL'
    assert len(f.calls)==1


def test_identity_bleed_third_probe_stops_all():
    count=0
    def mutate(t,r,result):
        nonlocal count
        count+=1
        if count==3:result['identity']=identity(target('bob',1002))
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['overall']=='FAIL' and len(f.calls)==3


def test_same_uid_or_same_user_cannot_be_two_users():
    a,b,f=contexts()
    alias=client.TwoUserContext(target('other',1001),experiment=a._experiment,transport=f)
    with pytest.raises(ValueError):client.TwoUserExperiment(a,alias)
    with pytest.raises(ValueError):client.TwoUserExperiment(a,a)


def test_owner_mismatch_stops_without_cancelling_anything():
    def mutate(t,r,result):
        if r['operation']=='submit':result['data']['owner']['reported_user']='someone_else'
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['overall']=='FAIL' and report['error_code']=='SLURM_OWNER_MISMATCH'
    assert not any(r['operation'] in ('cleanup','cross_cancel') for t,r,k in f.calls)
    assert a.job is not None  # safe own receipt retained for review, not cancellation authority


def test_unexpected_cross_access_partial_and_own_cleanup_only():
    def mutate(t,r,result):
        if r['operation']=='cross_fs':result['data']=dict(access='ALLOW',errno=0,category='FILESYSTEM_POLICY_FINDING',mode='0700',uid=1002,gid=1002)
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['overall']=='PARTIAL' and report['finding']=='FILESYSTEM_POLICY_FINDING'
    assert not any(r['operation']=='submit' for t,r,k in f.calls)
    assert all(r['cleanup']['filesystem']=='REMOVED' for r in report['users'])


def test_slurm_control_finding_stops_negative_tests_but_cleans_own_jobs():
    def mutate(t,r,result):
        if r['operation']=='cross_cancel':result['data']=dict(result='FAIL',category='SLURM_AUTHORIZATION_FINDING',returncode=0)
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['overall']=='PARTIAL' and report['cross_user_slurm']=='FAIL'
    assert len([1 for t,r,k in f.calls if r['operation']=='cross_cancel'])==1
    assert len([1 for t,r,k in f.calls if r['operation']=='cleanup'])==2


def test_finished_job_negative_test_not_required_for_core_pass():
    def mutate(t,r,result):
        if r['operation']=='cross_cancel':result['data']=dict(result='NOT_TESTED',reason='ALREADY_TERMINAL',returncode=None)
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run(authorize_two_smokes=True)
    assert report['overall']=='PASS' and report['cross_user_slurm']=='NOT_TESTED'


def test_current_run_job_receipt_required_no_arbitrary_job_id():
    a,b,f=contexts();a.run(client.Phase.IDENTITY);b.run(client.Phase.IDENTITY)
    with pytest.raises(ValueError):a.run(client.Phase.CROSS_CANCEL,peer=b)
    a.workspace=workspace(a.target,a._experiment);a.job=job(a.target,a._experiment)
    a.job['experiment']=str(uuid4())
    with pytest.raises(original.SSHProbeError,match='POC_OWNERSHIP_GUARD'):a.run(client.Phase.CLEANUP)
    assert len(f.calls)==2


def test_credential_noise_never_in_report_or_logs(caplog):
    def mutate(t,r,result):
        result.update(result='FAIL',error_code='SSH_AUTH_FAILED',raw='synthetic-sensitive-value')
        result['data']['unexpected']='synthetic-sensitive-value'
    caplog.set_level('INFO',logger='sbatch_agent.ssh_poc_two_user')
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run()
    assert 'synthetic-sensitive-value' not in json.dumps(report)+caplog.text
    assert report['error_code']=='SSH_AUTH_FAILED'


@pytest.mark.parametrize('bad', ['whoami','id;false',None,['id']])
def test_operation_allowlist(bad):
    a,b,f=contexts()
    with pytest.raises(ValueError):a.run(bad)
    assert not f.calls


def test_payload_is_fixed_and_does_not_execute_main_on_import(monkeypatch):
    argv=original.ssh_arguments(target(),program=original.ProbeProgram.TWO_USER)
    assert 'StrictHostKeyChecking=yes' in argv and 'ControlPath=none' in argv
    import shlex,sys,io
    source=shlex.split(argv[-1])[-1]
    # Execute payload with invalid input, proving local/remote import wiring only.
    monkeypatch.setattr(sys,'stdin',io.TextIOWrapper(io.BytesIO(b'{}')))
    exec(compile(source,'<fixed-poc-payload>','exec'),{})


def local_ident(tmp):
    return dict(home=str(tmp),uid=os.getuid(),username='alice')


def test_real_local_temp_setup_and_guarded_nonrecursive_cleanup(tmp_path):
    ident=local_ident(tmp_path); experiment=str(uuid4())
    r=remote.setup(ident,experiment)
    assert r['temp_deleted'] and r['mode']=='0700'
    descriptors=remote.open_workspace(ident,experiment,r['workspace'])
    try:assert remote.cleanup(ident,experiment,descriptors,None)==dict(filesystem='REMOVED',job='NONE')
    finally:
        for fd in descriptors:os.close(fd)
    assert not list(tmp_path.iterdir())


def test_preexisting_directory_never_adopted_or_chmodded(tmp_path):
    base=tmp_path/remote.DIRECTORY;base.mkdir(mode=0o755)
    sentinel=base/'existing';sentinel.write_text('untouched')
    before=base.stat().st_mode
    with pytest.raises(FileExistsError):remote.setup(local_ident(tmp_path),str(uuid4()))
    assert base.stat().st_mode==before and sentinel.read_text()=='untouched'


def test_cleanup_rejects_other_owner_receipt_before_file_access(tmp_path):
    ident=local_ident(tmp_path);experiment=str(uuid4())
    with pytest.raises(remote.F,match='POC_OWNERSHIP_GUARD'):
        remote.open_workspace(ident,experiment,dict(username='bob',uid=123,home=str(tmp_path),experiment=experiment))
    assert not list(tmp_path.iterdir())


def test_cleanup_preserves_unknown_file(tmp_path):
    ident=local_ident(tmp_path);experiment=str(uuid4());r=remote.setup(ident,experiment)
    sentinel=tmp_path/remote.DIRECTORY/'unknown';sentinel.write_text('keep')
    descriptors=remote.open_workspace(ident,experiment,r['workspace'])
    try:
        with pytest.raises(remote.F,match='POC_OWNERSHIP_GUARD'):remote.cleanup(ident,experiment,descriptors,None)
    finally:
        for fd in descriptors:os.close(fd)
    assert sentinel.read_text()=='keep'


@pytest.mark.parametrize('number,expected', [(errno.EACCES,'DENY'),(errno.EPERM,'DENY'),(errno.ENOENT,'ERROR')])
def test_cross_permission_classification_requires_os_error(monkeypatch,number,expected):
    from types import SimpleNamespace
    other=workspace(target('bob',1002),'experiment');calls=[]
    monkeypatch.setattr(remote.pwd,'getpwnam',lambda _:SimpleNamespace(pw_uid=1002,pw_dir=other['home']))
    def open_(path):
        calls.append(path);raise OSError(number,'synthetic-sensitive-value')
    monkeypatch.setattr(remote,'open_cross_directory',open_)
    if expected=='DENY':
        result=remote.cross_fs(dict(username='alice',uid=1001),'experiment',other)
        assert result['access']=='DENY' and result['errno']==number
    else:
        with pytest.raises(remote.F,match='REMOTE_COMMAND_FAILED'):remote.cross_fs(dict(username='alice',uid=1001),'experiment',other)
    assert calls==[other['home']+'/'+remote.DIRECTORY+'/private-test']


@pytest.mark.parametrize('returncode,stderr,expected', [(1,'Access/permission denied','PASS'),(1,'Invalid job id specified','NOT_TESTED'),(0,'','FAIL')])
def test_slurm_cross_control_classification(monkeypatch,returncode,stderr,expected):
    exp=str(uuid4());other=job(target('bob',1002),exp);commands=[]
    monkeypatch.setattr(remote,'job_owner',lambda *a:dict(state='PENDING',reported_user='bob'))
    def call(args,**kw):
        commands.append(args);return subprocess.CompletedProcess(args,returncode,'',stderr)
    monkeypatch.setattr(remote.base,'call',call)
    result=remote.cross_cancel(dict(username='alice'),exp,other)
    assert result['result']==expected and commands==[['/usr/bin/scancel','1002']]


def test_identity_failure_prevents_remote_filesystem_and_slurm(monkeypatch):
    def mismatch(*a):raise remote.F('SSH_IDENTITY_MISMATCH')
    monkeypatch.setattr(remote.base,'identity',mismatch)
    result=remote.execute(dict(operation='setup',experiment=str(uuid4()),username='bob',expected_uid=1002,arguments={}))
    assert result['error_code']=='SSH_IDENTITY_MISMATCH'


def test_cross_probe_ancestors_require_traversal_not_listing(tmp_path,monkeypatch):
    # Only this local test directory is involved; not a two-user integration.
    ancestor=tmp_path/'traverse-only';ancestor.mkdir(mode=0o711)
    dedicated=ancestor/'dedicated';dedicated.mkdir()
    real_open=os.open;flags=[]
    def recorded(path,mode,*a,**kw):
        flags.append((path,mode));return real_open(path,mode,*a,**kw)
    monkeypatch.setattr(remote.os,'open',recorded)
    fd=remote.open_cross_directory(str(dedicated))
    try:assert os.listdir(fd)==[]
    finally:os.close(fd)
    assert all(mode & os.O_PATH for name,mode in flags[:-1])
    assert not flags[-1][1] & os.O_PATH


def test_cli_rejects_arbitrary_target_without_echoing_arguments(capsys):
    import importlib.util
    spec=importlib.util.spec_from_file_location('a2_cli',Path(__file__).parents[1]/'scripts/ssh_two_user_poc.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with pytest.raises(SystemExit):
        module.main(['--uid-a','1001','--user-b','bob','--uid-b','1002','--host','synthetic-sensitive-value'])
    output=capsys.readouterr()
    assert 'synthetic-sensitive-value' not in output.out+output.err
    assert 'POC_INPUT_INVALID' in output.err


def test_two_user_transport_itself_rejects_alternate_target():
    alternate=original.SSHExecutionTarget('127.0.0.1',SSH_PORT,'alice',1001)
    with pytest.raises(ValueError):original.ssh_arguments(alternate,program=original.ProbeProgram.TWO_USER)


def test_unknown_setup_outcome_is_explicit_manual_review():
    def mutate(t,r,result):
        if r['operation']=='setup':result.update(result='FAIL',error_code='REMOTE_COMMAND_FAILED',data={})
    a,b,f=contexts(Fake(mutate));report=client.TwoUserExperiment(a,b).run()
    assert report['users'][0]['cleanup']['reason']=='SETUP_OUTCOME_UNCONFIRMED'
    assert not any(r['operation']=='submit' for t,r,k in f.calls)
