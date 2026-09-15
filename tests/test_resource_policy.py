"""M9-B: explicit intent, deterministic evidence and immutable historical facts."""

from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest
import yaml
from pydantic import ValidationError

from sbatch_agent.models import JobSpec, Resources, ResourceValuePolicy
from sbatch_agent.persistence import JobRepository
from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.renderer import render_job_script
from sbatch_agent.smart_models import PreparationValues
from sbatch_agent.resource_policy import recommend_resource_values
from test_smart_service import setup_smart, offline
from test_recommender import NOW, request, snapshot, profiles, ResourceRecommender


ROOT = Path(__file__).parents[1]
FIELDS = [("memory_mib", "memory_policy"), ("time_limit_seconds", "walltime_policy")]
EVIDENCE = {"source": "fixture profile", "reason": "已复核的当前任务配置", "status": "DIRECT"}


def resource_data(**overrides):
    return {"partition": "a", "memory_mib": 2048, "time_limit_seconds": 7200, **overrides}


@pytest.mark.parametrize("key,policy", FIELDS)
@pytest.mark.parametrize("mode", ["cluster_default", "recommended", "explicit"])
def test_each_resource_mode_roundtrip_and_units(key, policy, mode):
    data = resource_data(**{policy: {"mode": mode}})
    if mode == "cluster_default":
        del data[key]
    if mode == "recommended":
        data[policy]["evidence"] = EVIDENCE
    resources = Resources.model_validate(data)
    assert getattr(resources, policy).mode == mode
    assert getattr(resources, key) == (None if mode == "cluster_default" else resource_data()[key])
    assert Resources.model_validate_json(resources.model_dump_json()) == resources


@pytest.mark.parametrize("key,policy", FIELDS)
@pytest.mark.parametrize("mode", ["recommended", "explicit"])
@pytest.mark.parametrize("value", [None, 0, -1, True, "2048", 2.5])
def test_finite_modes_require_strict_positive_values(key, policy, mode, value):
    definition = {"mode": mode, **({"evidence": EVIDENCE} if mode == "recommended" else {})}
    with pytest.raises(ValidationError):
        Resources.model_validate(resource_data(**{key: value, policy: definition}))


@pytest.mark.parametrize("key,policy", FIELDS)
@pytest.mark.parametrize("value", [0, 2048, "INFINITE", "UNLIMITED"])
def test_defaults_cannot_hide_sentinels_or_explicit_values(key, policy, value):
    with pytest.raises(ValidationError):
        Resources.model_validate(resource_data(**{key: value, policy: {"mode": "cluster_default"}}))


@pytest.mark.parametrize("definition", [{"mode": "recommended"}, {"mode": "recommended", "evidence": None},
    {"mode": "explicit", "evidence": EVIDENCE}, {"mode": "cluster_default", "evidence": EVIDENCE},
    {"mode": "recommended", "evidence": {**EVIDENCE, "status": "INFERRED"}},
    {"mode": "recommended", "evidence": {**EVIDENCE, "confidence": .9}}, {"mode": "unknown"}])
def test_policy_evidence_cannot_claim_guesses(definition):
    with pytest.raises(ValidationError):
        ResourceValuePolicy.model_validate(definition)


def test_absent_policy_is_legacy_explicit_not_an_implicit_default():
    r = Resources.model_validate(resource_data())
    assert r.memory_policy.mode == r.walltime_policy.mode == "explicit"
    for key, _ in FIELDS:
        data = resource_data()
        del data[key]
        with pytest.raises(ValidationError):
            Resources.model_validate(data)
        with pytest.raises(ValidationError):
            Resources.model_validate(resource_data(**{key: None}))


@pytest.mark.parametrize("name,memory,walltime", [
    ("both_default", ("cluster_default", None), ("cluster_default", None)),
    ("memory_explicit", ("explicit", 8192), ("cluster_default", None)),
    ("walltime_explicit", ("cluster_default", None), ("explicit", 14400)),
    ("both_explicit", ("explicit", 512), ("explicit", 7200)),
    ("recommended", ("recommended", 4096), ("recommended", 7200)),
])
def test_shell_policy_goldens(name, memory, walltime):
    data = yaml.safe_load((ROOT / "examples/rendering/python.yaml").read_text())
    for (key, policy), (mode, value) in zip(FIELDS, (memory, walltime)):
        data["resources"][key] = value
        data["resources"][policy] = {"mode": mode, **({"evidence": EVIDENCE} if mode == "recommended" else {})}
    registry = StaticProfiles.model_validate(yaml.safe_load((ROOT / "examples/profiles.yaml").read_text()))
    script = render_job_script(JobSpec.model_validate(data), profiles=registry)
    assert script == (ROOT / f"tests/fixtures/resource_policy/{name}.sbatch").read_text()
    for forbidden in ("--mem=0", "--mem-per-cpu", "--mem-per-gpu", "--time=0\n", "INFINITE", "UNLIMITED"):
        assert forbidden not in script
    if memory[0] == "cluster_default":
        assert "#SBATCH --mem" not in script
    if walltime[0] == "cluster_default":
        assert "#SBATCH --time" not in script


def test_old_sqlite_snapshot_and_actual_script_remain_byte_identical(tmp_path, monkeypatch):
    data = yaml.safe_load((ROOT / "examples/rendering/python.yaml").read_text())
    old_json = json.dumps(data, ensure_ascii=False, indent=1)
    actual_script = "#!/bin/bash\n# Historical script, saved before Resource Policy\necho old\n"
    database = tmp_path / "jobs.sqlite3"
    with JobRepository(database) as repo:
        record = repo.create(JobSpec.model_validate(data), actual_script)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE jobs SET job_spec_snapshot=? WHERE id=?", (old_json, record.id))
    import sbatch_agent.service as service
    monkeypatch.setattr(service, "render_job_script", lambda *a, **k: pytest.fail("History must never be rerendered"))
    for _ in range(2):
        with JobRepository(database) as repo:
            recovered = repo.get(record.id)
            assert recovered.job_spec.resources.memory_policy.mode == "explicit"
            assert recovered.job_spec.resources.walltime_policy.mode == "explicit"
            assert recovered.job_spec_snapshot == old_json
            assert recovered.rendered_script == actual_script
            assert repo.list() == [recovered]
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT job_spec_snapshot,rendered_script FROM jobs").fetchone() == (old_json, actual_script)


def defaults(**changes):
    return PreparationValues(memory_mode="cluster_default", walltime_mode="cluster_default", **changes)


def test_smart_two_missing_fields_become_zero_with_explicit_default_intent(tmp_path):
    smart, root, model, cluster = setup_smart(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    p = smart.prepare(project_dir=root, task_intent="run")
    assert {q.field for q in p.unresolved_fields} == {"memory_mib", "time_limit_seconds"}
    ready = smart.finalize(prepared=p, user_values=defaults())
    assert ready.state == "READY_TO_SUBMIT" and not ready.unresolved_fields
    assert ready.job_spec.resources.memory_mib is ready.job_spec.resources.time_limit_seconds is None
    assert "#SBATCH --mem" not in ready.rendered_script and "#SBATCH --time" not in ready.rendered_script
    assert ready.resolved_fields["memory_mib"].source == ready.resolved_fields["time_limit_seconds"].source == "CLUSTER_DEFAULT"
    assert len(model.calls) == cluster.calls == 1
    assert p.job_spec is None and not (tmp_path / "jobs.sqlite3").exists()


def test_prepare_can_apply_default_intent_before_first_review(tmp_path):
    smart, root, model, cluster = setup_smart(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    p = smart.prepare(project_dir=root, task_intent="run", user_values=defaults())
    assert p.state == "READY_TO_SUBMIT" and not p.unresolved_fields
    assert len(model.calls) == cluster.calls == 1


@pytest.mark.parametrize("memory_mode,walltime_mode,memory,time", [
    ("recommended", "cluster_default", 256, None), ("explicit", "recommended", 8192, 120),
    ("recommended", "recommended", 256, 120),
])
def test_smart_mixed_policies_keep_provenance(tmp_path, memory_mode, walltime_mode, memory, time):
    smart, root, model, cluster = setup_smart(tmp_path)
    with (root / "old.sbatch").open("a") as f:
        f.write("python run.py --input inputs/case01.json\n")
    values = {"memory_mode": memory_mode, "walltime_mode": walltime_mode}
    if memory_mode == "explicit": values["memory_mib"] = memory
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(**values))
    assert p.state == "READY_TO_SUBMIT", p.unresolved_fields
    assert (p.job_spec.resources.memory_mib, p.job_spec.resources.time_limit_seconds) == (memory, time)
    for key, mode in (("memory_mib", memory_mode), ("time_limit_seconds", walltime_mode)):
        assert p.resolved_fields[key].source == {"recommended": "RESOURCE_POLICY_RECOMMENDATION", "cluster_default": "CLUSTER_DEFAULT", "explicit": "USER"}[mode]
    p = smart.finalize(prepared=p)
    assert (p.job_spec.resources.memory_mib, p.job_spec.resources.time_limit_seconds) == (memory, time)
    assert len(model.calls) == cluster.calls == 1


@pytest.mark.parametrize("mode_key,key", [("memory_mode", "memory_mib"), ("walltime_mode", "time_limit_seconds")])
def test_recommended_without_matching_evidence_blocks_ready(tmp_path, mode_key, key):
    smart, root, model, cluster = setup_smart(tmp_path)
    # The old directives have no associated invocation. AI's numbers are not
    # sufficient to turn them into a scoped resource recommendation.
    values = {"memory_mode": "cluster_default", "walltime_mode": "cluster_default", mode_key: "recommended"}
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(**values))
    assert p.state == "NEEDS_INPUT" and p.job_spec is None and p.rendered_script is None
    assert {q.field for q in p.unresolved_fields} == {key}
    assert smart.finalize(prepared=p, user_values=defaults()).state == "READY_TO_SUBMIT"
    assert len(model.calls) == cluster.calls == 1


def rule(**updates):
    return {"run_type": "python", "command": {"executable": "python", "args": ["run.py", "--input", "inputs/case01.json"]},
        "cpus_per_task": 1, "memory_mib": 4096, "time_limit_seconds": 120,
        "evidence": EVIDENCE, "verification_status": "VERIFIED", "verified_at": NOW,
        "verification_scope": "离线示例任务及其输入、单 CPU 布局", **updates}


@pytest.mark.parametrize("memory,time", [(4096, None), (None, 120), (4096, 120)])
def test_verified_profile_values_do_not_depend_on_ai_numbers(tmp_path, memory, time):
    smart, root, model, cluster = setup_smart(tmp_path, omit=("memory_mib", "time_limit_seconds"),
        extra_profile={"resource_rules": [rule(memory_mib=memory, time_limit_seconds=time)]})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended" if memory else "cluster_default", walltime_mode="recommended" if time else "cluster_default"))
    assert p.state == "READY_TO_SUBMIT"
    assert p.job_spec.resources.memory_mib == memory and p.job_spec.resources.time_limit_seconds == time
    for key, value in (("memory_mib", memory), ("time_limit_seconds", time)):
        if value:
            candidate = p.resource_value_recommendations[key]
            assert candidate.value == value and candidate.evidence.source == "fixture profile"
            assert "复核范围" in candidate.evidence.reason and candidate.evidence.status == "DIRECT"
    assert len(model.calls) == cluster.calls == 1


@pytest.mark.parametrize("changes", [{"cpus_per_task": 2}, {"nodes": 2}, {"ntasks": 2},
    {"gpus": {"count": 1}}, {"run_type": "installed"},
    {"command": {"executable": "different", "args": []}},
    {"command": {"executable": "python", "args": ["run.py", "--input", "different.json"]}}])
def test_profile_rules_require_exact_invocation_and_layout(tmp_path, changes):
    smart, root, _, _ = setup_smart(tmp_path, extra_profile={"resource_rules": [rule(**changes)]})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    assert p.state == "NEEDS_INPUT" and "memory_mib" not in p.resource_value_recommendations


@pytest.mark.parametrize("changes", [{"verification_status": "DOCUMENTED"}, {"verified_at": None},
    {"verified_at": "2026-01-01T00:00:00"}, {"verification_scope": " "},
    {"memory_mib": None, "time_limit_seconds": None}])
def test_unverified_resource_rule_cannot_be_registered(changes):
    from sbatch_agent.profiles import VerifiedResourceRule
    with pytest.raises(ValidationError):
        VerifiedResourceRule.model_validate(rule(**changes))


def test_conflicting_verified_rules_do_not_fall_back_to_old_project_values(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path, extra_profile={"resource_rules": [rule(), rule(memory_mib=2048)]})
    with (root / "old.sbatch").open("a") as f:
        f.write("python run.py --input inputs/case01.json\n")
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="recommended"))
    assert p.state == "NEEDS_INPUT" and {q.field for q in p.unresolved_fields} == {"memory_mib"}
    assert p.values.time_limit_seconds == 120


def test_explicit_values_override_profile_and_survive_other_edits(tmp_path):
    smart, root, model, cluster = setup_smart(tmp_path, extra_profile={
        "resource_rules": [rule()], "resource_options": [{"partitions": ["a", "b"],
            "shape": {"cpus_per_task": 1, "memory_mib": 4096}}]})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="explicit", memory_mib=8192, walltime_mode="explicit", time_limit_seconds=180))
    assert p.state == "READY_TO_SUBMIT", p.unresolved_fields
    p = smart.finalize(prepared=p, user_values=PreparationValues(partition="b"))
    assert p.state == "READY_TO_SUBMIT" and p.job_spec.resources.memory_mib == 8192
    assert p.job_spec.resources.time_limit_seconds == 180
    assert p.job_spec.resources.memory_policy.mode == p.job_spec.resources.walltime_policy.mode == "explicit"
    assert len(model.calls) == cluster.calls == 1


def test_bare_numeric_edit_becomes_explicit_not_recommended(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path, extra_profile={"resource_rules": [rule()]})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    p = smart.finalize(prepared=p, user_values=PreparationValues(memory_mib=8192))
    assert p.job_spec.resources.memory_mib == 8192
    assert p.job_spec.resources.memory_policy.mode == "explicit" and p.job_spec.resources.memory_policy.evidence is None


def test_switching_to_empty_explicit_does_not_reuse_ai_or_old_recommendation(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path, extra_profile={"resource_rules": [rule()]})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    p = smart.finalize(prepared=p, user_values=PreparationValues(memory_mode="explicit", memory_mib=None))
    assert p.state == "NEEDS_INPUT" and p.values.memory_mib is None


def test_layout_edit_invalidates_recommendation_without_reanalysis(tmp_path):
    smart, root, model, cluster = setup_smart(tmp_path, extra_profile={"resource_rules": [rule()]})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    p = smart.finalize(prepared=p, user_values=PreparationValues(cpus_per_task=2))
    assert p.state == "NEEDS_INPUT" and p.values.memory_mib is None
    assert len(model.calls) == cluster.calls == 1


@pytest.mark.parametrize("directive", ["--mem=0", "--mem=64T", "--mem=bad", "--mem=1024M\n#SBATCH --mem=2048M",
    "--mem=1024M\n#SBATCH --mem-per-cpu=512", "--mem=1024M\n#SBATCH --mem-per-gpu=512"])
def test_conflicting_or_unsupported_sbatch_memory_is_unavailable(tmp_path, directive):
    smart, root, _, _ = setup_smart(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    (root / "old.sbatch").write_text(f"#!/bin/bash\n#SBATCH {directive}\npython run.py --input inputs/case01.json\n")
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    assert p.state == "NEEDS_INPUT" and p.values.memory_mib is None


@pytest.mark.parametrize("time", ["0", "INFINITE", "UNLIMITED", "02:80:00", "24h", "00:00:00"])
def test_walltime_never_guesses_or_invents_unlimited(tmp_path, time):
    smart, root, _, _ = setup_smart(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    (root / "old.sbatch").write_text(f"#!/bin/bash\n#SBATCH --time={time}\npython run.py --input inputs/case01.json\n")
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="cluster_default", walltime_mode="recommended"))
    assert p.state == "NEEDS_INPUT" and p.values.time_limit_seconds is None


def test_explicit_readme_resource_sentences_are_scoped_without_ai_quantities(tmp_path):
    smart, root, model, _ = setup_smart(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    with (root / "README.md").open("a") as f:
        f.write("requires 1024 MiB memory per node.\nrequires 120 seconds walltime.\n")
    from test_analyzer import python_output
    model.output = python_output(smart.scanner.scan(root))
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="recommended"))
    assert p.state == "READY_TO_SUBMIT", p.unresolved_fields
    assert p.values.memory_mib == 1024 and p.values.time_limit_seconds == 120
    assert p.job_spec.resources.memory_policy.evidence.source == "README.md:3"


def test_default_does_not_need_known_memory_capacity_or_max_time():
    req = request()
    req.resources = type(req.resources).model_validate({"memory_policy": {"mode": "cluster_default"},
        "walltime_policy": {"mode": "cluster_default"}, "cpus_per_task": 4})
    snap = snapshot()
    snap = replace(snap, partitions=tuple(replace(p, max_time=None) for p in snap.partitions),
        nodes=tuple(replace(n, memory_total_mib=None) for n in snap.nodes))
    report = ResourceRecommender().recommend(spec=req, snapshot=snap, profiles=profiles(), as_of=NOW)
    assert report.recommendations
    for candidate in report.recommendations:
        assert candidate.proposed_resources.memory_mib is candidate.proposed_resources.time_limit_seconds is None
        assert "未校验默认申请量" in str(candidate.reasons)
        assert "优先级" in str(candidate.warnings)


def test_queue_does_not_determine_policy_recommendations(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path, extra_profile={"resource_rules": [rule()]})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="recommended"))
    original = dict(p.resource_value_recommendations)
    p.snapshot = replace(p.snapshot, queue=None)
    after = smart.finalize(prepared=p)
    assert after.resource_value_recommendations == original
    assert after.job_spec.resources == p.job_spec.resources


@pytest.mark.parametrize("status,ready", [("VERIFIED", True), ("DOCUMENTED", False), ("INFERRED", False), ("UNVERIFIED", False)])
def test_catalog_recommendations_need_resource_specific_verified_rules(tmp_path, status, ready):
    from test_server_catalog import payload, catalog, setup_catalog_smart
    data = payload()
    data["software"][0]["verification_status"] = status
    data["software"][0]["resource_rules"] = [rule(run_type="installed", command={
        "executable": "/opt/example/gromacs/bin/gmx_mpi", "args": ["--version"]})]
    smart, root, _ = setup_catalog_smart(tmp_path, catalog(data))
    p = smart.prepare(project_dir=root, task_intent="version", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    assert (p.state == "READY_TO_SUBMIT") is ready
    assert bool(p.resource_value_recommendations) is ready
    if ready:
        assert p.job_spec.resources.memory_mib == 4096
        assert p.job_spec.resources.memory_policy.evidence.source == "fixture profile"


def test_verified_software_version_alone_cannot_supply_memory_or_time(tmp_path):
    from test_server_catalog import catalog, setup_catalog_smart
    smart, root, _ = setup_catalog_smart(tmp_path, catalog())
    p = smart.prepare(project_dir=root, task_intent="version", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="recommended"))
    assert p.state == "NEEDS_INPUT" and not p.resource_value_recommendations
    assert {q.field for q in p.unresolved_fields} == {"memory_mib", "time_limit_seconds"}


def test_program_profile_priority_over_catalog_and_user_over_both(tmp_path):
    from test_server_catalog import payload, catalog, setup_catalog_smart
    from sbatch_agent.profiles import VerifiedResourceRule
    invocation = {"executable": "/opt/example/gromacs/bin/gmx_mpi", "args": ["--version"]}
    data = payload()
    data["software"][0]["resource_rules"] = [rule(run_type="installed", command=invocation, memory_mib=2048)]
    smart, root, model = setup_catalog_smart(tmp_path, catalog(data))
    smart.profiles.environments[0].resource_rules = [VerifiedResourceRule.model_validate(rule(run_type="installed", command=invocation))]
    p = smart.prepare(project_dir=root, task_intent="version", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    assert p.job_spec.resources.memory_mib == 4096
    p = smart.finalize(prepared=p, user_values=PreparationValues(memory_mode="explicit", memory_mib=8192))
    assert p.job_spec.resources.memory_mib == 8192 and len(model.calls) == 1


def test_partition_candidate_cannot_make_a_resource_rule_stale(tmp_path):
    options = [{"partitions": [partition], "shape": {"cpus_per_task": cpus, "memory_mib": 256}}
               for partition, cpus in (("a", 2), ("b", 4))]
    smart, root, _, _ = setup_smart(tmp_path, extra_profile={"resource_rules": [rule()], "resource_options": options})
    p = smart.prepare(project_dir=root, task_intent="run", user_values=PreparationValues(
        memory_mode="recommended", walltime_mode="cluster_default"))
    assert p.state == "NEEDS_INPUT" and p.job_spec is None
    assert "partition" in {q.field for q in p.unresolved_fields}
