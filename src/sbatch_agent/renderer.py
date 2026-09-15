"""Deterministic JobSpec -> Bash/sbatch text, without filesystem/process I/O."""

import re
import shlex

from pydantic import ValidationError

from .models import Command, CommandStep, JobSpec, ProfileReference
from .profiles import StaticProfiles


class JobSpecValidationError(ValueError):
    """Rendering was refused; issues contains field-oriented explanations."""

    def __init__(self, issues: list[str]):
        self.issues = tuple(issues)
        super().__init__("Cannot render job script:\n" + "\n".join(issues))


def _validated_copy(value, model, name):
    if not isinstance(value, model):
        raise JobSpecValidationError([f"{name}: expected {model.__name__}"])
    try:
        # Pydantic models/lists are mutable. Revalidate their data rather than
        # trusting a previously validated instance or model_copy(update=...).
        return model.model_validate(value.model_dump(mode="python", warnings=False))
    except ValidationError as exc:
        raise JobSpecValidationError([
            f"{name}.{'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]) from exc


def _resolve(profiles, reference: ProfileReference, field: str):
    for profile in profiles:
        if (profile.id, profile.version) == (reference.id, reference.version):
            return profile
    raise JobSpecValidationError([
        f"{field}: no static profile for id={reference.id!r}, "
        f"version={reference.version!r}"
    ])


def _directive_value(value: str, field: str) -> str:
    """Encode ONE sbatch option value, separately from Bash quoting.

    Slurm's get_argument() treats backslash as an escape even inside quotes.
    Use a restricted unquoted alphabet, otherwise double-quote and escape both
    backslashes and double quotes. Reject controls before encoding anything.
    Reference: SchedMD/slurm, slurm-25-05-5-1, src/sbatch/opt.c:get_argument.
    """
    if not value.strip() or any(not char.isprintable() for char in value):
        raise JobSpecValidationError([f"{field}: invalid or multiline directive value"])
    if re.fullmatch(r"[A-Za-z0-9_./:%+-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _walltime(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    result = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if days:
        result = f"{days}-{result}"
    # Input is a validated positive integer, not a user-supplied Slurm string.
    if not re.fullmatch(r"(?:[1-9][0-9]*-)?[0-2][0-9]:[0-5][0-9]:[0-5][0-9]", result):
        raise JobSpecValidationError(["resources.time_limit_seconds: invalid walltime"])
    return result


def _command(command: Command, extra_args: list[str] | None = None) -> str:
    executable = shlex.quote(command.executable)
    if executable == command.executable:
        # quote() leaves shell keywords and assignment-like safe words bare.
        # Explicit quoting keeps even a command called 'if' or 'A=B' a word.
        # An unchanged quote() result contains no apostrophe, so this is safe.
        executable = f"'{executable}'"
    args = [*command.args, *(extra_args or [])]
    return " ".join([executable, *(shlex.quote(arg) for arg in args)])


def render_job_script(spec: JobSpec, *, profiles: StaticProfiles) -> str:
    """Return a complete script; never write files, execute or submit it.

    Identical spec data and static profile contents yield identical bytes.
    Any unresolved item blocks formal rendering. Profile references are exact;
    their registration/cluster suitability is the caller's responsibility.
    """
    spec = _validated_copy(spec, JobSpec, "spec")
    profiles = _validated_copy(profiles, StaticProfiles, "profiles")
    if spec.unresolved:
        raise JobSpecValidationError([
            f"unresolved.{item.field}: {item.reason}" for item in spec.unresolved
        ])

    environment = _resolve(
        profiles.environments, spec.environment_profile, "environment_profile"
    )
    launcher = None
    if spec.run_step.launcher_profile is not None:
        launcher = _resolve(
            profiles.launchers, spec.run_step.launcher_profile,
            "run_step.launcher_profile",
        )
    if spec.run_type == "compiled" and "/" not in spec.run_step.executable:
        raise JobSpecValidationError([
            "run_step.executable: compiled targets need an explicit path, "
            "for example ./solver, to avoid running a different binary from PATH"
        ])

    resources = spec.resources
    directives = [
        ("job-name", spec.job_name),
        ("partition", resources.partition),
        ("account", resources.account),
        ("qos", resources.qos),
        ("nodes", str(resources.nodes)),
        ("ntasks", str(resources.ntasks)),
        ("cpus-per-task", str(resources.cpus_per_task)),
    ]
    if resources.gpus is not None:
        gpu = resources.gpus
        gres = f"gpu:{gpu.gpu_type}:{gpu.count}" if gpu.gpu_type else f"gpu:{gpu.count}"
        directives.append(("gres", gres))
    directives.extend([
        ("mem", f"{resources.memory_mib}M" if resources.memory_policy.mode != "cluster_default" else None),
        ("time", _walltime(resources.time_limit_seconds) if resources.walltime_policy.mode != "cluster_default" else None),
        ("chdir", spec.work_dir),
        ("output", spec.stdout),
        ("error", spec.stderr),
    ])
    lines = ["#!/usr/bin/env bash", ""]
    lines.extend(
        f"#SBATCH --{name}={_directive_value(value, name)}"
        for name, value in directives if value is not None
    )
    lines.extend(["", "set -e", "", "# Environment"])
    lines.extend(_command(step) for step in environment.load_steps)
    lines.extend([
        # Activation scripts can change shell flags; restore our execution
        # contract explicitly after they return, before checks/build/run.
        "", "set -e", "set -u", "set -o pipefail", "", "# Working directory",
        f"cd -- {shlex.quote(spec.work_dir)}",
    ])
    if spec.required_inputs:
        lines.extend(["", "# Required inputs (relative to work_dir)"])
        lines.extend(f"test -r {shlex.quote(path)}" for path in spec.required_inputs)

    for index, step in enumerate(spec.prepare_steps, 1):
        lines.extend(["", f"# Prepare step {index}", "("])
        if step.work_dir is not None:
            lines.append(f"cd -- {shlex.quote(step.work_dir)}")
        if isinstance(step, CommandStep):
            lines.append(_command(step))
        else:
            # Raw shell is explicitly trusted code, isolated in a child Bash.
            # This prevents exit/cd/set or unmatched grouping from altering the
            # surrounding script. No condition wraps it: errexit stays active.
            lines.append("bash -euo pipefail -c " + shlex.quote(step.script))
        lines.append(")")

    if spec.run_type == "compiled":
        target = shlex.quote(spec.run_step.executable)
        lines.extend(["", "# Compiled target", f"test -f {target}", f"test -x {target}"])
    lines.extend(["", "# Run"])
    if launcher is None:
        lines.append(_command(spec.run_step))
    else:
        lines.append(_command(
            launcher.command, [spec.run_step.executable, *spec.run_step.args]
        ))
    return "\n".join(lines) + "\n"
