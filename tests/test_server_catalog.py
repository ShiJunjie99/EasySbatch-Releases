"""M8-A contract: fixture facts only; no process, network, Slurm or discovery."""

from copy import deepcopy
from pathlib import Path
import socket
import subprocess

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sbatch_agent.analysis_models import EnvironmentRequirements
from sbatch_agent.analyzer import AIProjectAnalyzer
from sbatch_agent.environment_resolver import EnvironmentResolver
from sbatch_agent.model_client import AnalysisOutputValidationError
from sbatch_agent.models import EnvironmentProfile
from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.scanner import ProjectScanner
from sbatch_agent.server_catalog import CatalogError, ServerCatalog
from sbatch_agent.smart_models import FieldSource, PreparationValues
from sbatch_agent.smart_service import SmartJobService
from sbatch_agent.software_resolver import SoftwareResolver
from sbatch_agent.web import WebConfig, create_app
from sbatch_agent.web_forms import DEFAULT_FORM, apply_catalog_to_form, profile_key
from test_analyzer import FakeModelClient, proposed, ref
from test_recommender import NOW
from test_smart_service import FakeCluster
from test_smart_web import act, count
from test_web import FakeSlurmClient, post, form, profiles


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Catalog tests must not access software/processes/network")
    for obj, name in ((subprocess, "Popen"), (socket, "create_connection"), (socket.socket, "connect")):
        monkeypatch.setattr(obj, name, forbidden)
    for name in ("SBATCH_AGENT_AI_PROVIDER", "SBATCH_AGENT_SERVER_CATALOG_PATH"):
        monkeypatch.delenv(name, raising=False)


def registry():
    return StaticProfiles.model_validate({"environments": [
        {"id": "gmx-env", "version": "1", "load_steps": []},
        {"id": "alternative", "version": "1", "load_steps": []},
    ]})


def fact(identifier, **kwargs):
    return {"id": identifier, "display_name": identifier, "version": "1",
            "source": {"document": "fixture audit", "section": "test", "detail": "Offline example"},
            "verification_status": "VERIFIED", "last_verified_at": NOW,
            "verification_scope": "Fixture version query only; not a compute test", **kwargs}


def payload():
    return {"metadata": {"description": "Offline test catalog", "source_document": "fixture audit"},
            "environments": [fact("gmx-env", type="module", environment_profile={"id": "gmx-env", "version": "1"})],
            "software": [fact("gromacs", display_name="GROMACS", aliases=["gmx", "gmx_mpi"],
                              executable="/opt/example/gromacs/bin/gmx_mpi",
                              environment_profile={"id": "gmx-env", "version": "1"}, parallelism=["threads", "mpi", "gpu"])],
            "compilers": [fact("gcc", kind="c", executable="/usr/bin/gcc")]}


def catalog(data=None):
    return ServerCatalog.model_validate(data or payload()).validate_profiles(registry())


def load(tmp_path, data):
    target = tmp_path / "catalog.yaml"
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    return ServerCatalog.load(target, profiles=registry())


def test_valid_catalog_yaml_roundtrip_and_example(tmp_path):
    assert load(tmp_path, payload()) == catalog()
    root = Path(__file__).resolve().parents[1]
    profiles = StaticProfiles.model_validate(yaml.safe_load((root / "config/server_profiles.example.yaml").read_text()))
    public = ServerCatalog.load(root / "config/server_catalog.example.yaml", profiles=profiles)
    assert public.software[0].verification_status == "DOCUMENTED"
    assert public.software[0].last_verified_at is None


@pytest.mark.parametrize("group", ["software", "environments", "compilers"])
def test_duplicate_id(tmp_path, group):
    data = payload()
    data[group].append(deepcopy(data[group][0]))
    with pytest.raises(CatalogError):
        load(tmp_path, data)


@pytest.mark.parametrize("alias", ["gMx", "GROMACS"])
def test_duplicate_alias_including_canonical_collision(tmp_path, alias):
    data = payload()
    data["software"].append({**deepcopy(data["software"][0]), "id": "second", "aliases": [alias]})
    with pytest.raises(CatalogError):
        load(tmp_path, data)


@pytest.mark.parametrize("reference", ["environment_profile", "launch_profile"])
def test_missing_profile_reference(tmp_path, reference):
    data = payload()
    data["software"][0][reference] = {"id": "missing", "version": "1"}
    with pytest.raises(CatalogError):
        load(tmp_path, data)


def test_software_environment_requires_catalog_entry(tmp_path):
    data = payload()
    data["environments"] = []
    with pytest.raises(CatalogError):
        load(tmp_path, data)


@pytest.mark.parametrize("path", ["../foo", "/opt/../foo", "~/foo", "$PATH/foo", "/opt/$FOO", "/opt//foo", "/", "gmx --help", "/opt/foo\nbar"])
def test_invalid_executable_path(tmp_path, path):
    data = payload()
    data["software"][0]["executable"] = path
    with pytest.raises(CatalogError):
        load(tmp_path, data)


@pytest.mark.parametrize("field", ["api_key", "password", "unknown"])
def test_unknown_fields_rejected_without_value_echo(tmp_path, field):
    data = payload()
    data["software"][0][field] = "SECRET-SENTINEL"
    with pytest.raises(CatalogError) as error:
        load(tmp_path, data)
    assert "SECRET-SENTINEL" not in str(error.value)


@pytest.mark.parametrize("status", ["VERIFIED", "DOCUMENTED", "INFERRED", "UNVERIFIED"])
def test_verification_enum_preserved(tmp_path, status):
    data = payload()
    data["software"][0]["verification_status"] = status
    assert load(tmp_path, data).software[0].verification_status == status


@pytest.mark.parametrize("changes", [{"verification_status": "verified"}, {"last_verified_at": None},
                                     {"last_verified_at": "2026-09-08T12:00:00"}])
def test_invalid_verification(tmp_path, changes):
    data = payload()
    data["software"][0].update(changes)
    with pytest.raises(CatalogError):
        load(tmp_path, data)


def test_duplicate_yaml_key_and_size_limit(tmp_path):
    path = tmp_path / "bad.yaml"
    for text in ("software: []\nsoftware: []", " " * (512 * 1024 + 1)):
        path.write_text(text)
        with pytest.raises(CatalogError):
            ServerCatalog.load(path, profiles=registry())


@pytest.mark.parametrize("name", ["gromacs", "gmx", "GmX", "GROMACS"])
def test_exact_software_resolution(name):
    result = SoftwareResolver().resolve(name, catalog())
    assert result.status == "MATCHED" and result.choices[0].id == "gromacs"


@pytest.mark.parametrize("name", ["unknown", "grom", "/guess/gmx", "GROMACS please"])
def test_no_fuzzy_or_path_matching(name):
    assert SoftwareResolver().resolve(name, catalog()).status == "NO_MATCH"


def multiple_catalog():
    data = payload()
    data["environments"].append(fact("alternative", type="module", environment_profile={"id": "alternative", "version": "1"}))
    data["software"][0].update(id="gromacs-old", aliases=["gmx-old"])
    data["software"].append({**deepcopy(data["software"][0]), "id": "gromacs-new", "aliases": ["gmx-new"],
                              "version": "99", "environment_profile": {"id": "alternative", "version": "1"}})
    return catalog(data)


def test_multiple_display_names_no_version_preference():
    result = SoftwareResolver().resolve("GROMACS", multiple_catalog())
    assert result.status == "MULTIPLE" and len(result.choices) == 2
    req = EnvironmentRequirements(software=["GROMACS"])
    assert EnvironmentResolver(multiple_catalog()).resolve(req, registry()).status == "MULTIPLE"


def test_requirement_to_unique_catalog_environment():
    req = EnvironmentRequirements(software=["gMx"])
    result = EnvironmentResolver(catalog()).resolve(req, registry())
    assert result.status == "MATCHED" and result.choices[0].id == "gmx-env"
    assert EnvironmentResolver().resolve(req, registry()).status == "NO_MATCH"


@pytest.mark.parametrize("status", ["INFERRED", "UNVERIFIED"])
def test_untrusted_software_cannot_auto_resolve_environment(status):
    data = payload()
    data["software"][0]["verification_status"] = status
    assert EnvironmentResolver(catalog(data)).resolve(EnvironmentRequirements(software=["gmx"]), registry()).status == "NO_MATCH"


def test_python_environment_needs_capability_and_preserves_multiple():
    data = payload()
    data["environments"] = [fact(name, type="system_python", python_executable="/usr/bin/python3",
                                environment_profile={"id": name, "version": "1"},
                                capabilities={"python_version": "3.12.1", "dependencies": ["numpy"]})
                            for name in ("gmx-env", "alternative")]
    resolver = EnvironmentResolver(catalog(data))
    assert resolver.resolve(None, registry(), python_required=True).status == "MULTIPLE"
    assert resolver.resolve(EnvironmentRequirements(dependencies=["scipy"]), registry(), python_required=True).status == "NO_MATCH"
    assert resolver.resolve(EnvironmentRequirements(python_min_version="3.13"), registry(), python_required=True).status == "NO_MATCH"


def setup_catalog_smart(tmp_path, cat=None, *, invocation="gmx", bad_path=False):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text(f"# GROMACS\n```bash\n{invocation} --version\n```\n")
    (root / "smoke.sbatch").write_text("#!/bin/bash\n#SBATCH --mem=256M\n#SBATCH --time=00:02:00\n")
    evidence = ProjectScanner().scan(root)
    refs = ref(evidence, "command_text", source="README.md")
    output = {"draft": {
        "run_type": proposed("installed", ref(evidence, "run_type"), "INFERRED"),
        "entrypoint": proposed({"kind": "command", "value": invocation}, refs, "INFERRED"),
        "run_step": {"executable": proposed("/guess/gmx" if bad_path else invocation, refs),
                     "args": proposed(["--version"], refs)},
        "environment_requirements": proposed({"software": ["GROMACS"]}, ref(evidence, "readme")),
        "resource_requirements": {"memory_mib": proposed(256, ref(evidence, "sbatch.mem")),
                                  "time_limit_seconds": proposed(120, ref(evidence, "sbatch.time"))}}}
    profiles = registry()
    model = FakeModelClient(output)
    analyzer = AIProjectAnalyzer(model_client=model, profiles=profiles)
    smart = SmartJobService(analyzer=analyzer, profiles=profiles, cluster_service=FakeCluster(),
                            catalog=cat, clock=lambda: NOW)
    return smart, root, model


def test_smart_auto_software_environment_executable_and_manual_reduction(tmp_path):
    old, root, _ = setup_catalog_smart(tmp_path)
    before = old.prepare(project_dir=root, task_intent="GROMACS version smoke")
    smart, root, model = setup_catalog_smart(tmp_path, catalog())
    after = smart.prepare(project_dir=root, task_intent="GROMACS version smoke")
    assert "environment_profile" in {q.field for q in before.unresolved_fields}
    assert after.state == "READY_TO_SUBMIT" and not after.unresolved_fields
    assert after.values.software_id == "gromacs"
    assert after.values.executable == "/opt/example/gromacs/bin/gmx_mpi"
    for field in ("software_id", "executable", "environment_profile"):
        assert after.resolved_fields[field].source == FieldSource.SERVER_CATALOG
    assert any(e.source_file == "server_catalog.yaml" for e in after.job_spec.evidence)
    assert after.values.ntasks == after.values.nodes == after.values.cpus_per_task == 1
    assert after.values.gpu_count == 0 and after.values.launcher_profile is None
    assert "gpu" in str(after.resource_recommendations.evidence)
    assert len(model.calls) == 1


def test_post_validator_still_rejects_ai_absolute_path(tmp_path):
    smart, root, _ = setup_catalog_smart(tmp_path, catalog(), bad_path=True)
    with pytest.raises(AnalysisOutputValidationError):
        smart.prepare(project_dir=root, task_intent="version")


def test_user_environment_and_custom_executable_override(tmp_path):
    smart, root, _ = setup_catalog_smart(tmp_path, catalog())
    p = smart.prepare(project_dir=root, task_intent="version")
    p = smart.finalize(prepared=p, user_values=PreparationValues(
        environment_profile=EnvironmentProfile(id="alternative", version="1"), executable="custom-gmx"))
    assert p.state == "READY_TO_SUBMIT"
    assert p.job_spec.environment_profile.id == "alternative" and p.job_spec.run_step.executable == "custom-gmx"
    assert p.resolved_fields["environment_profile"].source == FieldSource.USER
    assert any("候选之外" in w for w in p.warnings)


def test_multiple_software_selection_resolves_environment(tmp_path):
    smart, root, _ = setup_catalog_smart(tmp_path, multiple_catalog(), invocation="GROMACS")
    p = smart.prepare(project_dir=root, task_intent="version")
    assert {"software_id", "environment_profile"} <= {q.field for q in p.unresolved_fields}
    p = smart.finalize(prepared=p, user_values=PreparationValues(software_id="gromacs-new"))
    assert p.state == "READY_TO_SUBMIT" and p.values.environment_profile.id == "alternative"


def test_catalog_partition_filter_not_scoring_or_auto_gpu(tmp_path):
    data = payload()
    data["software"][0]["compatible_partitions"] = ["b"]
    smart, root, _ = setup_catalog_smart(tmp_path, catalog(data))
    p = smart.prepare(project_dir=root, task_intent="version")
    assert p.state == "READY_TO_SUBMIT" and p.values.partition == "b"
    assert [r.partition for r in p.resource_recommendations.recommendations] == ["b"]
    assert p.values.gpu_count == 0
    p = smart.finalize(prepared=p, user_values=PreparationValues(partition="a"))
    assert p.state == "NEEDS_INPUT" and "partition" in {q.field for q in p.unresolved_fields}


def test_catalog_does_not_replace_direct_unknown_with_dependency(tmp_path):
    smart, root, _ = setup_catalog_smart(tmp_path, catalog(), invocation="custom-gmx")
    p = smart.prepare(project_dir=root, task_intent="version")
    assert p.software_resolution.status == "NO_MATCH"
    assert p.values.executable == "custom-gmx" and p.state == "NEEDS_INPUT"


def test_manual_catalog_apply_preserves_user_environment():
    form = {**DEFAULT_FORM, "software_id": "gromacs", "environment": '["alternative", "1"]'}
    result = apply_catalog_to_form(form, catalog(), explicit=True)
    assert result["environment"] == form["environment"]
    assert result["executable"] == catalog().software[0].executable
    assert result["run_type"] == "installed"


def test_web_catalog_manual_and_smart_final_review_no_submit(tmp_path):
    smart, root, model = setup_catalog_smart(tmp_path, catalog())
    config = WebConfig(tmp_path / "jobs.sqlite3", tmp_path / "runs", workspace_root=tmp_path)
    fake = FakeSlurmClient()
    app = create_app(config, profiles=registry(), catalog=catalog(), project_analyzer=smart.analyzer,
                     cluster_service=FakeCluster(), slurm_client=fake)
    with TestClient(app, base_url="http://localhost") as browser:
        page = browser.get("/new?mode=manual")
        assert 'name="software_id"' in page.text and "上次复核：" in page.text
        page = post(browser, "/new/catalog", {**DEFAULT_FORM, "software_id": "gromacs"})
        assert page.status_code == 200 and "/opt/example/gromacs/bin/gmx_mpi" in page.text
        assert "VERIFIED" in page.text and "Fixture version query only" in page.text
        response = post(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "version"})
        assert response.status_code == 303, response.text
        url = response.headers["location"]
        page = browser.get(url)
        assert '提交前确认' in page.text and "SERVER_CATALOG" in page.text
        assert '查看生成脚本' in page.text and '确认并提交' in page.text
        assert count(config) == 0 and not fake.submit_calls
        assert len(model.calls) == 1


def test_web_config_path_load_and_invalid_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(payload()))
    monkeypatch.setenv("SBATCH_AGENT_SERVER_CATALOG_PATH", str(path))
    assert WebConfig.from_env().catalog_path == path
    app = create_app(WebConfig(tmp_path / "db", tmp_path / "runs", catalog_path=path), profiles=registry())
    with TestClient(app, base_url="http://localhost") as browser:
        assert '软件目录' in browser.get("/new").text
    path.write_text("invalid: true")
    with pytest.raises(CatalogError):
        create_app(WebConfig(tmp_path / "db", tmp_path / "runs", catalog_path=path), profiles=registry())


def test_post_validator_environment_downgrade_is_not_overridden(tmp_path):
    smart, root, model = setup_catalog_smart(tmp_path, catalog())
    refs = model.output["draft"]["environment_requirements"]["evidence_refs"]
    model.output["draft"]["environment_requirements"] = proposed(None, refs, "UNRESOLVED", "Version constraint requires confirmation")
    p = smart.prepare(project_dir=root, task_intent="version")
    assert p.values.environment_profile is None
    assert "environment_profile" in {q.field for q in p.unresolved_fields}


def test_user_software_selection_overrides_other_direct_catalog_program(tmp_path):
    data = payload()
    data["software"].append(fact("other", aliases=["other-command"], executable="/opt/example/other",
                                 environment_profile={"id": "gmx-env", "version": "1"}))
    smart, root, _ = setup_catalog_smart(tmp_path, catalog(data))
    p = smart.prepare(project_dir=root, task_intent="version")
    p = smart.finalize(prepared=p, user_values=PreparationValues(software_id="other"))
    assert p.job_spec.run_step.executable == "/opt/example/other"


def test_manual_recommendation_uses_catalog_compatibility(tmp_path, form):
    data = payload()
    data["software"][0]["compatible_partitions"] = ["b"]
    config = WebConfig(tmp_path / "db", tmp_path / "runs", workspace_root=tmp_path)
    app = create_app(config, profiles=registry(), catalog=catalog(data),
                     cluster_service=FakeCluster(), slurm_client=FakeSlurmClient())
    values = {**form, "environment": '["gmx-env", "1"]', "software_id": "gromacs", "partition": ""}
    with TestClient(app, base_url="http://localhost") as browser:
        page = post(browser, "/new/recommend", values)
        assert page.status_code == 200
        assert "Server catalog 不允许该 partition" in page.text
