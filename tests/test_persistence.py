"""M4-A: temporary SQLite only; no CLI, network or real working directories."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import UUID, uuid4

import pytest
import yaml

from sbatch_agent import (
    CommandResult, DB_SCHEMA_VERSION, EnvironmentDefinition, JobRecord, JobRepository,
    JobSpec, JobState, JobStatus, PersistenceError, RecordNotFoundError,
    SchemaVersionError, SlurmClient, SlurmCommandError, StaticProfiles,
    SubmissionResult, SubmissionState, SubprocessRunner, render_job_script,
    resolve_log_path,
)
import sbatch_agent.persistence as persistence


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("M4-A must not invoke SlurmClient or start a process")
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
            "hello.py", "中文 case 01; ' \" $ & (x)", "line\nbreak", "",
        ]},
        "resources": {"partition": "offline_cpu", "account": "default", "qos": "normal",
                      "memory_mib": 256, "time_limit_seconds": 120},
        "job_name": "离线快照", "stdout": "logs/smoke-%j.out", "stderr": "logs/smoke-%j.err",
        "required_inputs": ["hello.py"], "spec_version": 7,
        "evidence": [{"field": "entrypoint", "source_file": "README.md", "line": 3,
                      "kind": "direct", "detail": "离线输入\n不运行"}],
        "source_fingerprints": [{"path": "hello.py", "sha256": "ab" * 32}],
    })


@pytest.fixture
def script(spec):
    profiles = StaticProfiles(environments=[
        EnvironmentDefinition(id="system-python", version="1", load_steps=[]),
    ])
    return render_job_script(spec, profiles=profiles)


@pytest.fixture
def repo(tmp_path):
    with JobRepository(tmp_path / "jobs.sqlite3") as repository:
        yield repository


def receipt(job_id="542850", cluster=None, stderr=""):
    # Synthetic values only, never query the similarly numbered real job.
    output = job_id + (f";{cluster}" if cluster else "") + "\n"
    return SubmissionResult(job_id, cluster, CommandResult(
        ("sbatch", "--parsable", "/offline/submit.sh"), 0, output, stderr,
    ))


def observation(state, *, job_id="542850", raw=None, reason=None, exit_code=None, signal=None):
    source = "sacct" if exit_code is not None else "squeue"
    raw_state = state.value if raw is None else raw
    raw_exit = f"{exit_code}:{signal}" if exit_code is not None else None
    command = CommandResult((source, "--local", f"--jobs={job_id}"), 0, "raw response\n", "")
    return JobStatus(
        job_id, state, raw_state, source, (command,), reason=reason,
        partition="offline_cpu" if source == "squeue" else None,
        user="offline-user" if source == "squeue" else None,
        exit_code=exit_code, signal=signal, raw_exit_code=raw_exit,
        start="2026-09-07T10:00:00" if source == "sacct" else None,
        end="2026-09-07T10:00:10" if source == "sacct" else None,
    )


def test_create_get_and_readable_full_json(repo, spec, script, tmp_path):
    record = repo.create(spec, script, script_path=tmp_path / "absent.sh")
    assert isinstance(record, JobRecord)
    assert UUID(record.id).version == 4 and record.id != "542850"
    assert record.name == spec.job_name
    assert record.spec_version == 7
    assert record.job_spec == spec
    assert record.rendered_script == script
    assert repo.get(record.id) == record
    assert record.submission_state is SubmissionState.SCRIPT_RENDERED
    assert record.slurm_job_id is record.cluster_name is None
    assert record.normalized_slurm_state is record.raw_slurm_state is None
    assert record.submit_returncode is record.submit_stdout is record.submit_stderr is None
    assert record.exit_code is record.signal is record.raw_exit_code is None
    assert record.created_at == record.updated_at
    assert record.created_at.tzinfo is timezone.utc
    assert record.stdout_path == spec.stdout and record.stderr_path == spec.stderr
    assert not (tmp_path / "absent.sh").exists()
    assert not Path(spec.work_dir).exists()
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        data = connection.execute("SELECT job_spec_snapshot, created_at, updated_at FROM jobs").fetchone()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION == 1
    assert "中文" in data[0] and "\n" in data[0]
    assert JobSpec.model_validate_json(data[0]) == spec
    assert json.loads(data[0]) == spec.model_dump(mode="json")
    assert data[1].endswith("Z") and data[1] == data[2]


def test_restart_retains_submission_status_and_all_snapshots(spec, script, tmp_path):
    path = tmp_path / "restart.sqlite3"
    first = JobRepository(path)
    original = first.create(spec, script)
    first.update_submission(original.id, receipt(cluster="test-cluster", stderr="warning\n"))
    status = observation(JobState.COMPLETED, exit_code=0, signal=0)
    expected = first.update_status(original.id, status)
    first.close()
    with JobRepository(path) as second:
        recovered = second.get(original.id)
        assert recovered == expected
        assert recovered.job_spec == spec
        assert recovered.rendered_script == script
        assert recovered.job_status == status
        assert recovered.submission_command == receipt(cluster="test-cluster", stderr="warning\n").command
        assert recovered.created_at == original.created_at


@pytest.mark.parametrize("name", ["python", "compiled", "installed"])
def test_three_existing_models_renderer_persistence_round_trip(name, tmp_path):
    spec = JobSpec.model_validate(yaml.safe_load(
        (ROOT / f"examples/rendering/{name}.yaml").read_text(encoding="utf-8")
    ))
    profiles = StaticProfiles.model_validate(yaml.safe_load(
        (ROOT / "examples/profiles.yaml").read_text(encoding="utf-8")
    ))
    script = render_job_script(spec, profiles=profiles)
    with JobRepository(tmp_path / "jobs.sqlite3") as repository:
        record = repository.create(spec, script)
    with JobRepository(tmp_path / "jobs.sqlite3") as repository:
        assert repository.get(record.id).job_spec == spec
        assert repository.get(record.id).rendered_script == script


def test_snapshot_survives_source_model_result_and_file_changes(repo, spec, script, tmp_path):
    path = tmp_path / "submit.sh"
    path.write_text(script, encoding="utf-8")
    original = repo.create(spec, script, script_path=path)
    repo.update_submission(original.id, receipt())
    before = spec.model_dump()
    spec.spec_version = 8
    spec.run_step.args.append("v2")
    spec.resources.memory_mib = 1024
    path.write_text("script v2\n", encoding="utf-8")
    # Even the returned model is a detached copy; frozen outer record is no ORM.
    copy = original.job_spec
    copy.run_step.args.clear()
    copy.environment_profile.version = "new"
    assert original.job_spec.model_dump() == before
    with pytest.raises(FrozenInstanceError):
        original.rendered_script = "changed"
    saved = repo.get(original.id)
    assert saved.job_spec.model_dump() == before
    assert saved.spec_version == 7 and saved.rendered_script == script
    assert saved.script_path == str(path)
    new = repo.create(spec, "script v2\n")
    assert new.id != saved.id and new.spec_version == 8
    assert repo.get(saved.id).job_spec.model_dump() == before


def test_json_is_deterministic_even_with_reordered_dictionary(repo, spec, script):
    data = spec.model_dump()
    reordered = JobSpec.model_validate(dict(reversed(list(data.items()))))
    assert repo.create(spec, script).job_spec_snapshot == repo.create(reordered, script).job_spec_snapshot


@pytest.mark.parametrize("cluster", [None, "cluster-one"])
def test_update_submission_success(repo, spec, script, cluster):
    record = repo.create(spec, script)
    submitting = repo.update_submission(record.id, state=SubmissionState.SUBMITTING)
    assert submitting.submission_state is SubmissionState.SUBMITTING
    ack = receipt(cluster=cluster, stderr="site warning\n")
    saved = repo.update_submission(record.id, ack)
    assert saved.slurm_job_id == "542850" and saved.cluster_name == cluster
    assert saved.submission_state is SubmissionState.SUBMITTED
    assert saved.normalized_slurm_state is None  # Accepted is not COMPLETED/PENDING.
    assert saved.submit_returncode == 0 and saved.submit_stdout == ack.stdout
    assert saved.submit_stderr == "site warning\n" and saved.submission_error is None
    assert saved.created_at == record.created_at
    assert record.updated_at < submitting.updated_at < saved.updated_at
    assert repo.update_submission(record.id, ack) == saved  # Receipt replay does not mutate.
    assert resolve_log_path(saved.stdout_path, saved.slurm_job_id, work_dir=saved.job_spec.work_dir).endswith("smoke-542850.out")


@pytest.mark.parametrize("state,raw,code,signal", [
    (JobState.PENDING, "PENDING", None, None),
    (JobState.RUNNING, "RUNNING", None, None),
    (JobState.COMPLETED, "COMPLETED", 0, 0),
    (JobState.FAILED, "OUT_OF_MEMORY", 1, 0),
    (JobState.CANCELLED, "CANCELLED by 123", 0, 15),
    (JobState.TIMEOUT, "TIMEOUT", 0, 9),
    (JobState.UNKNOWN, "FUTURE_STATE", None, None),
])
def test_status_fields_and_existing_enum(repo, spec, script, state, raw, code, signal):
    record = repo.create(spec, script)
    submitted = repo.update_submission(record.id, receipt())
    status = observation(state, raw=raw, reason="Priority" if state is JobState.PENDING else None,
                         exit_code=code, signal=signal)
    saved = repo.update_status(record.id, status)
    assert saved.normalized_slurm_state is state
    assert saved.raw_slurm_state == raw and saved.status_reason == status.reason
    assert saved.exit_code == code and saved.signal == signal
    assert saved.raw_exit_code == status.raw_exit_code
    assert saved.job_status == status  # Includes source, argv, outputs, Start/End.
    assert saved.submission_state is SubmissionState.SUBMITTED
    assert saved.submission_command == submitted.submission_command
    assert saved.created_at == record.created_at and saved.updated_at > submitted.updated_at


def test_unknown_replaces_observation_without_stale_exit_code_or_erasing_record(repo, spec, script):
    record = repo.create(spec, script)
    repo.update_submission(record.id, receipt())
    previous = record
    for status in [
        observation(JobState.PENDING, reason="Priority"), observation(JobState.RUNNING),
        observation(JobState.COMPLETED, exit_code=0, signal=0),
        JobStatus("542850", JobState.UNKNOWN, None, "none", ()),
    ]:
        latest = repo.update_status(record.id, status)
        assert latest.updated_at > previous.updated_at
        assert latest.job_status == status
        previous = latest
    assert latest.normalized_slurm_state is JobState.UNKNOWN
    assert latest.raw_slurm_state is latest.exit_code is latest.signal is latest.status_reason is None
    assert latest.job_spec == spec and latest.rendered_script == script
    assert latest.slurm_job_id == "542850" and len(repo.list()) == 1


@pytest.mark.parametrize("state,code,out,err,message", [
    (SubmissionState.SUBMIT_FAILED, 1, "", "invalid account\n", "sbatch exited 1"),
    (SubmissionState.SUBMIT_FAILED, None, "", "", "Cannot execute sbatch: not found"),
    (SubmissionState.SUBMISSION_UNKNOWN, None, "542850", "partial\n", "timeout; outcome unknown"),
    (SubmissionState.SUBMISSION_UNKNOWN, 0, "bad receipt\n", "notice", "cannot parse receipt"),
])
def test_failure_and_unknown_preserve_actual_diagnostics(repo, spec, script, state, code, out, err, message):
    record = repo.create(spec, script)
    command = CommandResult(("sbatch", "--parsable", "/offline/submit.sh"), code, out, err)
    failure = SlurmCommandError(message, command)  # Construct only; nothing executed.
    saved = repo.update_submission(record.id, state=state, command_result=failure.result,
                                   error_message=str(failure))
    assert saved.submission_state is state
    assert saved.slurm_job_id is saved.cluster_name is None
    assert (saved.submit_returncode, saved.submit_stdout, saved.submit_stderr) == (code, out, err)
    assert saved.submission_error == message
    assert saved.normalized_slurm_state is None
    assert saved.job_spec == spec and saved.rendered_script == script
    # Classification/reconciliation belongs to caller, with no automatic retry.
    if state is SubmissionState.SUBMISSION_UNKNOWN:
        reconciled = repo.update_submission(record.id, receipt())
        assert reconciled.slurm_job_id == "542850"


def test_unknown_can_be_saved_without_any_returncode_or_fake_job_id(repo, spec, script):
    record = repo.create(spec, script)
    saved = repo.update_submission(record.id, state=SubmissionState.SUBMISSION_UNKNOWN,
                                   error_message="process interrupted before receipt was saved")
    assert saved.submission_command is None and saved.slurm_job_id is None


def test_submitting_survives_restart_without_automatic_recovery(spec, script, tmp_path):
    path = tmp_path / "jobs.sqlite3"
    with JobRepository(path) as repository:
        record = repository.create(spec, script)
        repository.update_submission(record.id, state=SubmissionState.SUBMITTING)
    with JobRepository(path) as repository:
        assert repository.get(record.id).submission_state is SubmissionState.SUBMITTING


@pytest.mark.parametrize("change", ["job", "cluster", "receipt", "failure"])
def test_bound_receipt_cannot_be_replaced(repo, spec, script, change):
    record = repo.create(spec, script)
    saved = repo.update_submission(record.id, receipt())
    with pytest.raises(PersistenceError, match="cannot be replaced"):
        if change == "failure":
            repo.update_submission(record.id, state=SubmissionState.SUBMISSION_UNKNOWN, error_message="unknown")
        else:
            ack = {"job": receipt("123"), "cluster": receipt(cluster="other"),
                   "receipt": receipt(stderr="changed")}[change]
            repo.update_submission(record.id, ack)
    assert repo.get(record.id) == saved


def test_same_slurm_id_can_exist_in_separate_clusters_or_records(repo, spec, script):
    records = [repo.create(spec, script) for _ in range(3)]
    for record, cluster in zip(records, ["one", "two", "one"]):
        repo.update_submission(record.id, receipt(cluster=cluster))
    assert len({record.id for record in repo.list()}) == 3
    assert {record.slurm_job_id for record in repo.list()} == {"542850"}


def test_list_newest_first_and_limit(repo, spec, script, monkeypatch):
    start = datetime(2026, 9, 7, tzinfo=timezone.utc)
    records = []
    for offset in range(3):
        monkeypatch.setattr(persistence, "_utc_now", lambda: start + timedelta(seconds=offset))
        records.append(repo.create(spec, script))
    assert repo.list() == records[::-1]
    assert repo.list(limit=2) == records[:0:-1]
    # Updating an old record does not reorder by updated_at.
    repo.update_submission(records[0].id, receipt())
    assert [r.id for r in repo.list()] == [r.id for r in records[::-1]]


def test_tied_creation_times_have_stable_order(repo, spec, script, monkeypatch):
    monkeypatch.setattr(persistence, "_utc_now", lambda: datetime(2026, 9, 7, tzinfo=timezone.utc))
    records = [repo.create(spec, script) for _ in range(3)]
    assert [r.id for r in repo.list()] == sorted((r.id for r in records), reverse=True)


@pytest.mark.parametrize("limit", [0, -1, True, "2", 1.5, None])
def test_invalid_limit(repo, limit):
    with pytest.raises(PersistenceError, match="limit"):
        repo.list(limit=limit)


def test_updated_at_advances_even_when_clock_moves_back(repo, spec, script, monkeypatch):
    record = repo.create(spec, script)
    monkeypatch.setattr(persistence, "_utc_now", lambda: record.created_at - timedelta(days=1))
    saved = repo.update_submission(record.id, receipt())
    assert saved.updated_at > record.updated_at and saved.created_at == record.created_at


def test_empty_list_missing_record_and_duplicate_id(repo, spec, script):
    assert repo.list() == []
    missing = str(uuid4())
    with pytest.raises(RecordNotFoundError):
        repo.get(missing)
    with pytest.raises(RecordNotFoundError):
        repo.update_submission(missing, receipt())
    with pytest.raises(RecordNotFoundError):
        repo.update_status(missing, observation(JobState.RUNNING))
    record = repo.create(spec, script, record_id=missing)
    with pytest.raises(PersistenceError, match="Duplicate internal"):
        repo.create(spec, "different script", record_id=missing)
    assert repo.get(missing) == record and len(repo.list()) == 1


@pytest.mark.parametrize("record_id", ["542850", "bad", "'; DROP TABLE jobs;--", ""])
def test_invalid_internal_id(repo, spec, script, record_id):
    with pytest.raises(PersistenceError, match="Cannot create"):
        repo.create(spec, script, record_id=record_id)
    assert repo.list() == []


def test_sql_parameters_keep_names_and_lookup_literal(repo, spec, script):
    name = "中文 '; DROP TABLE jobs; --"
    record = repo.create(spec, script, name=name)
    assert repo.get(record.id).name == name
    with pytest.raises(RecordNotFoundError):
        repo.get("' OR 1=1 --")
    assert len(repo.list()) == 1


@pytest.mark.parametrize("script", ["", "   ", None, 42, "bad\x00script", "bad\ud800"])
def test_invalid_script_does_not_create_partial_record(repo, spec, script):
    with pytest.raises(PersistenceError, match="Cannot create"):
        repo.create(spec, script)
    assert repo.list() == []


def test_revalidate_mutated_jobspec_and_unresolved(repo, spec, script):
    spec.resources.cpus_per_task = True
    with pytest.raises(PersistenceError, match="cpus_per_task"):
        repo.create(spec, script)
    spec.resources.cpus_per_task = 1
    invalid = spec.model_dump()
    invalid["unresolved"] = [{"field": "resources", "reason": "needs confirmation"}]
    with pytest.raises(PersistenceError, match="unresolved"):
        repo.create(JobSpec.model_validate(invalid), script)
    del invalid["run_step"]
    with pytest.raises(PersistenceError, match="run_step"):
        repo.create(JobSpec.model_construct(**invalid), script)
    assert repo.list() == []


@pytest.mark.parametrize("corrupted", ["{broken JSON", "{}", "[]", '{"run_type": "invalid"}'])
def test_corrupt_stored_jobspec_is_explicit_error(repo, spec, script, tmp_path, corrupted):
    record = repo.create(spec, script)
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        connection.execute("UPDATE jobs SET job_spec_snapshot = ? WHERE id = ?", (corrupted, record.id))
    with pytest.raises(PersistenceError, match="Invalid stored.*JobSpec"):
        repo.get(record.id)
    with pytest.raises(PersistenceError, match="Invalid stored"):
        repo.list()


@pytest.mark.parametrize("field,value", [
    ("spec_version", 9), ("created_at", "2026-09-07T08:00:00"),
    ("updated_at", "2000-01-01T00:00:00Z"), ("submission_command", "not JSON"),
    ("job_status", '{"normalized_state":"INVALID"}'),
])
def test_corrupt_stored_metadata_fails_clearly(repo, spec, script, tmp_path, field, value):
    record = repo.create(spec, script)
    repo.update_submission(record.id, receipt())
    # Column name is a fixed test parameter, not external query input.
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        connection.execute(f"UPDATE jobs SET {field} = ? WHERE id = ?", (value, record.id))
    with pytest.raises(PersistenceError, match="Invalid stored"):
        repo.get(record.id)


@pytest.mark.parametrize("version", [2, 99, -1])
def test_unknown_schema_version_fails_without_modifying_database(tmp_path, version):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('keep')")
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError, match="Unsupported"):
        JobRepository(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("version,name", [(0, "unrelated"), (0, "sqlitexforeign"), (1, "jobs")])
def test_unknown_table_schema_is_not_adopted_or_cleared(tmp_path, version, name):
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(f"CREATE TABLE {name} (value TEXT)")
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError):
        JobRepository(path)
    assert path.read_bytes() == before


def test_version_one_without_schema_is_not_silently_initialized(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(SchemaVersionError, match="does not match"):
        JobRepository(path)


def test_cannot_open_database_and_non_sqlite_file_are_clear(tmp_path):
    for path in (tmp_path, tmp_path / "missing-parent" / "jobs.sqlite3"):
        with pytest.raises(PersistenceError, match="Cannot open"):
            JobRepository(path)
    invalid = tmp_path / "not-sqlite"
    invalid.write_text("not a SQLite database", encoding="utf-8")
    with pytest.raises(PersistenceError, match="SQLite operation failed"):
        JobRepository(invalid)
    assert invalid.read_text() == "not a SQLite database"


def test_close_is_idempotent_and_context_manager_closes_on_error(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError, match="caller failure"):
        with repository:
            raise ValueError("caller failure")
    repository.close()
    with pytest.raises(PersistenceError, match="closed"):
        repository.list()
    with JobRepository(tmp_path / "jobs.sqlite3") as reopened:
        assert reopened.list() == []


def test_init_failure_closes_connection(tmp_path, monkeypatch):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")
    original_connect = sqlite3.connect
    connections = []
    def track(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(persistence.sqlite3, "connect", track)
    with pytest.raises(SchemaVersionError):
        JobRepository(path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_write_failure_rolls_back_entire_transaction(repo, spec, script, tmp_path):
    record = repo.create(spec, script)
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        # RAISE(FAIL) itself does not undo preceding writes in this statement.
        # Repository rollback must also undo the trigger's mutation.
        connection.execute("""CREATE TRIGGER fail_update BEFORE UPDATE OF submission_state ON jobs
            BEGIN
                UPDATE jobs SET name = 'partial write' WHERE id = NEW.id;
                SELECT RAISE(FAIL, 'simulated storage failure');
            END""")
    with pytest.raises(PersistenceError, match="simulated storage failure"):
        repo.update_submission(record.id, receipt())
    assert repo.get(record.id) == record


def test_failed_create_validation_rolls_back_insert(repo, spec, script, monkeypatch):
    original_decode = persistence._decode
    def fail_decode(row):
        raise PersistenceError("post-write validation failed")
    with monkeypatch.context() as patch:
        patch.setattr(persistence, "_decode", fail_decode)
        with pytest.raises(PersistenceError, match="post-write"):
            repo.create(spec, script)
    assert persistence._decode is original_decode and repo.list() == []


def test_two_repositories_observe_committed_updates(spec, script, tmp_path):
    path = tmp_path / "jobs.sqlite3"
    with JobRepository(path) as first, JobRepository(path) as second:
        record = first.create(spec, script)
        assert second.get(record.id) == record
        updated = second.update_submission(record.id, receipt())
        assert first.get(record.id) == updated


@pytest.mark.parametrize("submitted,job_id", [(False, "542850"), (True, "123")])
def test_status_must_match_bound_job(repo, spec, script, submitted, job_id):
    record = repo.create(spec, script)
    if submitted:
        record = repo.update_submission(record.id, receipt())
    with pytest.raises(PersistenceError, match="match.*bound"):
        repo.update_status(record.id, observation(JobState.RUNNING, job_id=job_id))
    assert repo.get(record.id) == record


@pytest.mark.parametrize("kwargs", [
    {}, {"state": SubmissionState.SUBMITTED}, {"state": SubmissionState.SCRIPT_RENDERED},
    {"state": SubmissionState.SUBMISSION_UNKNOWN},
    {"state": SubmissionState.SUBMITTING, "error_message": "bad"},
    {"state": SubmissionState.SUBMIT_FAILED, "error_message": " "},
    {"result": receipt(), "error_message": "contradiction"},
    {"result": replace(receipt(), job_id="fake")},
    {"result": replace(receipt(), command=CommandResult(("sbatch",), 1, "", "fail"))},
])
def test_invalid_submission_updates_leave_record_unchanged(repo, spec, script, kwargs):
    record = repo.create(spec, script)
    with pytest.raises(PersistenceError, match="Invalid submission"):
        repo.update_submission(record.id, **kwargs)
    assert repo.get(record.id) == record


def test_unsupported_log_token_never_loses_submission_receipt(repo, spec, script):
    spec.stdout = "array-%A_%a.out"
    # Persistence stores literal declarations; no new pattern support is claimed.
    record = repo.create(spec, script)
    saved = repo.update_submission(record.id, receipt())
    assert saved.slurm_job_id == "542850" and saved.stdout_path == "array-%A_%a.out"
    with pytest.raises(ValueError, match="only %j"):
        resolve_log_path(saved.stdout_path, saved.slurm_job_id, work_dir=spec.work_dir)


@pytest.mark.parametrize("field,value", [
    ("exit_code", -1), ("signal", -1), ("signal", True),
    ("normalized_state", "unrecognized-enum"), ("query_results", ["bad result"]),
])
def test_invalid_status_data_is_rejected_atomically(repo, spec, script, field, value):
    record = repo.create(spec, script)
    submitted = repo.update_submission(record.id, receipt())
    invalid = replace(observation(JobState.RUNNING), **{field: value})
    with pytest.raises(PersistenceError, match="Invalid status"):
        repo.update_status(record.id, invalid)
    assert repo.get(record.id) == submitted


def test_sqlite_integer_overflow_is_explicit_and_rolls_back(repo, spec, script):
    spec.spec_version = 2**63
    with pytest.raises(PersistenceError, match="SQLite operation failed"):
        repo.create(spec, script)
    assert repo.list() == []


@pytest.mark.parametrize("state", [SubmissionState.SUBMIT_FAILED, SubmissionState.SUBMISSION_UNKNOWN])
def test_previous_attempt_cannot_be_marked_submitting_again(repo, spec, script, state):
    record = repo.create(spec, script)
    previous = repo.update_submission(record.id, state=state, error_message="recorded outcome")
    with pytest.raises(PersistenceError, match="fresh SCRIPT_RENDERED"):
        repo.update_submission(record.id, state=SubmissionState.SUBMITTING)
    assert repo.get(record.id) == previous


def test_existing_schema_with_extra_objects_is_rejected(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    JobRepository(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE VIEW other_view AS SELECT id FROM jobs")
    before = path.read_bytes()
    with pytest.raises(SchemaVersionError, match="does not match"):
        JobRepository(path)
    assert path.read_bytes() == before


def test_raw_shell_gpu_and_launcher_survive_without_execution(repo, spec, script):
    data = spec.model_dump()
    data.update(run_type="compiled", entrypoint="./solver", prepare_steps=[
        {"kind": "shell", "script": "false\necho should-never-run", "work_dir": spec.work_dir},
    ], run_step={"executable": "./solver", "args": ["one argument"],
                 "launcher_profile": {"id": "explicit-launch", "version": "2"}})
    data["resources"].update(ntasks=2, gpus={"count": 1, "gpu_type": "example_gpu"})
    compiled = JobSpec.model_validate(data)
    saved = repo.create(compiled, script)
    assert saved.job_spec.model_dump() == data
    assert saved.job_spec.prepare_steps[0].script == "false\necho should-never-run"


def test_default_name_and_absent_log_paths(repo, spec, script):
    spec.job_name = spec.stdout = spec.stderr = None
    record = repo.create(spec, script)
    assert record.name == spec.entrypoint
    submitted = repo.update_submission(record.id, receipt())
    assert submitted.stdout_path is submitted.stderr_path is None
