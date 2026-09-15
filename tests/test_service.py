"""Offline application orchestration: fake client, real temporary SQLite/files."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import subprocess
from threading import Barrier

import pytest

from sbatch_agent import (
    CommandResult, EnvironmentDefinition, JobNotSubmittableError, JobNotSubmittedError,
    JobRepository, JobSpec, JobSpecValidationError, JobState, JobStatus, PersistenceError,
    RecordNotFoundError, SlurmClient, SlurmCommandError, SlurmParseError, StaticProfiles,
    SubmissionConflictError, SubmissionResult, SubmissionService, SubmissionServiceError,
    SubmissionState, SubprocessRunner, render_job_script,
)
import sbatch_agent.service as service_module


@pytest.fixture(autouse=True)
def forbid_real_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("M4-B tests must only use FakeSlurmClient; no process or real CLI")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(SubprocessRunner, "run", forbidden)
    monkeypatch.setattr(SlurmClient, "submit", forbidden)
    monkeypatch.setattr(SlurmClient, "get_status", forbidden)


@pytest.fixture
def spec(tmp_path):
    return JobSpec.model_validate({
        "project_dir": str(tmp_path / "project"), "work_dir": str(tmp_path / "work"),
        "run_type": "python", "entrypoint": "hello.py",
        "environment_profile": {"id": "system-python", "version": "1"},
        "run_step": {"executable": "/usr/bin/python3", "args": [
            "hello.py", "中文 case 01; ' \" $ & (x)",
        ]},
        "resources": {"partition": "offline_cpu", "memory_mib": 256, "time_limit_seconds": 120},
        "stdout": "logs/demo-%j.out", "stderr": "logs/demo-%j.err",
        "spec_version": 3,
    })


@pytest.fixture
def profiles():
    return StaticProfiles(environments=[
        EnvironmentDefinition(id="system-python", version="1", load_steps=[]),
    ])


@pytest.fixture
def repo(tmp_path):
    with JobRepository(tmp_path / "jobs.sqlite3") as repository:
        yield repository


def status(state, *, raw=None, code=None, signal=None, reason=None):
    return JobStatus(
        "123", state, state.value if raw is None else raw,
        "sacct" if code is not None else "squeue",
        (CommandResult(("fake-status", "123"), 0, "synthetic response\n", ""),),
        reason=reason, exit_code=code, signal=signal,
        raw_exit_code=f"{code}:{signal}" if code is not None else None,
    )


class FakeSlurmClient:
    def __init__(self, *, failure=None, states=()):
        self.failure = failure
        self.states = list(states)
        self.submit_calls = []
        self.status_calls = []
        self.before_submit = None
        self.receipt = None

    def submit(self, script_path):
        path = Path(script_path)
        self.submit_calls.append(path)
        if self.before_submit is not None:
            self.before_submit(path)
        if self.failure is not None:
            raise self.failure
        self.receipt = SubmissionResult("123", "offline-cluster", CommandResult(
            ("fake-submit", str(path)), 0, "123;offline-cluster\n", "site warning\n",
        ))
        return self.receipt

    def get_status(self, job_id):
        self.status_calls.append(job_id)
        assert self.states, "unexpected extra query or retry"
        result = self.states.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_service(repo, client, profiles, tmp_path):
    return SubmissionService(
        repository=repo, slurm_client=client, profiles=profiles,
        submission_root=tmp_path / "durable submissions",
    )


def test_full_lifecycle_commits_snapshots_and_claim_before_client(repo, spec, profiles, tmp_path):
    observations = [status(JobState.PENDING, reason="Priority"), status(JobState.RUNNING),
                    status(JobState.COMPLETED, code=0, signal=0)]
    client = FakeSlurmClient(states=observations)
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(name="demo", spec=spec)
    assert record.submission_state is SubmissionState.SCRIPT_RENDERED
    assert record.job_spec == spec and record.spec_version == 3
    assert record.rendered_script == render_job_script(spec, profiles=profiles)
    assert record.stdout_path == spec.stdout and record.stderr_path == spec.stderr
    assert repo.get(record.id) == record
    assert not service.submission_root.exists()
    assert not client.submit_calls and not client.status_calls

    def before_submit(path):
        # Separate connection proves the claim and inputs were committed before
        # invoking the client, not merely changed inside the submitting process.
        with JobRepository(tmp_path / "jobs.sqlite3") as witness:
            saved = witness.get(record.id)
        assert saved.submission_state is SubmissionState.SUBMITTING
        assert saved.job_spec_snapshot == record.job_spec_snapshot
        assert saved.rendered_script == record.rendered_script
        assert path == Path(saved.script_path)
        assert path.read_bytes() == saved.rendered_script.encode("utf-8")
        assert not path.with_name(".submit.sh.pending").exists()
    client.before_submit = before_submit
    submitted = service.submit_job(record.id)
    assert submitted.submission_state is SubmissionState.SUBMITTED
    assert submitted.normalized_slurm_state is None
    assert submitted.slurm_job_id == "123" and submitted.cluster_name == "offline-cluster"
    assert submitted.submission_command == client.receipt.command
    assert submitted.submit_stdout == "123;offline-cluster\n"
    assert submitted.submit_stderr == "site warning\n" and submitted.submit_returncode == 0

    previous = submitted
    for expected in observations:
        updated = service.refresh_status(record.id)
        assert updated.job_status == expected
        assert updated.updated_at > previous.updated_at
        assert updated.submission_state is SubmissionState.SUBMITTED
        previous = updated
    assert updated.normalized_slurm_state is JobState.COMPLETED
    assert updated.exit_code == updated.signal == 0 and updated.raw_exit_code == "0:0"
    assert updated.job_spec_snapshot == record.job_spec_snapshot
    assert updated.rendered_script == record.rendered_script
    assert updated.created_at == record.created_at
    assert client.status_calls == ["123", "123", "123"] and len(client.submit_calls) == 1
    assert not Path(spec.work_dir).exists()  # Service only owns submission artifacts.


@pytest.mark.parametrize("invalid", ["resource", "unresolved", "profile", "directive", "type", "missing"])
def test_failed_create_has_no_record_or_artifacts(repo, spec, profiles, tmp_path, invalid):
    client = FakeSlurmClient()
    if invalid == "resource":
        spec.resources.memory_mib = 0
    elif invalid == "unresolved":
        data = spec.model_dump()
        data["unresolved"] = [{"field": "resources", "reason": "needs confirmation"}]
        spec = JobSpec.model_validate(data)
    elif invalid == "profile":
        profiles.environments.clear()
    elif invalid == "directive":
        spec.job_name = "injected\n#SBATCH --nodes=99"
    elif invalid == "type":
        spec = {}
    else:
        data = spec.model_dump()
        del data["run_step"]
        spec = JobSpec.model_construct(**data)
    service = make_service(repo, client, profiles, tmp_path)
    with pytest.raises(JobSpecValidationError):
        service.create_job(spec=spec)
    assert repo.list() == [] and not service.submission_root.exists()
    assert client.submit_calls == []


def test_renderer_exception_or_storage_failure_does_not_start_lifecycle(repo, spec, profiles, tmp_path, monkeypatch):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("render failed")
    with monkeypatch.context() as patch:
        patch.setattr(service_module, "render_job_script", fail)
        with pytest.raises(RuntimeError, match="render failed"):
            service.create_job(spec=spec)
    assert repo.list() == []
    repo.close()
    with pytest.raises(PersistenceError, match="closed"):
        service.create_job(spec=spec)
    assert client.submit_calls == [] and not service.submission_root.exists()


def test_duplicate_submit_is_refused_without_rewriting_script(repo, spec, profiles, tmp_path):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    saved = service.submit_job(record.id)
    path = Path(saved.script_path)
    before = path.stat()
    with pytest.raises(JobNotSubmittableError, match="SUBMITTED"):
        service.submit_job(record.id)
    assert len(client.submit_calls) == 1
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert repo.get(record.id) == saved


@pytest.mark.parametrize("state", [SubmissionState.SUBMITTING, SubmissionState.SUBMIT_FAILED,
                                    SubmissionState.SUBMISSION_UNKNOWN])
def test_every_existing_attempt_blocks_submit(repo, spec, profiles, tmp_path, state):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    kwargs = {} if state is SubmissionState.SUBMITTING else {"error_message": "previous outcome"}
    previous = repo.update_submission(record.id, state=state, **kwargs)
    with pytest.raises(JobNotSubmittableError, match=state.value):
        service.submit_job(record.id)
    assert not client.submit_calls and not service.submission_root.exists()
    assert repo.get(record.id) == previous


def command_error(code, stdout="", stderr="", cause=None):
    error = SlurmCommandError("synthetic execution failure", CommandResult(
        ("fake-submit",), code, stdout, stderr,
    ))
    error.__cause__ = cause
    return error


@pytest.mark.parametrize("failure", [
    command_error(1, "diagnostic\n", "invalid account\n"),
    command_error(None, cause=FileNotFoundError("missing executable")),
    command_error(None, cause=PermissionError("cannot execute")),
])
def test_definite_failure_retains_inputs_and_diagnostics_without_retry(repo, spec, profiles, tmp_path, failure):
    client = FakeSlurmClient(failure=failure)
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    with pytest.raises(SubmissionServiceError) as caught:
        service.submit_job(record.id)
    assert caught.value.__cause__ is failure and caught.value.record_id == record.id
    saved = repo.get(record.id)
    assert saved.submission_state is SubmissionState.SUBMIT_FAILED
    assert saved.submission_command == failure.result
    assert str(failure) in saved.submission_error
    assert saved.slurm_job_id is None
    assert saved.job_spec_snapshot == record.job_spec_snapshot
    assert saved.rendered_script == record.rendered_script
    with pytest.raises(JobNotSubmittableError):
        service.submit_job(record.id)
    assert len(client.submit_calls) == 1


@pytest.mark.parametrize("failure", [
    command_error(None, "partial receipt", "partial error", subprocess.TimeoutExpired("fake", 1)),
    command_error(-9),
    command_error(None, cause=OSError("unclassified OS error")),
    SlurmParseError("malformed acknowledgement", CommandResult(("fake-submit",), 0, "bad reply", "notice")),
    RuntimeError("client unexpectedly disconnected"),
])
def test_uncertain_result_is_unknown_and_never_retried(repo, spec, profiles, tmp_path, failure):
    client = FakeSlurmClient(failure=failure)
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    with pytest.raises(SubmissionServiceError) as caught:
        service.submit_job(record.id)
    saved = repo.get(record.id)
    assert caught.value.__cause__ is failure
    assert saved.submission_state is SubmissionState.SUBMISSION_UNKNOWN
    assert saved.slurm_job_id is None and saved.normalized_slurm_state is None
    assert saved.submission_command == getattr(failure, "result", None)
    with pytest.raises(JobNotSubmittableError):
        service.submit_job(record.id)
    assert len(client.submit_calls) == 1


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(2)])
def test_soft_interruption_checkpoints_unknown_and_propagates(repo, spec, profiles, tmp_path, interruption):
    client = FakeSlurmClient(failure=interruption)
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    with pytest.raises(type(interruption)):
        service.submit_job(record.id)
    assert repo.get(record.id).submission_state is SubmissionState.SUBMISSION_UNKNOWN
    with pytest.raises(JobNotSubmittableError):
        service.submit_job(record.id)
    assert len(client.submit_calls) == 1


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.UNKNOWN])
def test_refresh_preserves_submission_and_saves_state(repo, spec, profiles, tmp_path, state):
    observed = (status(JobState.FAILED, code=1, signal=0) if state is JobState.FAILED else
                JobStatus("123", JobState.UNKNOWN, None, "none", ()))
    client = FakeSlurmClient(states=[status(JobState.COMPLETED, code=0, signal=0), observed])
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    submitted = service.submit_job(record.id)
    service.refresh_status(record.id)
    saved = service.refresh_status(record.id)
    assert saved.normalized_slurm_state is state and saved.job_status == observed
    assert saved.submission_state is SubmissionState.SUBMITTED
    assert saved.slurm_job_id == submitted.slurm_job_id == "123"
    assert saved.submission_command == submitted.submission_command
    assert len(client.submit_calls) == 1 and client.status_calls == ["123", "123"]


def test_refresh_without_bound_job_is_refused(repo, spec, profiles, tmp_path):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    with pytest.raises(JobNotSubmittedError):
        service.refresh_status(record.id)
    assert not client.status_calls


def test_query_failure_does_not_replace_last_observation(repo, spec, profiles, tmp_path):
    failure = SlurmCommandError("query unavailable", CommandResult(("fake-status",), 1, "", "offline"))
    client = FakeSlurmClient(states=[status(JobState.RUNNING), failure])
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    service.submit_job(record.id)
    previous = service.refresh_status(record.id)
    with pytest.raises(SubmissionServiceError) as caught:
        service.refresh_status(record.id)
    assert caught.value.__cause__ is failure
    assert repo.get(record.id) == previous and len(client.status_calls) == 2


def test_restart_can_refresh_without_profiles_or_script_file(spec, profiles, tmp_path):
    path = tmp_path / "restart.sqlite3"
    first_client = FakeSlurmClient()
    with JobRepository(path) as first_repo:
        first = make_service(first_repo, first_client, profiles, tmp_path)
        record = first.create_job(spec=spec)
        first.submit_job(record.id)
    # Refresh only requires the persisted job association; the file is not read.
    Path(record.script_path).write_text("external change", encoding="utf-8")
    second_client = FakeSlurmClient(states=[status(JobState.RUNNING)])
    with JobRepository(path) as second_repo:
        second = make_service(second_repo, second_client, StaticProfiles(), tmp_path)
        saved = second.refresh_status(record.id)
        assert saved.normalized_slurm_state is JobState.RUNNING
        assert saved.rendered_script == record.rendered_script
        with pytest.raises(JobNotSubmittableError):
            second.submit_job(record.id)
    assert not second_client.submit_calls and second_client.status_calls == ["123"]


def test_post_acceptance_storage_failure_retains_receipt_and_blocks_retry(repo, spec, profiles, tmp_path, monkeypatch):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    original = repo.update_submission
    failure = PersistenceError("simulated database full")
    def fail_receipt(record_id, result=None, **kwargs):
        if result is not None:
            raise failure
        return original(record_id, **kwargs)
    monkeypatch.setattr(repo, "update_submission", fail_receipt)
    with pytest.raises(SubmissionServiceError) as caught:
        service.submit_job(record.id)
    assert caught.value.submission_result == client.receipt
    assert caught.value.__cause__ is failure and caught.value.persistence_error is failure
    assert repo.get(record.id).submission_state is SubmissionState.SUBMITTING
    with JobRepository(tmp_path / "jobs.sqlite3") as reopened:
        resumed = make_service(reopened, client, profiles, tmp_path)
        with pytest.raises(JobNotSubmittableError):
            resumed.submit_job(record.id)
    assert len(client.submit_calls) == 1


def test_interruption_between_acceptance_and_receipt_save_stays_submitting(repo, spec, profiles, tmp_path, monkeypatch):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    original = repo.update_submission
    def interrupt(record_id, result=None, **kwargs):
        if result is not None:
            raise SystemExit("simulated process stop before save")
        return original(record_id, **kwargs)
    monkeypatch.setattr(repo, "update_submission", interrupt)
    with pytest.raises(SystemExit):
        service.submit_job(record.id)
    repo.close()
    with JobRepository(tmp_path / "jobs.sqlite3") as reopened:
        assert reopened.get(record.id).submission_state is SubmissionState.SUBMITTING
        with pytest.raises(JobNotSubmittableError):
            make_service(reopened, client, profiles, tmp_path).submit_job(record.id)
    assert len(client.submit_calls) == 1


def test_error_outcome_storage_failure_keeps_both_errors(repo, spec, profiles, tmp_path, monkeypatch):
    original_failure = command_error(None)
    client = FakeSlurmClient(failure=original_failure)
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    original = repo.update_submission
    storage_failure = PersistenceError("write unavailable")
    def fail_outcome(record_id, result=None, **kwargs):
        if kwargs.get("state") is not SubmissionState.SUBMITTING:
            raise storage_failure
        return original(record_id, **kwargs)
    monkeypatch.setattr(repo, "update_submission", fail_outcome)
    with pytest.raises(SubmissionServiceError) as caught:
        service.submit_job(record.id)
    assert caught.value.__cause__ is original_failure
    assert caught.value.persistence_error is storage_failure
    assert repo.get(record.id).submission_state is SubmissionState.SUBMITTING
    assert len(client.submit_calls) == 1


def test_failed_atomic_claim_never_writes_or_calls_client(repo, spec, profiles, tmp_path, monkeypatch):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    def fail(*args, **kwargs):
        raise PersistenceError("claim could not commit")
    monkeypatch.setattr(repo, "update_submission", fail)
    with pytest.raises(PersistenceError, match="claim"):
        service.submit_job(record.id)
    assert not service.submission_root.exists() and not client.submit_calls
    assert repo.get(record.id) == record


def test_two_independent_connections_compete_for_one_atomic_claim(spec, profiles, tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    client = FakeSlurmClient()
    with JobRepository(path) as initial:
        record = make_service(initial, client, profiles, tmp_path).create_job(spec=spec)
    barrier = Barrier(2)
    def competitor():
        # Each test thread creates and closes its own SQLite connection.
        with JobRepository(path) as repository:
            service = make_service(repository, client, profiles, tmp_path)
            original = repository.update_submission
            def claim_at_same_time(record_id, result=None, **kwargs):
                if kwargs.get("state") is SubmissionState.SUBMITTING:
                    barrier.wait(timeout=5)  # Both have read SCRIPT_RENDERED.
                return original(record_id, result, **kwargs)
            repository.update_submission = claim_at_same_time
            try:
                return service.submit_job(record.id)
            except JobNotSubmittableError as exc:
                assert isinstance(exc.__cause__, SubmissionConflictError)
                return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(competitor) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(result, JobNotSubmittableError) for result in results) == 1
    assert len(client.submit_calls) == 1
    with JobRepository(path) as repository:
        assert repository.get(record.id).submission_state is SubmissionState.SUBMITTED


def test_snapshot_is_used_after_original_spec_and_profiles_change(repo, spec, profiles, tmp_path):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    original = service.create_job(spec=spec)
    spec.spec_version = 4
    spec.run_step.args.append("changed")
    profiles.environments.clear()
    submitted = service.submit_job(original.id)
    assert submitted.job_spec_snapshot == original.job_spec_snapshot
    assert Path(submitted.script_path).read_bytes() == original.rendered_script.encode("utf-8")
    assert submitted.spec_version == 3


def test_job_name_cannot_control_artifact_path_or_overwrite_another_record(repo, spec, profiles, tmp_path):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    first = service.create_job(name="../../escape/submit.sh", spec=spec)
    second = service.create_job(name="../../escape/submit.sh", spec=spec)
    service.submit_job(first.id)
    service.submit_job(second.id)
    for record in (first, second):
        assert Path(record.script_path) == service.submission_root / record.id / "submit.sh"
        assert Path(record.script_path).read_bytes() == record.rendered_script.encode("utf-8")
    assert first.script_path != second.script_path
    assert len(client.submit_calls) == 2
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("existing", ["directory", "symlink", "file"])
def test_existing_attempt_path_is_never_adopted_or_overwritten(repo, spec, profiles, tmp_path, existing):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    parent = Path(record.script_path).parent
    service.submission_root.mkdir()
    target = tmp_path / "unrelated"
    target.mkdir()
    sentinel = target / "submit.sh"
    sentinel.write_text("keep unknown content", encoding="utf-8")
    if existing == "directory":
        parent.mkdir()
        (parent / "submit.sh").write_text("existing content", encoding="utf-8")
    elif existing == "symlink":
        parent.symlink_to(target, target_is_directory=True)
    else:
        parent.write_text("existing file", encoding="utf-8")
    with pytest.raises(SubmissionServiceError):
        service.submit_job(record.id)
    saved = repo.get(record.id)
    assert saved.submission_state is SubmissionState.SUBMIT_FAILED
    assert saved.submission_command is None
    assert saved.rendered_script == record.rendered_script
    assert sentinel.read_text() == "keep unknown content" and not client.submit_calls
    if existing == "directory":
        assert (parent / "submit.sh").read_text() == "existing content"


def test_partial_write_is_not_published_or_submitted(repo, spec, profiles, tmp_path, monkeypatch):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    def fail_fsync(*args):
        raise OSError("simulated storage failure")
    monkeypatch.setattr(service_module.os, "fsync", fail_fsync)
    with pytest.raises(SubmissionServiceError) as caught:
        service.submit_job(record.id)
    assert isinstance(caught.value.__cause__, OSError)
    assert not Path(record.script_path).exists()
    assert Path(record.script_path).with_name(".submit.sh.pending").exists()
    saved = repo.get(record.id)
    assert saved.submission_state is SubmissionState.SUBMIT_FAILED
    assert "storage failure" in saved.submission_error and not client.submit_calls


def test_atomic_publication_refuses_destination_collision(repo, spec, profiles, tmp_path, monkeypatch):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    original_link = service_module.os.link
    def collision(source, destination):
        Path(destination).write_text("unknown concurrent file", encoding="utf-8")
        return original_link(source, destination)
    monkeypatch.setattr(service_module.os, "link", collision)
    with pytest.raises(SubmissionServiceError):
        service.submit_job(record.id)
    assert Path(record.script_path).read_text() == "unknown concurrent file"
    assert not client.submit_calls


def test_content_mismatch_stops_before_client(repo, spec, profiles, tmp_path, monkeypatch):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    read_bytes = Path.read_bytes
    def mismatch(path):
        if path == Path(record.script_path):
            return b"modified content"
        return read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", mismatch)
    with pytest.raises(SubmissionServiceError, match="differs"):
        service.submit_job(record.id)
    assert not client.submit_calls
    assert repo.get(record.id).submission_state is SubmissionState.SUBMIT_FAILED


def test_changed_root_configuration_is_rejected_before_claim(repo, spec, profiles, tmp_path):
    client = FakeSlurmClient()
    record = make_service(repo, client, profiles, tmp_path).create_job(spec=spec)
    other = SubmissionService(repository=repo, slurm_client=client, profiles=profiles,
                              submission_root=tmp_path / "different-root")
    with pytest.raises(JobNotSubmittableError, match="script_path"):
        other.submit_job(record.id)
    assert repo.get(record.id) == record and not client.submit_calls
    assert not other.submission_root.exists()


def test_legacy_record_without_service_script_path_is_not_submitted(repo, spec, profiles, tmp_path):
    client = FakeSlurmClient()
    record = repo.create(spec, render_job_script(spec, profiles=profiles))
    with pytest.raises(JobNotSubmittableError, match="script_path"):
        make_service(repo, client, profiles, tmp_path).submit_job(record.id)
    assert repo.get(record.id) == record and not client.submit_calls


@pytest.mark.parametrize("root", ["relative", "", "/absolute/../elsewhere", "/bad\nroot"])
def test_root_requires_explicit_absolute_safe_path(repo, profiles, root):
    with pytest.raises(ValueError, match="submission_root"):
        SubmissionService(repository=repo, slurm_client=FakeSlurmClient(), profiles=profiles,
                          submission_root=root)


def test_nonexistent_record_errors_are_preserved(repo, profiles, tmp_path):
    client = FakeSlurmClient()
    service = make_service(repo, client, profiles, tmp_path)
    for method in (service.submit_job, service.refresh_status):
        with pytest.raises(RecordNotFoundError):
            method("nonexistent")
    assert not client.submit_calls and not client.status_calls


def test_status_wrong_job_is_rejected_without_overwriting_snapshot(repo, spec, profiles, tmp_path):
    client = FakeSlurmClient(states=[replace(status(JobState.RUNNING), job_id="999")])
    service = make_service(repo, client, profiles, tmp_path)
    record = service.create_job(spec=spec)
    saved = service.submit_job(record.id)
    with pytest.raises(SubmissionServiceError) as caught:
        service.refresh_status(record.id)
    assert isinstance(caught.value.__cause__, PersistenceError)
    assert repo.get(record.id) == saved
