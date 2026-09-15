"""Bounded stdio bridge used by the Beta EasySbatch desktop application.

The model-facing surface remains read-only apart from saving a local draft.
User-driven desktop panels may also inspect the cluster, submit an already
reviewed immutable draft, and refresh its status through the password-only,
fixed-command in-app SSH adapter.
"""

from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import BinaryIO, TextIO

from pydantic import SecretStr, ValidationError

from .cluster import ClusterService, ClusterUnavailableError, SlurmClusterClient
from .cluster_models import ClusterSnapshot
from .cluster_profile import ClusterProfile
from .desktop_catalog import catalog_source, managed_catalog
from .desktop_profiles import managed_profiles, profile_source
from .desktop_ssh import (
    DesktopSSHAuthenticationError, DesktopSSHDirectoryError,
    DesktopSSHPasswordRunner, DesktopSSHProjectScanError,
    DesktopSSHUnavailableError, SSHHostKey, inspect_ssh_host_key,
)
from .desktop_state import DesktopStateError, DesktopStateRepository
from .launcher_client import validate_username
from .models import JobSpec
from .persistence import JobRecord, JobRepository, PersistenceError
from .profiles import StaticProfiles
from .recommendation_models import RecommendationReport, RecommendationRequest, UserPreference
from .recommender import RecommendationInputError, ResourceRecommender
from .renderer import JobSpecValidationError, render_job_script
from .resource_policy import recommend_resource_values
from .scanner import ProjectScanError, ProjectScanner
from .scanner_models import ProjectEvidence, ScanConfig, ScannedFile
from .server_catalog import CatalogError, ServerCatalog, compatibility
from .service import (
    JobNotSubmittedError, JobNotSubmittableError, SubmissionService,
    SubmissionServiceError,
)
from .slurm import SlurmClient, resolve_log_path
from .smart_models import PreparationValues


PRODUCT_NAME = "Beta EasySbatch"
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_PROFILE_BYTES = 512 * 1024
MAX_CATALOG_BYTES = 512 * 1024
MAX_CONNECTION_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
_REQUEST_PASSWORD: ContextVar[SecretStr | None] = ContextVar(
    "beta_easysbatch_request_password", default=None,
)


class SidecarError(ValueError):
    """Expected request failure whose message is safe to return to the caller."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _exact_params(params: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(params, dict) or any(not isinstance(key, str) for key in params):
        raise SidecarError("INVALID_PARAMS", "params must be a JSON object")
    actual = set(params)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise SidecarError("INVALID_PARAMS", "; ".join(detail))
    return params


def _validation_message(exc: ValidationError) -> str:
    issues = []
    for error in exc.errors(include_input=False, include_url=False)[:30]:
        location = ".".join(str(part) for part in error["loc"]) or "value"
        issues.append(f"{location}: {error['msg']}")
    return "Invalid structured data: " + "; ".join(issues)


def _review_sha256(spec: JobSpec, script: str) -> str:
    canonical = json.dumps(
        spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical + b"\0" + script.encode("utf-8")).hexdigest()


def _render(spec: JobSpec, profiles: StaticProfiles) -> tuple[str, str]:
    try:
        script = render_job_script(spec, profiles=profiles)
    except JobSpecValidationError as exc:
        raise SidecarError("JOB_SPEC_NOT_RENDERABLE", "; ".join(exc.issues[:30])) from None
    return script, _review_sha256(spec, script)


def _read_regular_file(path_value: object, *, limit: int, label: str = "profiles_path") -> bytes:
    if not isinstance(path_value, str) or not path_value.strip() or not path_value.isprintable():
        raise SidecarError("INVALID_PARAMS", f"{label} must be a printable absolute path")
    path = Path(path_value)
    if not path.is_absolute() or ".." in path.parts:
        raise SidecarError("INVALID_PARAMS", f"{label} must be absolute and cannot contain '..'")
    try:
        path_info = os.lstat(path)
    except OSError:
        raise SidecarError("PROFILE_UNAVAILABLE", "The configured profile file cannot be opened") from None
    if stat.S_ISLNK(path_info.st_mode) or bool(
        getattr(path_info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise SidecarError("PROFILE_UNAVAILABLE", "The configured profile path cannot be a link or reparse point")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise SidecarError("PROFILE_UNAVAILABLE", "The configured profile file cannot be opened") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SidecarError("PROFILE_UNAVAILABLE", "The configured profile path is not a regular file")
        if (before.st_dev, before.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise SidecarError("PROFILE_INVALID", "The configured profile file changed while being opened")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if len(raw) > limit:
            raise SidecarError("PROFILE_INVALID", "The configured profile file exceeds the size limit")
        if len(raw) != before.st_size:
            raise SidecarError("PROFILE_INVALID", "The configured profile file could not be read completely")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise SidecarError("PROFILE_INVALID", "The configured profile file changed while being read")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _load_profiles(path_value: object, *, expected_sha256: str | None = None) -> StaticProfiles:
    import yaml

    class UniqueSafeLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise SidecarError("PROFILE_INVALID", "The configured profile file contains duplicate keys")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        raw = _read_regular_file(path_value, limit=MAX_PROFILE_BYTES)
        if expected_sha256 is not None and not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), expected_sha256,
        ):
            raise SidecarError(
                "PROFILE_CHANGED",
                "The automatic environment configuration changed after the cluster was saved",
            )
        data = yaml.load(raw, Loader=UniqueSafeLoader)
        return StaticProfiles.model_validate(data)
    except SidecarError:
        raise
    except (UnicodeError, yaml.YAMLError, ValidationError, TypeError, ValueError, RecursionError):
        raise SidecarError("PROFILE_INVALID", "The configured profile file is not a valid StaticProfiles document") from None


def _load_catalog(
    path_value: object, *, profiles: StaticProfiles, expected_sha256: str | None = None,
) -> ServerCatalog:
    try:
        raw = _read_regular_file(
            path_value, limit=MAX_CATALOG_BYTES, label="catalog_path",
        )
        if expected_sha256 is not None and not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), expected_sha256,
        ):
            raise SidecarError(
                "CATALOG_CHANGED",
                "The automatic software catalog changed after the cluster was saved",
            )
        return ServerCatalog.load(path_value, profiles=profiles)
    except SidecarError as exc:
        if exc.code in {"CATALOG_CHANGED", "INVALID_PARAMS"}:
            raise
        raise SidecarError(
            "CATALOG_UNAVAILABLE", "The desktop software catalog is unavailable",
        ) from None
    except CatalogError:
        raise SidecarError(
            "CATALOG_INVALID", "The desktop software catalog is invalid",
        ) from None


def _absolute_path(path_value: object, *, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip() or not path_value.isprintable():
        raise SidecarError("INVALID_PARAMS", f"{label} must be a printable absolute path")
    path = Path(path_value)
    if not path.is_absolute() or ".." in path.parts:
        raise SidecarError("INVALID_PARAMS", f"{label} must be absolute and cannot contain '..'")
    return path


def _unique_json(raw: bytes, *, code: str, message: str) -> object:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise SidecarError(code, message) from None


def _load_cluster_configuration(
    path_value: object,
) -> tuple[ClusterProfile, str, str | None, str | None, SSHHostKey | None]:
    try:
        raw = _read_regular_file(
            path_value, limit=MAX_CONNECTION_BYTES, label="cluster_config_path",
        )
    except SidecarError:
        raise SidecarError(
            "CLUSTER_CONFIG_UNAVAILABLE",
            "The desktop cluster configuration is missing or cannot be opened",
        ) from None
    data = _unique_json(
        raw, code="CLUSTER_CONFIG_INVALID",
        message="The desktop cluster configuration is invalid",
    )
    try:
        if not isinstance(data, dict) or set(data) not in (
            {"profile", "username"},
            {"profile", "username", "profiles_sha256"},
            {"profile", "username", "profiles_sha256", "catalog_sha256"},
            {"profile", "username", "profiles_sha256", "catalog_sha256", "host_key"},
        ):
            raise ValueError
        profile = ClusterProfile.from_mapping(data["profile"])
        username = validate_username(data["username"])
        fingerprint = data.get("profiles_sha256")
        catalog_fingerprint = data.get("catalog_sha256")
        for value in (fingerprint, catalog_fingerprint):
            if value is not None and (
                not isinstance(value, str) or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError
        host_key = SSHHostKey.from_mapping(data["host_key"]) if "host_key" in data else None
        return profile, username, fingerprint, catalog_fingerprint, host_key
    except SidecarError:
        raise
    except (ValueError, TypeError, DesktopSSHUnavailableError):
        raise SidecarError(
            "CLUSTER_CONFIG_INVALID",
            "The desktop cluster configuration is invalid",
        ) from None


def _load_cluster_runner(
    path_value: object,
) -> tuple[DesktopSSHPasswordRunner, ClusterProfile, str, str | None, str | None]:
    profile, username, fingerprint, catalog_fingerprint, host_key = (
        _load_cluster_configuration(path_value)
    )
    password = _REQUEST_PASSWORD.get()
    if host_key is None or password is None:
        raise SidecarError(
            "SSH_AUTH_REQUIRED",
            "Enter the server IP, port, username, and password in the app to connect",
        )
    runner = DesktopSSHPasswordRunner(
        profile=profile, username=username, password=password, host_key=host_key,
    )
    return runner, profile, username, fingerprint, catalog_fingerprint


def _repository(path_value: object) -> JobRepository:
    path = _absolute_path(path_value, label="database_path")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return JobRepository(path)
    except (OSError, PersistenceError):
        raise SidecarError("JOB_STORE_UNAVAILABLE", "The desktop task history is unavailable") from None


def _write_private_json(path_value: object, value: object) -> None:
    path = _absolute_path(path_value, label="cluster_config_path")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise OSError("configuration path cannot be a link")
        payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        raise SidecarError(
            "CLUSTER_CONFIG_UNAVAILABLE",
            "The desktop cluster configuration could not be saved",
        ) from None


def _write_private_profiles(path_value: object, profiles: StaticProfiles) -> str:
    import yaml

    path = _absolute_path(path_value, label="profiles_path")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise OSError("profile path cannot be a link")
        payload = yaml.safe_dump(
            profiles.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        if len(payload) > MAX_PROFILE_BYTES:
            raise OSError("managed profile document exceeds the size limit")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(payload).hexdigest()
    except OSError:
        raise SidecarError(
            "PROFILE_UNAVAILABLE",
            "The desktop environment configuration could not be saved",
        ) from None


def _write_private_catalog(path_value: object, catalog: ServerCatalog) -> str:
    import yaml

    path = _absolute_path(path_value, label="catalog_path")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists() and path.is_symlink():
            raise OSError("catalog path cannot be a link")
        payload = yaml.safe_dump(
            catalog.model_dump(mode="json"), allow_unicode=True, sort_keys=False,
        ).encode("utf-8")
        if len(payload) > MAX_CATALOG_BYTES:
            raise OSError("managed catalog exceeds the size limit")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(payload).hexdigest()
    except OSError:
        raise SidecarError(
            "CATALOG_UNAVAILABLE", "The desktop software catalog could not be saved",
        ) from None


def _submission_service(values: dict[str, object]) -> SubmissionService:
    runner, _, _, expected_profiles, _ = _load_cluster_runner(values["cluster_config_path"])
    profiles = _load_profiles(
        values["profiles_path"], expected_sha256=expected_profiles,
    )
    repository = _repository(values["database_path"])
    try:
        root = _absolute_path(values["submission_root"], label="submission_root")
        return SubmissionService(
            repository=repository,
            slurm_client=SlurmClient(runner=runner),
            profiles=profiles,
            submission_root=root,
        )
    except Exception:
        repository.close()
        raise


def _status_view(record: JobRecord) -> dict[str, object] | None:
    status = record.job_status
    if status is None:
        return None
    return {
        "normalized_state": status.normalized_state.value,
        "raw_state": status.raw_state,
        "source": status.source,
        "reason": status.reason,
        "partition": status.partition,
        "exit_code": status.exit_code,
        "signal": status.signal,
        "start": status.start,
        "end": status.end,
    }


def _record_view(record: JobRecord, *, detail: bool) -> dict[str, object]:
    spec = record.job_spec
    stdout = stderr = None
    if record.slurm_job_id is not None:
        try:
            stdout = resolve_log_path(record.stdout_path, record.slurm_job_id, work_dir=spec.work_dir)
            stderr = resolve_log_path(record.stderr_path, record.slurm_job_id, work_dir=spec.work_dir)
        except ValueError:
            # Preserve declarations below; an unsupported token is not guessed.
            pass
    result: dict[str, object] = {
        "id": record.id,
        "name": record.name,
        "slurm_job_id": record.slurm_job_id,
        "cluster_name": record.cluster_name,
        "submission_state": record.submission_state.value,
        "submission_error": record.submission_error,
        "status": _status_view(record),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "entrypoint": spec.entrypoint,
        "run_type": spec.run_type,
        "work_dir": spec.work_dir,
        "resources": spec.resources.model_dump(mode="json"),
        "stdout_path": stdout if stdout is not None else record.stdout_path,
        "stderr_path": stderr if stderr is not None else record.stderr_path,
    }
    if detail:
        result.update({
            "job_spec": spec.model_dump(mode="json"),
            "rendered_script": record.rendered_script,
            "script_path": record.script_path,
        })
    return result


def _snapshot_view(snapshot: ClusterSnapshot) -> dict[str, object]:
    summary = asdict(snapshot.summary)
    return {
        "captured_at": snapshot.captured_at.isoformat(),
        "cluster_name": snapshot.cluster_name,
        "current_user": snapshot.current_user,
        "summary": summary,
        "queue": asdict(snapshot.queue) if snapshot.queue is not None else None,
        "partitions": [asdict(item) for item in snapshot.partitions],
        "nodes": [asdict(item) for item in snapshot.nodes],
        "warnings": list(snapshot.warnings),
    }


def _recommendation_view(report: RecommendationReport) -> dict[str, object]:
    return {
        "snapshot_captured_at": report.snapshot_captured_at.isoformat(),
        "preference": report.preference.value,
        "warnings": list(report.warnings),
        "evidence": [asdict(item) for item in report.evidence],
        "recommendations": [{
            "id": item.id,
            "partition": item.partition,
            "proposed_resources": item.proposed_resources.model_dump(mode="json"),
            "score": item.score,
            "rank": item.rank,
            "eligibility": item.eligibility.value,
            "reasons": list(item.reasons),
            "warnings": list(item.warnings),
            "evidence": [asdict(value) for value in item.evidence],
            "components": asdict(item.components),
            "snapshot_captured_at": item.snapshot_captured_at.isoformat(),
        } for item in report.recommendations],
        "rejections": [{
            "partition": item.partition,
            "reasons": list(item.reasons),
            "proposed_resources": (
                item.proposed_resources.model_dump(mode="json")
                if item.proposed_resources is not None else None
            ),
            "eligibility": item.eligibility.value,
        } for item in report.rejections],
    }


def _compact_scan(evidence: ProjectEvidence, *, scan_id: str | None = None) -> dict[str, object]:
    dumped = evidence.model_dump(mode="json")
    candidate_names = (
        "project_type_candidates",
        "entrypoint_candidates",
        "executable_candidates",
        "existing_run_commands",
        "build_candidates",
        "environment_hints",
        "input_candidates",
        "cli_hints",
        "installed_software_hints",
        "existing_shell_scripts",
        "existing_sbatch_scripts",
        "parallelism_hints",
    )
    candidates = {name: dumped[name][:12] for name in candidate_names if dumped[name]}
    evidence_ids = {
        evidence_id
        for values in candidates.values()
        for candidate in values
        for evidence_id in candidate.get("evidence_ids", [])
    }
    evidence_items = [
        item for item in dumped["evidence_items"] if item["id"] in evidence_ids
    ][:120]
    return {
        "scan_id": scan_id,
        "project_dir": dumped["project_dir"],
        "scanned_at": dumped["scanned_at"],
        "summary": {
            "files_considered": dumped["files_considered"],
            "files_skipped": dumped["files_skipped"],
            "bytes_read": dumped["bytes_read"],
            "git_present": dumped["git_present"],
        },
        "candidates": candidates,
        "evidence_items": evidence_items,
        "source_fingerprints": dumped["source_fingerprints"][:120],
        "ambiguities": dumped["ambiguities"][:40],
        "warnings": dumped["warnings"][:40],
        "limits_reached": dumped["limits_reached"],
    }


def _state_repository(path_value: object) -> DesktopStateRepository:
    path = _absolute_path(path_value, label="state_database_path")
    try:
        if path.exists():
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise OSError("state database must be a regular non-link file")
        return DesktopStateRepository(path)
    except (OSError, DesktopStateError):
        raise SidecarError(
            "PREPARATION_STORE_UNAVAILABLE", "The desktop preparation state is unavailable",
        ) from None


def _remote_scan_evidence(manifest: dict[str, object]) -> ProjectEvidence:
    """Run the existing deterministic detectors over a validated remote snapshot."""
    files = manifest["files"]
    assert isinstance(files, list)
    try:
        with tempfile.TemporaryDirectory(prefix="beta-easysbatch-remote-scan-") as name:
            root = Path(name)
            for row in files:
                assert isinstance(row, dict)
                content = row["data"]
                if content is None:
                    continue
                assert isinstance(content, bytes)
                target = root.joinpath(*str(row["path"]).split("/"))
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(content)
                mode = row["mode"]
                if type(mode) is int:
                    target.chmod(0o700 if mode & 0o111 else 0o600)
            evidence = ProjectScanner(ScanConfig(
                max_files=500,
                max_total_text_bytes=384 * 1024,
                max_directory_entries=1000,
                max_directories=160,
                max_evidence_items=500,
            )).scan(root)
    except (OSError, ProjectScanError):
        raise SidecarError(
            "REMOTE_SCAN_INVALID", "The bounded remote project snapshot could not be analyzed",
        ) from None
    inventory = tuple(ScannedFile(
        path=str(row["path"]), size_bytes=row["size"], status=str(row["status"]),
        reason=row["reason"],
    ) for row in files)
    return evidence.model_copy(update={
        "project_dir": manifest["path"],
        "files_considered": len(inventory),
        "files_skipped": sum(item.status == "skipped" for item in inventory),
        "bytes_read": manifest["bytes_read"],
        "files": tuple(sorted(inventory, key=lambda item: item.path)),
        "skipped_directories": tuple(manifest["skipped_directories"]),
        "git_present": manifest["git_present"],
        "warnings": tuple(sorted(set(evidence.warnings) | set(manifest["warnings"]))),
        "limits_reached": tuple(sorted(set(evidence.limits_reached) | set(manifest["limits_reached"]))),
    })


def _desktop_scan_config() -> ScanConfig:
    return ScanConfig(
        max_files=500,
        max_total_text_bytes=2 * 1024 * 1024,
        max_directory_entries=1000,
        max_directories=160,
        max_evidence_items=500,
    )


def _review_spec(
    spec: JobSpec, *, profiles: StaticProfiles, catalog: ServerCatalog,
    software_id: str | None,
) -> tuple[str, str, object | None, object | None]:
    software = catalog.software_by_id(software_id) if software_id else None
    if software_id and software is None:
        raise SidecarError("CATALOG_ITEM_NOT_FOUND", "The selected software is not registered")
    if software is not None:
        mismatches = []
        if software.executable != spec.run_step.executable:
            mismatches.append("executable")
        if software.run_type != spec.run_type:
            mismatches.append("run_type")
        if software.environment_profile and (
            software.environment_profile.id != spec.environment_profile.id
            or software.environment_profile.version != spec.environment_profile.version
        ):
            mismatches.append("environment_profile")
        if mismatches:
            raise SidecarError(
                "CATALOG_MISMATCH",
                "The draft no longer matches the selected software: " + ", ".join(mismatches),
            )
    script, review_sha256 = _render(spec, profiles)
    return script, review_sha256, software, catalog.environment(spec.environment_profile)


def _preparation_values(spec: JobSpec) -> PreparationValues:
    resources = spec.resources
    return PreparationValues.model_validate({
        "name": spec.job_name,
        "work_dir": spec.work_dir,
        "run_type": spec.run_type,
        "entrypoint": spec.entrypoint,
        "executable": spec.run_step.executable,
        "args": spec.run_step.args,
        "required_inputs": spec.required_inputs,
        "environment_profile": spec.environment_profile.model_dump(mode="json"),
        "prepare_steps": [value.model_dump(mode="json") for value in spec.prepare_steps],
        "launcher_profile": (
            spec.run_step.launcher_profile.model_dump(mode="json")
            if spec.run_step.launcher_profile else None
        ),
        "partition": resources.partition,
        "account": resources.account,
        "qos": resources.qos,
        "nodes": resources.nodes,
        "ntasks": resources.ntasks,
        "cpus_per_task": resources.cpus_per_task,
        "gpu_count": resources.gpus.count if resources.gpus else 0,
        "gpu_type": resources.gpus.gpu_type if resources.gpus else None,
        "memory_mib": resources.memory_mib,
        "time_limit_seconds": resources.time_limit_seconds,
        "memory_mode": resources.memory_policy.mode,
        "walltime_mode": resources.walltime_policy.mode,
        "stdout": spec.stdout,
        "stderr": spec.stderr,
    })


def _resource_value_recommendations(
    spec: JobSpec, *, profiles: StaticProfiles, catalog: ServerCatalog,
    software_id: str | None, evidence: ProjectEvidence | None,
) -> dict[str, object]:
    environment = next((
        item for item in profiles.environments
        if item.id == spec.environment_profile.id and item.version == spec.environment_profile.version
    ), None)
    software = catalog.software_by_id(software_id) if software_id else None
    if software_id and software is None:
        raise SidecarError("CATALOG_ITEM_NOT_FOUND", "The selected software is not registered")
    recommendations = recommend_resource_values(
        _preparation_values(spec), environment=environment,
        software=software, project_evidence=evidence,
    )
    return {
        key: value.model_dump(mode="json") for key, value in recommendations.items()
    }


def _scan_for_spec(
    repository: DesktopStateRepository, *, scan_id: object, project_dir: str,
) -> tuple[str | None, ProjectEvidence | None]:
    try:
        if scan_id is not None:
            if not isinstance(scan_id, str):
                raise DesktopStateError("remote scan identifier is invalid")
            evidence = repository.get_scan(scan_id)
            if evidence.project_dir != project_dir:
                raise DesktopStateError("remote scan does not match the task project directory")
            return scan_id, evidence
        active = repository.active_scan()
        if active is not None and active[1].project_dir == project_dir:
            return active
        return None, None
    except DesktopStateError:
        raise SidecarError(
            "SCAN_EVIDENCE_INVALID", "The selected project scan is unavailable or belongs to another directory",
        ) from None


def _with_scan_fingerprints(spec: JobSpec, evidence: ProjectEvidence | None) -> JobSpec:
    if evidence is None:
        return spec
    values = spec.model_dump(mode="json")
    values["source_fingerprints"] = [
        value.model_dump(mode="json") for value in evidence.source_fingerprints
    ]
    return JobSpec.model_validate(values)


def _preparation_material(
    spec: JobSpec, *, name: str | None, software_id: str | None,
    scan_id: object, repository: DesktopStateRepository,
    profiles: StaticProfiles, catalog: ServerCatalog,
) -> dict[str, object]:
    selected_scan, evidence = _scan_for_spec(
        repository, scan_id=scan_id, project_dir=spec.project_dir,
    )
    spec = _with_scan_fingerprints(spec, evidence)
    warnings = list(evidence.warnings[:40]) if evidence is not None else [
        "此草稿未关联用户确认的服务器项目扫描；保存前无法复核项目内容是否变化。",
    ]
    if spec.unresolved:
        state, script, review_sha256 = "NEEDS_INPUT", None, None
    else:
        script, review_sha256, _, _ = _review_spec(
            spec, profiles=profiles, catalog=catalog, software_id=software_id,
        )
        state = "READY_TO_SAVE"
    return {
        "spec": spec, "name": name, "software_id": software_id,
        "scan_id": selected_scan, "state": state, "rendered_script": script,
        "review_sha256": review_sha256, "warnings": warnings,
    }


def dispatch(method: str, params: object) -> dict[str, object]:
    """Execute one allowlisted desktop operation."""
    if method == "health":
        _exact_params(params, set())
        return {
            "product": PRODUCT_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": [
                "scan_project", "scan_remote_project", "active_remote_scan",
                "validate_job", "render_job", "review_job", "list_profiles", "list_catalog",
                "inspect_ssh_host_key", "connect_cluster", "configure_cluster",
                "cluster_snapshot", "recommend_job", "create_job", "list_jobs",
                "get_job", "submit_job", "refresh_job",
                "browse_remote_directory",
                "recommend_resource_values", "start_preparation", "revise_preparation",
                "list_preparations", "get_preparation", "finalize_preparation",
            ],
            "submission_supported": True,
            # Runtime readiness is reported only after validating the explicit
            # product-owned paths with ``runtime_status``.
            "submission_enabled": False,
        }
    if method == "runtime_status":
        values = _exact_params(
            params, {"cluster_config_path", "profiles_path", "catalog_path"},
        )
        problems = []
        cluster = None
        expected_profiles = None
        expected_catalog = None
        trusted_host_key = None
        try:
            cluster, username, expected_profiles, expected_catalog, trusted_host_key = _load_cluster_configuration(
                values["cluster_config_path"],
            )
        except SidecarError as exc:
            username = None
            problems.append(str(exc))
        try:
            profiles = _load_profiles(
                values["profiles_path"], expected_sha256=expected_profiles,
            )
        except SidecarError as exc:
            profiles = None
            problems.append(str(exc))
        if profiles is not None:
            try:
                catalog = _load_catalog(
                    values["catalog_path"], profiles=profiles,
                    expected_sha256=expected_catalog,
                )
            except SidecarError as exc:
                catalog = None
                problems.append(str(exc))
        else:
            catalog = None
        return {
            "cluster_configured": cluster is not None,
            "profiles_configured": profiles is not None,
            "catalog_configured": catalog is not None,
            "submission_enabled": (
                cluster is not None and profiles is not None
                and trusted_host_key is not None and _REQUEST_PASSWORD.get() is not None
            ),
            "authentication_required": (
                cluster is not None
                and (trusted_host_key is None or _REQUEST_PASSWORD.get() is None)
            ),
            "cluster": None if cluster is None else {
                "id": cluster.id,
                "display_name": cluster.display_name,
                "host": cluster.host,
                "ssh_port": cluster.ssh_port,
                "username": username,
            },
            "profile_counts": None if profiles is None else {
                "environments": len(profiles.environments),
                "launchers": len(profiles.launchers),
            },
            "profile_source": (
                None if profiles is None or cluster is None
                else profile_source(profiles, cluster.host, cluster.ssh_port)
            ),
            "catalog_counts": None if catalog is None else {
                "environments": len(catalog.environments),
                "software": len(catalog.software),
                "compilers": len(catalog.compilers),
            },
            "catalog_source": (
                None if catalog is None or cluster is None or profiles is None
                else catalog_source(catalog, cluster.host, cluster.ssh_port, profiles)
            ),
            "trusted_host_key": None if trusted_host_key is None else {
                "algorithm": trusted_host_key.algorithm,
                "fingerprint": trusted_host_key.fingerprint,
            },
            "problems": problems,
        }
    if method == "inspect_ssh_host_key":
        values = _exact_params(params, {"host", "ssh_port"})
        try:
            profile = ClusterProfile(
                "inspection", "SSH server", values["host"], values["ssh_port"],
            )
            return inspect_ssh_host_key(profile.host, profile.ssh_port)
        except (ValueError, TypeError):
            raise SidecarError(
                "CLUSTER_CONFIG_INVALID", "The SSH host or port is invalid",
            ) from None
        except DesktopSSHUnavailableError:
            raise SidecarError(
                "SSH_HOST_UNAVAILABLE", "The SSH server is unavailable or its handshake failed",
            ) from None
    if method == "connect_cluster":
        values = _exact_params(
            params, {
                "cluster_config_path", "profiles_path", "catalog_path",
                "profile", "username", "host_key",
            },
        )
        password = _REQUEST_PASSWORD.get()
        if password is None:
            raise SidecarError(
                "SSH_AUTH_REQUIRED", "Enter the SSH password for this app session",
            )
        try:
            profile = ClusterProfile.from_mapping(values["profile"])
            username = validate_username(values["username"])
            host_key = SSHHostKey.from_mapping(values["host_key"])
            runner = DesktopSSHPasswordRunner(
                profile=profile, username=username, password=password, host_key=host_key,
            )
            runner.connect()
            # Authentication and a fresh Slurm read happen before any connection
            # or preset state is committed locally.
            snapshot = ClusterService(
                SlurmClusterClient(runner=runner), current_user=username,
            ).get_snapshot()
        except (ValueError, TypeError):
            raise SidecarError(
                "CLUSTER_CONFIG_INVALID",
                "Cluster name, host, port, username, or approved host key is invalid",
            ) from None
        except DesktopSSHAuthenticationError:
            raise SidecarError(
                "SSH_AUTH_FAILED", "The SSH username or password is incorrect, or the server identity changed",
            ) from None
        except (DesktopSSHUnavailableError, ClusterUnavailableError, SubmissionServiceError):
            raise SidecarError(
                "CLUSTER_UNAVAILABLE", "The SSH server or Slurm resource service is unavailable",
            ) from None
        finally:
            if "runner" in locals():
                runner.close()
        profiles, source = managed_profiles(profile.host, profile.ssh_port)
        profiles_fingerprint = _write_private_profiles(values["profiles_path"], profiles)
        catalog, catalog_mode = managed_catalog(profile.host, profile.ssh_port, profiles)
        catalog_fingerprint = _write_private_catalog(values["catalog_path"], catalog)
        _write_private_json(values["cluster_config_path"], {
            "profile": profile.to_mapping(),
            "username": username,
            "profiles_sha256": profiles_fingerprint,
            "catalog_sha256": catalog_fingerprint,
            "host_key": host_key.to_mapping(),
        })
        return {
            "saved": True,
            "cluster": {**profile.to_mapping(), "username": username},
            "password_stored": False,
            "host_key": {
                "algorithm": host_key.algorithm,
                "fingerprint": host_key.fingerprint,
            },
            "profile_source": source,
            "catalog_source": catalog_mode,
            "snapshot": _snapshot_view(snapshot),
        }
    if method == "configure_cluster":
        values = _exact_params(
            params, {
                "cluster_config_path", "profiles_path", "catalog_path",
                "profile", "username",
            },
        )
        try:
            profile = ClusterProfile.from_mapping(values["profile"])
            username = validate_username(values["username"])
        except (ValueError, TypeError):
            raise SidecarError(
                "CLUSTER_CONFIG_INVALID",
                "Cluster name, host, port, or Linux username is invalid",
            ) from None
        profiles, source = managed_profiles(profile.host, profile.ssh_port)
        fingerprint = _write_private_profiles(values["profiles_path"], profiles)
        catalog, catalog_mode = managed_catalog(profile.host, profile.ssh_port, profiles)
        catalog_fingerprint = _write_private_catalog(values["catalog_path"], catalog)
        _write_private_json(values["cluster_config_path"], {
            "profile": profile.to_mapping(),
            "username": username,
            "profiles_sha256": fingerprint,
            "catalog_sha256": catalog_fingerprint,
        })
        return {
            "saved": True,
            "cluster": {
                **profile.to_mapping(),
                "username": username,
            },
            "credentials_stored": False,
            "profile_source": source,
            "profile_counts": {
                "environments": len(profiles.environments),
                "launchers": len(profiles.launchers),
            },
            "catalog_source": catalog_mode,
            "catalog_counts": {
                "environments": len(catalog.environments),
                "software": len(catalog.software),
                "compilers": len(catalog.compilers),
            },
        }
    if method == "scan_project":
        values = _exact_params(params, {"project_dir"})
        project_dir = values["project_dir"]
        if not isinstance(project_dir, str):
            raise SidecarError("INVALID_PARAMS", "project_dir must be a string")
        scanner = ProjectScanner(_desktop_scan_config())
        try:
            return _compact_scan(scanner.scan(project_dir))
        except ProjectScanError as exc:
            raise SidecarError("SCAN_REFUSED", str(exc)) from None
    if method == "validate_job":
        values = _exact_params(params, {"job_spec"})
        try:
            spec = JobSpec.model_validate(values["job_spec"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        return {
            "valid": True,
            "job_spec": spec.model_dump(mode="json"),
            "submission_enabled": False,
        }
    if method == "render_job":
        values = _exact_params(params, {"job_spec", "profiles_path"})
        try:
            spec = JobSpec.model_validate(values["job_spec"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        profiles = _load_profiles(values["profiles_path"])
        script, review_sha256 = _render(spec, profiles)
        return {
            "job_spec": spec.model_dump(mode="json"),
            "script": script,
            "review_sha256": review_sha256,
            "submission_enabled": False,
        }
    if method == "review_job":
        values = _exact_params(
            params, {"job_spec", "profiles_path", "catalog_path", "software_id"},
        )
        try:
            spec = JobSpec.model_validate(values["job_spec"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        software_id = values["software_id"]
        if software_id is not None and not isinstance(software_id, str):
            raise SidecarError("INVALID_PARAMS", "software_id must be a string or null")
        profiles = _load_profiles(values["profiles_path"])
        catalog = _load_catalog(values["catalog_path"], profiles=profiles)
        script, review_sha256, software, environment = _review_spec(
            spec, profiles=profiles, catalog=catalog, software_id=software_id,
        )
        return {
            "job_spec": spec.model_dump(mode="json"),
            "script": script,
            "review_sha256": review_sha256,
            "software": None if software is None else {
                "id": software.id,
                "verification_status": software.verification_status,
                "verification_scope": software.verification_scope,
            },
            "environment": None if environment is None else {
                "id": environment.id,
                "verification_status": environment.verification_status,
                "verification_scope": environment.verification_scope,
            },
            "submission_enabled": False,
        }
    if method == "list_profiles":
        values = _exact_params(params, {"profiles_path"})
        profiles = _load_profiles(values["profiles_path"])
        return {
            "environments": [{
                "id": item.id,
                "version": item.version,
                "allowed_partitions": item.allowed_partitions,
                "resource_options": [option.model_dump(mode="json") for option in item.resource_options],
            } for item in profiles.environments],
            "launchers": [{
                "id": item.id,
                "version": item.version,
                "supported_layouts": (
                    None if item.supported_layouts is None
                    else [layout.model_dump(mode="json") for layout in item.supported_layouts]
                ),
            } for item in profiles.launchers],
        }
    if method == "list_catalog":
        values = _exact_params(params, {"profiles_path", "catalog_path"})
        profiles = _load_profiles(values["profiles_path"])
        catalog = _load_catalog(values["catalog_path"], profiles=profiles)
        return {
            "metadata": catalog.metadata.model_dump(mode="json"),
            "environments": [{
                "id": item.id,
                "display_name": item.display_name,
                "version": item.version,
                "verification_status": item.verification_status,
                "last_verified_at": (
                    item.last_verified_at.isoformat() if item.last_verified_at else None
                ),
                "verification_scope": item.verification_scope,
                "python_executable": item.python_executable,
                "environment_profile": (
                    item.environment_profile.model_dump(mode="json")
                    if item.environment_profile else None
                ),
                "available_partitions": item.available_partitions,
                "capabilities": (
                    item.capabilities.model_dump(mode="json") if item.capabilities else None
                ),
            } for item in catalog.environments],
            "software": [{
                "id": item.id,
                "display_name": item.display_name,
                "version": item.version,
                "aliases": list(item.aliases),
                "executable": item.executable,
                "environment_profile": (
                    item.environment_profile.model_dump(mode="json")
                    if item.environment_profile else None
                ),
                "run_type": item.run_type,
                "parallelism": list(item.parallelism),
                "compatible_partitions": item.compatible_partitions,
                "launch_profile": (
                    item.launch_profile.model_dump(mode="json")
                    if item.launch_profile else None
                ),
                "verification_status": item.verification_status,
                "last_verified_at": (
                    item.last_verified_at.isoformat() if item.last_verified_at else None
                ),
                "verification_scope": item.verification_scope,
            } for item in catalog.software],
            "compilers": [{
                "id": item.id,
                "display_name": item.display_name,
                "version": item.version,
                "kind": item.kind,
                "executable": item.executable,
                "environment_profile": (
                    item.environment_profile.model_dump(mode="json")
                    if item.environment_profile else None
                ),
                "verification_status": item.verification_status,
                "last_verified_at": (
                    item.last_verified_at.isoformat() if item.last_verified_at else None
                ),
                "verification_scope": item.verification_scope,
            } for item in catalog.compilers],
        }
    if method == "cluster_snapshot":
        values = _exact_params(params, {"cluster_config_path"})
        runner, _, username, _, _ = _load_cluster_runner(values["cluster_config_path"])
        try:
            snapshot = ClusterService(
                SlurmClusterClient(runner=runner), current_user=username,
            ).get_snapshot()
        except (ClusterUnavailableError, SubmissionServiceError):
            raise SidecarError(
                "CLUSTER_UNAVAILABLE",
                "The cluster snapshot is unavailable; verify the in-app SSH login and server identity",
            ) from None
        return _snapshot_view(snapshot)
    if method == "browse_remote_directory":
        values = _exact_params(params, {"cluster_config_path", "path"})
        runner, _, _, _, _ = _load_cluster_runner(values["cluster_config_path"])
        try:
            return runner.list_directory(values["path"], timeout=15)
        except (DesktopSSHDirectoryError, ValueError):
            raise SidecarError(
                "REMOTE_DIRECTORY_UNAVAILABLE",
                "The selected remote directory is unavailable; check the absolute path and SSH access",
            ) from None
    if method == "scan_remote_project":
        values = _exact_params(params, {"cluster_config_path", "state_database_path", "path"})
        runner, _, _, _, _ = _load_cluster_runner(values["cluster_config_path"])
        try:
            manifest = runner.scan_project(values["path"], timeout=30)
            evidence = _remote_scan_evidence(manifest)
        except (DesktopSSHProjectScanError, ValueError):
            raise SidecarError(
                "REMOTE_SCAN_UNAVAILABLE",
                "The selected remote project could not be scanned safely; check the path and SSH access",
            ) from None
        repository = _state_repository(values["state_database_path"])
        try:
            scan_id = repository.save_scan(evidence)
            return _compact_scan(evidence, scan_id=scan_id)
        except DesktopStateError:
            raise SidecarError(
                "PREPARATION_STORE_UNAVAILABLE", "The project evidence could not be saved locally",
            ) from None
        finally:
            repository.close()
    if method == "active_remote_scan":
        values = _exact_params(params, {"state_database_path"})
        repository = _state_repository(values["state_database_path"])
        try:
            active = repository.active_scan()
            return {"scan": None} if active is None else {
                "scan": _compact_scan(active[1], scan_id=active[0]),
            }
        except DesktopStateError:
            raise SidecarError(
                "SCAN_EVIDENCE_INVALID", "The saved project scan is unavailable",
            ) from None
        finally:
            repository.close()
    if method == "recommend_resource_values":
        values = _exact_params(params, {
            "job_spec", "profiles_path", "catalog_path", "state_database_path",
            "software_id", "scan_id",
        })
        try:
            spec = JobSpec.model_validate(values["job_spec"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        software_id = values["software_id"]
        if software_id is not None and not isinstance(software_id, str):
            raise SidecarError("INVALID_PARAMS", "software_id must be a string or null")
        profiles = _load_profiles(values["profiles_path"])
        catalog = _load_catalog(values["catalog_path"], profiles=profiles)
        repository = _state_repository(values["state_database_path"])
        try:
            scan_id, evidence = _scan_for_spec(
                repository, scan_id=values["scan_id"], project_dir=spec.project_dir,
            )
            recommendations = _resource_value_recommendations(
                spec, profiles=profiles, catalog=catalog, software_id=software_id,
                evidence=evidence,
            )
            return {
                "scan_id": scan_id,
                "recommendations": recommendations,
                "unavailable": {
                    key: "No exact verified rule or matching project declaration was found"
                    for key in {"memory_mib", "time_limit_seconds"} - set(recommendations)
                },
            }
        finally:
            repository.close()
    if method in {"start_preparation", "revise_preparation"}:
        expected = {
            "job_spec", "name", "software_id", "scan_id", "profiles_path",
            "catalog_path", "state_database_path",
        }
        if method == "revise_preparation":
            expected.update({"preparation_id", "revision"})
        values = _exact_params(params, expected)
        try:
            spec = JobSpec.model_validate(values["job_spec"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        name = values["name"]
        if name is not None and (
            not isinstance(name, str) or not name.strip() or not name.isprintable()
        ):
            raise SidecarError("INVALID_PARAMS", "name must be null or printable nonblank text")
        software_id = values["software_id"]
        if software_id is not None and not isinstance(software_id, str):
            raise SidecarError("INVALID_PARAMS", "software_id must be a string or null")
        profiles = _load_profiles(values["profiles_path"])
        catalog = _load_catalog(values["catalog_path"], profiles=profiles)
        repository = _state_repository(values["state_database_path"])
        try:
            material = _preparation_material(
                spec, name=name, software_id=software_id, scan_id=values["scan_id"],
                repository=repository, profiles=profiles, catalog=catalog,
            )
            if method == "start_preparation":
                return repository.create_preparation(**material)
            return repository.revise_preparation(
                values["preparation_id"], expected_revision=values["revision"], **material,
            )
        except DesktopStateError as exc:
            raise SidecarError("PREPARATION_CONFLICT", str(exc)) from None
        finally:
            repository.close()
    if method == "list_preparations":
        values = _exact_params(params, {"state_database_path", "limit"})
        repository = _state_repository(values["state_database_path"])
        try:
            return {"preparations": repository.list_preparations(limit=values["limit"])}
        except DesktopStateError:
            raise SidecarError(
                "PREPARATION_STORE_UNAVAILABLE", "The preparation list is unavailable",
            ) from None
        finally:
            repository.close()
    if method == "get_preparation":
        values = _exact_params(params, {"state_database_path", "preparation_id"})
        repository = _state_repository(values["state_database_path"])
        try:
            return repository.get_preparation(values["preparation_id"])
        except DesktopStateError:
            raise SidecarError("PREPARATION_NOT_FOUND", "The preparation was not found") from None
        finally:
            repository.close()
    if method == "finalize_preparation":
        values = _exact_params(params, {
            "preparation_id", "revision", "state_database_path", "profiles_path",
            "catalog_path", "cluster_config_path", "database_path", "submission_root",
        })
        repository = _state_repository(values["state_database_path"])
        job_repository = None
        try:
            try:
                prepared = repository.get_preparation(values["preparation_id"])
            except DesktopStateError:
                raise SidecarError("PREPARATION_NOT_FOUND", "The preparation was not found") from None
            if prepared["revision"] != values["revision"] or prepared["state"] != "READY_TO_SAVE":
                raise SidecarError(
                    "PREPARATION_CHANGED", "The preparation is not ready or has a newer revision",
                )
            spec = JobSpec.model_validate(prepared["job_spec"])
            profiles = _load_profiles(values["profiles_path"])
            catalog = _load_catalog(values["catalog_path"], profiles=profiles)
            script, digest, _, _ = _review_spec(
                spec, profiles=profiles, catalog=catalog,
                software_id=prepared["software_id"],
            )
            if (
                prepared["rendered_script"] != script
                or prepared["review_sha256"] != digest
            ):
                raise SidecarError(
                    "PREPARATION_CHANGED", "The rendered preparation changed and must be revised",
                )
            if prepared["scan_id"] is not None:
                old = repository.get_scan(prepared["scan_id"])
                runner, _, _, _, _ = _load_cluster_runner(values["cluster_config_path"])
                try:
                    fresh = _remote_scan_evidence(runner.scan_project(spec.project_dir, timeout=30))
                except (DesktopSSHProjectScanError, ValueError):
                    raise SidecarError(
                        "PROJECT_RECHECK_FAILED",
                        "The remote project could not be rechecked; the preparation was not saved",
                    ) from None
                old_fingerprints = {(item.path, item.sha256) for item in old.source_fingerprints}
                fresh_fingerprints = {(item.path, item.sha256) for item in fresh.source_fingerprints}
                if old_fingerprints != fresh_fingerprints:
                    raise SidecarError(
                        "PROJECT_CHANGED",
                        "The remote project changed after preparation; scan and revise it before saving",
                    )
            job_repository = _repository(values["database_path"])
            root = _absolute_path(values["submission_root"], label="submission_root")
            record = SubmissionService(
                repository=job_repository, slurm_client=SlurmClient(),
                profiles=profiles, submission_root=root,
            ).create_job(spec=spec, name=prepared["name"])
            prepared = repository.mark_saved(
                values["preparation_id"], expected_revision=values["revision"],
                record_id=record.id,
            )
            return {"preparation": prepared, "job": _record_view(record, detail=True)}
        except DesktopStateError as exc:
            raise SidecarError("PREPARATION_CONFLICT", str(exc)) from None
        except SidecarError:
            raise
        except (JobSpecValidationError, PersistenceError, ValueError):
            raise SidecarError(
                "JOB_NOT_READY", "The prepared task could not be saved",
            ) from None
        finally:
            if job_repository is not None:
                job_repository.close()
            repository.close()
    if method == "recommend_job":
        values = _exact_params(params, {
            "job_spec", "profiles_path", "catalog_path", "cluster_config_path",
            "preference", "software_id", "consider_all_partitions",
        })
        try:
            spec = JobSpec.model_validate(values["job_spec"])
            preference = UserPreference(values["preference"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        except (ValueError, TypeError):
            raise SidecarError("INVALID_PARAMS", "preference is invalid") from None
        software_id = values["software_id"]
        if software_id is not None and not isinstance(software_id, str):
            raise SidecarError("INVALID_PARAMS", "software_id must be a string or null")
        consider_all = values["consider_all_partitions"]
        if type(consider_all) is not bool:
            raise SidecarError("INVALID_PARAMS", "consider_all_partitions must be a boolean")
        runner, _, username, expected_profiles, expected_catalog = _load_cluster_runner(
            values["cluster_config_path"],
        )
        profiles = _load_profiles(
            values["profiles_path"], expected_sha256=expected_profiles,
        )
        catalog = _load_catalog(
            values["catalog_path"], profiles=profiles,
            expected_sha256=expected_catalog,
        )
        software = catalog.software_by_id(software_id) if software_id else None
        if software_id and software is None:
            raise SidecarError("CATALOG_ITEM_NOT_FOUND", "The selected software is not registered")
        requested_resources = spec.resources.model_dump(mode="json")
        if consider_all:
            requested_resources["partition"] = None
        request = RecommendationRequest.model_validate({
            "run_type": spec.run_type,
            "environment_profile": spec.environment_profile.model_dump(mode="json"),
            "launcher_profile": (
                spec.run_step.launcher_profile.model_dump(mode="json")
                if spec.run_step.launcher_profile else None
            ),
            "resources": requested_resources,
            "catalog_compatibility": compatibility(
                software, catalog.environment(spec.environment_profile),
            ).model_dump(mode="json"),
        })
        try:
            snapshot = ClusterService(
                SlurmClusterClient(runner=runner), current_user=username,
            ).get_snapshot()
            report = ResourceRecommender().recommend(
                spec=request, snapshot=snapshot, profiles=profiles,
                preference=preference, as_of=datetime.now(timezone.utc),
            )
        except ClusterUnavailableError:
            raise SidecarError(
                "CLUSTER_UNAVAILABLE",
                "Resource recommendation needs a fresh cluster snapshot; verify the SSH connection",
            ) from None
        except RecommendationInputError as exc:
            raise SidecarError("RECOMMENDATION_UNAVAILABLE", str(exc)) from None
        return _recommendation_view(report)
    if method == "create_job":
        values = _exact_params(params, {
            "job_spec", "name", "profiles_path", "database_path",
            "submission_root", "review_sha256",
        })
        name = values["name"]
        if name is not None and (not isinstance(name, str) or not name.strip() or not name.isprintable()):
            raise SidecarError("INVALID_PARAMS", "name must be null or printable nonblank text")
        try:
            spec = JobSpec.model_validate(values["job_spec"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        profiles = _load_profiles(values["profiles_path"])
        review_sha256 = values["review_sha256"]
        if (
            not isinstance(review_sha256, str)
            or len(review_sha256) != 64
            or any(char not in "0123456789abcdef" for char in review_sha256)
        ):
            raise SidecarError("REVIEW_REQUIRED", "A valid script review token is required")
        _, expected_review = _render(spec, profiles)
        if not hmac.compare_digest(review_sha256, expected_review):
            raise SidecarError(
                "REVIEW_CHANGED", "The task changed after preview; render and review it again",
            )
        repository = _repository(values["database_path"])
        try:
            root = _absolute_path(values["submission_root"], label="submission_root")
            record = SubmissionService(
                repository=repository, slurm_client=SlurmClient(),
                profiles=profiles, submission_root=root,
            ).create_job(spec=spec, name=name)
            return _record_view(record, detail=True)
        except (JobSpecValidationError, PersistenceError, ValueError):
            raise SidecarError(
                "JOB_NOT_READY",
                "The reviewed JobSpec could not be saved; resolve all fields and verify the profile configuration",
            ) from None
        finally:
            repository.close()
    if method == "list_jobs":
        values = _exact_params(params, {"database_path", "limit"})
        limit = values["limit"]
        if type(limit) is not int or not 1 <= limit <= 200:
            raise SidecarError("INVALID_PARAMS", "limit must be an integer from 1 through 200")
        repository = _repository(values["database_path"])
        try:
            return {"jobs": [_record_view(record, detail=False) for record in repository.list(limit=limit)]}
        except PersistenceError:
            raise SidecarError("JOB_STORE_UNAVAILABLE", "The desktop task history is unavailable") from None
        finally:
            repository.close()
    if method == "get_job":
        values = _exact_params(params, {"database_path", "record_id"})
        repository = _repository(values["database_path"])
        try:
            return _record_view(repository.get(values["record_id"]), detail=True)
        except (PersistenceError, ValueError, TypeError):
            raise SidecarError("JOB_NOT_FOUND", "The requested task record was not found") from None
        finally:
            repository.close()
    if method in {"submit_job", "refresh_job"}:
        expected = {
            "record_id", "profiles_path", "database_path", "submission_root",
            "cluster_config_path",
        }
        if method == "submit_job":
            expected.add("confirmation")
        values = _exact_params(params, expected)
        record_id = values["record_id"]
        if method == "submit_job" and values["confirmation"] != record_id:
            raise SidecarError(
                "SUBMISSION_CONFIRMATION_REQUIRED",
                "Submitting a real job requires an exact record confirmation",
            )
        service = None
        try:
            service = _submission_service(values)
            record = (
                service.submit_job(record_id)
                if method == "submit_job" else service.refresh_status(record_id)
            )
            return _record_view(record, detail=True)
        except JobNotSubmittableError:
            raise SidecarError("JOB_NOT_SUBMITTABLE", "This task record cannot be submitted again") from None
        except JobNotSubmittedError:
            raise SidecarError("JOB_NOT_SUBMITTED", "This task has no submitted Slurm job to refresh") from None
        except SubmissionServiceError:
            raise SidecarError(
                "SUBMISSION_OUTCOME_REQUIRES_REVIEW" if method == "submit_job" else "STATUS_REFRESH_FAILED",
                "The Slurm operation did not complete cleanly; inspect the saved task state and do not resubmit automatically",
            ) from None
        except (PersistenceError, ValueError, TypeError):
            raise SidecarError("JOB_OPERATION_FAILED", "The desktop task operation failed validation") from None
        finally:
            if service is not None:
                service.repository.close()
    raise SidecarError("METHOD_NOT_FOUND", f"Unsupported method: {method}")


def _response(request_id: object, *, result: object = None, error: SidecarError | None = None) -> dict[str, object]:
    value: dict[str, object] = {"protocol_version": PROTOCOL_VERSION, "id": request_id}
    if error is None:
        value["result"] = result
    else:
        value["error"] = {"code": error.code, "message": str(error)}
    return value


def handle_request(value: object) -> dict[str, object]:
    required = {"protocol_version", "id", "method", "params"}
    if not isinstance(value, dict) or set(value) not in (required, required | {"authentication"}):
        return _response(None, error=SidecarError(
            "INVALID_REQUEST",
            "Request fields must be protocol_version, id, method, params, and optional authentication",
        ))
    request_id = value["id"]
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return _response(None, error=SidecarError("INVALID_REQUEST", "id must be a string or integer"))
    if value["protocol_version"] != PROTOCOL_VERSION:
        return _response(request_id, error=SidecarError("PROTOCOL_MISMATCH", "Unsupported protocol version"))
    method = value["method"]
    if not isinstance(method, str):
        return _response(request_id, error=SidecarError("INVALID_REQUEST", "method must be a string"))
    password = None
    if "authentication" in value:
        authentication = value["authentication"]
        if not isinstance(authentication, dict) or set(authentication) != {"password"}:
            return _response(request_id, error=SidecarError(
                "INVALID_REQUEST", "authentication must contain only password",
            ))
        raw_password = authentication["password"]
        if (
            not isinstance(raw_password, str) or not raw_password or len(raw_password) > 1024
            or any(character in raw_password for character in ("\n", "\r", "\x00"))
        ):
            return _response(request_id, error=SidecarError(
                "INVALID_REQUEST", "SSH password is invalid",
            ))
        password = SecretStr(raw_password)
    try:
        token = _REQUEST_PASSWORD.set(password)
        try:
            return _response(request_id, result=dispatch(method, value["params"]))
        finally:
            _REQUEST_PASSWORD.reset(token)
    except SidecarError as exc:
        return _response(request_id, error=exc)
    except Exception:
        return _response(request_id, error=SidecarError("INTERNAL_ERROR", "The desktop core operation failed"))


def run_stream(source: BinaryIO, destination: TextIO) -> int:
    """Serve newline-delimited requests until EOF without emitting diagnostics."""
    while True:
        line = source.readline(MAX_REQUEST_BYTES + 2)
        if not line:
            return 0
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_REQUEST_BYTES + 2)
            response = _response(None, error=SidecarError("REQUEST_TOO_LARGE", "Request exceeds the size limit or lacks a newline"))
        else:
            try:
                response = handle_request(json.loads(line))
            except (json.JSONDecodeError, UnicodeError):
                response = _response(None, error=SidecarError("INVALID_JSON", "Request is not valid UTF-8 JSON"))
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_RESPONSE_BYTES:
            encoded = json.dumps(_response(
                response.get("id"),
                error=SidecarError("RESPONSE_TOO_LARGE", "Response exceeded the desktop protocol limit"),
            ), ensure_ascii=False, separators=(",", ":"))
        destination.write(encoded + "\n")
        destination.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="beta-easysbatch-core")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.version:
        print(f"{PRODUCT_NAME} core protocol {PROTOCOL_VERSION}")
        return 0
    if args.self_test:
        result = dispatch("health", {})
        if result["submission_enabled"] is not False:
            return 1
        print("ok")
        return 0
    return run_stream(sys.stdin.buffer, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
