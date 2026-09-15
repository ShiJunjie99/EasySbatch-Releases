"""Read-only project observations, independent of JobSpec and execution models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_IGNORE_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist",
    ".cache", ".pytest_cache", ".idea", ".vscode", ".sbatch-agent",
    "trajectory", "trajectories", "output", "outputs", "results", "checkpoints",
})
BINARY_SUFFIXES = frozenset({
    ".xtc", ".trr", ".dcd", ".tng", ".nc", ".h5", ".hdf5", ".npy", ".npz",
    ".pt", ".pth", ".ckpt", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".so", ".o", ".a", ".bin", ".db", ".sqlite", ".sqlite3", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".woff", ".tpr", ".exe",
})


@dataclass(frozen=True)
class ScanConfig:
    max_files: int = 1000
    max_file_size: int = 512 * 1024
    max_total_text_bytes: int = 5 * 1024 * 1024
    max_depth: int = 6
    max_snippet_chars: int = 500
    # Bound directory enumeration and output expansion as well as text I/O.
    max_directory_entries: int = 2000
    max_directories: int = 256
    max_evidence_items: int = 2000
    ignore_dirs: frozenset[str] = DEFAULT_IGNORE_DIRS
    binary_suffixes: frozenset[str] = BINARY_SUFFIXES

    def __post_init__(self):
        for name in (
            "max_files", "max_file_size", "max_total_text_bytes", "max_depth",
            "max_snippet_chars", "max_directory_entries", "max_directories", "max_evidence_items",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "max_depth" else 1):
                raise ValueError(f"{name} must be an integer >= {0 if name == 'max_depth' else 1}")
        for name in ("ignore_dirs", "binary_suffixes"):
            value = getattr(self, name)
            if not isinstance(value, (set, frozenset)) or any(
                not isinstance(item, str) or not item or "/" in item for item in value
            ):
                raise ValueError(f"{name} must contain simple nonempty names")
            object.__setattr__(self, name, frozenset(value))


class EvidenceLevel(StrEnum):
    DIRECT = "DIRECT"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class _Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceItem(_Observation):
    id: str
    kind: str
    source_path: str
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    snippet: str
    description: str
    level: EvidenceLevel
    value: str | None = None


class ProjectCandidate(_Observation):
    kind: str
    value: str
    level: EvidenceLevel
    priority: int
    evidence_ids: tuple[str, ...]


class ScannedFile(_Observation):
    path: str
    size_bytes: int | None
    status: str  # read / skipped / metadata_only
    reason: str | None = None


class FileFingerprint(_Observation):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectEvidence(_Observation):
    project_dir: str
    scanned_at: datetime
    files_considered: int
    files_skipped: int
    bytes_read: int
    files: tuple[ScannedFile, ...] = ()
    skipped_directories: tuple[str, ...] = ()
    git_present: bool = False
    project_type_candidates: tuple[ProjectCandidate, ...] = ()
    entrypoint_candidates: tuple[ProjectCandidate, ...] = ()
    executable_candidates: tuple[ProjectCandidate, ...] = ()
    existing_run_commands: tuple[ProjectCandidate, ...] = ()
    build_candidates: tuple[ProjectCandidate, ...] = ()
    environment_hints: tuple[ProjectCandidate, ...] = ()
    input_candidates: tuple[ProjectCandidate, ...] = ()
    cli_hints: tuple[ProjectCandidate, ...] = ()
    installed_software_hints: tuple[ProjectCandidate, ...] = ()
    existing_shell_scripts: tuple[ProjectCandidate, ...] = ()
    existing_sbatch_scripts: tuple[ProjectCandidate, ...] = ()
    parallelism_hints: tuple[ProjectCandidate, ...] = ()
    evidence_items: tuple[EvidenceItem, ...] = ()
    source_fingerprints: tuple[FileFingerprint, ...] = ()
    ambiguities: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    limits_reached: tuple[str, ...] = ()

    @field_validator("scanned_at")
    @classmethod
    def aware_time(cls, value):
        if value.utcoffset() is None:
            raise ValueError("scanned_at must include a timezone")
        return value
