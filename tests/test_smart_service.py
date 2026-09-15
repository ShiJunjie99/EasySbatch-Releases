"""M6-C uses real Scanner/Analyzer/resolver/renderer/repository with fake I/O."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import socket
import subprocess

import pytest
from pydantic import ValidationError

from sbatch_agent.analyzer import AIProjectAnalyzer
from sbatch_agent.model_client import AnalysisOutputValidationError, ModelUnavailableError
from sbatch_agent.models import EnvironmentProfile, JobSpec
from sbatch_agent.persistence import JobRepository, SubmissionState
from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.project_checks import ProjectChangedError
from sbatch_agent.runner import SubprocessRunner
from sbatch_agent.scanner import ProjectScanner
from sbatch_agent.service import SubmissionService, SubmissionServiceError
from sbatch_agent.smart_models import FieldSource, PreparationValues
from sbatch_agent.smart_service import PreparationError, SmartJobService
from test_analyzer import FakeModelClient, python_output, ref, proposed
from test_recommender import NOW, snapshot
from test_web import FakeSlurmClient


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Smart tests must not access real model/network/Slurm/processes")
    for owner, name in ((subprocess, "Popen"), (SubprocessRunner, "run"), (socket, "create_connection"),
                        (socket.socket, "connect")):
        monkeypatch.setattr(owner, name, forbidden)


class FakeCluster:
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure
    def get_snapshot(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return snapshot()


def setup_smart(tmp_path, *, omit=(), multiple=False, cluster_failure=None, output_change=None, extra_profile=None):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "inputs").mkdir(exist_ok=True)
    (root / "README.md").write_text("# Demo\npython run.py --input inputs/case01.json\n")
    (root / "run.py").write_text('if __name__ == "__main__":\n    raise RuntimeError("never execute")\n')
    (root / "requirements.txt").write_text("numpy\n")
    (root / "inputs/case01.json").write_text("{}")
    (root / "old.sbatch").write_text("#!/bin/bash\n#SBATCH --mem=256M\n#SBATCH --time=00:02:00\n")
    evidence = ProjectScanner().scan(root)
    output = python_output(evidence)
    output["draft"]["resource_requirements"] = {
        "memory_mib": proposed(256, ref(evidence, "sbatch.mem")),
        "time_limit_seconds": proposed(120, ref(evidence, "sbatch.time")),
    }
    for key in omit:
        output["draft"]["resource_requirements"].pop(key)
    if output_change:
        output_change(output, evidence)
    definitions = [{"id": "python", "version": "1", "load_steps": [], "allowed_partitions": ["a", "b"],
                    "analysis_capabilities": {"dependencies": ["numpy"]}, **(extra_profile or {})}]
    if multiple:
        definitions.append({**definitions[0], "id": "python-alt"})
    profiles = StaticProfiles.model_validate({"environments": definitions})
    model, cluster = FakeModelClient(output), FakeCluster(cluster_failure)
    smart = SmartJobService(analyzer=AIProjectAnalyzer(model_client=model, profiles=profiles), cluster_service=cluster,
                           profiles=profiles, clock=lambda: NOW)
    return smart, root, model, cluster


def lifecycle(tmp_path, smart, fake=None):
    repo = JobRepository(tmp_path / "jobs.sqlite3")
    return repo, SubmissionService(repository=repo, profiles=smart.profiles,
        slurm_client=fake or FakeSlurmClient(), submission_root=tmp_path / "runs")


def test_zero_unresolved_strict_ready_snapshot_sources_no_effects(tmp_path):
    smart, root, model, cluster = setup_smart(tmp_path)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    assert p.state == "READY_TO_SUBMIT" and not p.unresolved_fields
    assert JobSpec.model_validate(p.job_spec.model_dump()) == p.job_spec
    assert p.values.partition == "a" and p.selected_recommendation.partition == "a"
    assert p.resolved_fields["entrypoint"].source == FieldSource.AI_DIRECT
    assert p.resolved_fields["environment_profile"].source == FieldSource.ENVIRONMENT_RESOLVER
    assert p.resolved_fields["stdout"].source == FieldSource.SYSTEM_DEFAULT
    assert "#SBATCH --partition=a" in p.rendered_script
    again = smart.finalize(prepared=p)
    assert again.state == "READY_TO_SUBMIT" and cluster.calls == 1 and len(model.calls) == 1
    assert not (tmp_path / "runs").exists() and not (tmp_path / "jobs.sqlite3").exists()
    assert before == {str(f.relative_to(root)): f.read_bytes() for f in root.rglob("*") if f.is_file()}


@pytest.mark.parametrize("missing", [("time_limit_seconds",), ("time_limit_seconds", "memory_mib")])
def test_only_true_unresolved_then_finalize_without_repeat_queries(tmp_path, missing):
    smart, root, model, cluster = setup_smart(tmp_path, omit=missing)
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    assert p.state == "NEEDS_INPUT" and p.job_spec is None and p.rendered_script is None
    # Partition is deferred while recommendation awaits memory/time, not asked
    # redundantly when a snapshot is available (covered by Web as well).
    assert {q.field for q in p.unresolved_fields} == set(missing)
    ready = smart.finalize(prepared=p, user_values=PreparationValues(**{k: 120 if k == "time_limit_seconds" else 256 for k in missing}))
    assert ready.state == "READY_TO_SUBMIT"
    assert all(ready.resolved_fields[k].source == FieldSource.USER for k in missing)
    assert cluster.calls == 1 and len(model.calls) == 1


def test_environment_multiple_requires_explicit_choice(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path, multiple=True)
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    q = next(q for q in p.unresolved_fields if q.field == "environment_profile")
    assert len(q.choices) == 2 and p.values.environment_profile is None
    ready = smart.finalize(prepared=p, user_values=PreparationValues(environment_profile=EnvironmentProfile(id="python-alt", version="1")))
    assert ready.state == "READY_TO_SUBMIT" and ready.values.environment_profile.id == "python-alt"


def test_cluster_failure_preserves_ai_and_accepts_manual_resources(tmp_path):
    smart, root, _, cluster = setup_smart(tmp_path, cluster_failure=RuntimeError("PRIVATE"))
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    assert p.values.entrypoint == "run.py" and {q.field for q in p.unresolved_fields} == {"partition"}
    assert "PRIVATE" not in str(p.warnings)
    ready = smart.finalize(prepared=p, user_values=PreparationValues(partition="b"))
    assert ready.state == "READY_TO_SUBMIT" and cluster.calls == 1


def test_user_override_and_alternative_never_mutate_previous(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    before = deepcopy(p)
    b = next(r for r in p.resource_recommendations.recommendations if r.partition == "b")
    ready = smart.finalize(prepared=p, recommendation_id=b.id,
                           user_values=PreparationValues(cpus_per_task=2, memory_mib=512))
    assert p == before and ready.values.partition == "b"
    assert ready.job_spec.resources.cpus_per_task == 2 and ready.job_spec.resources.memory_mib == 512
    assert ready.resolved_fields["partition"].source == FieldSource.USER


@pytest.mark.parametrize("change", ["contents", "removed", "symlink", "root_link"])
def test_changed_project_blocks_finalize_and_confirm(tmp_path, change):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    target = root / "run.py"
    if change == "contents":
        target.write_text("modified\n")
    elif change == "removed":
        target.unlink()
    elif change == "symlink":
        target.unlink()
        target.symlink_to(root / "README.md")
    else:
        root.rename(tmp_path / "moved")
        root.symlink_to(tmp_path / "moved", target_is_directory=True)
    with pytest.raises(ProjectChangedError):
        smart.finalize(prepared=p)
    repo, service = lifecycle(tmp_path, smart)
    with repo:
        with pytest.raises(ProjectChangedError):
            smart.confirm(prepared=p, submission_service=service)
        assert not repo.list()


def test_confirm_twice_exact_script_single_record_and_restart(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    fake = FakeSlurmClient()
    repo, service = lifecycle(tmp_path, smart, fake)
    def before_submit(path):
        record = repo.get(p.id)
        assert record.submission_state == SubmissionState.SUBMITTING
        assert path.read_text() == p.rendered_script == record.rendered_script
    fake.before_submit = before_submit
    with repo:
        first = smart.confirm(prepared=p, submission_service=service)
        second = smart.confirm(prepared=p, submission_service=service)
        assert first.id == second.id == p.id and first.slurm_job_id == "123"
        assert len(repo.list()) == 1 and len(fake.submit_calls) == 1
    fake.before_submit = None
    repo2, service2 = lifecycle(tmp_path, smart, fake)
    with repo2:
        assert smart.confirm(prepared=p, submission_service=service2).slurm_job_id == "123"
        assert len(fake.submit_calls) == 1


def test_unknown_no_retry(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    fake = FakeSlurmClient()
    fake.failure = TimeoutError("uncertain")
    repo, service = lifecycle(tmp_path, smart, fake)
    with repo:
        with pytest.raises(SubmissionServiceError):
            smart.confirm(prepared=p, submission_service=service)
        assert repo.get(p.id).submission_state == SubmissionState.SUBMISSION_UNKNOWN
        smart.confirm(prepared=p, submission_service=service)
        assert len(fake.submit_calls) == 1


@pytest.mark.parametrize("values", [{"memory_mib": -1}, {"time_limit_seconds": "120"}, {"nodes": True}, {"unresolved": []}])
def test_strict_user_patch(values):
    with pytest.raises(ValidationError):
        PreparationValues.model_validate(values)


@pytest.mark.parametrize("values", [{"work_dir": "/etc"}, {"required_inputs": ["../../etc/passwd"]}])
def test_user_path_escape(tmp_path, values):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="运行 case01")
    with pytest.raises(ProjectChangedError):
        smart.finalize(prepared=p, user_values=PreparationValues(**values))


def test_ai_schema_failure_not_bypassed(tmp_path):
    smart, root, model, cluster = setup_smart(tmp_path)
    model.output = {"shell_script": "do not execute"}
    with pytest.raises(AnalysisOutputValidationError):
        smart.prepare(project_dir=root, task_intent="run")
    assert cluster.calls == 0


def test_stale_snapshot_warning_no_silent_refresh(tmp_path):
    smart, root, _, cluster = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="run")
    smart.clock = lambda: NOW + timedelta(seconds=301)
    ready = smart.finalize(prepared=p)
    assert any("300 秒" in w for w in ready.warnings) and cluster.calls == 1


def test_known_hard_failure_cannot_become_ready(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="run")
    result = smart.finalize(prepared=p, user_values=PreparationValues(memory_mib=999999))
    assert result.job_spec is None and any("兼容" in q.reason for q in result.unresolved_fields)


def test_profile_common_defaults_and_explicit_override_not_silently_increased(tmp_path):
    options = [{"partitions": ["a", "b"], "shape": {"cpus_per_task": 2, "memory_mib": 512}}]
    smart, root, _, _ = setup_smart(tmp_path, omit=("memory_mib",), extra_profile={"resource_options": options})
    p = smart.prepare(project_dir=root, task_intent="run")
    assert p.state == "READY_TO_SUBMIT" and p.values.memory_mib == 512 and p.values.cpus_per_task == 2
    result = smart.finalize(prepared=p, user_values=PreparationValues(cpus_per_task=1))
    assert result.state == "NEEDS_INPUT" and result.values.cpus_per_task == 1


def test_unknown_cluster_does_not_bypass_known_profile_shapes(tmp_path):
    options = [{"partitions": ["a"], "shape": {"cpus_per_task": 2, "memory_mib": 256}}]
    smart, root, _, _ = setup_smart(tmp_path, extra_profile={"resource_options": options}, cluster_failure=RuntimeError())
    p = smart.prepare(project_dir=root, task_intent="run")
    result = smart.finalize(prepared=p, user_values=PreparationValues(partition="a", cpus_per_task=3))
    assert result.state == "NEEDS_INPUT" and "resource_options" in str(result.unresolved_fields)


def test_recommender_failure_manual_fallback_and_no_rescan(tmp_path):
    smart, root, model, cluster = setup_smart(tmp_path)
    class BrokenAdvisor:
        def recommend(self, **kwargs):
            raise RuntimeError("PRIVATE")
    smart.recommender = BrokenAdvisor()
    p = smart.prepare(project_dir=root, task_intent="run")
    assert any(q.field == "partition" for q in p.unresolved_fields)
    ready = smart.finalize(prepared=p, user_values=PreparationValues(partition="a", stdout=None, stderr=None))
    assert ready.state == "READY_TO_SUBMIT" and ready.job_spec.stdout is None
    assert "PRIVATE" not in str(ready.warnings) and cluster.calls == 1 and len(model.calls) == 1


def test_missing_required_input_and_profile_change_block_before_create(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="run")
    with pytest.raises(ProjectChangedError):
        smart.finalize(prepared=p, user_values=PreparationValues(required_inputs=["missing.json"]))
    repo, service = lifecycle(tmp_path, smart)
    with repo:
        service.profiles = StaticProfiles.model_validate({"environments": [{"id": "python", "version": "1",
            "load_steps": [{"executable": "module", "args": ["load", "different"]}]}]})
        with pytest.raises(PreparationError, match="Profile"):
            smart.confirm(prepared=p, submission_service=service)
        assert not repo.list()


def test_concurrent_confirmation_claims_same_uuid_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="run")
    fake, barrier = FakeSlurmClient(), Barrier(2)
    # Initialize schema before concurrent connections, as app factory does.
    with JobRepository(tmp_path / "jobs.sqlite3"):
        pass
    def confirm():
        repo, service = lifecycle(tmp_path, smart, fake)
        with repo:
            barrier.wait(timeout=5)
            try:
                return smart.confirm(prepared=p, submission_service=service).id
            except SubmissionServiceError as exc:
                # Existing service rejects a loser racing the SQLite claim.
                return exc.record_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert results == [p.id, p.id] and len(fake.submit_calls) == 1
    with JobRepository(tmp_path / "jobs.sqlite3") as repo:
        assert len(repo.list()) == 1 and repo.get(p.id).submission_state == SubmissionState.SUBMITTED


def test_parallel_hint_requires_layout_not_automatic_launcher(tmp_path):
    smart, root, model, _ = setup_smart(tmp_path)
    (root / "parallel.sh").write_text("mpirun -np 2 ./solver\n")
    evidence = smart.scanner.scan(root)
    hint = next(e.id for e in evidence.evidence_items if e.kind == "mpi")
    model.output["draft"]["parallelism"] = {"mpi": proposed(True, [hint], status="INFERRED")}
    p = smart.prepare(project_dir=root, task_intent="run.py")
    assert {"ntasks", "launcher_profile"} <= {q.field for q in p.unresolved_fields}
    assert p.values.launcher_profile is None and p.rendered_script is None


def test_manual_field_reduction_fixture(tmp_path):
    # Fixed, explicit comparison scope: 20 core plan fields. No claim of
    # real-model accuracy or that Manual Mode requires twenty typed values.
    core = {"name", "work_dir", "run_type", "entrypoint", "executable", "args", "required_inputs",
            "environment_profile", "prepare_steps", "partition", "nodes", "ntasks", "cpus_per_task",
            "gpu_count", "memory_mib", "time_limit_seconds", "stdout", "stderr"}
    smart, root, _, _ = setup_smart(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    p = smart.prepare(project_dir=root, task_intent="run.py")
    assert len(p.unresolved_fields) == 2
    ready = smart.finalize(prepared=p, user_values=PreparationValues(memory_mib=256, time_limit_seconds=120))
    user = [k for k in core if ready.resolved_fields[k].source == FieldSource.USER]
    auto = [k for k in core if ready.resolved_fields[k].source != FieldSource.USER]
    assert set(user) == {"memory_mib", "time_limit_seconds"}
    assert len(core) + 2 == 20  # project_dir + spec_version, both mechanical
    assert len(auto) + 2 == 18


@pytest.mark.parametrize("kind", ["compiled", "installed", "module"])
def test_other_run_types_finalize_with_explicit_build_only(tmp_path, kind):
    smart, root, model, _ = setup_smart(tmp_path)
    if kind == "compiled":
        (root / "README.md").write_text("```bash\n./solver --input inputs/case01.json\n```\n")
        (root / "Makefile").write_text("solver:\n\tnever-run-this\n")
    elif kind == "installed":
        (root / "README.md").write_text("```bash\nexample_solver --input inputs/case01.json\n```\n")
    else:
        (root / "README.md").write_text("python -m run --input inputs/case01.json\n")
    evidence = smart.scanner.scan(root)
    # Start from validated partial inference and let the user resolve the
    # future compiled target; no executable is run or created by preparation.
    model.output = {"draft": {"resource_requirements": {
        "memory_mib": proposed(256, ref(evidence, "sbatch.mem")),
        "time_limit_seconds": proposed(120, ref(evidence, "sbatch.time"))},
        "environment_requirements": proposed({"dependencies": ["numpy"]}, ref(evidence, "python_dependency"))}}
    p = smart.prepare(project_dir=root, task_intent="run")
    values = dict(run_type="python" if kind == "module" else kind, entrypoint="run" if kind == "module" else "solver",
        executable="python" if kind == "module" else "./solver" if kind == "compiled" else "example_solver",
        args=["-m", "run"] if kind == "module" else [], required_inputs=[])
    if kind == "compiled":
        values["prepare_steps"] = [{"kind": "command", "executable": "make", "args": ["solver"]}]
    ready = smart.finalize(prepared=p, user_values=PreparationValues.model_validate(values))
    assert ready.state == "READY_TO_SUBMIT"
    assert "srun" not in ready.rendered_script and not (root / "solver").exists()
    if kind == "compiled":
        assert ready.rendered_script.index("make") < ready.rendered_script.index("test -x")
