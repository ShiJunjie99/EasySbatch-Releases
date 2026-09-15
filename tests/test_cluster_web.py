"""Snapshot rendering tests; intentionally independent of all CLI parsers."""

from dataclasses import replace
from datetime import datetime, timezone
from html import unescape
import subprocess

from fastapi.testclient import TestClient
import pytest

from sbatch_agent.cluster import ClusterUnavailableError, SlurmClusterClient
from sbatch_agent.cluster_models import (
    ClusterSnapshot, NodeSnapshot, GPUCount, PartitionSnapshot, QueueCounts,
    QueueSnapshot, PartitionQueue, PendingReasonCount, summarize_nodes,
)
from sbatch_agent import JobRepository, SlurmClient, StaticProfiles, SubprocessRunner
from sbatch_agent.web import WebConfig, create_app


@pytest.fixture(autouse=True)
def forbid_real_commands(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Dashboard rendering tests must only use a fake ClusterService")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(SubprocessRunner, "run", forbidden)
    monkeypatch.setattr(SlurmClusterClient, "query", forbidden)
    monkeypatch.setattr(SlurmClient, "submit", forbidden)
    monkeypatch.setattr(SlurmClient, "get_status", forbidden)


class FakeClusterService:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.failure = None
        self.calls = 0

    def get_snapshot(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.snapshot


@pytest.fixture
def snapshot():
    node = NodeSnapshot("node-example", ("partition-example",), "MIXED", "mixed",
                        32, 8, 24, 0, 64000, 4096, "gpu:a100:2", "gpu:a100:1(IDX:0)",
                        (GPUCount("a100", 2, 1),), None)
    queue = QueueSnapshot(QueueCounts(7, 11), QueueCounts(2, 3),
                          (PartitionQueue("partition-example", QueueCounts(7, 11)),),
                          (PendingReasonCount("Priority", 8), PendingReasonCount('计算资源', 3)))
    partition = PartitionSnapshot("partition-example", "UP", "up", True, "infinite", None,
                                  summarize_nodes((node,)), queue=QueueCounts(7, 11))
    return ClusterSnapshot(datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc), "example-cluster",
                           "test-user", (partition,), (node,), queue)


@pytest.fixture
def config(tmp_path):
    return WebConfig(tmp_path / "jobs.sqlite3", tmp_path / "runs")


@pytest.fixture
def fake(snapshot):
    return FakeClusterService(snapshot)


@pytest.fixture
def browser(config, fake):
    app = create_app(config, profiles=StaticProfiles(), cluster_service=fake)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def test_dashboard_uses_snapshot_once_and_never_job_repository(browser, fake, config, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Cluster snapshots must not read or write JobRepository")
    for name in ("get", "list", "create", "update_status", "update_submission"):
        monkeypatch.setattr(JobRepository, name, forbidden)
    response = browser.get("/cluster")
    assert response.status_code == 200 and fake.calls == 1
    for value in ('资源汇总与统计口径', '分区概览', '节点', '作业队列', '采集时间',
                  "example-cluster", "2026-01-02T03:04:00+00:00", "partition-example", "node-example",
                  "Priority", '计算资源', '全部可见作业', '我的作业', "7 / 11", "a100", '默认'):
        assert value in response.text
    assert '<a class="button secondary" href="/cluster">刷新快照</a>' in response.text
    assert 'http-equiv="refresh"' not in response.text and 'hx-trigger="every' not in response.text
    assert '<script src="/static/web.js" defer></script>' in response.text
    assert '<script>' not in response.text  # Local enhancement only; no inline code.
    assert not config.runs_root.exists()


def test_only_cluster_get_queries_and_manual_reload_updates(browser, fake):
    for path in ("/", "/new", "/jobs", "/static/web.css"):
        response = browser.get(path)
        assert response.status_code == 200
        if path != "/static/web.css":
            assert 'href="/cluster" class="nav-link' in response.text and '>集群资源</a>' in response.text
    assert fake.calls == 0
    browser.get("/cluster")
    fake.snapshot = replace(fake.snapshot, cluster_name="new-observation")
    response = browser.get("/cluster")
    assert fake.calls == 2 and "new-observation" in response.text
    assert browser.post("/cluster").status_code == 405
    assert fake.calls == 2


def test_unavailable_cluster_is_friendly_503_without_raw_error(browser, fake):
    fake.failure = ClusterUnavailableError("private internal diagnostics should not be a browser traceback")
    response = browser.get("/cluster")
    assert response.status_code == 503 and fake.calls == 1
    assert '集群信息暂不可用' in response.text
    assert "private internal" not in response.text and "Traceback" not in response.text
    assert "example-cluster" not in response.text  # no fabricated/stale snapshot


def test_partial_queue_unavailability_is_not_zero(browser, fake):
    fake.snapshot = replace(fake.snapshot, queue=None, warnings=("queue unavailable",),
                            partitions=(replace(fake.snapshot.partitions[0], queue=None),))
    response = browser.get("/cluster")
    assert response.status_code == 200
    assert "queue unavailable" in response.text and "队列查询不可用" in response.text
    assert "node-example" in response.text


def test_unknown_state_and_missing_gpu_allocation_render(browser, fake):
    node = replace(fake.snapshot.nodes[0], normalized_state="UNKNOWN", raw_state="NEW_STATE",
                   gpus=None, memory_allocated_mib=None, raw_gres="new-gres-format")
    fake.snapshot = replace(fake.snapshot, nodes=(node,), warnings=("unrecognized state",))
    response = browser.get("/cluster")
    assert response.status_code == 200
    assert "NEW_STATE" in response.text and "new-gres-format" in response.text
    assert "不可用 / 64000" in response.text


@pytest.mark.parametrize("field", ["cluster_name", "warnings", "reason", "raw_state", "raw_gres"])
def test_slurm_strings_are_autoescaped(browser, fake, field):
    payload = '<script>alert("unsafe")</script>'
    if field == "warnings":
        fake.snapshot = replace(fake.snapshot, warnings=(payload,))
    elif field == "cluster_name":
        fake.snapshot = replace(fake.snapshot, cluster_name=payload)
    else:
        fake.snapshot = replace(fake.snapshot, nodes=(replace(fake.snapshot.nodes[0], **{field: payload}),))
    response = browser.get("/cluster")
    assert payload in unescape(response.text)
    assert "<script>" not in response.text and "&lt;script&gt;" in response.text


def test_queue_empty_and_cluster_empty_are_distinct_from_failure(browser, fake):
    fake.snapshot = replace(fake.snapshot, nodes=(), partitions=(),
                            queue=QueueSnapshot(QueueCounts(), QueueCounts(), (), ()))
    response = browser.get("/cluster")
    assert response.status_code == 200 and "没有可见节点" in response.text
    assert "没有可见分区" in response.text and "0 / 0" in response.text
