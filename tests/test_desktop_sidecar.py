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
    reviewed = dispatch("render_job", {
        "job_spec": job, "profiles_path": str(profiles),
    })
    created = dispatch("create_job", {
        "job_spec": job,
        "name": "Desktop test",
        "review_sha256": reviewed["review_sha256"],
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


def test_changed_job_cannot_be_saved_with_an_old_review_token(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(yaml.safe_dump({
        "environments": [{"id": "python", "version": "1", "load_steps": []}],
        "launchers": [],
    }), encoding="utf-8")
    job = {
        "project_dir": "/cluster/project", "work_dir": "/cluster/project",
        "run_type": "python", "entrypoint": "train.py",
        "environment_profile": {"id": "python", "version": "1"},
        "run_step": {"executable": "python", "args": ["train.py"]},
        "resources": {"partition": "cpu", "memory_mib": 256, "time_limit_seconds": 120},
        "spec_version": 1,
    }
    reviewed = dispatch("render_job", {"job_spec": job, "profiles_path": str(profiles)})
    job["resources"]["cpus_per_task"] = 2
    response = handle_request(request("create_job", {
        "job_spec": job, "name": "Changed", "review_sha256": reviewed["review_sha256"],
        "profiles_path": str(profiles), "database_path": str(tmp_path / "jobs.sqlite3"),
        "submission_root": str(tmp_path / "runs"),
    }))
    assert response["error"]["code"] == "REVIEW_CHANGED"


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
    catalog = tmp_path / "server-catalog.yaml"
    cluster = tmp_path / "state" / "cluster.json"
    saved = dispatch("configure_cluster", {
        "cluster_config_path": str(cluster),
        "profiles_path": str(profiles),
        "catalog_path": str(catalog),
        "profile": {
            "id": "primary",
            "display_name": "Synthetic cluster",
            "host": "cluster.example.edu",
            "ssh_port": 22,
        },
        "username": "student",
    })
    assert saved["credentials_stored"] is False
    assert saved["profile_source"] == "cluster_discovery"
    assert saved["profile_counts"] == {"environments": 1, "launchers": 0}
    assert saved["catalog_counts"] == {"environments": 0, "software": 0, "compilers": 0}
    assert yaml.safe_load(profiles.read_text(encoding="utf-8")) == {
        "environments": [{
            "id": "cluster-default",
            "version": "1",
            "load_steps": [],
            "allowed_partitions": None,
            "resource_options": [],
            "analysis_capabilities": None,
            "resource_rules": [],
        }],
        "launchers": [],
    }
    stored = json.loads(cluster.read_text(encoding="utf-8"))
    assert stored["profile"] == {
        "id": "primary",
        "display_name": "Synthetic cluster",
        "host": "cluster.example.edu",
        "ssh_port": 22,
    }
    assert stored["username"] == "student"
    assert len(stored["profiles_sha256"]) == 64
    int(stored["profiles_sha256"], 16)
    assert len(stored["catalog_sha256"]) == 64
    int(stored["catalog_sha256"], 16)
    status = dispatch("runtime_status", {
        "cluster_config_path": str(cluster),
        "profiles_path": str(profiles),
        "catalog_path": str(catalog),
    })
    assert status["submission_enabled"] is True
    assert status["cluster"]["username"] == "student"
    assert status["profile_source"] == "cluster_discovery"


def test_runtime_status_reports_missing_first_run_configuration(tmp_path):
    status = dispatch("runtime_status", {
        "cluster_config_path": str(tmp_path / "cluster.json"),
        "profiles_path": str(tmp_path / "profiles.yaml"),
        "catalog_path": str(tmp_path / "server-catalog.yaml"),
    })
    assert status["cluster_configured"] is False
    assert status["profiles_configured"] is False
    assert status["catalog_configured"] is False
    assert status["submission_enabled"] is False
    assert status["profile_source"] is None


def test_known_cluster_automatically_uses_shared_audited_profiles_only(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    catalog = tmp_path / "server-catalog.yaml"
    cluster = tmp_path / "cluster.json"
    saved = dispatch("configure_cluster", {
        "cluster_config_path": str(cluster),
        "profiles_path": str(profiles),
        "catalog_path": str(catalog),
        "profile": {
            "id": "primary",
            "display_name": "Known GPU cluster",
            "host": "10.158.132.77",
            "ssh_port": 22,
        },
        "username": "student",
    })
    assert saved["profile_source"] == "known_cluster"
    configured = dispatch("list_profiles", {"profiles_path": str(profiles)})
    identifiers = {item["id"] for item in configured["environments"]}
    assert identifiers == {
        "system-python312", "shared-conda-base", "shared-sci",
        "shared-pygamd", "system-toolchain", "gromacs-2026",
    }
    assert "user-dpd-pygamd" not in identifiers
    assert "newtorch" not in identifiers
    configured_catalog = dispatch("list_catalog", {
        "profiles_path": str(profiles),
        "catalog_path": str(catalog),
    })
    assert {item["id"] for item in configured_catalog["software"]} == {
        "gromacs-2026", "tops-2020", "scft-2026",
    }
    assert configured_catalog["software"][0]["verification_status"] == "VERIFIED"
    dumped_catalog = catalog.read_text(encoding="utf-8")
    assert "/home/shijunjie/" not in dumped_catalog
    assert "/mnt/sdc/" not in dumped_catalog
    assert "newtorch" not in dumped_catalog
    gromacs_job = {
        "project_dir": "/home/student/project", "work_dir": "/home/student/project",
        "run_type": "installed", "entrypoint": "gromacs-2026",
        "environment_profile": {"id": "gromacs-2026", "version": "1"},
        "run_step": {
            "executable": next(
                item["executable"] for item in configured_catalog["software"]
                if item["id"] == "gromacs-2026"
            ),
            "args": ["--version"],
        },
        "resources": {"partition": "cpu", "memory_mib": 256, "time_limit_seconds": 120},
        "spec_version": 1,
    }
    reviewed = dispatch("review_job", {
        "job_spec": gromacs_job, "profiles_path": str(profiles),
        "catalog_path": str(catalog), "software_id": "gromacs-2026",
    })
    assert len(reviewed["review_sha256"]) == 64
    assert reviewed["software"]["verification_status"] == "VERIFIED"
    gromacs_job["run_step"]["executable"] = "/usr/bin/false"
    mismatch = handle_request(request("review_job", {
        "job_spec": gromacs_job, "profiles_path": str(profiles),
        "catalog_path": str(catalog), "software_id": "gromacs-2026",
    }))
    assert mismatch["error"]["code"] == "CATALOG_MISMATCH"
    assert dispatch("runtime_status", {
        "cluster_config_path": str(cluster),
        "profiles_path": str(profiles),
        "catalog_path": str(catalog),
    })["profile_source"] == "known_cluster"


def test_managed_profiles_cannot_change_silently_after_cluster_configuration(tmp_path):
    profiles = tmp_path / "profiles.yaml"
    catalog = tmp_path / "server-catalog.yaml"
    cluster = tmp_path / "cluster.json"
    dispatch("configure_cluster", {
        "cluster_config_path": str(cluster),
        "profiles_path": str(profiles),
        "catalog_path": str(catalog),
        "profile": {
            "id": "primary",
            "display_name": "Synthetic cluster",
            "host": "cluster.example.edu",
            "ssh_port": 22,
        },
        "username": "student",
    })
    profiles.write_text("environments: []\nlaunchers: []\n", encoding="utf-8")
    status = dispatch("runtime_status", {
        "cluster_config_path": str(cluster),
        "profiles_path": str(profiles),
        "catalog_path": str(catalog),
    })
    assert status["cluster_configured"] is True
    assert status["profiles_configured"] is False
    assert status["submission_enabled"] is False
    assert any("changed after the cluster was saved" in item for item in status["problems"])


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


def test_user_driven_remote_directory_browse_uses_bounded_runner(monkeypatch):
    class SyntheticRunner:
        def list_directory(self, path, *, timeout):
            assert path == "/home/student/project"
            assert timeout == 15
            return {
                "path": path,
                "entries": [{
                    "name": "inputs", "kind": "directory", "size": None,
                    "modified_ns": 1,
                }],
                "truncated": False,
            }

    monkeypatch.setattr(
        "sbatch_agent.desktop_sidecar._load_cluster_runner",
        lambda _path: (SyntheticRunner(), None, "student", None, None),
    )
    result = dispatch("browse_remote_directory", {
        "cluster_config_path": "/synthetic/cluster.json",
        "path": "/home/student/project",
    })
    assert result["entries"][0]["name"] == "inputs"


def test_desktop_recommendation_can_compare_all_visible_partitions(tmp_path, monkeypatch):
    profiles = tmp_path / "profiles.yaml"
    catalog = tmp_path / "catalog.yaml"
    cluster = tmp_path / "cluster.json"
    dispatch("configure_cluster", {
        "cluster_config_path": str(cluster), "profiles_path": str(profiles),
        "catalog_path": str(catalog),
        "profile": {
            "id": "primary", "display_name": "Synthetic",
            "host": "cluster.example.edu", "ssh_port": 22,
        },
        "username": "student",
    })
    fingerprints = json.loads(cluster.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        "sbatch_agent.desktop_sidecar._load_cluster_runner",
        lambda _path: (
            object(), None, "student", fingerprints["profiles_sha256"],
            fingerprints["catalog_sha256"],
        ),
    )

    class SyntheticClusterService:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_snapshot(self):
            return object()

    captured = {}

    class SyntheticRecommender:
        def recommend(self, *, spec, **_kwargs):
            captured["partition"] = spec.resources.partition
            return object()

    monkeypatch.setattr("sbatch_agent.desktop_sidecar.ClusterService", SyntheticClusterService)
    monkeypatch.setattr("sbatch_agent.desktop_sidecar.ResourceRecommender", SyntheticRecommender)
    monkeypatch.setattr("sbatch_agent.desktop_sidecar._recommendation_view", lambda _report: {"ok": True})
    result = dispatch("recommend_job", {
        "job_spec": {
            "project_dir": "/home/student/project", "work_dir": "/home/student/project",
            "run_type": "python", "entrypoint": "train.py",
            "environment_profile": {"id": "cluster-default", "version": "1"},
            "run_step": {"executable": "python", "args": ["train.py"]},
            "resources": {"partition": "first", "memory_mib": 256, "time_limit_seconds": 120},
            "spec_version": 1,
        },
        "profiles_path": str(profiles), "catalog_path": str(catalog),
        "cluster_config_path": str(cluster), "preference": "BALANCED",
        "software_id": None, "consider_all_partitions": True,
    })
    assert result == {"ok": True}
    assert captured["partition"] is None


def test_remote_scan_drives_evidence_based_values_and_revisioned_preparation(tmp_path, monkeypatch):
    import stat

    profiles = tmp_path / "profiles.yaml"
    catalog = tmp_path / "catalog.yaml"
    cluster = tmp_path / "cluster.json"
    dispatch("configure_cluster", {
        "cluster_config_path": str(cluster), "profiles_path": str(profiles),
        "catalog_path": str(catalog),
        "profile": {
            "id": "primary", "display_name": "Synthetic",
            "host": "cluster.example.edu", "ssh_port": 22,
        },
        "username": "student",
    })
    script = (
        b"#!/usr/bin/env bash\n#SBATCH --nodes=1\n#SBATCH --ntasks=1\n"
        b"#SBATCH --cpus-per-task=1\n#SBATCH --mem=512M\n"
        b"#SBATCH --time=00:10:00\npython train.py\n"
    )
    current_script = [script]

    class SyntheticRunner:
        def scan_project(self, path, *, timeout):
            assert path == "/home/student/project"
            assert timeout == 30
            content = current_script[0]
            return {
                "path": path,
                "files": [{
                    "path": "run.sbatch", "size": len(content), "status": "read",
                    "reason": None, "mode": stat.S_IFREG | 0o755, "data": content,
                }],
                "skipped_directories": [], "git_present": False,
                "bytes_read": len(content), "warnings": [], "limits_reached": [],
            }

    monkeypatch.setattr(
        "sbatch_agent.desktop_sidecar._load_cluster_runner",
        lambda _path: (SyntheticRunner(), None, "student", None, None),
    )
    state = tmp_path / "desktop-state.sqlite3"
    scanned = dispatch("scan_remote_project", {
        "cluster_config_path": str(cluster), "state_database_path": str(state),
        "path": "/home/student/project",
    })
    assert scanned["scan_id"]
    assert scanned["summary"]["bytes_read"] == len(script)

    job = {
        "project_dir": "/home/student/project", "work_dir": "/home/student/project",
        "run_type": "python", "entrypoint": "train.py",
        "environment_profile": {"id": "cluster-default", "version": "1"},
        "run_step": {"executable": "python", "args": ["train.py"]},
        "resources": {
            "partition": "cpu", "memory_mib": None, "time_limit_seconds": None,
            "memory_policy": {"mode": "cluster_default"},
            "walltime_policy": {"mode": "cluster_default"},
        },
        "unresolved": [{"field": "account", "reason": "Confirm the billing account"}],
        "spec_version": 1,
    }
    recommended = dispatch("recommend_resource_values", {
        "job_spec": job, "profiles_path": str(profiles), "catalog_path": str(catalog),
        "state_database_path": str(state), "software_id": None,
        "scan_id": scanned["scan_id"],
    })
    assert recommended["recommendations"]["memory_mib"]["value"] == 512
    assert recommended["recommendations"]["time_limit_seconds"]["value"] == 600
    assert recommended["recommendations"]["memory_mib"]["evidence"]["status"] == "DIRECT"

    started = dispatch("start_preparation", {
        "job_spec": job, "name": "Prepared run", "software_id": None,
        "scan_id": scanned["scan_id"], "profiles_path": str(profiles),
        "catalog_path": str(catalog), "state_database_path": str(state),
    })
    assert started["state"] == "NEEDS_INPUT"
    assert started["revision"] == 1
    assert started["rendered_script"] is None
    job["unresolved"] = []
    revised = dispatch("revise_preparation", {
        "preparation_id": started["id"], "revision": started["revision"],
        "job_spec": job, "name": "Prepared run", "software_id": None,
        "scan_id": scanned["scan_id"], "profiles_path": str(profiles),
        "catalog_path": str(catalog), "state_database_path": str(state),
    })
    assert revised["state"] == "READY_TO_SAVE"
    assert revised["revision"] == 2
    assert revised["rendered_script"].startswith("#!/usr/bin/env bash")
    assert revised["job_spec"]["source_fingerprints"]

    finalize_params = {
        "preparation_id": revised["id"], "revision": revised["revision"],
        "state_database_path": str(state), "profiles_path": str(profiles),
        "catalog_path": str(catalog), "cluster_config_path": str(cluster),
        "database_path": str(tmp_path / "jobs.sqlite3"),
        "submission_root": str(tmp_path / "runs"),
    }
    current_script[0] = script + b"# changed\n"
    changed = handle_request(request("finalize_preparation", finalize_params))
    assert changed["error"]["code"] == "PROJECT_CHANGED"
    current_script[0] = script
    finalized = dispatch("finalize_preparation", finalize_params)
    assert finalized["preparation"]["state"] == "SAVED"
    assert finalized["job"]["submission_state"] == "SCRIPT_RENDERED"
