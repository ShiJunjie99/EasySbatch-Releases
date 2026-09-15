"""Small caller-supplied profile table; no discovery, I/O or user registry."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StringConstraints, model_validator

from .models import (Command, GPUResources, Identifier, PositiveInteger, ProfileReference,
                     ResourceValueEvidence, RunType, SingleLineText, _Model)


class VerifiedResourceRule(_Model):
    """Maintainer-verified resource configuration for one exact invocation/layout.

    A software version check alone is insufficient to register this rule.
    Empty resource_options do not imply a memory/time recommendation.
    """

    run_type: RunType
    command: Command
    nodes: PositiveInteger = 1
    ntasks: PositiveInteger = 1
    cpus_per_task: PositiveInteger
    gpus: GPUResources | None = None
    memory_mib: PositiveInteger | None = None
    time_limit_seconds: PositiveInteger | None = None
    evidence: ResourceValueEvidence
    verification_status: Literal["VERIFIED"]
    verified_at: Annotated[datetime, BeforeValidator(
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")) if isinstance(v, str) else v)]
    verification_scope: SingleLineText

    @model_validator(mode="after")
    def verified_configuration(self) -> Self:
        if self.verified_at.utcoffset() is None:
            raise ValueError("verification timestamp must include timezone")
        if self.memory_mib is None and self.time_limit_seconds is None:
            raise ValueError("a resource rule must provide memory or walltime")
        return self


class ResourceShape(_Model):
    """Explicit registered combination; only the advisor consumes this metadata."""

    nodes: PositiveInteger = 1
    ntasks: PositiveInteger = 1
    cpus_per_task: PositiveInteger
    memory_mib: PositiveInteger
    gpus: GPUResources | None = None


class ResourceOption(_Model):
    partitions: list[Identifier] = Field(min_length=1)
    shape: ResourceShape


class LaunchLayout(_Model):
    """Exact layout declared compatible with the fixed launcher command."""

    nodes: PositiveInteger
    ntasks: PositiveInteger


class EnvironmentCapabilities(_Model):
    """Maintainer-declared facts for analysis matching; never inferred from names.

    No environment is probed. Empty lists assert no known packages/software,
    not their absence. This metadata does not change rendering or ranking.
    """
    python_version: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$")] | None = None
    dependencies: list[Identifier] = Field(default_factory=list, max_length=200)
    software: list[Identifier] = Field(default_factory=list, max_length=100)


class EnvironmentDefinition(ProfileReference):
    """Ordered literal commands, e.g. source, module load and conda activate.

    Commands run in the batch shell so sourced functions and exports persist.
    An empty list explicitly describes an environment requiring no loading.
    """

    load_steps: list[Command]
    # None means unknown applicability; [] explicitly allows no partitions.
    allowed_partitions: list[Identifier] | None = None
    # When supplied, these are the supported combinations, not arbitrary tuning.
    resource_options: list[ResourceOption] = Field(default_factory=list, max_length=8)
    analysis_capabilities: EnvironmentCapabilities | None = None
    resource_rules: list[VerifiedResourceRule] = Field(default_factory=list, max_length=32)


class LaunchDefinition(ProfileReference):
    """A fixed command prefix to prepend to the final program's argv.

    Arguments are literal: there is no parameter substitution or MPI inference.
    The caller supplies an appropriate, previously checked launch configuration.
    """

    command: Command
    supported_layouts: list[LaunchLayout] | None = None


class StaticProfiles(_Model):
    """Exact (id, version) lookup tables, validated again by the renderer."""

    environments: list[EnvironmentDefinition] = Field(default_factory=list)
    launchers: list[LaunchDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicate_versions(self) -> Self:
        for name in ("environments", "launchers"):
            seen = set()
            for profile in getattr(self, name):
                key = (profile.id, profile.version)
                if key in seen:
                    raise ValueError(f"duplicate {name} profile {key!r}")
                seen.add(key)
        return self
