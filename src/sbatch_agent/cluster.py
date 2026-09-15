"""Bounded batch read-only queries and conservative Slurm 25.05 projections."""

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import os
import re

try:  # ``pwd`` is unavailable in the Windows desktop build.
    import pwd
except ImportError:  # pragma: no cover - exercised by Windows packaging smoke
    pwd = None

from .cluster_models import (
    ClusterSnapshot, GPUCount, NodeSnapshot, PartitionSnapshot, PartitionQueue,
    PendingReasonCount, QueueCounts, QueueSnapshot, summarize_nodes,
)
from .runner import CommandRunner, SubprocessRunner, SlurmCommandError, _validate_timeout


# No user-controlled argv, per-node queries, retries, submission or mutation API.
QUERIES = {
    "partitions": ("sinfo", "--local", "--noheader", "--format=%P|%a|%l|%L"),
    "nodes": ("sinfo", "--local", "--noheader", "--Node", "--exact",
              "--format=%N|%R|%T|%C|%m|%G|%E"),
    "queue": ("squeue", "--local", "--noheader", "--array", "--states=all",
              "--format=%i|%T|%P|%u|%r"),
    "node_details": ("sinfo", "--local", "--noheader", "--Node", "--exact",
                     "--Format=NodeList:0|,AllocMem:0|,GresUsed:0"),
    "partition_details": ("scontrol", "--local", "--oneliner", "show", "partitions"),
    "cluster_name": ("scontrol", "--local", "show", "config"),
}


class ClusterUnavailableError(RuntimeError):
    """Required resource query failed; cause retains diagnostics, never a fake snapshot."""


class ClusterParseError(ValueError):
    """Malformed projection; messages do not contain raw queue rows or usernames."""


class SlurmClusterClient:
    """Fixed read-only commands using the existing current-process Runner."""

    def __init__(self, runner: CommandRunner | None = None, *, timeout: float = 10):
        _validate_timeout(timeout)
        self.runner = runner if runner is not None else SubprocessRunner()
        self.timeout = timeout

    def query(self, name: str):
        if name not in QUERIES:
            raise ValueError("Unknown read-only cluster query")
        result = self.runner.run(list(QUERIES[name]), timeout=self.timeout)
        if result.returncode != 0:
            raise SlurmCommandError(f"Cluster {name} query failed (exit {result.returncode})", result)
        return result


def _optional(value):
    return None if value is None or value.strip().lower() in {"", "(null)", "n/a", "none", "unknown"} else value.strip()


def _number(value: str) -> int | None:
    if _optional(value) is None:
        return None
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ClusterParseError("Invalid nonnegative resource count")
    return int(value)


def parse_partitions(output: str) -> tuple[PartitionSnapshot, ...]:
    partitions = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 4 or not fields[0] or not fields[1]:
            raise ClusterParseError("Expected partition|availability|max_time|default_time")
        name, state, max_time, default_time = fields
        default = name.endswith("*")
        name = name.removesuffix("*")
        if not name:
            raise ClusterParseError("Missing partition name")
        partition = PartitionSnapshot(name, state.upper(), state, default,
                                      _optional(max_time), _optional(default_time))
        if name in partitions and partitions[name] != partition:
            raise ClusterParseError("Conflicting partition rows")
        partitions[name] = partition  # sinfo may split a partition into state/config groups
    return tuple(partitions[key] for key in sorted(partitions))


def _node_state(raw: str) -> str:
    # Extended sinfo state can contain suffix flags (*, ~, #, etc.). Never call
    # total-allocated an idle count or ignore a DOWN/DRAIN flag in state totals.
    state = raw.upper().rstrip("*~#+!%$@^-=")
    flags = state.split("+")
    if "DOWN" in flags:
        return "DOWN"
    if any(flag in {"DRAIN", "DRAINED", "DRAINING"} for flag in flags):
        return "DRAIN"
    return flags[0] if flags[0] in {"IDLE", "ALLOCATED", "MIXED"} else "UNKNOWN"


def _gres_counts(raw: str | None) -> dict[str | None, int] | None:
    """Only whole GPU counts with optional S/IDX numeric topology annotations.

    A new/shared GRES grammar is unavailable, never guessed or partly totaled.
    Commas in parenthesized IDX/S ranges are not resource separators.
    """
    if raw is None or raw.strip().lower() in {"", "n/a", "unknown"}:
        return None
    if raw.strip().lower() in {"(null)", "none"}:
        return {}
    entries, start, depth = [], 0, 0
    for i, char in enumerate(raw):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            entries.append(raw[start:i])
            start = i + 1
        if depth not in {0, 1}:
            return None
    if depth:
        return None
    entries.append(raw[start:])
    counts = {}
    for entry in entries:
        match = re.fullmatch(
            r"gpu:(?:([A-Za-z0-9_.-]+):)?([0-9]+)(?:\((?:S|IDX):(?:[0-9,\-]+|N/A)\))?", entry,
        )
        if not match:
            # Ordinary unrelated GRES don't change whole-GPU totals. Shared
            # GPUs (mps/shard) may overlap physical devices: decline summary.
            if re.fullmatch(r"(?!gpu:|mps:|shard:)[A-Za-z0-9_.-]+:[0-9]+", entry):
                continue
            return None
        kind, count = match.groups()
        if kind in counts:
            return None  # duplicate type/unknown layout, not additive evidence
        counts[kind] = int(count)
    return counts


def _gpu_summary(raw, used=None):
    totals, allocated = _gres_counts(raw), _gres_counts(used)
    if totals is None:
        return None
    # Missing used types do not imply zero; keep available counts by exact type.
    return tuple(GPUCount(kind, total, allocated.get(kind) if allocated is not None else None)
                 for kind, total in sorted(totals.items(), key=lambda item: item[0] or ""))


def parse_nodes(output: str) -> tuple[NodeSnapshot, ...]:
    nodes = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        # Reason is last and can itself contain '|'; never query setter username.
        fields = [field.strip() for field in line.split("|", 6)]
        if len(fields) != 7 or not all(fields[:3]):
            raise ClusterParseError("Expected seven node projection fields")
        name, partition, state, cpus, memory, gres, reason = fields
        values = cpus.split("/") if _optional(cpus) is not None else [None] * 4
        if len(values) != 4:
            raise ClusterParseError("Expected allocated/idle/other/total CPUs")
        allocated, idle, other, total = map(_number, values)
        if all(value is not None for value in (allocated, idle, other, total)):
            if allocated + idle + other != total:
                raise ClusterParseError("Inconsistent Slurm CPU counters")
        node = NodeSnapshot(name, (partition,), _node_state(state), state,
                            total, allocated, idle, other, _number(memory), None,
                            gres, None, _gpu_summary(gres), _optional(reason))
        previous = nodes.get(name)
        if previous:
            if replace(previous, partitions=(partition,)) != node:
                raise ClusterParseError("Conflicting rows for shared node")
            node = replace(node, partitions=tuple(sorted(set(previous.partitions + (partition,)))))
        nodes[name] = node
    return tuple(nodes[key] for key in sorted(nodes))


def parse_queue(output: str, current_user: str) -> tuple[QueueSnapshot, tuple[str, ...]]:
    total, mine, reasons, partitions = Counter(), Counter(), Counter(), {}
    seen, warnings = set(), []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split("|", 4)]
        if len(fields) != 5 or not all(fields[:4]):
            raise ClusterParseError("Expected five queue projection fields")
        job_id, state, partition, user, reason = fields
        # --array expands pending arrays. Count visible elements and heterogeneous
        # components, never count compressed expressions as one whole array.
        if re.fullmatch(r"[0-9]+(?:_[0-9]+)?(?:\+[0-9]+)?", job_id) is None or job_id in seen:
            raise ClusterParseError("Invalid/duplicate expanded queue record")
        seen.add(job_id)
        field = {"RUNNING": "running_jobs", "PENDING": "pending_jobs"}.get(state, "other_jobs")
        total[field] += 1
        if user == current_user:
            mine[field] += 1
        names = partition.split(",")
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ClusterParseError("Invalid partition list in queue record")
        if len(names) > 1:
            warnings.append("Queue contains multi-partition jobs; partition counts overlap and must not be summed.")
        for name in names:
            partitions.setdefault(name, Counter())[field] += 1
        if state == "PENDING":
            # %r is the reason code, NOT %R's potentially private reason text.
            # Unknown/free text is grouped, never exposed as a job-specific value.
            safe_reason = reason if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", reason) else "UNKNOWN"
            reasons[safe_reason] += 1
    return QueueSnapshot(
        QueueCounts(**total), QueueCounts(**mine),
        tuple(PartitionQueue(name, QueueCounts(**counts)) for name, counts in sorted(partitions.items())),
        tuple(PendingReasonCount(reason, count) for reason, count in sorted(reasons.items())),
    ), tuple(dict.fromkeys(warnings))


def parse_node_details(output: str) -> dict[str, tuple[int | None, str]]:
    records = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 3 or not fields[0]:
            raise ClusterParseError("Expected node|allocated_memory|gres_used")
        name, memory, used = fields
        value = (_number(memory), used)
        if name in records and records[name] != value:
            raise ClusterParseError("Conflicting node detail rows")
        records[name] = value
    return records


def parse_partition_details(output: str) -> dict[str, dict[str, str | None]]:
    # Slurm 25.05.5 on the audited server lacks the JSON serializer plugin.
    # Only these whitespace-free key=value fields from --oneliner are consumed;
    # no broad regex over prose, no interpretation of arbitrary extra fields.
    keys = {"PartitionName": "name", "AllowAccounts": "allow_accounts",
            "DenyAccounts": "deny_accounts", "AllowQos": "allow_qos",
            "DenyQos": "deny_qos", "QoS": "qos"}
    records = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        values = {}
        for token in line.split():
            key, separator, value = token.partition("=")
            if separator and key in keys:
                if key in values:
                    raise ClusterParseError("Duplicate partition detail field")
                values[key] = value
        name = values.pop("PartitionName", None)
        if not name or name in records:
            raise ClusterParseError("Missing/duplicate partition detail name")
        records[name] = {keys[key]: _optional(value) for key, value in values.items()}
    return records


def parse_cluster_name(output: str) -> str:
    names = []
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "ClusterName":
            names.append(value.strip())
    if len(names) != 1 or not names[0] or any(char.isspace() for char in names[0]):
        raise ClusterParseError("Missing/invalid ClusterName configuration field")
    return names[0]


class ClusterService:
    """One bounded observation under current Linux identity. No DB or cache."""

    def __init__(self, client: SlurmClusterClient | None = None, *, current_user: str | None = None):
        if current_user is not None and (
            not isinstance(current_user, str) or not current_user
            or not current_user.isprintable() or "|" in current_user
        ):
            raise ValueError("current_user must be printable nonblank text without '|'")
        self.client = client if client is not None else SlurmClusterClient()
        self.current_user = current_user

    def get_snapshot(self) -> ClusterSnapshot:
        warnings = []
        if self.current_user is not None:
            current_user = self.current_user
        elif pwd is not None:
            current_user = pwd.getpwuid(os.geteuid()).pw_name
        else:  # A desktop caller should always pass the configured SSH user.
            raise ClusterUnavailableError("Current cluster user is unavailable.")

        def query(name, parser, *, required=False):
            try:
                result = self.client.query(name)
                parsed = parser(result.stdout)
                if result.stderr.strip():
                    warnings.append(f"{name}: CLI diagnostic received; raw text omitted.")
                return parsed
            except (SlurmCommandError, ClusterParseError, TypeError, KeyError, AttributeError) as exc:
                if required:
                    raise ClusterUnavailableError(f"Cluster information unavailable: {name} query failed.") from exc
                warnings.append(f"{name} unavailable; values are unknown, not zero.")
                return None

        partitions = query("partitions", parse_partitions, required=True)
        nodes = query("nodes", parse_nodes, required=True)
        queue_result = query("queue", lambda text: parse_queue(text, current_user))
        queue = None
        if queue_result:
            queue, messages = queue_result
            warnings.extend(messages)

        node_details = query("node_details", parse_node_details)
        partition_details = query("partition_details", parse_partition_details)
        cluster_name = query("cluster_name", parse_cluster_name)

        enriched = []
        for node in nodes:
            if node_details is not None:
                detail = node_details.get(node.name)
                if detail is not None:
                    allocated, used = detail
                    if allocated is None:
                        warnings.append(f"{node.name}: Slurm allocated memory unavailable.")
                    gpus = _gpu_summary(node.raw_gres, used)
                    if gpus and any(g.allocated is not None and g.allocated > g.total for g in gpus):
                        warnings.append(f"{node.name}: GPU counts changed between queries; allocation unavailable.")
                        gpus = _gpu_summary(node.raw_gres)
                    node = replace(node, memory_allocated_mib=allocated, raw_gres_used=used, gpus=gpus)
                else:
                    warnings.append(f"{node.name}: optional node details absent.")
            if node.gpus is None:
                warnings.append(f"{node.name}: unsupported GRES format; only raw configuration is available.")
            elif any(g.allocated is None for g in node.gpus):
                warnings.append(f"{node.name}: GPU allocation by type unavailable.")
            if node.normalized_state == "UNKNOWN":
                warnings.append(f"{node.name}: unfamiliar node state; raw state retained.")
            enriched.append(node)
        nodes = tuple(enriched)
        result_partitions = []
        queue_by_partition = {item.partition: item.counts for item in queue.partitions} if queue else {}
        for partition in partitions:
            members = tuple(node for node in nodes if partition.name in node.partitions)
            partition = replace(partition, summary=summarize_nodes(members),
                                queue=queue_by_partition.get(partition.name, QueueCounts()) if queue else None)
            if partition_details is not None:
                detail = partition_details.get(partition.name)
                if detail is not None:
                    partition = replace(partition, **detail)
                else:
                    warnings.append(f"{partition.name}: optional access configuration absent.")
            result_partitions.append(partition)
        known = {partition.name for partition in partitions}
        if any(name not in known for node in nodes for name in node.partitions):
            warnings.append("Node/partition membership changed between queries or is partially visible.")
        if not nodes or not partitions:
            warnings.append("No visible nodes or partitions in successful resource queries.")
        return ClusterSnapshot(
            datetime.now(timezone.utc), cluster_name,
            current_user, tuple(result_partitions), nodes, queue, tuple(dict.fromkeys(warnings)),
        )
