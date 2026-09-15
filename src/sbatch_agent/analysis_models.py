"""Unconfirmed analysis proposals, deliberately separate from executable JobSpec."""

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .models import EnvironmentProfile, RunType, SingleLineText, UnresolvedField

T = TypeVar("T")
Status = Literal["DIRECT", "INFERRED", "UNRESOLVED"]
Text = Annotated[str, StringConstraints(max_length=2000)]
Name = Annotated[SingleLineText, Field(max_length=500)]
Count = Annotated[int, Field(gt=0, le=2**31 - 1)]
Version = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$")]
PackageName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")]


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class FieldProposal(AnalysisModel, Generic[T]):
    """DIRECT/INFERRED require a non-null value AND nonempty evidence_refs.

    This includes boolean false: false is a claim, not an unknown value.
    Missing evidence requires value=null and status=UNRESOLVED.
    """
    value: T | None = Field(default=None, description=
        "DIRECT/INFERRED require a non-null value AND nonempty evidence_refs. "
        "Boolean false is a claim, not unknown. Missing evidence requires null/UNRESOLVED.")
    status: Status = "UNRESOLVED"
    evidence_refs: list[Name] = Field(default_factory=list, max_length=16)
    reason: Annotated[SingleLineText, Field(max_length=1000)] = "证据不足，需用户确认。"

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "UNRESOLVED":
            if self.value is not None:
                raise ValueError("UNRESOLVED must have a null value")
        elif self.value is None or not self.evidence_refs:
            raise ValueError("a proposed value requires scanner evidence references")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("duplicate evidence references")
        return self


class Entrypoint(AnalysisModel):
    kind: Literal["file", "module", "command"]
    value: Name


class EnvironmentRequirements(AnalysisModel):
    """Minimum numeric Python version and bare package/software names only.

    Version constraints, extras, platform markers and optional dependency groups
    are not silently simplified into a match; leave those requirements unresolved.
    """
    python_min_version: Version | None = None
    dependencies: list[PackageName] = Field(default_factory=list, max_length=40)
    software: list[PackageName] = Field(default_factory=list, max_length=20)


class BuildInformation(AnalysisModel):
    system: Literal["cmake", "make", "other"]
    target: Name | None = None
    # No executable commands or raw Shell field in the model output schema.


class DraftRunStep(AnalysisModel):
    executable: FieldProposal[Name] = Field(default_factory=FieldProposal[Name])
    args: FieldProposal[list[Text]] = Field(default_factory=FieldProposal[list[Text]])


PARALLELISM_RULES = """Parallelism is evidence about possible execution modes, NOT counts.
parallelism.threads means possible use of threading (boolean true/false or null),
NEVER a thread count. serial, mpi and gpu also use boolean-or-null proposals.
DIRECT requires an explicit cited declaration; INFERRED requires a cited positive
hint and a reason. OpenMP or OMP_NUM_THREADS is an INFERRED threading hint, not a
verified requirement. GPU/MPI hints follow the same rule. No hints, negative
inferences from absence, or conflicting evidence require null / UNRESOLVED.
In particular, hello-world simplicity does not prove serial execution or false
threads/mpi/gpu. Never use false / INFERRED with empty evidence_refs. Boolean false
is a claim and requires evidence just like true; it is NOT an unknown placeholder.
With no threading evidence emit exactly the unknown proposal from the template:
value=null, status=UNRESOLVED, evidence_refs=[], reason explaining missing evidence.
OpenMP alone supplies no thread count. Even OMP_NUM_THREADS=8 is a mode/configuration
hint, not proof of a required cpus_per_task allocation. There is no thread-count
field in this draft. Never place 1 or 8 in parallelism.threads or infer CPU counts
from it; resource_requirements.cpus_per_task needs its own explicit resource evidence.
Post-validation conservatively retains only positive Scanner mode hints as INFERRED;
serial/negative declarations remain UNRESOLVED for human review in this version.
"""


class Parallelism(AnalysisModel):
    """Boolean execution-mode hints, not thread/task/GPU resource quantities."""
    model_config = ConfigDict(json_schema_extra={"description": PARALLELISM_RULES})
    serial: FieldProposal[bool] = Field(default_factory=FieldProposal[bool])
    threads: FieldProposal[bool] = Field(default_factory=FieldProposal[bool],
        description="Possible threading use: boolean or null, never a number. "
                    "No threading evidence: value=null, status=UNRESOLVED, evidence_refs=[]. "
                    "Cited OpenMP/OMP_NUM_THREADS hint: true/INFERRED. See Parallelism's shared mode rules.")
    mpi: FieldProposal[bool] = Field(default_factory=FieldProposal[bool])
    gpu: FieldProposal[bool] = Field(default_factory=FieldProposal[bool])


class ResourceRequirements(AnalysisModel):
    """Evidence-backed quantities, NOT final allocation or performance estimates."""
    nodes: FieldProposal[Count] = Field(default_factory=FieldProposal[Count])
    ntasks: FieldProposal[Count] = Field(default_factory=FieldProposal[Count])
    cpus_per_task: FieldProposal[Count] = Field(default_factory=FieldProposal[Count])
    gpu_count: FieldProposal[Count] = Field(default_factory=FieldProposal[Count])
    memory_mib: FieldProposal[Count] = Field(default_factory=FieldProposal[Count])
    time_limit_seconds: FieldProposal[Count] = Field(default_factory=FieldProposal[Count])


class DraftFields(AnalysisModel):
    """Provider output contains only proposed fields, never root or profile IDs."""
    run_type: FieldProposal[RunType] = Field(default_factory=FieldProposal[RunType])
    work_dir: FieldProposal[Name] = Field(default_factory=FieldProposal[Name])
    entrypoint: FieldProposal[Entrypoint] = Field(default_factory=FieldProposal[Entrypoint])
    run_step: DraftRunStep = Field(default_factory=DraftRunStep)
    required_inputs: FieldProposal[list[Name]] = Field(default_factory=FieldProposal[list[Name]])
    environment_requirements: FieldProposal[EnvironmentRequirements] = Field(default_factory=FieldProposal[EnvironmentRequirements])
    parallelism: Parallelism = Field(default_factory=Parallelism)
    build: FieldProposal[BuildInformation] = Field(default_factory=FieldProposal[BuildInformation])
    resource_requirements: ResourceRequirements = Field(default_factory=ResourceRequirements)


class AnalysisConflict(AnalysisModel):
    field: Name
    reason: Name
    evidence_refs: list[Name] = Field(min_length=2, max_length=16)


class StructuredAnalysis(AnalysisModel):
    """The ONLY model response schema. Unknown keys, including Shell, fail."""
    draft: DraftFields
    conflicts: list[AnalysisConflict] = Field(default_factory=list, max_length=30)
    notes: list[Text] = Field(default_factory=list, max_length=20)


class EnvironmentResolution(AnalysisModel):
    status: Literal["MATCHED", "MULTIPLE", "NO_MATCH", "UNRESOLVED"]
    choices: list[EnvironmentProfile] = Field(default_factory=list)
    reason: str


class JobSpecDraft(DraftFields):
    # Supplied by the application, never by the model; not sent to the provider.
    project_dir: str
    environment_resolution: EnvironmentResolution
    unresolved: list[UnresolvedField]


class ModelMetadata(AnalysisModel):
    provider: Name
    model: Name
    analyzed_at: datetime
    request_id: Name | None = None

    @model_validator(mode="after")
    def aware_time(self):
        if self.analyzed_at.utcoffset() is None:
            raise ValueError("analysis timestamp must have a timezone")
        return self


class AIAnalysisResult(AnalysisModel):
    draft: JobSpecDraft
    conflicts: list[AnalysisConflict]
    warnings: list[str]
    notes: list[Text]
    model_metadata: ModelMetadata
    context_evidence_refs: list[str]


def field_proposals(fields: DraftFields):
    """Stable field paths for validation and UI; one source of proposal values."""
    for key in DraftFields.model_fields:
        value = getattr(fields, key)
        if isinstance(value, FieldProposal):
            yield key, value
        else:
            for name in type(value).model_fields:
                yield f"{key}.{name}", getattr(value, name)
