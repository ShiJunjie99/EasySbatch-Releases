"""Bounded stdio bridge used by the Beta EasySbatch desktop application.

The bridge is deliberately smaller than the existing Web service. It exposes
read-only project inspection and deterministic JobSpec validation/rendering;
it never starts a shell, opens SSH, contacts Slurm, or submits a job.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import BinaryIO, TextIO

from pydantic import ValidationError

from .models import JobSpec
from .profiles import StaticProfiles
from .renderer import JobSpecValidationError, render_job_script
from .scanner import ProjectScanError, ProjectScanner
from .scanner_models import ProjectEvidence, ScanConfig


PRODUCT_NAME = "Beta EasySbatch"
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_PROFILE_BYTES = 512 * 1024
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


def _read_regular_file(path_value: object, *, limit: int) -> bytes:
    if not isinstance(path_value, str) or not path_value.strip() or not path_value.isprintable():
        raise SidecarError("INVALID_PARAMS", "profiles_path must be a printable absolute path")
    path = Path(path_value)
    if not path.is_absolute() or ".." in path.parts:
        raise SidecarError("INVALID_PARAMS", "profiles_path must be absolute and cannot contain '..'")
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


def _load_profiles(path_value: object) -> StaticProfiles:
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
        data = yaml.load(_read_regular_file(path_value, limit=MAX_PROFILE_BYTES), Loader=UniqueSafeLoader)
        return StaticProfiles.model_validate(data)
    except SidecarError:
        raise
    except (UnicodeError, yaml.YAMLError, ValidationError, TypeError, ValueError, RecursionError):
        raise SidecarError("PROFILE_INVALID", "The configured profile file is not a valid StaticProfiles document") from None


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
    """Execute one allowlisted, non-submitting desktop operation."""
    if method == "health":
        _exact_params(params, set())
        return {
            "product": PRODUCT_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": ["scan_project", "validate_job", "render_job"],
            "submission_enabled": False,
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
