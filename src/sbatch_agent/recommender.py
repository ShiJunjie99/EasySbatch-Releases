"""Deterministic advice from supplied facts; no CLI, Web, DB, clock or file I/O."""

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import re
from types import MappingProxyType

from pydantic import ValidationError

from .cluster_models import ClusterSnapshot
from .models import JobSpec, Resources
from .profiles import StaticProfiles
from .recommendation_models import (
    CandidateRejection, Eligibility, Recommendation, RecommendationEvidence as Evidence,
    RecommendationReport, RecommendationRequest, ScoreComponents, UserPreference,
)


class RecommendationInputError(ValueError):
    """The advisory input cannot be evaluated; manual creation remains separate."""


@dataclass(frozen=True)
class RecommendationWeights:
    availability: float
    queue: float
    efficiency: float

    def __post_init__(self):
        values = (self.availability, self.queue, self.efficiency)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("Weights must be finite nonnegative numbers")
        if not math.isclose(sum(values), 1.0):
            raise ValueError("Weights must sum to 1")


WEIGHTS = MappingProxyType({
    UserPreference.FASTEST_AVAILABLE: RecommendationWeights(.65, .30, .05),
    UserPreference.BALANCED: RecommendationWeights(.40, .30, .30),
    UserPreference.RESOURCE_EFFICIENT: RecommendationWeights(.15, .15, .70),
})
STALE_AFTER_SECONDS = 300
UNKNOWN_COMPONENT_SCORE = 0.0
BOUNDARIES = (
    "这不是等待时间或运行时间预测；最终分配由 Slurm 决定。",
    "Account/QOS 用户关联、有效额度、reservation 与站点策略尚未完整确认。",
    "环境、程序和工作目录在计算节点的实际可用性仍需验证。",
)


def _canonical(resources):
    return json.dumps(resources.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _request(spec):
    if isinstance(spec, JobSpec):
        spec = JobSpec.model_validate(spec.model_dump())
        if spec.unresolved:
            raise RecommendationInputError("请先解决 JobSpec unresolved 项，再请求推荐。")
        return RecommendationRequest.model_validate({
            "resources": spec.resources.model_dump(), "run_type": spec.run_type,
            "environment_profile": spec.environment_profile.model_dump(),
            "launcher_profile": spec.run_step.launcher_profile.model_dump() if spec.run_step.launcher_profile else None,
        })
    if isinstance(spec, RecommendationRequest):
        return RecommendationRequest.model_validate(spec.model_dump())
    raise RecommendationInputError("需要 JobSpec 或 RecommendationRequest。")


def _find(definitions, reference):
    return next((p for p in definitions if (p.id, p.version) == (reference.id, reference.version)), None)


def _max_time(raw):
    """sinfo's elapsed time forms only; None is unknown, infinity is explicit."""
    if raw is not None and raw.lower() in {"infinite", "unlimited"}:
        return math.inf
    if raw is None:
        return None
    match = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d{2}):(\d{2})", raw)
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    if int(minutes) > 59 or int(seconds) > 59 or (days is not None and int(hours) > 23):
        return None
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def _gpu_total(node, gpu):
    if gpu is None:
        return 0
    if node.gpus is None:
        return None
    # A missing type cannot prove compatibility with an explicit typed request.
    if gpu.gpu_type and any(g.gpu_type is None for g in node.gpus):
        return None
    return sum(g.total for g in node.gpus if gpu.gpu_type is None or g.gpu_type == gpu.gpu_type)


def _capacity(nodes, resources):
    """Joint per-node constraints, then integer task slots over N distinct nodes.

    Does not choose or return a nodelist. Current allocation/state is not a
    static capacity test; full/down nodes can recover while a job queues.
    """
    gpu = resources.gpus
    cpu_ok = [n for n in nodes if n.cpus_total is not None and n.cpus_total >= resources.cpus_per_task]
    memory_ok = list(nodes) if resources.memory_policy.mode == "cluster_default" else [
        n for n in nodes if n.memory_total_mib is not None and n.memory_total_mib >= resources.memory_mib]
    gpu_ok = [n for n in nodes if (total := _gpu_total(n, gpu)) is not None and total >= (gpu.count if gpu else 0)]
    failures = []
    for label, matching in (("节点", nodes), ("CPU", cpu_ok), ("Memory", memory_ok), ("GPU 数量/型号", gpu_ok)):
        if len(matching) < resources.nodes:
            failures.append(f"{label} 配置容量不足或未知，无法确认满足 {resources.nodes} node 的请求。")
    compatible = tuple(n for n in nodes if n in cpu_ok and n in memory_ok and n in gpu_ok)
    slots = sorted((n.cpus_total // resources.cpus_per_task for n in compatible), reverse=True)
    if len(slots) < resources.nodes or sum(slots[:resources.nodes]) < resources.ntasks:
        failures.append("同一组节点的联合 CPU/GPU/Memory 容量不足或未知，不能容纳所需 tasks。")
    return compatible, failures


def _access(partition, resources):
    failures, warnings = [], []
    for label, requested, allowed, denied in (
        ("Account", resources.account, partition.allow_accounts, partition.deny_accounts),
        ("QOS", resources.qos, partition.allow_qos, partition.deny_qos),
    ):
        if requested is None:
            warnings.append(f"{label} 未显式指定；不猜测默认关联。")
            continue
        # ALL and exact comma-separated identifiers only. No hierarchy guessing.
        def tokens(raw):
            if raw is None:
                return None
            if re.fullmatch(r"[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*", raw):
                return set(raw.split(","))
            return None
        allow, deny = tokens(allowed), tokens(denied)
        if allow is not None and "ALL" not in allow and requested not in allow:
            failures.append(f"{label}={requested} 不在分区明确允许列表中。")
        if deny is not None and (requested in deny or "ALL" in deny):
            failures.append(f"{label}={requested} 在分区明确拒绝列表中。")
        if allow is None:
            warnings.append(f"{label} 分区允许列表未知；不能据此确认权限。")
    return failures, warnings


def _respect_request(requested, proposed):
    errors = []
    if ("recommended" in {requested.memory_policy.mode, requested.walltime_policy.mode} and
            (requested.nodes, requested.ntasks, requested.cpus_per_task, requested.gpus) !=
            (proposed.nodes, proposed.ntasks, proposed.cpus_per_task, proposed.gpus)):
        errors.append("资源推荐依据仅适用于已确认的 CPU/GPU 布局；请修改配置后重新检查依据。")
    if (requested.nodes, requested.ntasks) != (proposed.nodes, proposed.ntasks):
        errors.append("登记组合改变了明确的 nodes/ntasks 布局。")
    if proposed.cpus_per_task < requested.cpus_per_task or (
            requested.memory_mib is not None and proposed.memory_mib < requested.memory_mib):
        errors.append("登记组合低于明确的 CPU/Memory 最低需求。")
    a, b = requested.gpus, proposed.gpus
    if (a is None) != (b is None):
        errors.append("登记组合改变了明确的 GPU/无 GPU 要求。")
    elif a and (b.count < a.count or (a.gpu_type is not None and b.gpu_type != a.gpu_type)):
        errors.append("登记组合低于 GPU 数量需求或改变了明确的 GPU 型号。")
    return errors


def _availability(nodes, resources):
    ratios = []
    for node in nodes:
        # Flags such as PLANNED/nonresponding are not proof of usable idle slots.
        if node.raw_state.lower() not in {"idle", "mixed", "allocated"}:
            return None
        if node.cpus_idle is None or not 0 <= node.cpus_idle <= node.cpus_total:
            return None
        ratio = node.cpus_idle / node.cpus_total
        if resources.gpus:
            gpus = [g for g in node.gpus if resources.gpus.gpu_type is None or g.gpu_type == resources.gpus.gpu_type]
            if any(g.allocated is None or not 0 <= g.allocated <= g.total for g in gpus):
                return None
            ratio = min(ratio, sum(g.total - g.allocated for g in gpus) / sum(g.total for g in gpus))
        ratios.append(ratio)
    return sum(ratios) / len(ratios)


def _score(requested, resources, nodes, queue, weights):
    availability = _availability(nodes, resources)
    queue_score = 1 / (1 + queue.pending_jobs / len(nodes)) if queue is not None else None
    efficiency = [requested.cpus_per_task / resources.cpus_per_task,
                  requested.memory_mib / resources.memory_mib if requested.memory_mib is not None else 1.0]
    if requested.gpus:
        efficiency.append(requested.gpus.count / resources.gpus.count)
    components = ScoreComponents(availability, queue_score, sum(efficiency) / len(efficiency))
    total = sum((getattr(components, field) if getattr(components, field) is not None else UNKNOWN_COMPONENT_SCORE)
                * getattr(weights, field) for field in ("availability", "queue", "efficiency"))
    return components, round(100 * total, 6)


class ResourceRecommender:
    def __init__(self, *, stale_after_seconds: int = STALE_AFTER_SECONDS, weights=None):
        if type(stale_after_seconds) is not int or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be a positive integer")
        self.stale_after_seconds = stale_after_seconds
        self.weights = dict(WEIGHTS if weights is None else weights)
        if set(self.weights) != set(UserPreference) or not all(isinstance(v, RecommendationWeights) for v in self.weights.values()):
            raise ValueError("Provide validated weights for all three preferences")

    def recommend(self, *, spec: JobSpec | RecommendationRequest, snapshot: ClusterSnapshot,
                  profiles: StaticProfiles, preference: UserPreference = UserPreference.BALANCED,
                  as_of: datetime | None = None, fixed_memory: bool = False) -> RecommendationReport:
        # The original M5 advisory can offer registered larger shapes. Resource
        # Policy callers have already selected who decides memory, so retain it.
        if type(fixed_memory) is not bool:
            raise RecommendationInputError("fixed_memory must be boolean")
        request = _request(spec)
        profiles = StaticProfiles.model_validate(profiles.model_dump())
        preference = UserPreference(preference)
        warnings = list(BOUNDARIES) + sorted(snapshot.warnings)
        if as_of is None:
            warnings.append("未提供 as_of，未校验快照年龄；请核对 captured_at。")
        else:
            if as_of.utcoffset() is None:
                raise RecommendationInputError("as_of 必须包含时区。")
            age = (as_of - snapshot.captured_at).total_seconds()
            if age < 0:
                warnings.append("快照时间晚于 as_of，请核对时钟。")
            elif age > self.stale_after_seconds:
                warnings.append(f"Stale snapshot：年龄超过 {self.stale_after_seconds} 秒，请手动刷新。")
        evidence = [Evidence("request", "resources", _canonical(request.resources)),
                    Evidence("preference", "mode", preference.value)]
        compatibility = request.catalog_compatibility
        if compatibility:
            evidence.append(Evidence("server_catalog", "software_id", compatibility.software_id))
            evidence.append(Evidence("server_catalog", "capabilities", ",".join(compatibility.capabilities)))
            warnings.append("Catalog 能力表示支持的模式，不自动要求 GPU/MPI，不预测性能。")
        if snapshot.queue:
            evidence.extend(Evidence("snapshot.queue.global", f"pending_reason.{item.reason}", item.count)
                            for item in sorted(snapshot.queue.pending_reasons, key=lambda x: x.reason))
            if snapshot.queue.pending_reasons:
                warnings.append("Priority/Resources/Dependency 等原因只有全局汇总，不推断各分区原因，也不把 pending 等同资源不足。")
        environment = _find(profiles.environments, request.environment_profile)
        global_errors = []
        if environment is None:
            global_errors.append("Environment profile id/version 未登记。")
        launcher = _find(profiles.launchers, request.launcher_profile) if request.launcher_profile else None
        layout = (request.resources.nodes, request.resources.ntasks)
        if request.launcher_profile:
            if launcher is None or launcher.supported_layouts is None:
                global_errors.append("Launch profile 未登记或缺少 supported_layouts，无法确认启动布局。")
            elif layout not in {(item.nodes, item.ntasks) for item in launcher.supported_layouts}:
                global_errors.append("Launch profile 不支持请求的 nodes/ntasks 布局。")
        elif layout != (1, 1):
            global_errors.append("多节点/多 task 必须有明确支持该布局的 Launch profile。")
        if request.resources.ntasks < request.resources.nodes:
            global_errors.append("第一版不评估 ntasks 少于 nodes 的布局。")
        partitions = sorted(snapshot.partitions, key=lambda p: p.name)
        if len({p.name for p in partitions}) != len(partitions) or len({n.name for n in snapshot.nodes}) != len(snapshot.nodes):
            raise RecommendationInputError("Snapshot 含重复分区或节点，拒绝重复计算容量。")
        selected = request.resources.partition
        if selected:
            partitions = [p for p in partitions if p.name == selected]
        rejected, recommendations = [], []
        if not partitions:
            rejected.append(CandidateRejection(selected or "(auto)", ("请求的 partition 不存在或没有可见 partition。",)))
        for partition in partitions:
            errors = global_errors.copy()
            local_warnings = warnings.copy()
            if (compatibility and compatibility.allowed_partitions is not None
                    and partition.name not in compatibility.allowed_partitions):
                errors.append("Server catalog 不允许该 partition。")
            if partition.state.upper() != "UP":
                errors.append("Partition 非 UP；未知、DOWN 或 INACTIVE 状态不作为候选。")
            limit = _max_time(partition.max_time)
            if request.resources.walltime_policy.mode == "cluster_default":
                local_warnings.append("运行时限使用集群默认；最终限制由 Slurm / QOS / 账户策略决定。")
            elif limit is None:
                errors.append("Partition max_time 未知或格式不支持，无法确认 walltime。")
            elif ((request.resources.time_limit_seconds + 59) // 60) * 60 > limit:
                errors.append("请求 walltime（按 Slurm 分钟向上取整）超过 partition max_time。")
            if environment:
                if environment.allowed_partitions is None:
                    local_warnings.append("Environment profile 未登记分区适用范围，需确认环境兼容性。")
                elif partition.name not in environment.allowed_partitions:
                    errors.append("Environment profile 不允许该 partition。")
            if errors:
                rejected.append(CandidateRejection(partition.name, tuple(errors)))
                continue
            shapes = [option.shape for option in environment.resource_options if partition.name in option.partitions]
            if environment.resource_options and not shapes:
                rejected.append(CandidateRejection(partition.name, ("该 partition 没有已登记的 resource option。",)))
                continue
            candidates = {}
            for shape in shapes or [None]:
                data = {**request.resources.model_dump(), "partition": partition.name}
                if shape:
                    data.update(shape.model_dump())  # account/QOS/time never come from an option
                    if fixed_memory or request.resources.memory_policy.mode != "explicit":
                        data["memory_mib"] = request.resources.memory_mib
                try:
                    resources = Resources.model_validate(data)
                except ValidationError:
                    rejected.append(CandidateRejection(partition.name, ("该候选无法由当前 Resources 安全表达。",)))
                    continue
                candidates[_canonical(resources)] = resources
            for key, resources in sorted(candidates.items()):
                failures = _respect_request(request.resources, resources)
                access_errors, access_warnings = _access(partition, resources)
                failures.extend(access_errors)
                nodes, capacity_errors = _capacity(tuple(sorted(
                    (n for n in snapshot.nodes if partition.name in n.partitions), key=lambda n: n.name,
                )), resources)
                failures.extend(capacity_errors)
                if failures:
                    rejected.append(CandidateRejection(partition.name, tuple(failures), resources))
                    continue
                queue = partition.queue if snapshot.queue is not None else None
                components, score = _score(request.resources, resources, nodes, queue, self.weights[preference])
                notices = local_warnings + access_warnings
                if components.availability is None:
                    notices.append("动态 CPU/GPU allocation 或节点状态信息不足，availability 不可用；排序贡献为 0，不代表空闲量为 0。")
                if queue is None:
                    notices.append("分区队列不可用；queue 排序贡献为 0，不虚构 pending 数量。")
                reasons = ["已确认可见节点配置满足联合 CPU/GPU/Memory 与任务布局容量。",
                           "当前满载不排除排队候选；此结论不保证立即调度。",
                           f"资源来源：{'已登记 environment resource option' if shapes else '保持请求资源值'}；没有降低明确需求。"]
                if resources.memory_policy.mode == "cluster_default":
                    reasons[0] = "已确认可见节点配置满足 CPU/GPU 与任务布局；内存使用集群默认，未校验默认申请量。"
                    notices.append("内存默认申请量由 Slurm 决定；不推断 DefMemPerCPU / DefMemPerGPU 的优先级。")
                if components.availability is not None:
                    reasons.append(f"兼容节点 CPU/GPU 空闲比例指标为 {components.availability:.3f}。")
                if queue:
                    reasons.append(f"归一化队列压力 = {queue.pending_jobs} pending / {len(nodes)} 个容量兼容节点；仅用于相对排序。")
                reasons.append(f"资源效率指标为 {components.efficiency:.3f}（明确需求 / 建议资源），偏好 {preference.value}。")
                facts = [Evidence("snapshot.partition", "state", partition.raw_state),
                         Evidence("snapshot.partition", "max_time", partition.max_time),
                         Evidence("snapshot.nodes", "capacity_compatible_nodes", len(nodes)),
                         Evidence("snapshot.partition.queue", "pending_jobs", queue.pending_jobs if queue else None),
                         Evidence("profile.environment", "reference", f"{environment.id}/{environment.version}"),
                         Evidence("score", "availability", components.availability),
                         Evidence("score", "queue", components.queue),
                         Evidence("score", "efficiency", components.efficiency)]
                if compatibility:
                    facts.append(Evidence("server_catalog", "software_id", compatibility.software_id))
                rid = hashlib.sha256((snapshot.captured_at.isoformat() + key).encode()).hexdigest()[:20]
                recommendations.append(Recommendation(
                    rid, partition.name, resources, score, 0,
                    Eligibility.ELIGIBLE_WITH_WARNING if notices else Eligibility.ELIGIBLE,
                    tuple(reasons), tuple(dict.fromkeys(notices)), tuple(facts), components, snapshot.captured_at,
                ))
        recommendations.sort(key=lambda item: (-item.score, item.partition, _canonical(item.proposed_resources)))
        return RecommendationReport(
            tuple(replace(item, rank=i) for i, item in enumerate(recommendations, 1)),
            tuple(rejected), tuple(dict.fromkeys(warnings)), tuple(evidence), snapshot.captured_at, preference,
        )
