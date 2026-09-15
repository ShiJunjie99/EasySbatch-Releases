"""Pure rules over anonymous structured facts; no cluster collection or I/O."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import subprocess

import pytest

from sbatch_agent.cluster import ClusterService
from sbatch_agent.cluster_models import (
    ClusterSnapshot, GPUCount, NodeSnapshot, PartitionSnapshot, QueueCounts,
    QueueSnapshot, PendingReasonCount, summarize_nodes,
)
from sbatch_agent.models import JobSpec
from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.recommendation_models import RecommendationRequest, UserPreference, Eligibility
from sbatch_agent.recommender import ResourceRecommender, RecommendationInputError, RecommendationWeights, WEIGHTS
from sbatch_agent.renderer import render_job_script
from sbatch_agent.runner import SubprocessRunner


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Recommendation rules may not access processes or the cluster")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(SubprocessRunner, "run", forbidden)
    monkeypatch.setattr(ClusterService, "get_snapshot", forbidden)


def node(name="n-a", partition="a", *, idle=8, cpus=16, memory=8192, gpu_type="a100", gpus=4, used=0, state="mixed"):
    return NodeSnapshot(name, (partition,), state.upper(), state, cpus,
                        cpus - idle if idle is not None else None, idle, 0, memory, 0,
                        "gpu:example:4", "gpu:example:0", (GPUCount(gpu_type, gpus, used),), None)


def snapshot(nodes=None, pending=None):
    nodes = nodes if nodes is not None else (node(), node("n-b", "b"))
    pending = pending or {}
    partitions = tuple(PartitionSnapshot(
        name, "UP", "up", name == "a", "01:00:00", None,
        summarize_nodes(tuple(n for n in nodes if name in n.partitions)),
        allow_accounts="ALL", allow_qos="ALL", queue=QueueCounts(0, pending.get(name, 0)),
    ) for name in sorted({p for n in nodes for p in n.partitions}))
    return ClusterSnapshot(NOW, "anonymous-cluster", "test-user", partitions, tuple(nodes),
                           QueueSnapshot(QueueCounts(0, sum(pending.values())), QueueCounts(), (),
                                         (PendingReasonCount("Priority", sum(pending.values())),)))


def request(**resources):
    return RecommendationRequest.model_validate({
        "run_type": "python", "environment_profile": {"id": "example", "version": "1"},
        "resources": {"cpus_per_task": 4, "memory_mib": 1024, "time_limit_seconds": 120,
                      "account": "group", "qos": "normal", **resources},
    })


def profiles(**extra):
    return StaticProfiles.model_validate({"environments": [{"id": "example", "version": "1",
        "load_steps": [], "allowed_partitions": ["a", "b", "c"], **extra}]})


def recommend(spec=None, snap=None, registry=None, **kwargs):
    return ResourceRecommender().recommend(spec=spec or request(), snapshot=snap or snapshot(),
                                         profiles=registry or profiles(), as_of=NOW, **kwargs)


def test_baseline_explanations_evidence_and_separate_filter_rank():
    report = recommend()
    assert [r.partition for r in report.recommendations] == ["a", "b"]
    assert [r.rank for r in report.recommendations] == [1, 2] and not report.rejections
    for item in report.recommendations:
        assert item.reasons and item.warnings and item.evidence
        assert item.eligible and item.eligibility == Eligibility.ELIGIBLE_WITH_WARNING
        assert item.snapshot_captured_at == NOW and 0 <= item.score <= 100
        assert "node" not in type(item.proposed_resources).model_fields


@pytest.mark.parametrize("state", ["DOWN", "INACTIVE", "DRAIN", "FUTURE"])
def test_partition_unavailable_is_excluded(state):
    snap = snapshot()
    snap = replace(snap, partitions=(replace(snap.partitions[0], state=state),))
    report = recommend(snap=snap)
    assert not report.recommendations and "非 UP" in str(report.rejections)


@pytest.mark.parametrize("resources, fragment", [
    ({"cpus_per_task": 17}, "CPU"), ({"memory_mib": 9000}, "Memory"),
    ({"gpus": {"count": 5}}, "GPU"), ({"gpus": {"count": 1, "gpu_type": "different"}}, "GPU"),
    ({"time_limit_seconds": 3601}, "walltime"), ({"partition": "missing"}, "不存在"),
])
def test_hard_resources_excluded_without_silent_lowering(resources, fragment):
    report = recommend(spec=request(**resources))
    assert not report.recommendations and fragment in str(report.rejections)


@pytest.mark.parametrize("field", ["cpus_total", "memory_total_mib", "gpus"])
def test_unknown_static_capability_is_not_assumed(field):
    n = replace(node(), **{field: None})
    report = recommend(request(gpus={"count": 1}), snapshot((n,)))
    assert not report.recommendations and "未知" in str(report.rejections)


def test_joint_resources_must_fit_same_nodes_not_independent_totals():
    nodes = (node(cpus=64, memory=512), node("n2", "a", cpus=2, idle=2, memory=64000))
    report = recommend(request(), snapshot(nodes))
    assert not report.recommendations and "联合" in str(report.rejections)


def test_environment_scope_missing_profile_and_version_exact_match():
    assert [r.partition for r in recommend(registry=profiles(allowed_partitions=["b"])).recommendations] == ["b"]
    assert not recommend(registry=profiles(allowed_partitions=[])).recommendations
    missing = profiles()
    missing.environments[0].version = "2"
    assert "未登记" in str(recommend(registry=missing).rejections)
    assert "适用范围" in str(recommend(registry=profiles(allowed_partitions=None)).recommendations[0].warnings)


def test_fixed_partition_and_empty_auto_scope():
    assert [r.partition for r in recommend(request(partition="b")).recommendations] == ["b"]
    empty = replace(snapshot(), partitions=(), nodes=())
    assert recommend(snap=empty).recommendations == ()
    assert recommend(snap=empty).rejections


@pytest.mark.parametrize("allowed, denied, field", [
    ("different", None, "account"), ("ALL", "group", "account"),
    ("other", None, "qos"), ("ALL", "normal", "qos"),
])
def test_known_access_restriction_blocks(allowed, denied, field):
    snap = snapshot((node(),))
    suffix = "accounts" if field == "account" else "qos"
    snap = replace(snap, partitions=(replace(snap.partitions[0], **{f"allow_{suffix}": allowed, f"deny_{suffix}": denied}),))
    assert not recommend(snap=snap).recommendations


def test_unknown_access_does_not_invent_associations_or_defaults():
    report = recommend(request(account=None, qos=None))
    assert report.recommendations and "不猜测默认" in str(report.recommendations[0].warnings)
    assert report.recommendations[0].proposed_resources.account is None


def test_launcher_exact_layout_and_multi_node_integer_task_capacity():
    req = request(nodes=2, ntasks=5, cpus_per_task=4)
    from sbatch_agent.models import ProfileReference
    req.launcher_profile = ProfileReference(id="mpi", version="1")
    registry = profiles()
    data = registry.model_dump()
    data["launchers"] = [{"id": "mpi", "version": "1", "command": {"executable": "launcher", "args": []},
                          "supported_layouts": [{"nodes": 2, "ntasks": 5}]}]
    registry = StaticProfiles.model_validate(data)
    snap = snapshot((node(cpus=16), node("n2", "a", cpus=4, idle=4)))
    assert recommend(req, snap, registry).recommendations
    assert not recommend(req, snapshot((node(cpus=12), node("n2", "a", cpus=4, idle=4))), registry).recommendations
    registry.launchers[0].supported_layouts = None
    assert "supported_layouts" in str(recommend(req, snap, registry).rejections)
    req.launcher_profile = None
    assert "Launch profile" in str(recommend(req, snap, registry).rejections)


def test_current_full_or_down_nodes_do_not_become_capacity_failures():
    snap = snapshot((node(idle=16, state="idle"), node("n-b", "b", idle=0, state="allocated")))
    report = recommend(snap=snap)
    assert [r.partition for r in report.recommendations] == ["a", "b"]
    assert report.recommendations[1].components.availability == 0
    snap = snapshot((node(idle=0, state="down*"),))
    assert recommend(snap=snap).recommendations[0].components.availability is None


def test_queue_pressure_normalizes_by_capable_node_count():
    nodes = tuple(node(f"n{i}", "a") for i in range(10)) + (node("b", "b"),)
    report = recommend(snap=snapshot(nodes, {"a": 10, "b": 5}), preference=UserPreference.FASTEST_AVAILABLE)
    assert report.recommendations[0].partition == "a"
    assert report.recommendations[0].components.queue == .5
    assert report.recommendations[1].components.queue == 1 / 6


def test_global_priority_resources_dependency_are_not_fabricated_per_partition():
    snap = snapshot(pending={"a": 2, "b": 8})
    first = recommend(snap=snap)
    queue = replace(snap.queue, pending_reasons=(PendingReasonCount("Resources", 3), PendingReasonCount("Dependency", 7)))
    second = recommend(snap=replace(snap, queue=queue))
    assert [r.score for r in first.recommendations] == [r.score for r in second.recommendations]
    assert any(e.field == "pending_reason.Resources" and e.value == 3 for e in second.evidence)
    assert "只有全局汇总" in str(second.warnings)


def option(partition, gpu_count=1, cpus=4, memory=1024):
    return {"partitions": [partition], "shape": {"cpus_per_task": cpus, "memory_mib": memory,
                                                "gpus": {"count": gpu_count}}}


def test_preferences_change_order_and_efficiency_uses_known_one_gpu_requirement():
    req = request(gpus={"count": 1})
    registry = profiles(resource_options=[option("a", 4, cpus=16, memory=4096), option("b", 1)])
    snap = snapshot((node(idle=16, state="idle"), node("n-b", "b", idle=0, used=4, state="allocated")), {"b": 8})
    fastest = recommend(req, snap, registry, preference=UserPreference.FASTEST_AVAILABLE)
    efficient = recommend(req, snap, registry, preference=UserPreference.RESOURCE_EFFICIENT)
    balanced = recommend(req, snap, registry, preference=UserPreference.BALANCED)
    assert fastest.recommendations[0].partition == "a"
    assert efficient.recommendations[0].partition == "b"
    assert efficient.recommendations[0].proposed_resources.gpus.count == 1
    assert len({tuple(r.score for r in x.recommendations) for x in (fastest, efficient, balanced)}) == 3


@pytest.mark.parametrize("shape, resource", [
    ({"cpus_per_task": 2}, {}), ({"memory_mib": 512}, {}),
    ({"nodes": 2}, {}), ({"ntasks": 2}, {}),
    ({"gpus": None}, {"gpus": {"count": 1}}),
    ({"gpus": {"count": 1}}, {}),
    ({"gpus": {"count": 1, "gpu_type": "other"}}, {"gpus": {"count": 1, "gpu_type": "a100"}}),
])
def test_registered_options_cannot_reduce_or_change_hard_requests(shape, resource):
    value = {"partitions": ["a"], "shape": {"cpus_per_task": 4, "memory_mib": 1024, **shape}}
    report = recommend(request(**resource), registry=profiles(resource_options=[value]))
    assert not report.recommendations and report.rejections


def test_options_are_bounded_deduplicated_and_not_a_search_space():
    reg = profiles(resource_options=[option("a"), option("a")])
    report = recommend(request(gpus={"count": 1}), registry=reg)
    assert len(report.recommendations) == 1 and report.rejections[0].partition == "b"


@pytest.mark.parametrize("missing", ["queue", "gpu_allocation", "cpu_idle", "state"])
def test_unknown_dynamic_values_warn_and_do_not_crash_or_invent(missing):
    snap = snapshot((node(),))
    if missing == "queue":
        snap = replace(snap, queue=None)
    else:
        updates = {"gpu_allocation": {"gpus": (GPUCount("a100", 4, None),)},
                   "cpu_idle": {"cpus_idle": None}, "state": {"raw_state": "new-state"}}[missing]
        snap = replace(snap, nodes=(replace(snap.nodes[0], **updates),))
    item = recommend(request(gpus={"count": 1}), snap).recommendations[0]
    assert item.eligible and "不可用" in str(item.warnings)
    assert (item.components.queue if missing == "queue" else item.components.availability) is None


def test_determinism_across_runs_input_order_and_no_mutation():
    req, snap, reg = request(), snapshot(), profiles()
    before = req.model_dump_json(), asdict(snap), reg.model_dump_json()
    first = recommend(req, snap, reg)
    assert first == recommend(req, snap, reg) == recommend(req, snap, reg)
    reordered = replace(snap, nodes=tuple(reversed(snap.nodes)), partitions=tuple(reversed(snap.partitions)))
    assert first == recommend(req, reordered, reg)
    assert before == (req.model_dump_json(), asdict(snap), reg.model_dump_json())
    first.recommendations[0].proposed_resources.memory_mib = 999
    assert req.resources.memory_mib == 1024


def test_real_jobspec_is_preserved_and_profile_metadata_does_not_change_shell():
    spec = JobSpec.model_validate({"project_dir": "/example", "work_dir": "/example", "run_type": "python",
        "entrypoint": "hello.py", "run_step": {"executable": "python", "args": ["hello.py"]},
        "environment_profile": {"id": "example", "version": "1"}, "spec_version": 1,
        "resources": request(partition="a").resources.model_dump()})
    before = spec.model_dump_json()
    script = render_job_script(spec, profiles=profiles())
    reg = profiles(resource_options=[{"partitions": ["a"], "shape": {"cpus_per_task": 8, "memory_mib": 1024}}])
    assert render_job_script(spec, profiles=reg) == script
    assert recommend(spec, registry=reg).recommendations[0].proposed_resources.cpus_per_task == 8
    assert spec.model_dump_json() == before


def test_staleness_uses_explicit_clock_input_without_hidden_nondeterminism():
    engine, req, snap, reg = ResourceRecommender(stale_after_seconds=60), request(), snapshot(), profiles()
    def run(as_of):
        return engine.recommend(spec=req, snapshot=snap, profiles=reg, as_of=as_of)
    assert "Stale snapshot" in str(run(NOW + timedelta(seconds=61)).warnings)
    assert "Stale snapshot" not in str(run(NOW + timedelta(seconds=60)).warnings)
    assert "未校验快照年龄" in str(run(None).warnings)
    assert "核对时钟" in str(run(NOW - timedelta(seconds=1)).warnings)
    assert run(NOW + timedelta(seconds=61)) == run(NOW + timedelta(seconds=61))
    with pytest.raises(RecommendationInputError):
        run(datetime(2026, 1, 1))


@pytest.mark.parametrize("limit, seconds, eligible", [("infinite", 999999, True), ("UNLIMITED", 999999, True),
    ("1-00:00:00", 86400, True), ("00:02:00", 120, True), ("00:02:00", 121, False),
    ("00:02:30", 121, False), (None, 120, False), ("bad", 120, False), ("00:90:00", 120, False)])
def test_max_time_formats_and_slurm_minute_rounding(limit, seconds, eligible):
    snap = snapshot((node(),))
    snap = replace(snap, partitions=(replace(snap.partitions[0], max_time=limit),))
    assert bool(recommend(request(time_limit_seconds=seconds), snap).recommendations) is eligible


@pytest.mark.parametrize("values", [(1, 1, 1), (-.1, .5, .6), (float("nan"), 0, 1), (True, 0, 0)])
def test_weights_are_named_normalized_validated(values):
    with pytest.raises(ValueError):
        RecommendationWeights(*values)
    for weights in WEIGHTS.values():
        assert sum(asdict(weights).values()) == pytest.approx(1)


def test_duplicate_snapshot_nodes_are_not_double_counted():
    snap = snapshot()
    with pytest.raises(RecommendationInputError, match="重复"):
        recommend(snap=replace(snap, nodes=snap.nodes + snap.nodes))


@pytest.mark.parametrize("preference, expected", [(UserPreference.FASTEST_AVAILABLE, 67.5),
    (UserPreference.BALANCED, 80), (UserPreference.RESOURCE_EFFICIENT, 92.5)])
def test_score_formula_has_documented_normalization(preference, expected):
    item = recommend(preference=preference).recommendations[0]
    assert item.components.availability == .5
    assert item.components.queue == item.components.efficiency == 1
    assert item.score == expected


def test_unrepresentable_partition_rejected_without_losing_valid_candidates():
    snap = snapshot((node(), node("other", "bad name")))
    report = recommend(snap=snap, registry=profiles(allowed_partitions=None))
    assert [r.partition for r in report.recommendations] == ["a"]
    assert "安全表达" in str(report.rejections)


def test_gpu_types_are_compatibility_not_theoretical_performance():
    snap = snapshot((node(gpu_type="small-label"), node("b", "b", gpu_type="large-label")))
    report = recommend(request(gpus={"count": 1}), snap)
    assert report.recommendations[0].score == report.recommendations[1].score
