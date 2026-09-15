"""Replaceable, synchronous Slurm command execution under the current identity."""

from collections.abc import Sequence
from dataclasses import dataclass
import math
import os
import subprocess
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    """Captured command output; returncode=None means no exit code was obtained."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str


class SlurmCommandError(RuntimeError):
    """Execution failed. result preserves output; __cause__ holds OS failures.

    A submission timeout does NOT prove the job was rejected. Never retry a
    submission automatically after this exception.
    """

    def __init__(self, message: str, result: CommandResult):
        self.result = result
        super().__init__(message)


class CommandRunner(Protocol):
    """Return completed commands (including nonzero exits); raise on OS failure."""

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult: ...


def _validate_timeout(timeout: float) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number of seconds")


def _text(value: str | bytes | None) -> str:
    # TimeoutExpired can contain bytes even with subprocess text mode enabled.
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


class SubprocessRunner:
    """No shell, impersonation, retries, or process-wide environment changes."""

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        _validate_timeout(timeout)
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("argv must be a nonempty sequence of strings")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in argv) or not argv[0]:
            raise ValueError("argv must contain strings without NUL and an executable")
        command = tuple(argv)
        # sbatch defaults to exporting its caller's environment to the job.
        # Backend configuration and the configured AI credential are not part
        # of the computation environment. Filter a COPY for every Slurm CLI;
        # never unset the Web process's credential or alter ordinary PATH, etc.
        credential_name = os.environ.get("SBATCH_AGENT_AI_API_KEY_ENV")
        environment = {key: value for key, value in os.environ.items()
                       if key != credential_name and not key.startswith("SBATCH_AGENT_")}
        environment.update(LC_ALL="C", SLURM_TIME_FORMAT="standard")
        try:
            completed = subprocess.run(
                list(command), shell=False, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(command, None, _text(exc.stdout), _text(exc.stderr))
            raise SlurmCommandError(
                f"{command[0]} timed out after {timeout}s; submission outcome may be unknown",
                result,
            ) from exc
        except OSError as exc:
            result = CommandResult(command, None, "", "")
            raise SlurmCommandError(f"Cannot execute {command[0]}: {exc}", result) from exc
        return CommandResult(command, completed.returncode, completed.stdout, completed.stderr)
