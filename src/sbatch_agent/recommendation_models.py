"""Small advisory input/output types; existing JobSpec and Resources stay intact."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from pydantic import Field

from .models import EnvironmentProfile, Identifier, ProfileReference, Resources, RunType, _Model


class UserPreference(StrEnum):
    FASTEST_AVAILABLE = "FASTEST_AVAILABLE"
    BALANCED = "BALANCED"
    RESOURCE_EFFICIENT = "RESOURCE_EFFICIENT"


class RequestedResources(Resources):
    """Preview only: blank partition means consider visible partitions.

    All numeric requirements are still required/validated exactly as Resources.
    This object cannot be rendered or submitted as a JobSpec.
    """

    partition: Identifier | None = None


class CatalogCompatibility(_Model):
    """Known compatibility, never performance estimates or resource requirements."""
    software_id: Identifier | None = None
    allowed_partitions: list[Identifier] | None = None
    capabilities: list[Literal["serial", "threads", "mpi", "gpu"]] = Field(default_factory=list)


class RecommendationRequest(_Model):
    run_type: RunType
    environment_profile: EnvironmentProfile
    launcher_profile: ProfileReference | None = None
    resources: RequestedResources
    catalog_compatibility: CatalogCompatibility | None = None


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    ELIGIBLE_WITH_WARNING = "eligible_with_warning"
    INELIGIBLE = "ineligible"


@dataclass(frozen=True)
class RecommendationEvidence:
    source: str
    field: str
    value: str | int | float | None


@dataclass(frozen=True)
class ScoreComponents:
    availability: float | None
    queue: float | None
    efficiency: float


@dataclass(frozen=True)
class Recommendation:
    id: str
    partition: str
    proposed_resources: Resources
    score: float
    rank: int
    eligibility: Eligibility
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence: tuple[RecommendationEvidence, ...]
    components: ScoreComponents
    snapshot_captured_at: datetime

    @property
    def eligible(self) -> bool:
        return self.eligibility is not Eligibility.INELIGIBLE


@dataclass(frozen=True)
class CandidateRejection:
    partition: str
    reasons: tuple[str, ...]
    proposed_resources: Resources | None = None
    eligibility: Eligibility = Eligibility.INELIGIBLE


@dataclass(frozen=True)
class RecommendationReport:
    recommendations: tuple[Recommendation, ...]
    rejections: tuple[CandidateRejection, ...]
    warnings: tuple[str, ...]
    evidence: tuple[RecommendationEvidence, ...]
    snapshot_captured_at: datetime
    preference: UserPreference
