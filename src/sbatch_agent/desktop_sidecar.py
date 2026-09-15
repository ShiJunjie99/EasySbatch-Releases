"""Bounded stdio bridge used by the Beta EasySbatch desktop application.

The model-facing surface remains read-only apart from saving a local draft.
User-driven desktop panels may also inspect the cluster, submit an already
reviewed immutable draft, and refresh its status through the credential-free,
fixed-command OpenSSH adapter.
"""

from __future__ import annotations

import argparse
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

from pydantic import ValidationError

from .cluster import ClusterService, ClusterUnavailableError, SlurmClusterClient
from .cluster_models import ClusterSnapshot
from .cluster_profile import ClusterProfile
from .desktop_profiles import managed_profiles, profile_source
from .desktop_ssh import DesktopSSHSlurmRunner, DesktopSSHUnavailableError
from .launcher_client import validate_username
from .models import JobSpec
from .persistence import JobRecord, JobRepository, PersistenceError
from .profiles import StaticProfiles
from .recommendation_models import RecommendationReport, UserPreference
from .recommender import RecommendationInputError, ResourceRecommender
from .renderer import JobSpecValidationError, render_job_script
from .scanner import ProjectScanError, ProjectScanner
from .scanner_models import ProjectEvidence, ScanConfig
from .service import (
    JobNotSubmittedError, JobNotSubmittableError, SubmissionService,
    SubmissionServiceError,
)
from .slurm import SlurmClient, resolve_log_path


PRODUCT_NAME = "Beta EasySbatch"
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_PROFILE_BYTES = 512 * 1024
MAX_CONNECTION_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


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


def _load_cluster_runner(
    path_value: object,
) -> tuple[DesktopSSHSlurmRunner, ClusterProfile, str, str | None]:
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
        ):
            raise ValueError
        profile = ClusterProfile.from_mapping(data["profile"])
        username = validate_username(data["username"])
        fingerprint = data.get("profiles_sha256")
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise ValueError
        return (
            DesktopSSHSlurmRunner(profile=profile, username=username),
            profile,
            username,
            fingerprint,
        )
    except (ValueError, TypeError, DesktopSSHUnavailableError):
        raise SidecarError(
            "CLUSTER_CONFIG_INVALID",
            "The desktop cluster configuration is invalid or system OpenSSH is unavailable",
        ) from None


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


def _submission_service(values: dict[str, object]) -> SubmissionService:
    runner, _, _, expected_profiles = _load_cluster_runner(values["cluster_config_path"])
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


def _compact_scan(evidence: ProjectEvidence) -> dict[str, object]:
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


def dispatch(method: str, params: object) -> dict[str, object]:
    """Execute one allowlisted desktop operation."""
    if method == "health":
        _exact_params(params, set())
        return {
            "product": PRODUCT_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": [
                "scan_project", "validate_job", "render_job", "list_profiles",
                "configure_cluster", "cluster_snapshot", "recommend_job", "create_job", "list_jobs",
                "get_job", "submit_job", "refresh_job",
            ],
            "submission_supported": True,
            # Runtime readiness is reported only after validating the explicit
            # product-owned paths with ``runtime_status``.
            "submission_enabled": False,
        }
    if method == "runtime_status":
        values = _exact_params(params, {"cluster_config_path", "profiles_path"})
        problems = []
        cluster = None
        expected_profiles = None
        try:
            _, cluster, username, expected_profiles = _load_cluster_runner(
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
        return {
            "cluster_configured": cluster is not None,
            "profiles_configured": profiles is not None,
            "submission_enabled": cluster is not None and profiles is not None,
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
                else profile_source(profiles, cluster.host)
            ),
            "problems": problems,
        }
    if method == "configure_cluster":
        values = _exact_params(
            params, {"cluster_config_path", "profiles_path", "profile", "username"},
        )
        try:
            profile = ClusterProfile.from_mapping(values["profile"])
            username = validate_username(values["username"])
        except (ValueError, TypeError):
            raise SidecarError(
                "CLUSTER_CONFIG_INVALID",
                "Cluster name, host, port, or Linux username is invalid",
            ) from None
        profiles, source = managed_profiles(profile.host)
        fingerprint = _write_private_profiles(values["profiles_path"], profiles)
        _write_private_json(values["cluster_config_path"], {
            "profile": profile.to_mapping(),
            "username": username,
            "profiles_sha256": fingerprint,
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
        }
    if method == "scan_project":
        values = _exact_params(params, {"project_dir"})
        project_dir = values["project_dir"]
        if not isinstance(project_dir, str):
            raise SidecarError("INVALID_PARAMS", "project_dir must be a string")
        scanner = ProjectScanner(ScanConfig(
            max_files=500,
            max_total_text_bytes=2 * 1024 * 1024,
            max_directory_entries=1000,
            max_directories=160,
            max_evidence_items=500,
        ))
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
        try:
            script = render_job_script(spec, profiles=profiles)
        except JobSpecValidationError as exc:
            raise SidecarError("JOB_SPEC_NOT_RENDERABLE", "; ".join(exc.issues[:30])) from None
        return {
            "job_spec": spec.model_dump(mode="json"),
            "script": script,
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
    if method == "cluster_snapshot":
        values = _exact_params(params, {"cluster_config_path"})
        runner, _, username, _ = _load_cluster_runner(values["cluster_config_path"])
        try:
            snapshot = ClusterService(
                SlurmClusterClient(runner=runner), current_user=username,
            ).get_snapshot()
        except (ClusterUnavailableError, SubmissionServiceError):
            raise SidecarError(
                "CLUSTER_UNAVAILABLE",
                "The cluster snapshot is unavailable; verify network, host key, and system OpenSSH authentication",
            ) from None
        return _snapshot_view(snapshot)
    if method == "recommend_job":
        values = _exact_params(params, {"job_spec", "profiles_path", "cluster_config_path", "preference"})
        try:
            spec = JobSpec.model_validate(values["job_spec"])
            preference = UserPreference(values["preference"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        except (ValueError, TypeError):
            raise SidecarError("INVALID_PARAMS", "preference is invalid") from None
        runner, _, username, expected_profiles = _load_cluster_runner(
            values["cluster_config_path"],
        )
        profiles = _load_profiles(
            values["profiles_path"], expected_sha256=expected_profiles,
        )
        try:
            snapshot = ClusterService(
                SlurmClusterClient(runner=runner), current_user=username,
            ).get_snapshot()
            report = ResourceRecommender().recommend(
                spec=spec, snapshot=snapshot, profiles=profiles,
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
        values = _exact_params(params, {"job_spec", "name", "profiles_path", "database_path", "submission_root"})
        name = values["name"]
        if name is not None and (not isinstance(name, str) or not name.strip() or not name.isprintable()):
            raise SidecarError("INVALID_PARAMS", "name must be null or printable nonblank text")
        try:
            spec = JobSpec.model_validate(values["job_spec"])
        except ValidationError as exc:
            raise SidecarError("JOB_SPEC_INVALID", _validation_message(exc)) from None
        profiles = _load_profiles(values["profiles_path"])
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
    if not isinstance(value, dict) or set(value) != {"protocol_version", "id", "method", "params"}:
        return _response(None, error=SidecarError("INVALID_REQUEST", "Request fields must be protocol_version, id, method, and params"))
    request_id = value["id"]
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        return _response(None, error=SidecarError("INVALID_REQUEST", "id must be a string or integer"))
    if value["protocol_version"] != PROTOCOL_VERSION:
        return _response(request_id, error=SidecarError("PROTOCOL_MISMATCH", "Unsupported protocol version"))
    method = value["method"]
    if not isinstance(method, str):
        return _response(request_id, error=SidecarError("INVALID_REQUEST", "method must be a string"))
    try:
        return _response(request_id, result=dispatch(method, value["params"]))
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
