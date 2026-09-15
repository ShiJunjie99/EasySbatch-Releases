"""Synchronous single-attempt lifecycle using existing renderer, storage and CLI adapter."""

import os
from pathlib import Path
from uuid import uuid4

from .models import JobSpec
from .persistence import (
    JobRecord, JobRepository, PersistenceError, SubmissionConflictError, SubmissionState,
)
from .profiles import StaticProfiles
from .renderer import render_job_script
from .runner import SlurmCommandError
from .slurm import SlurmClient, SlurmParseError, SubmissionResult


class SubmissionServiceError(RuntimeError):
    """Lifecycle failure. Original error is retained as __cause__.

    submission_result retains a returned receipt if saving it failed.
    persistence_error retains a second failure while saving an error outcome.
    Neither is an instruction to retry a submission.
    """

    def __init__(
        self, message: str, *, record_id: str,
        submission_result: SubmissionResult | None = None,
        persistence_error: Exception | None = None,
    ):
        self.record_id = record_id
        self.submission_result = submission_result
        self.persistence_error = persistence_error
        super().__init__(message)


class JobNotSubmittableError(SubmissionServiceError):
    """This record is not a fresh attempt or has an incompatible script path."""


class JobNotSubmittedError(SubmissionServiceError):
    """No bound Slurm job exists to refresh."""


def _definite_failure(exc: BaseException) -> bool:
    # Only established execution outcomes from the existing adapter contract.
    # Negative codes indicate termination by signal, so acceptance is uncertain.
    if not isinstance(exc, SlurmCommandError):
        return False
    result = exc.result
    if result.returncode is not None:
        return result.returncode > 0
    return (
        isinstance(exc.__cause__, (FileNotFoundError, PermissionError))
        and not result.stdout and not result.stderr
    )


class SubmissionService:
    """Compose existing capabilities; constructor itself never submits or queries.

    submission_root must be an explicit absolute path on trusted, durable local
    storage visible to the injected client. Each thread/process owns its own
    repository connection. The caller owns and closes that repository.
    """

    def __init__(
        self, *, repository: JobRepository, slurm_client: SlurmClient,
        profiles: StaticProfiles, submission_root: str | os.PathLike[str],
    ):
        root = os.fspath(submission_root)
        if (
            not isinstance(root, str) or not root.strip() or not root.isprintable()
            or not Path(root).is_absolute() or ".." in Path(root).parts
        ):
            raise ValueError("submission_root must be an absolute path without controls or '..'")
        # Freeze the configured root spelling, including existing parent links.
        # No directories are created until a record wins its submission claim.
        self.submission_root = Path(root).resolve()
        self.repository = repository
        self.slurm_client = slurm_client
        self.profiles = profiles

    def create_job(self, *, spec: JobSpec, name: str | None = None, record_id: str | None = None) -> JobRecord:
        """Render and persist together, with no files or Slurm calls on this path."""
        # Work on one detached proposal for both rendering and persistence.
        # Existing renderer handles invalid types, mutations and unresolved data.
        proposal = spec.model_copy(deep=True) if isinstance(spec, JobSpec) else spec
        script = render_job_script(proposal, profiles=self.profiles)
        # An application may reserve one UUID for a reviewed submission. The
        # repository still validates it and rejects duplicates atomically.
        record_id = str(uuid4()) if record_id is None else record_id
        return self.repository.create(
            proposal, script, name=name, record_id=record_id,
            script_path=self.submission_root / record_id / "submit.sh",
        )

    def _script_path(self, record: JobRecord) -> Path:
        # Repository validates the internal UUID. Job names and project/work
        # paths never participate in submission artifact placement.
        expected = self.submission_root / record.id / "submit.sh"
        if record.script_path != str(expected):
            raise JobNotSubmittableError(
                "Stored script_path does not match this service's submission_root and record ID",
                record_id=record.id,
            )
        return expected

    def _write_script(self, record: JobRecord) -> Path:
        path = self._script_path(record)
        payload = record.rendered_script.encode("utf-8")
        self.submission_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.submission_root.resolve() != self.submission_root:
            raise OSError("submission_root changed to a symbolic link")
        # Refuse existing directories/files/links, even when their contents look
        # identical. No adoption, replacement, or cleanup of an older attempt.
        path.parent.mkdir(mode=0o700, exist_ok=False)
        pending = path.parent / ".submit.sh.pending"
        with pending.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-filesystem hard-link publication is atomic and never overwrites
        # an existing destination (unlike replace). Partial writes stay pending.
        os.link(pending, path)
        pending.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if path.read_bytes() != payload:
            raise OSError("Written script differs from the stored snapshot")
        return path

    def _save_error(
        self, record_id: str, exc: BaseException, state: SubmissionState,
    ) -> SubmissionServiceError:
        command = exc.result if isinstance(exc, (SlurmCommandError, SlurmParseError)) else None
        detail = f"{type(exc).__name__}: {exc}"
        storage_error = None
        try:
            self.repository.update_submission(
                record_id, state=state, command_result=command, error_message=detail,
            )
            message = f"{record_id}: {state.value}; {detail}; no automatic retry"
        except Exception as failure:
            storage_error = failure
            message = (
                f"{record_id}: could not persist {state.value}; record may remain SUBMITTING. "
                f"Reconcile before any new attempt. Original error: {detail}"
            )
        return SubmissionServiceError(
            message, record_id=record_id, persistence_error=storage_error,
        )

    def submit_job(self, record_id: str) -> JobRecord:
        """Claim once, publish the stored script, submit once and save the result.

        All states except SCRIPT_RENDERED are refused, including failed/unknown
        attempts. A crashed SUBMITTING record is never reset automatically.
        """
        record = self.repository.get(record_id)
        if record.submission_state is not SubmissionState.SCRIPT_RENDERED:
            raise JobNotSubmittableError(
                f"{record_id}: cannot submit from {record.submission_state.value}",
                record_id=record_id,
            )
        self._script_path(record)
        try:
            record = self.repository.update_submission(record_id, state=SubmissionState.SUBMITTING)
        except SubmissionConflictError as exc:
            raise JobNotSubmittableError(str(exc), record_id=record_id) from exc
        # Claim committed before touching artifacts or invoking the adapter.
        # A persistence failure above propagates without invoking the client.
        try:
            path = self._write_script(record)
        except Exception as exc:
            raise self._save_error(record_id, exc, SubmissionState.SUBMIT_FAILED) from exc

        try:
            result = self.slurm_client.submit(path)
        except BaseException as exc:
            state = (
                SubmissionState.SUBMIT_FAILED if _definite_failure(exc)
                else SubmissionState.SUBMISSION_UNKNOWN
            )
            failure = self._save_error(record_id, exc, state)
            if not isinstance(exc, Exception):
                # Do not swallow KeyboardInterrupt/SystemExit. Best-effort
                # checkpoint above; SIGKILL/power loss can leave SUBMITTING.
                if failure.persistence_error is not None:
                    exc.add_note(str(failure))
                raise
            raise failure from exc
        try:
            return self.repository.update_submission(record_id, result)
        except PersistenceError as exc:
            # The adapter may already have accepted a real job. Retain its
            # receipt on the exception; never submit again to repair storage.
            raise SubmissionServiceError(
                f"{record_id}: submission returned but saving its receipt failed; "
                "record may remain SUBMITTING. Reconcile; do not resubmit.",
                record_id=record_id, submission_result=result, persistence_error=exc,
            ) from exc

    def refresh_status(self, record_id: str) -> JobRecord:
        """Exactly one adapter call; persist even UNKNOWN, with no polling loop."""
        record = self.repository.get(record_id)
        if record.submission_state is not SubmissionState.SUBMITTED or record.slurm_job_id is None:
            raise JobNotSubmittedError(
                f"{record_id}: no submitted Slurm job is bound", record_id=record_id,
            )
        try:
            status = self.slurm_client.get_status(record.slurm_job_id)
            return self.repository.update_status(record_id, status)
        except Exception as exc:
            raise SubmissionServiceError(
                f"{record_id}: status refresh failed; stored observation is unchanged",
                record_id=record_id,
            ) from exc
