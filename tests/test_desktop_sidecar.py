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
    assert result["submission_supported"] is True
    assert "render_job" in result["capabilities"]
    assert "recommend_job" in result["capabilities"]
    assert "submit_job" in result["capabilities"]


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


def test_create_list_and_get_reviewed_desktop_job(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(yaml.safe_dump({
        "environments": [{"id": "python", "version": "1", "load_steps": []}],
        "launchers": [],
    }), encoding="utf-8")
    database = tmp_path / "state" / "jobs.sqlite3"
    runs = tmp_path / "state" / "runs"
    job = {
        "project_dir": "/cluster/project",
        "work_dir": "/cluster/project",
        "run_type": "python",
        "entrypoint": "train.py",
        "environment_profile": {"id": "python", "version": "1"},
        "run_step": {"executable": "python", "args": ["train.py"]},
        "resources": {"partition": "cpu", "memory_mib": 256, "time_limit_seconds": 120},
        "spec_version": 1,
        "job_name": "desktop-test",
        "stdout": "logs/%j.out",
        "stderr": "logs/%j.err",
    }
    created = dispatch("create_job", {
        "job_spec": job,
        "name": "Desktop test",
        "profiles_path": str(profiles),
        "database_path": str(database),
        "submission_root": str(runs),
    })
    assert created["submission_state"] == "SCRIPT_RENDERED"
    assert created["name"] == "Desktop test"
    assert "#SBATCH --partition=cpu" in created["rendered_script"]

    listed = dispatch("list_jobs", {"database_path": str(database), "limit": 100})
    assert [item["id"] for item in listed["jobs"]] == [created["id"]]
    assert "rendered_script" not in listed["jobs"][0]

    detail = dispatch("get_job", {"database_path": str(database), "record_id": created["id"]})
    assert detail["job_spec"]["entrypoint"] == job["entrypoint"]
    assert detail["job_spec"]["resources"]["memory_mib"] == 256
    assert detail["stdout_path"] == "logs/%j.out"


def test_submit_requires_exact_user_confirmation_before_cluster_access(tmp_path):
    response = handle_request(request("submit_job", {
        "record_id": "3a9a8fd8-b8cc-49ba-9238-a673158b06b2",
        "confirmation": "different-record",
        "profiles_path": str(tmp_path / "profiles.yaml"),
        "database_path": str(tmp_path / "jobs.sqlite3"),
        "submission_root": str(tmp_path / "runs"),
        "cluster_config_path": str(tmp_path / "cluster.json"),
    }))
    assert response["error"]["code"] == "SUBMISSION_CONFIRMATION_REQUIRED"


def test_cluster_configuration_contains_no_credentials_and_enables_runtime_metadata(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text("environments: []\nlaunchers: []\n", encoding="utf-8")
    cluster = tmp_path / "state" / "cluster.json"
    saved = dispatch("configure_cluster", {
        "cluster_config_path": str(cluster),
        "profile": {
            "id": "primary",
            "display_name": "Synthetic cluster",
            "host": "cluster.example.edu",
            "ssh_port": 22,
        },
        "username": "student",
    })
    assert saved["credentials_stored"] is False
    stored = json.loads(cluster.read_text(encoding="utf-8"))
    assert stored == {
        "profile": {
            "id": "primary",
            "display_name": "Synthetic cluster",
            "host": "cluster.example.edu",
            "ssh_port": 22,
        },
        "username": "student",
    }
    status = dispatch("runtime_status", {
        "cluster_config_path": str(cluster),
        "profiles_path": str(profiles),
    })
    assert status["submission_enabled"] is True
    assert status["cluster"]["username"] == "student"


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
    assert handle_request(request("delete_job", {}))["error"]["code"] == "METHOD_NOT_FOUND"
