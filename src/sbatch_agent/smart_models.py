"""Temporary preparation state, separate from the strict execution contract."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .analysis_models import AIAnalysisResult, EnvironmentResolution
from .cluster_models import ClusterSnapshot
from .models import (AbsolutePath, Argument, EnvironmentProfile, Identifier, JobSpec,
                     PositiveInteger, PrepareStep, ProfileReference, RunType, SingleLineText,
                     ResourceMode, ResourceValueRecommendation)
from .recommendation_models import Recommendation, RecommendationReport
from .scanner_models import ProjectEvidence
from .software_resolver import SoftwareResolution
from .server_catalog import SoftwareCatalogEntry


class FieldSource(StrEnum):
    AI_DIRECT = "AI_DIRECT"
    AI_INFERRED = "AI_INFERRED"
    ENVIRONMENT_RESOLVER = "ENVIRONMENT_RESOLVER"
    RESOURCE_RECOMMENDER = "RESOURCE_RECOMMENDER"
    PROFILE_DEFAULT = "PROFILE_DEFAULT"
    SYSTEM_DEFAULT = "SYSTEM_DEFAULT"
    SERVER_CATALOG = "SERVER_CATALOG"
    USER = "USER"
    CLUSTER_DEFAULT = "CLUSTER_DEFAULT"
    RESOURCE_POLICY_RECOMMENDATION = "RESOURCE_POLICY_RECOMMENDATION"


class PreparationValues(BaseModel):
    """Editable partial values ONLY; never accepted by renderer or submission."""
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: SingleLineText | None = None
    work_dir: AbsolutePath | None = None
    run_type: RunType | None = None
    entrypoint: SingleLineText | None = None
    executable: SingleLineText | None = None
    software_id: Identifier | None = None
    args: list[Argument] | None = None
    required_inputs: list[SingleLineText] | None = None
    environment_profile: EnvironmentProfile | None = None
    prepare_steps: list[PrepareStep] | None = None
    launcher_profile: ProfileReference | None = None
    partition: Identifier | None = None
    account: Identifier | None = None
    qos: Identifier | None = None
    nodes: PositiveInteger | None = None
    ntasks: PositiveInteger | None = None
    cpus_per_task: PositiveInteger | None = None
    gpu_count: Annotated[int, Field(ge=0)] | None = None
    gpu_type: Identifier | None = None
    memory_mib: PositiveInteger | None = None
    time_limit_seconds: PositiveInteger | None = None
    memory_mode: ResourceMode | None = None
    walltime_mode: ResourceMode | None = None
    stdout: SingleLineText | None = None
    stderr: SingleLineText | None = None


@dataclass(frozen=True)
class FieldOrigin:
    source: FieldSource
    reason: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnresolvedQuestion:
    field: str
    reason: str
    choices: tuple[ProfileReference | SoftwareCatalogEntry, ...] = ()


@dataclass
class PreparedJob:
    """Server-held, bounded-lifetime state. Browser receives only ID/revision."""
    id: str
    created_at: datetime
    project_evidence: ProjectEvidence
    ai_analysis: AIAnalysisResult
    environment_resolution: EnvironmentResolution
    revision: int = 1
    values: PreparationValues = field(default_factory=PreparationValues)
    user_values: PreparationValues = field(default_factory=PreparationValues)
    resolved_fields: dict[str, FieldOrigin] = field(default_factory=dict)
    unresolved_fields: list[UnresolvedQuestion] = field(default_factory=list)
    snapshot: ClusterSnapshot | None = None
    resource_recommendations: RecommendationReport | None = None
    selected_recommendation: Recommendation | None = None
    warnings: list[str] = field(default_factory=list)
    job_spec: JobSpec | None = None
    rendered_script: str | None = None
    software_resolution: SoftwareResolution | None = None
    prepare_request_id: str | None = None  # Diagnostics only; never part of JobSpec/SQLite.
    resource_value_recommendations: dict[str, ResourceValueRecommendation] = field(default_factory=dict)

    @property
    def conflicts(self):
        return self.ai_analysis.conflicts

    @property
    def state(self):
        return "READY_TO_SUBMIT" if self.job_spec is not None and self.rendered_script is not None else "NEEDS_INPUT"
