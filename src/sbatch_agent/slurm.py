"""Small local Slurm CLI adapter. Importing this module executes nothing."""

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path, PurePosixPath
import re
from typing import Literal

from .runner import (
    CommandResult, CommandRunner, SlurmCommandError, SubprocessRunner, _validate_timeout,
)


class SlurmParseError(ValueError):
    """Unexpected CLI output; result retains the entire response for diagnosis.

    For submit(), a malformed acknowledgement may still follow an accepted job.
    The caller must reconcile that outcome instead of blindly resubmitting.
    """

    def __init__(self, message: str, result: CommandResult):
        self.result = result
        super().__init__(message)


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETING = "COMPLETING"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SubmissionResult:
    job_id: str
    cluster_name: str | None
    command: CommandResult

    @property
    def returncode(self) -> int | None:
        return self.command.returncode

    @property
    def stdout(self) -> str:
        return self.command.stdout

    @property
    def stderr(self) -> str:
        return self.command.stderr


@dataclass(frozen=True)
class JobStatus:
    """One main job record, never a step. Timestamps retain Slurm's text.

    source='none' means neither query supplied a main record; it does not prove
    accounting delay, completion, job existence, or visibility to this user.
    query_results retains every command in this get_status() call.
    """

    job_id: str
    normalized_state: JobState
    raw_state: str | None
    source: Literal["squeue", "sacct", "none"]
    query_results: tuple[CommandResult, ...]
    reason: str | None = None
    partition: str | None = None
    user: str | None = None
    exit_code: int | None = None
    signal: int | None = None
    raw_exit_code: str | None = None
    start: str | None = None
    end: str | None = None


def _job_id(value: str) -> str:
    # Ordinary single jobs only: no options, lists, arrays, steps or federation.
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("job_id must be a positive decimal string for one ordinary job")
    return value


def _state(raw: str) -> JobState:
    if re.fullmatch(r"CANCELLED by [0-9]+", raw):
        return JobState.CANCELLED
    aliases = {
        "CONFIGURING": JobState.PENDING,
        "BOOT_FAIL": JobState.FAILED,
        "NODE_FAIL": JobState.FAILED,
        "OUT_OF_MEMORY": JobState.FAILED,
        "DEADLINE": JobState.FAILED,
    }
    if raw in aliases:
        return aliases[raw]
    try:
        return JobState(raw)
    except ValueError:
        return JobState.UNKNOWN


def _main_record(result: CommandResult, job_id: str) -> list[str] | None:
    matches = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = [value.strip() for value in line.split("|")]
        if len(fields) != 5 or not fields[0] or not fields[1]:
            raise SlurmParseError("Expected five delimited fields with JobID and State", result)
        if fields[0] == job_id:
            matches.append(fields)
    if len(matches) > 1:
        raise SlurmParseError(f"Ambiguous duplicate main records for job {job_id}", result)
    return matches[0] if matches else None


def _check_exit(result: CommandResult) -> None:
    if result.returncode != 0:
        raise SlurmCommandError(
            f"{result.argv[0]} exited with {result.returncode}: {result.stderr.strip()}", result
        )


class SlurmClient:
    """Synchronous current-cluster client; no background polling or retries.

    Defaults to real CLI execution when methods are explicitly called. Tests
    must provide a fake runner or patch the subprocess execution boundary.
    Does not read/rewrite scripts, choose resources, or switch Linux identities.
    """

    def __init__(self, runner: CommandRunner | None = None, *, timeout: float = 30):
        _validate_timeout(timeout)
        self.runner = runner if runner is not None else SubprocessRunner()
        self.timeout = timeout

    def submit(self, script_path: str | os.PathLike[str]) -> SubmissionResult:
        """Submit an existing caller-prepared script; acceptance is not completion."""
        path = os.fspath(script_path)
        if not isinstance(path, str) or not path.strip() or not path.isprintable():
            raise ValueError("script_path must be nonblank text without control characters")
        # Absolute spelling prevents leading '-' filenames from becoming CLI
        # options. No symlink resolution, file creation or script inspection.
        result = self.runner.run(
            ["sbatch", "--parsable", str(Path(path).absolute())], timeout=self.timeout
        )
        _check_exit(result)
        output = result.stdout.removesuffix("\n").removesuffix("\r")
        match = re.fullmatch(r"([1-9][0-9]*)(?:;([A-Za-z0-9_][A-Za-z0-9_.-]*))?", output)
        if match is None:
            raise SlurmParseError("Invalid sbatch --parsable acknowledgement", result)
        # A successful CLI may report warnings on stderr. Do not invent an
        # error-keyword classifier or throw away a valid acknowledgement.
        return SubmissionResult(match[1], match[2], result)

    def get_status(self, job_id: str) -> JobStatus:
        """Query queue first, then accounting, or return UNKNOWN without a record."""
        job_id = _job_id(job_id)
        queue = self.runner.run([
            "squeue", "--local", "--noheader", "--states=all",
            f"--jobs={job_id}", "--format=%i|%T|%r|%P|%u",
        ], timeout=self.timeout)
        # A purged job may produce this specific diagnostic instead of an
        # empty success response. Do not mask controller/authentication errors.
        absent = (
            queue.returncode == 1 and not queue.stdout.strip()
            and queue.stderr.strip() == "slurm_load_jobs error: Invalid job id specified"
        )
        if not absent:
            _check_exit(queue)
        row = _main_record(queue, job_id)
        if row:
            return JobStatus(
                job_id, _state(row[1]), row[1], "squeue", (queue,),
                reason=row[2] or None, partition=row[3] or None, user=row[4] or None,
            )

        accounting = self.runner.run([
            "sacct", "--local", "--noheader", "--parsable2", "--allocations",
            f"--jobs={job_id}", "--format=JobID,State%80,ExitCode,Start,End",
        ], timeout=self.timeout)
        _check_exit(accounting)
        row = _main_record(accounting, job_id)
        results = (queue, accounting)
        if row is None:
            return JobStatus(job_id, JobState.UNKNOWN, None, "none", results)
        exit_code = signal = None
        if row[2]:
            match = re.fullmatch(r"([0-9]+):([0-9]+)", row[2])
            if match is None:
                raise SlurmParseError("Invalid sacct ExitCode; expected exit_code:signal", accounting)
            exit_code, signal = int(match[1]), int(match[2])
        return JobStatus(
            job_id, _state(row[1]), row[1], "sacct", results,
            exit_code=exit_code, signal=signal, raw_exit_code=row[2],
            start=row[3] or None, end=row[4] or None,
        )


def resolve_log_path(pattern: str | None, job_id: str, *, work_dir: str) -> str | None:
    """Resolve ONLY %j against an explicit POSIX work directory, without I/O.

    None stays None (no guessed default). Other percent tokens, padding, and
    backslash escape syntax are unsupported and rejected, not partly expanded.
    This is an expected path, not evidence that Slurm has created a log file.
    """
    job_id = _job_id(job_id)
    if (
        not isinstance(work_dir, str) or not work_dir.isprintable()
        or not PurePosixPath(work_dir).is_absolute()
    ):
        raise ValueError("work_dir must be an absolute POSIX path without controls")
    if pattern is None:
        return None
    if not isinstance(pattern, str) or not pattern.strip() or not pattern.isprintable():
        raise ValueError("log pattern must be nonblank text without controls")
    if "\\" in pattern or "%" in pattern.replace("%j", ""):
        raise ValueError("only %j is supported; other tokens and backslash escapes are unsupported")
    return str(PurePosixPath(work_dir) / pattern.replace("%j", job_id))
