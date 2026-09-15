import io
import json
from pathlib import Path
import sys

import yaml

from sbatch_agent.desktop_sidecar import PROTOCOL_VERSION, dispatch, handle_request, run_stream


ROOT = Path(__file__).resolve().parents[1]


def request(method, params, request_id="test"):
    return {
        "protocol_version": PROTOCOL_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    }


def test_health_is_explicitly_non_submitting():
    result = dispatch("health", {})
    assert result["product"] == "Beta EasySbatch"
    assert result["submission_enabled"] is False
    assert "render_job" in result["capabilities"]


def test_scan_is_bounded_and_returns_current_project_evidence(tmp_path):
    (tmp_path / "README.md").write_text("Run with `python train.py --epochs 3`.\n", encoding="utf-8")
    (tmp_path / "train.py").write_text("print('ok')\n", encoding="utf-8")
    result = dispatch("scan_project", {"project_dir": str(tmp_path)})
    assert result["project_dir"] == str(tmp_path)
    assert result["summary"]["files_considered"] == 2
    assert "project_type_candidates" in result["candidates"]
    assert "files" not in result


def test_windows_compatible_scanner_path_has_equivalent_evidence(tmp_path):
    from sbatch_agent.scanner import ProjectScanner

    (tmp_path / "README.md").write_text("```bash\npython train.py\n```\n", encoding="utf-8")
    (tmp_path / "train.py").write_text("print('ok')\n", encoding="utf-8")
    evidence = ProjectScanner()._scan_portable(tmp_path)
    assert evidence.project_type_candidates[0].value == "python"
    assert evidence.entrypoint_candidates[0].value == "train.py"
    assert evidence.files_considered == 2


def test_windows_compatible_scanner_rejects_linked_root(tmp_path):
    import pytest
    from sbatch_agent.scanner import ProjectScanError, ProjectScanner

    project = tmp_path / "project"
    project.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(project, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ProjectScanError):
        ProjectScanner()._scan_portable(linked)


def test_validate_job_rejects_coercion_and_extra_fields(tmp_path):
    response = handle_request(request("validate_job", {"job_spec": {
        "project_dir": str(tmp_path),
        "work_dir": "/cluster/work",
        "run_type": "python",
        "entrypoint": "train.py",
        "environment_profile": {"id": "python", "version": "1"},
        "run_step": {"executable": "python", "args": ["train.py"]},
        "resources": {"partition": "cpu", "memory_mib": "128", "time_limit_seconds": 60},
        "spec_version": 1,
    }}))
    assert response["error"]["code"] == "JOB_SPEC_INVALID"
    assert "memory_mib" in response["error"]["message"]


def test_render_uses_fixed_profile_document_and_never_submits(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(yaml.safe_dump({
        "environments": [{"id": "python", "version": "1", "load_steps": []}],
        "launchers": [],
    }), encoding="utf-8")
    job = {
        "project_dir": "/cluster/project",
        "work_dir": "/cluster/project",
        "run_type": "python",
        "entrypoint": "train.py",
        "environment_profile": {"id": "python", "version": "1"},
        "run_step": {"executable": sys.executable, "args": ["train.py"]},
        "resources": {"partition": "cpu", "memory_mib": 128, "time_limit_seconds": 60},
        "spec_version": 1,
    }
    result = dispatch("render_job", {"job_spec": job, "profiles_path": str(profiles)})
    assert result["script"].startswith("#!/usr/bin/env bash\n")
    assert "#SBATCH --partition=cpu" in result["script"]
    assert result["submission_enabled"] is False


def test_duplicate_profile_keys_are_rejected(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("environments: []\nenvironments: []\n", encoding="utf-8")
    job = yaml.safe_load((ROOT / "examples/rendering/python.yaml").read_text(encoding="utf-8"))
    response = handle_request(request("render_job", {"job_spec": job, "profiles_path": str(profiles)}))
    assert response["error"]["code"] == "PROFILE_INVALID"


def test_ndjson_loop_recovers_after_bad_json():
    source = io.BytesIO(b"not-json\n" + json.dumps(request("health", {})).encode() + b"\n")
    output = io.StringIO()
    assert run_stream(source, output) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == "INVALID_JSON"
    assert responses[1]["result"]["submission_enabled"] is False


def test_ndjson_loop_drains_one_oversized_record():
    oversized = b"{" + (b"x" * (1024 * 1024 + 10)) + b"}\n"
    source = io.BytesIO(oversized + json.dumps(request("health", {})).encode() + b"\n")
    output = io.StringIO()
    assert run_stream(source, output) == 0
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item.get("error", {}).get("code") for item in responses] == ["REQUEST_TOO_LARGE", None]
    assert responses[1]["result"]["submission_enabled"] is False


def test_protocol_rejects_unknown_fields_and_methods():
    malformed = request("health", {}) | {"extra": True}
    assert handle_request(malformed)["error"]["code"] == "INVALID_REQUEST"
    assert handle_request(request("submit_job", {}))["error"]["code"] == "METHOD_NOT_FOUND"
