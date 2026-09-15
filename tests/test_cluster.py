"""Anonymous CLI fixtures only; no real commands, SSH or cluster access."""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from sbatch_agent.cluster import (
    ClusterService, ClusterUnavailableError, ClusterParseError, SlurmClusterClient,
    QUERIES, parse_nodes, parse_partitions, parse_queue, parse_node_details,
    parse_partition_details, parse_cluster_name,
)
from sbatch_agent.cluster_models import ClusterSnapshot, QueueCounts, summarize_nodes
from sbatch_agent.runner import CommandResult, SlurmCommandError, SubprocessRunner


FIXTURES = Path(__file__).parent / "fixtures" / "cluster"


@pytest.fixture(autouse=True)
def forbid_real_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Cluster unit tests must never run real commands")
    monkeypatch.setattr(SubprocessRunner, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("sbatch_agent.cluster.pwd.getpwuid", lambda uid: SimpleNamespace(pw_name="test-user"))


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeRunner:
    def __init__(self, **overrides):
        self.outputs = {
            "partitions": fixture("partitions.txt"), "nodes": fixture("nodes.txt"),
            "queue": fixture("queue.txt"), "node_details": fixture("node_details.txt"),
            "partition_details": fixture("partition_details.txt"),
            "cluster_name": fixture("config.txt"), **overrides,
        }
        self.calls = []

    def run(self, argv, *, timeout):
        assert isinstance(argv, list)
        name = next(key for key, command in QUERIES.items() if tuple(argv) == command)
        self.calls.append((name, tuple(argv), timeout))
        output = self.outputs[name]
        if isinstance(output, Exception):
            raise output
        if isinstance(output, CommandResult):
            return replace(output, argv=tuple(argv))
        return CommandResult(tuple(argv), 0, output, "")


def service(**overrides):
    runner = FakeRunner(**overrides)
    return ClusterService(SlurmClusterClient(runner, timeout=7)), runner


def test_complete_snapshot_and_fixed_read_only_budget():
    client, runner = service()
    snapshot = client.get_snapshot()
    assert snapshot.cluster_name == "offline-cluster" and snapshot.current_user == "test-user"
    assert snapshot.captured_at.utcoffset().total_seconds() == 0
    assert len(runner.calls) == 6 and all(call[2] == 7 for call in runner.calls)
    assert [call[0] for call in runner.calls] == list(QUERIES)
    assert len(snapshot.nodes) == len(snapshot.partitions) == 3
    assert snapshot.summary.total_nodes == 3  # shared node02 is counted only once
    assert snapshot.summary.total_cpus == 56
    assert snapshot.summary.allocated_cpus == 8
    assert snapshot.summary.idle_cpus == 32
    assert (snapshot.summary.idle_nodes, snapshot.summary.mixed_nodes, snapshot.summary.down_nodes) == (1, 1, 1)
    assert not snapshot.warnings
    cpu, gpu, maintenance = snapshot.partitions
    assert cpu.is_default and cpu.state == "UP" and cpu.allow_accounts == "ALL"
    assert cpu.summary.total_nodes == 2 and cpu.summary.total_cpus == 40
    assert gpu.summary.total_nodes == 1 and gpu.max_time == "2-00:00:00"
    assert gpu.default_time == "00:30:00" and gpu.qos == "partition-limit"
    assert gpu.deny_qos == "blocked" and maintenance.state == "DOWN"
    assert cpu.default_time is None
    assert gpu.queue == QueueCounts(1, 3, 1)
    assert maintenance.queue == QueueCounts()
    node = snapshot.nodes[1]
    assert node.partitions == ("cpu", "gpu")
    assert node.memory_total_mib == 128000 and node.memory_allocated_mib == 8192
    assert node.gpus[0].gpu_type == "a100" and node.gpus[0].allocated == 0
    assert node.gpus[1].gpu_type == "rtx6000" and node.gpus[1].allocated == 2
    assert node.raw_gres_used == "gpu:rtx6000:2(IDX:0,1),gpu:a100:0(IDX:N/A)"
    assert snapshot.queue.total == QueueCounts(2, 4, 1)
    assert snapshot.queue.current_user == QueueCounts(1, 1, 0)
    serialized = json.dumps(asdict(snapshot), default=str)
    assert "other-user" not in serialized and "free_mem" not in serialized
    assert "110000" not in serialized and '"job_id"' not in serialized


def test_partitions_single_multiple_and_default_dedup():
    single = parse_partitions("alpha*|up|infinite|n/a\n")
    assert len(single) == 1 and single[0].name == "alpha" and single[0].is_default
    partitions = parse_partitions(fixture("partitions.txt"))
    assert [p.name for p in partitions] == ["cpu", "gpu", "maintenance"]
    assert parse_partitions("") == ()
    assert parse_partitions("new|future-state|infinite|n/a")[0].raw_state == "future-state"


@pytest.mark.parametrize("line", ["broken", "a|up|infinite", "|up|infinite|n/a", "*|up|1|n/a",
                                  "a||infinite|n/a", "a|up|infinite|n/a|extra",
                                  "a|up|infinite|n/a\na|down|infinite|n/a"])
def test_malformed_partition_output_is_explicit(line):
    with pytest.raises(ClusterParseError):
        parse_partitions(line)


@pytest.mark.parametrize("raw, expected", [
    ("idle", "IDLE"), ("allocated", "ALLOCATED"), ("mixed", "MIXED"),
    ("mixed+planned", "MIXED"), ("down*", "DOWN"), ("drained", "DRAIN"),
    ("draining", "DRAIN"), ("mixed+drain", "DRAIN"), ("idle~", "IDLE"),
    ("future-state", "UNKNOWN"), ("reboot_requested", "UNKNOWN"),
])
def test_node_state_flags_and_future_values(raw, expected):
    nodes = parse_nodes(f"node01|cpu|{raw}|0/8/0/8|32000|(null)|maintenance | window")
    assert nodes[0].raw_state == raw and nodes[0].normalized_state == expected
    assert nodes[0].reason == "maintenance | window"
    assert nodes[0].gpus == ()
    assert nodes[0].memory_allocated_mib is None


@pytest.mark.parametrize("line", ["broken", "n|p|idle|0/8/0/8|1", "n|p|idle|a/8/0/8|1|(null)|none",
                                  "n|p|idle|0/7/0/8|1|(null)|none", "n|p|idle|0/8/0/8|-1|(null)|none",
                                  "n|p|idle|0/8/8|1|(null)|none"])
def test_node_malformed_counts_rejected(line):
    with pytest.raises(ClusterParseError):
        parse_nodes(line)


def test_shared_nodes_conflicting_rows_fail_and_unknown_counts_stay_unknown():
    row = "n|p|idle|0/8/0/8|32000|(null)|none"
    assert len(parse_nodes(row + "\n" + row)) == 1
    with pytest.raises(ClusterParseError):
        parse_nodes(row + "\n" + row.replace("|p|", "|q|").replace("32000", "64000"))
    nodes = parse_nodes("n|p|idle|n/a|n/a|(null)|none")
    assert summarize_nodes(nodes).total_cpus is None


@pytest.mark.parametrize("gres, count, kind", [
    ("gpu:4", 4, None), ("gpu:rtx_2080:4", 4, "rtx_2080"),
    ("gpu:a100:2(S:0-1)", 2, "a100"), ("gpu:a100:2(S:0,1)", 2, "a100"),
    ("gpu:a100:0", 0, "a100"),
])
def test_simple_gres_configuration(gres, count, kind):
    node = parse_nodes(f"n|p|mixed|1/7/0/8|32000|{gres}|none")[0]
    assert node.gpus[0].gpu_type == kind and node.gpus[0].total == count
    assert node.gpus[0].allocated is None


@pytest.mark.parametrize("gres", ["gpu:new-format", "gpu:a100:4(S:0", "gpu:4,shard:40", "gpu:4,mps:400",
                                  "gpu:a100:2,gpu:a100:2", "gpu:a100:4((S:0))"])
def test_unrecognized_gres_keeps_raw_and_warns_without_crash(gres):
    client, _ = service(nodes=f"node01|cpu|idle|0/8/0/8|32000|{gres}|none")
    snapshot = client.get_snapshot()
    assert snapshot.nodes[0].gpus is None and snapshot.nodes[0].raw_gres == gres
    assert snapshot.summary.gpus is None and snapshot.warnings


@pytest.mark.parametrize("gres", ["n/a", "UNKNOWN", ""])
def test_unavailable_gres_is_not_reported_as_zero_gpus(gres):
    node = parse_nodes(f"n|p|idle|0/8/0/8|32000|{gres}|none")[0]
    assert node.gpus is None and summarize_nodes((node,)).gpus is None


def test_queue_reasons_user_arrays_and_other_states():
    snapshot, warnings = parse_queue(fixture("queue.txt"), "test-user")
    assert snapshot.total == QueueCounts(2, 4, 1)
    assert snapshot.current_user == QueueCounts(1, 1, 0)
    assert {r.reason: r.count for r in snapshot.pending_reasons} == {"Priority": 2, "Resources": 1, "Dependency": 1}
    assert not warnings
    empty, _ = parse_queue("", "test-user")
    assert empty.total == empty.current_user == QueueCounts()
    newer, _ = parse_queue("1|NEW_STATE|cpu|other-user|None", "test-user")
    assert newer.total.other_jobs == 1


def test_multi_partition_counts_overlap_but_total_does_not():
    queue, warnings = parse_queue("1|PENDING|cpu,gpu|test-user|Priority\n2+0|RUNNING|gpu|test-user|None", "test-user")
    assert queue.total == QueueCounts(1, 1, 0) and warnings
    assert sum(p.counts.pending_jobs for p in queue.partitions) == 2


def test_queue_does_not_retain_free_text_reason_or_other_users():
    queue, _ = parse_queue("1|PENDING|cpu|private-user|Dependency /private/path|do not publish", "test-user")
    serialized = json.dumps(asdict(queue))
    assert "private" not in serialized and "publish" not in serialized
    assert queue.pending_reasons[0].reason == "UNKNOWN"


@pytest.mark.parametrize("output", ["bad", "1||cpu|u|x", "1|PENDING||u|x", "1|PENDING|cpu||x",
                                    "10_[1-10]|PENDING|cpu|u|Priority", "1|PENDING|cpu,|u|Priority",
                                    "1|RUNNING|cpu|u|None\n1|RUNNING|gpu|u|None"])
def test_malformed_queue_is_not_counted_as_success(output):
    with pytest.raises(ClusterParseError):
        parse_queue(output, "test-user")


@pytest.mark.parametrize("name", ["partitions", "nodes"])
@pytest.mark.parametrize("failure", [CommandResult(("fake",), 1, "", "private raw failure"), "malformed",
                                     SlurmCommandError("timeout", CommandResult(("fake",), None, "", ""))])
def test_core_failure_is_unavailable_without_fake_values(name, failure):
    client, runner = service(**{name: failure})
    with pytest.raises(ClusterUnavailableError, match=name) as caught:
        client.get_snapshot()
    assert caught.value.__cause__ is not None
    assert "private raw failure" not in str(caught.value)
    assert len(runner.calls) <= 2


@pytest.mark.parametrize("name", ["queue", "node_details", "partition_details", "cluster_name"])
@pytest.mark.parametrize("failure", [CommandResult(("fake",), 1, "", "private diagnostic"), "broken output"])
def test_partial_failure_keeps_core_data(name, failure):
    client, runner = service(**{name: failure})
    snapshot = client.get_snapshot()
    assert len(snapshot.nodes) == 3 and len(snapshot.partitions) == 3
    assert len(runner.calls) == 6
    assert any(name in warning for warning in snapshot.warnings)
    assert "private diagnostic" not in str(snapshot.warnings)
    if name == "queue":
        assert snapshot.queue is None and snapshot.partitions[0].queue is None
    if name == "node_details":
        assert snapshot.nodes[1].memory_allocated_mib is None
        assert all(gpu.allocated is None for gpu in snapshot.nodes[1].gpus)


@pytest.mark.parametrize("memory, used", [
    ("-1", "(null)"), ("true", "(null)"), ("8192", "bad"),
    ("8192", "gpu:a100:3(IDX:0-2),gpu:rtx6000:2(IDX:0-1)"),
    ("8192", "gpu:2(IDX:0-1)"),
])
def test_bad_optional_node_values_are_not_invented(memory, used):
    client, _ = service(node_details=f"node02|{memory}|{used}")
    snapshot = client.get_snapshot()
    assert snapshot.warnings
    node = snapshot.nodes[1]
    if memory != "8192":
        assert node.memory_allocated_mib is None
    else:
        assert all(gpu.allocated is None for gpu in node.gpus)


def test_missing_details_and_cli_diagnostics_remain_partial_and_private():
    detail = CommandResult(("fake",), 0, "", "private internal data")
    client, _ = service(node_details=detail, cluster_name="")
    snapshot = client.get_snapshot()
    assert snapshot.cluster_name is None
    assert snapshot.warnings and "private internal data" not in str(snapshot.warnings)


@pytest.mark.parametrize("detail", ["{}", "node01|0", "|0|(null)",
                                    "node01|0|(null)|extra",
                                    "node01|0|(null)\nnode01|1|(null)"])
def test_malformed_detail_shape_becomes_warning(detail):
    client, _ = service(node_details=detail)
    assert client.get_snapshot().warnings


def test_empty_successful_sources_stay_visible_empty_with_warning():
    client, _ = service(partitions="", nodes="", queue="",
                        node_details="", partition_details="", cluster_name="")
    snapshot = client.get_snapshot()
    assert snapshot.summary.total_nodes == 0
    assert snapshot.queue.total == QueueCounts()
    assert snapshot.cluster_name is None and snapshot.warnings


def test_no_polling_no_cache_and_no_username_cli_input(monkeypatch):
    monkeypatch.setenv("USER", "forged user;touch marker")
    client, runner = service()
    first = client.get_snapshot()
    runner.outputs["queue"] = ""
    second = client.get_snapshot()
    assert first.queue.total.pending_jobs == 4 and second.queue.total.pending_jobs == 0
    assert len(runner.calls) == 12 and second.captured_at >= first.captured_at
    assert all("forged" not in str(call) for call in runner.calls)
    assert first.current_user == second.current_user == "test-user"
    with pytest.raises(ValueError):
        client.client.query("nodes; touch marker")
    assert len(runner.calls) == 12


def test_snapshot_is_detached_and_rejects_naive_capture_time():
    client, _ = service()
    snapshot = client.get_snapshot()
    with pytest.raises(FrozenInstanceError):
        snapshot.cluster_name = "changed"
    with pytest.raises(ValueError, match="timezone"):
        replace(snapshot, captured_at=datetime(2026, 1, 1))
    assert replace(snapshot, captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_text_queries_work_without_optional_slurm_json_plugin():
    # Regression: audited 25.05.5 exits with serializer_required for --json.
    # These projections never request JSON or fall back by running extra CLIs.
    client, runner = service()
    snapshot = client.get_snapshot()
    assert len(runner.calls) == 6 and not snapshot.warnings
    assert all("--json" not in arg for command in QUERIES.values() for arg in command)
    assert QUERIES["node_details"][-1] == "--Format=NodeList:0|,AllocMem:0|,GresUsed:0"
    assert QUERIES["partition_details"] == ("scontrol", "--local", "--oneliner", "show", "partitions")
    assert snapshot.cluster_name == "offline-cluster"


def test_node_detail_explicit_fields_preserve_topology_and_deduplicate():
    details = parse_node_details(fixture("node_details.txt"))
    assert len(details) == 3 and details["node02"][0] == 8192
    assert "(IDX:0,1)" in details["node02"][1]
    assert parse_node_details("n|n/a|(null)")["n"][0] is None
    assert parse_node_details("") == {}


def test_partition_access_fields_ignore_unrelated_config():
    details = parse_partition_details(fixture("partition_details.txt"))
    assert details["gpu"] == {"allow_accounts": "research", "allow_qos": "normal",
                              "deny_qos": "blocked", "qos": "partition-limit"}
    assert details["maintenance"]["deny_accounts"] == "blocked"
    assert details["cpu"]["qos"] is None
    assert "TotalCPUs" not in str(details) and "AllocNodes" not in str(details)
    assert parse_partition_details("") == {}


@pytest.mark.parametrize("output", ["State=UP", "PartitionName=", "PartitionName=a PartitionName=b",
                                    "PartitionName=a\nPartitionName=a",
                                    "PartitionName=a AllowQos=ALL AllowQos=normal"])
def test_partition_detail_malformed_whitelisted_fields_rejected(output):
    with pytest.raises(ClusterParseError):
        parse_partition_details(output)


@pytest.mark.parametrize("output", ["", "OtherClusterName=wrong", "ClusterName=",
                                    "ClusterName=two names", "ClusterName=a\nClusterName=b"])
def test_cluster_name_must_be_an_exact_single_config_field(output):
    with pytest.raises(ClusterParseError):
        parse_cluster_name(output)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), "5"])
def test_timeout_validated_before_runner(timeout):
    with pytest.raises(ValueError):
        SlurmClusterClient(FakeRunner(), timeout=timeout)
