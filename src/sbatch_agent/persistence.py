"""SQLite snapshots of one submission attempt; no Slurm or script execution."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import json
import os
import sqlite3
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from .models import JobSpec
from .runner import CommandResult
from .slurm import JobState, JobStatus, SubmissionResult, _job_id


DB_SCHEMA_VERSION = 1


class PersistenceError(RuntimeError):
    """Invalid input/stored data or database failure; cause retains diagnostics."""


class RecordNotFoundError(PersistenceError):
    """No record exists for the supplied internal ID."""


class SchemaVersionError(PersistenceError):
    """Unknown database version or schema; no automatic migration is attempted."""


class SubmissionConflictError(PersistenceError):
    """The atomic SCRIPT_RENDERED -> SUBMITTING transition was refused."""


class SubmissionState(StrEnum):
    SCRIPT_RENDERED = "SCRIPT_RENDERED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be nonblank text without NUL")
    value.encode("utf-8")
    return value


def _internal_id(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("record_id must be a canonical UUID string")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _read_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("record timestamps must be UTC text ending in Z")
    result = datetime.fromisoformat(value)
    if result.utcoffset() != timedelta(0):
        raise ValueError("record timestamps must be UTC")
    return result


_COMMAND = TypeAdapter(CommandResult)
_STATUS = TypeAdapter(JobStatus)
_SUBMISSION = TypeAdapter(SubmissionResult)


@dataclass(frozen=True)
class JobRecord:
    """Detached snapshot. job_spec returns a newly validated copy on each access.

    stdout_path/stderr_path retain the original declarations, possibly %j
    patterns. Resolve them with the existing resolve_log_path when needed.
    job_status is the latest observation, including raw CLI results, not a log
    of previous observations. None means no observation has been recorded.
    """

    id: str
    name: str
    spec_version: int
    job_spec_snapshot: str
    rendered_script: str
    script_path: str | None
    slurm_job_id: str | None
    cluster_name: str | None
    submission_state: SubmissionState
    submission_command: CommandResult | None
    submission_error: str | None
    job_status: JobStatus | None
    stdout_path: str | None
    stderr_path: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def job_spec(self) -> JobSpec:
        try:
            return JobSpec.model_validate_json(self.job_spec_snapshot)
        except ValidationError as exc:
            raise PersistenceError(f"Invalid stored JobSpec for record {self.id}: {exc}") from exc

    @property
    def submit_returncode(self) -> int | None:
        return self.submission_command.returncode if self.submission_command else None

    @property
    def submit_stdout(self) -> str | None:
        return self.submission_command.stdout if self.submission_command else None

    @property
    def submit_stderr(self) -> str | None:
        return self.submission_command.stderr if self.submission_command else None

    @property
    def normalized_slurm_state(self) -> JobState | None:
        return self.job_status.normalized_state if self.job_status else None

    @property
    def raw_slurm_state(self) -> str | None:
        return self.job_status.raw_state if self.job_status else None

    @property
    def status_reason(self) -> str | None:
        return self.job_status.reason if self.job_status else None

    @property
    def exit_code(self) -> int | None:
        return self.job_status.exit_code if self.job_status else None

    @property
    def signal(self) -> int | None:
        return self.job_status.signal if self.job_status else None

    @property
    def raw_exit_code(self) -> str | None:
        return self.job_status.raw_exit_code if self.job_status else None


# Existing result dataclasses are stored as readable JSON to retain argv,
# outputs, state source and Slurm timestamps without a second set of models.
_SCHEMA = """CREATE TABLE jobs (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    spec_version INTEGER NOT NULL CHECK(spec_version > 0),
    job_spec_snapshot TEXT NOT NULL,
    rendered_script TEXT NOT NULL,
    script_path TEXT,
    slurm_job_id TEXT,
    cluster_name TEXT,
    submission_state TEXT NOT NULL CHECK(submission_state IN (
        'SCRIPT_RENDERED', 'SUBMITTING', 'SUBMITTED',
        'SUBMIT_FAILED', 'SUBMISSION_UNKNOWN'
    )),
    submission_command TEXT,
    submission_error TEXT,
    job_status TEXT,
    stdout_path TEXT,
    stderr_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((submission_state = 'SUBMITTED' AND slurm_job_id IS NOT NULL)
       OR (submission_state != 'SUBMITTED' AND slurm_job_id IS NULL)),
    CHECK(slurm_job_id IS NOT NULL OR (cluster_name IS NULL AND job_status IS NULL))
)"""


def _decode(row: sqlite3.Row) -> JobRecord:
    data = dict(row)
    try:
        data["submission_state"] = SubmissionState(data["submission_state"])
        for field, adapter in (("submission_command", _COMMAND), ("job_status", _STATUS)):
            if data[field] is not None:
                data[field] = adapter.validate_json(data[field], strict=True)
        for field in ("created_at", "updated_at"):
            data[field] = _read_timestamp(data[field])
        record = JobRecord(**data)
        _internal_id(record.id)
        _text(record.name, "name")
        _text(record.rendered_script, "rendered_script")
        spec = record.job_spec
        if record.spec_version != spec.spec_version:
            raise ValueError("spec_version differs from JobSpec snapshot")
        if (record.stdout_path, record.stderr_path) != (spec.stdout, spec.stderr):
            raise ValueError("log declarations differ from JobSpec snapshot")
        if record.updated_at < record.created_at:
            raise ValueError("updated_at precedes created_at")
        if record.submission_state is SubmissionState.SUBMITTED:
            _job_id(record.slurm_job_id)
            if record.submit_returncode != 0 or record.submission_error is not None:
                raise ValueError("SUBMITTED requires a successful command and no error")
        elif record.slurm_job_id is not None or record.cluster_name is not None:
            raise ValueError("only SUBMITTED can bind a Slurm job")
        if record.job_status is not None:
            _check_status(record.job_status, record.slurm_job_id)
        return record
    except (ValueError, TypeError, PersistenceError) as exc:
        raise PersistenceError(f"Invalid stored job record {data.get('id')}: {exc}") from exc


def _check_status(status: JobStatus, slurm_job_id: str | None) -> None:
    if slurm_job_id is None or status.job_id != slurm_job_id:
        raise ValueError("status job_id must match the record's bound Slurm job")
    for field in ("exit_code", "signal"):
        value = getattr(status, field)
        if value is not None and value < 0:
            raise ValueError(f"{field} cannot be negative")


class JobRepository:
    """One synchronous SQLite connection; close explicitly or use a with block.

    Every write is transactional. Each record represents one attempt; snapshots
    have no update API, and a bound submission receipt cannot be replaced. This
    repository neither submits jobs nor makes a SQLite/Slurm atomic transaction.
    """

    def __init__(self, db_path: str | os.PathLike[str]):
        self._connection: sqlite3.Connection | None = None
        try:
            path = _text(os.fspath(db_path), "db_path")
            self._connection = sqlite3.connect(path, timeout=5, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            with self._transaction(write=True) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                objects = connection.execute(
                    "SELECT type, name, sql FROM sqlite_schema "
                    "WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
                ).fetchall()
                if version == 0 and not objects:
                    connection.execute(_SCHEMA)
                    connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")
                elif version != DB_SCHEMA_VERSION:
                    raise SchemaVersionError(
                        f"Unsupported database schema version {version}; "
                        f"expected {DB_SCHEMA_VERSION} (version 0 must be empty)"
                    )
                elif (
                    len(objects) != 1 or objects[0]["type"] != "table"
                    or objects[0]["name"] != "jobs"
                    or " ".join((objects[0]["sql"] or "").split()) != " ".join(_SCHEMA.split())
                ):
                    raise SchemaVersionError("Database schema does not match version 1")
        except (sqlite3.Error, OSError, ValueError, TypeError, PersistenceError) as exc:
            self.close()
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"Cannot open job database: {exc}") from exc

    @contextmanager
    def _transaction(self, *, write: bool = False):
        connection = self._connection
        if connection is None:
            raise PersistenceError("JobRepository is closed")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException as exc:
            connection.rollback()
            if isinstance(exc, (sqlite3.Error, OverflowError, UnicodeError)):
                raise PersistenceError(f"SQLite operation failed: {exc}") from exc
            raise

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        if self._connection is None:
            raise PersistenceError("JobRepository is closed")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _get(self, connection: sqlite3.Connection, record_id: str) -> JobRecord:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError(f"Job record not found: {record_id}")
        return _decode(row)

    def create(
        self, spec: JobSpec, rendered_script: str, *, name: str | None = None,
        script_path: str | os.PathLike[str] | None = None, record_id: str | None = None,
    ) -> JobRecord:
        """Save a complete spec and exact script before any possible submission.

        The caller renders/validates the script; this method does not re-render,
        inspect profiles, read script_path, or assert script/spec equivalence.
        """
        try:
            if not isinstance(spec, JobSpec):
                raise ValueError("spec must be a JobSpec")
            spec = JobSpec.model_validate(spec.model_dump(mode="python", warnings=False))
            if spec.unresolved:
                raise ValueError("SCRIPT_RENDERED requires a JobSpec without unresolved items")
            snapshot = _json(spec.model_dump(mode="json"))
            snapshot.encode("utf-8")
            script = _text(rendered_script, "rendered_script")
            name = _text(name if name is not None else spec.job_name or spec.entrypoint, "name")
            record_id = _internal_id(record_id if record_id is not None else str(uuid4()))
            if script_path is not None:
                script_path = _text(os.fspath(script_path), "script_path")
        except (ValueError, TypeError) as exc:
            raise PersistenceError(f"Cannot create JobRecord: {exc}") from exc
        now = _timestamp(_utc_now())
        with self._transaction(write=True) as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE id = ?", (record_id,)).fetchone():
                raise PersistenceError(f"Duplicate internal record id: {record_id}")
            connection.execute(
                "INSERT INTO jobs (id, name, spec_version, job_spec_snapshot, rendered_script, "
                "script_path, submission_state, stdout_path, stderr_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record_id, name, spec.spec_version, snapshot, script, script_path,
                 SubmissionState.SCRIPT_RENDERED.value, spec.stdout, spec.stderr, now, now),
            )
            return self._get(connection, record_id)

    def get(self, record_id: str) -> JobRecord:
        with self._transaction() as connection:
            return self._get(connection, record_id)

    def list(self, *, limit: int = 100) -> list[JobRecord]:
        """Newest first; UUID breaks exact timestamp ties deterministically."""
        if type(limit) is not int or limit <= 0:
            raise PersistenceError("limit must be a positive integer")
        with self._transaction() as connection:
            return [_decode(row) for row in connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            )]

    def update_submission(
        self, record_id: str, result: SubmissionResult | None = None, *,
        state: SubmissionState | None = None, command_result: CommandResult | None = None,
        error_message: str | None = None,
    ) -> JobRecord:
        """Store a receipt, or an explicitly classified outcome without a job ID.

        For failures/unknowns pass state, the exception's result, and str(exc).
        This API never classifies a timeout or retries a submission by itself.
        SUBMITTING atomically claims a fresh record under BEGIN IMMEDIATE;
        concurrent callers using separate connections cannot both claim it.
        """
        try:
            job_id = cluster = None
            if result is not None:
                if not isinstance(result, SubmissionResult):
                    raise ValueError("result must be SubmissionResult")
                if (
                    state not in (None, SubmissionState.SUBMITTED)
                    or command_result is not None or error_message is not None
                ):
                    raise ValueError("a submission receipt cannot be combined with failure fields")
                result = _SUBMISSION.validate_json(_json(asdict(result)), strict=True)
                job_id, cluster = _job_id(result.job_id), result.cluster_name
                if result.returncode != 0:
                    raise ValueError("SubmissionResult must have returncode 0")
                if cluster is not None:
                    _text(cluster, "cluster_name")
                state, command_result = SubmissionState.SUBMITTED, result.command
            else:
                if not isinstance(state, SubmissionState) or state not in (
                    SubmissionState.SUBMITTING, SubmissionState.SUBMIT_FAILED,
                    SubmissionState.SUBMISSION_UNKNOWN,
                ):
                    raise ValueError("without a receipt specify SUBMITTING, SUBMIT_FAILED or SUBMISSION_UNKNOWN")
                if state is SubmissionState.SUBMITTING:
                    if command_result is not None or error_message is not None:
                        raise ValueError("SUBMITTING cannot carry a command outcome")
                elif command_result is None and error_message is None:
                    raise ValueError("failed/unknown submission requires command output or an error message")
            if error_message is not None:
                _text(error_message, "error_message")
            command_json = None
            if command_result is not None:
                if not isinstance(command_result, CommandResult):
                    raise ValueError("command_result must be CommandResult")
                command_json = _json(asdict(command_result))
                _COMMAND.validate_json(command_json, strict=True)
                command_json.encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise PersistenceError(f"Invalid submission update: {exc}") from exc
        with self._transaction(write=True) as connection:
            previous = self._get(connection, record_id)
            if (
                state is SubmissionState.SUBMITTING
                and previous.submission_state is not SubmissionState.SCRIPT_RENDERED
            ):
                raise SubmissionConflictError(
                    "SUBMITTING requires a fresh SCRIPT_RENDERED record; "
                    f"current state is {previous.submission_state.value}"
                )
            if previous.submission_state is SubmissionState.SUBMITTED:
                if (job_id, cluster, command_result) == (
                    previous.slurm_job_id, previous.cluster_name, previous.submission_command,
                ) and state is SubmissionState.SUBMITTED:
                    return previous  # Exact repeated receipt is a no-op.
                raise PersistenceError("A bound submission receipt cannot be replaced")
            updated_at = _timestamp(max(_utc_now(), previous.updated_at + timedelta(microseconds=1)))
            connection.execute(
                "UPDATE jobs SET slurm_job_id = ?, cluster_name = ?, submission_state = ?, "
                "submission_command = ?, submission_error = ?, updated_at = ? WHERE id = ?",
                (job_id, cluster, state.value, command_json, error_message, updated_at, record_id),
            )
            return self._get(connection, record_id)

    def update_status(self, record_id: str, job_status: JobStatus) -> JobRecord:
        """Replace the latest observation, including None fields; retain history inputs.

        An UNKNOWN observation never erases the record or its submission. Missing
        fields stay missing rather than borrowing exit codes from an older query.
        The caller is responsible for querying the correct cluster in time order.
        """
        try:
            if not isinstance(job_status, JobStatus):
                raise ValueError("job_status must be JobStatus")
            status_json = _json(asdict(job_status))
            job_status = _STATUS.validate_json(status_json, strict=True)
            status_json.encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise PersistenceError(f"Invalid status update: {exc}") from exc
        with self._transaction(write=True) as connection:
            previous = self._get(connection, record_id)
            try:
                _check_status(job_status, previous.slurm_job_id)
            except ValueError as exc:
                raise PersistenceError(f"Invalid status update: {exc}") from exc
            updated_at = _timestamp(max(_utc_now(), previous.updated_at + timedelta(microseconds=1)))
            connection.execute(
                "UPDATE jobs SET job_status = ?, updated_at = ? WHERE id = ?",
                (status_json, updated_at, record_id),
            )
            return self._get(connection, record_id)
