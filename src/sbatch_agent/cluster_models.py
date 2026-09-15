"""Read-only, detached observations; no resource requests or recommendations."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GPUCount:
    gpu_type: str | None
    total: int
    allocated: int | None = None


@dataclass(frozen=True)
class NodeSnapshot:
    name: str
    partitions: tuple[str, ...]
    normalized_state: str
    raw_state: str
    cpus_total: int | None
    cpus_allocated: int | None
    cpus_idle: int | None
    cpus_other: int | None
    memory_total_mib: int | None
    memory_allocated_mib: int | None
    raw_gres: str | None
    raw_gres_used: str | None
    gpus: tuple[GPUCount, ...] | None
    reason: str | None


@dataclass(frozen=True)
class ResourceSummary:
    """Visible unique nodes, not schedulable capacity or an availability promise."""

    total_nodes: int
    idle_nodes: int
    allocated_nodes: int
    mixed_nodes: int
    down_nodes: int
    drain_nodes: int
    other_nodes: int
    total_cpus: int | None
    allocated_cpus: int | None
    idle_cpus: int | None
    gpus: tuple[GPUCount, ...] | None


@dataclass(frozen=True)
class PartitionSnapshot:
    name: str
    state: str
    raw_state: str
    is_default: bool
    max_time: str | None
    default_time: str | None
    summary: ResourceSummary | None = None
    allow_accounts: str | None = None
    deny_accounts: str | None = None
    allow_qos: str | None = None
    deny_qos: str | None = None
    qos: str | None = None
    queue: "QueueCounts | None" = None


@dataclass(frozen=True)
class QueueCounts:
    running_jobs: int = 0
    pending_jobs: int = 0
    other_jobs: int = 0


@dataclass(frozen=True)
class PartitionQueue:
    partition: str
    counts: QueueCounts


@dataclass(frozen=True)
class PendingReasonCount:
    reason: str
    count: int


@dataclass(frozen=True)
class QueueSnapshot:
    """Only aggregates. Array elements count individually; no job/user roster."""

    total: QueueCounts
    current_user: QueueCounts
    partitions: tuple[PartitionQueue, ...]
    pending_reasons: tuple[PendingReasonCount, ...]


def summarize_nodes(nodes: tuple[NodeSnapshot, ...]) -> ResourceSummary:
    def total(field):
        values = [getattr(node, field) for node in nodes]
        return None if any(value is None for value in values) else sum(values)

    def count(state):
        return sum(node.normalized_state == state for node in nodes)

    gpus = None
    if all(node.gpus is not None for node in nodes):
        groups = {}
        for node in nodes:
            for gpu in node.gpus:
                groups.setdefault(gpu.gpu_type, []).append(gpu)
        gpus = tuple(GPUCount(kind, sum(g.total for g in group),
                              None if any(g.allocated is None for g in group)
                              else sum(g.allocated for g in group))
                     for kind, group in sorted(groups.items(), key=lambda item: item[0] or ""))
    return ResourceSummary(
        len(nodes), count("IDLE"), count("ALLOCATED"), count("MIXED"),
        count("DOWN"), count("DRAIN"), count("UNKNOWN"),
        total("cpus_total"), total("cpus_allocated"), total("cpus_idle"), gpus,
    )


@dataclass(frozen=True)
class ClusterSnapshot:
    captured_at: datetime
    cluster_name: str | None
    current_user: str
    partitions: tuple[PartitionSnapshot, ...]
    nodes: tuple[NodeSnapshot, ...]
    queue: QueueSnapshot | None
    warnings: tuple[str, ...] = ()

    def __post_init__(self):
        if self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must include a timezone")

    @property
    def summary(self) -> ResourceSummary:
        return summarize_nodes(self.nodes)
