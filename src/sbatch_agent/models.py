"""JobSpec from 方案设计.md §7.1, validated without filesystem or process I/O.

Validation checks structure, values and a few layout constraints. It does not
establish whether a project, environment, launcher or resource request is usable.
"""

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def _without_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    return value


def _non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return _without_nul(value)


def _single_line(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("must not contain control characters")
    return value


def _absolute_posix_path(value: str) -> str:
    if not PurePosixPath(value).is_absolute():
        raise ValueError("must be an absolute POSIX path")
    return value


# Preserve text verbatim: in particular, never strip or split command arguments.
NonBlankText = Annotated[str, AfterValidator(_non_blank)]
SingleLineText = Annotated[NonBlankText, AfterValidator(_single_line)]
AbsolutePath = Annotated[SingleLineText, AfterValidator(_absolute_posix_path)]
Argument = Annotated[str, AfterValidator(_without_nul)]
Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
]
PositiveInteger = Annotated[int, Field(gt=0)]
RunType = Literal["python", "compiled", "installed"]


class _Model(BaseModel):
    # Reject misspelled keys and coercions such as "4" or True into a CPU count.
    model_config = ConfigDict(extra="forbid", strict=True)


class ProfileReference(_Model):
    """Reference to a registered configuration; lookup is outside this module."""

    id: SingleLineText
    version: SingleLineText


class EnvironmentProfile(ProfileReference):
    """Selected environment ID and version, not its activation commands.

    The future registry must resolve this reference in the actual user's scope.
    A reference alone does not verify environment availability.
    """


class Command(_Model):
    """A literal executable and argument vector, with no implicit shell parsing."""

    executable: SingleLineText
    args: list[Argument] = Field(default_factory=list)


class RunStep(Command):
    """Direct launch by default; parallel launch can reference a profile."""

    launcher_profile: ProfileReference | None = None


class CommandStep(Command):
    """A preparation command; work_dir=None means inherit JobSpec.work_dir."""

    kind: Literal["command"] = "command"
    work_dir: AbsolutePath | None = None


class ShellStep(_Model):
    """Explicit shell source for a preparation step; stored but never executed."""

    kind: Literal["shell"]
    script: NonBlankText
    work_dir: AbsolutePath | None = None


PrepareStep = Annotated[CommandStep | ShellStep, Field(discriminator="kind")]


class GPUResources(_Model):
    """GPU count per node and optional cluster-specific GPU type."""

    count: PositiveInteger
    gpu_type: Identifier | None = None


ResourceMode = Literal["cluster_default", "recommended", "explicit"]


class ResourceValueEvidence(_Model):
    """A deterministic, inspectable resource rule; no probability or AI estimate."""

    source: SingleLineText
    reason: NonBlankText
    status: Literal["DIRECT"] = "DIRECT"
    evidence_refs: list[SingleLineText] = Field(default_factory=list)


class ResourceValuePolicy(_Model):
    """Who chooses a resource. The value keeps its original Resources unit."""

    mode: ResourceMode = "explicit"
    evidence: ResourceValueEvidence | None = None

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        if (self.mode == "recommended") != (self.evidence is not None):
            raise ValueError("recommended requires evidence; other modes cannot claim recommendation evidence")
        return self


class ResourceValueRecommendation(_Model):
    """Draft candidate, in the unit of its resource field (MiB or seconds)."""

    value: PositiveInteger
    evidence: ResourceValueEvidence


class Resources(_Model):
    """Requested resources, not cluster capacity or current availability.

    memory_mib is per node. time_limit_seconds is a finite positive duration.
    Numeric legacy documents mean explicit. Only a cluster_default policy permits
    an absent value; no zero/sentinel value represents a scheduler default.
    """

    partition: Identifier
    account: Identifier | None = None
    qos: Identifier | None = None
    nodes: PositiveInteger = 1
    ntasks: PositiveInteger = 1
    cpus_per_task: PositiveInteger = 1
    gpus: GPUResources | None = None
    memory_mib: PositiveInteger | None
    time_limit_seconds: PositiveInteger | None
    memory_policy: ResourceValuePolicy = Field(default_factory=ResourceValuePolicy)
    walltime_policy: ResourceValuePolicy = Field(default_factory=ResourceValuePolicy)

    @model_validator(mode="before")
    @classmethod
    def omitted_cluster_values(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            for key, policy in (("memory_mib", "memory_policy"), ("time_limit_seconds", "walltime_policy")):
                choice = data.get(policy)
                mode = choice.get("mode") if isinstance(choice, dict) else getattr(choice, "mode", None)
                if mode == "cluster_default":
                    data.setdefault(key, None)
        return data

    @model_validator(mode="after")
    def policy_values(self) -> Self:
        for key, policy in (("memory_mib", self.memory_policy), ("time_limit_seconds", self.walltime_policy)):
            if (policy.mode == "cluster_default") != (getattr(self, key) is None):
                raise ValueError(f"{key}: cluster_default must omit value; recommended/explicit require a positive value")
        return self


class Evidence(_Model):
    """File-based support for a field, with an explicit certainty category."""

    field: SingleLineText
    source_file: SingleLineText
    line: PositiveInteger | None = None
    kind: Literal["direct", "inferred", "unconfirmed"]
    detail: NonBlankText


class UnresolvedField(_Model):
    """An outstanding question about a field, without a submission workflow."""

    field: SingleLineText
    reason: NonBlankText


class SourceFingerprint(_Model):
    """An existing SHA-256 digest; no file is opened or hashed here."""

    path: SingleLineText
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{64}$")]


class JobSpec(_Model):
    """One structurally complete run proposal, which may still have questions.

    entrypoint names the script/module/software/binary being run. run_step
    describes its actual invocation. Their semantic agreement is not inferred.
    spec_version is the proposal revision, not a model schema version.
    """

    project_dir: AbsolutePath
    work_dir: AbsolutePath
    run_type: RunType
    entrypoint: SingleLineText
    environment_profile: EnvironmentProfile
    prepare_steps: list[PrepareStep] = Field(default_factory=list)
    run_step: RunStep
    resources: Resources
    evidence: list[Evidence] = Field(default_factory=list)
    unresolved: list[UnresolvedField] = Field(default_factory=list)
    source_fingerprints: list[SourceFingerprint] = Field(default_factory=list)
    spec_version: PositiveInteger

    # Optional script-generation declarations; old JobSpec documents stay valid.
    job_name: SingleLineText | None = None
    stdout: SingleLineText | None = None
    stderr: SingleLineText | None = None
    required_inputs: list[SingleLineText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_run_structure(self) -> Self:
        if self.run_type == "compiled" and not self.prepare_steps:
            raise ValueError("compiled jobs require at least one prepare_step")
        if (
            self.resources.nodes > 1 or self.resources.ntasks > 1
        ) and self.run_step.launcher_profile is None:
            raise ValueError(
                "multiple nodes or tasks require a launcher_profile reference"
            )
        return self
