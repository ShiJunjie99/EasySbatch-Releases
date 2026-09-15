"""Web advisory flow over structured snapshots and temporary SQLite only."""

from dataclasses import replace
from html.parser import HTMLParser
from html import unescape
import json
import re
import subprocess

from fastapi.testclient import TestClient
import pytest

from sbatch_agent import JobRepository, SlurmClient, SubprocessRunner, SubmissionService
from sbatch_agent.cluster import ClusterUnavailableError
from sbatch_agent.recommender import ResourceRecommender
from sbatch_agent.web import WebConfig, create_app
from sbatch_agent.web_forms import DEFAULT_FORM, profile_key
from test_recommender import node, snapshot, profiles as registered_profiles


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Advisory Web tests must not execute real commands")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(SubprocessRunner, "run", forbidden)
    monkeypatch.setattr(SlurmClient, "submit", forbidden)
    monkeypatch.setattr(SlurmClient, "get_status", forbidden)


class FakeCluster:
    def __init__(self):
        self.calls = 0
        self.snapshot = snapshot()
        self.failure = None

    def get_snapshot(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.snapshot


@pytest.fixture
def config(tmp_path):
    return WebConfig(tmp_path / "jobs.sqlite3", tmp_path / "runs")


@pytest.fixture
def registry():
    return registered_profiles()


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def browser(config, registry, cluster):
    with TestClient(create_app(config, profiles=registry, cluster_service=cluster),
                    base_url="http://localhost") as client:
        yield client


@pytest.fixture
def form(registry):
    return {**DEFAULT_FORM, "name": "advisory example", "project_dir": "/example/project",
            "work_dir": "/example/work", "entrypoint": "hello.py", "executable": "python",
            "args": '["hello.py", "case ; literal"]', "environment": profile_key(registry.environments[0]),
            "partition": "", "memory_mib": "1024", "time_limit_seconds": "120", "cpus_per_task": "4",
            "memory_mode": "explicit", "walltime_mode": "explicit"}


def token(browser):
    return re.search(r'name="csrf_token" value="([^"]+)"', browser.get("/new").text)[1]


def post(browser, path, data):
    return browser.post(path, data={**data, "csrf_token": token(browser)}, follow_redirects=False)


def choice(response):
    return unescape(re.search(r'name="choice" value="([^"]+)"', response.text)[1])


def inputs(response):
    class Values(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}
        def handle_starttag(self, tag, attrs):
            if tag == "input":
                attr = dict(attrs)
                self.values[attr.get("name")] = attr.get("value", "")
    parsed = Values()
    parsed.feed(response.text)
    return parsed.values


def assert_no_records(config):
    with JobRepository(config.database_path) as repo:
        assert repo.list() == []
    assert not config.runs_root.exists()


def test_recommend_button_preview_reasons_snapshot_no_persistence(browser, form, cluster, config, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview/Apply must not call SubmissionService")
    for method in ("create_job", "submit_job", "refresh_status"):
        monkeypatch.setattr(SubmissionService, method, forbidden)
    page = browser.get("/new")
    assert '推荐资源' in page.text and 'formaction="/new/recommend" formnovalidate' in page.text
    assert cluster.calls == 0
    response = post(browser, "/new/recommend", form)
    assert response.status_code == 200 and cluster.calls == 1
    assert "推荐依据与限制" in response.text and '内部排序分数' in response.text
    assert '推荐方案' in response.text and '备选方案' in response.text
    assert cluster.snapshot.captured_at.isoformat() in response.text
    assert inputs(response)["partition"] == ""  # no automatic Apply
    assert_no_records(config)
    applied = post(browser, "/new/apply", {"choice": choice(response)})
    assert applied.status_code == 200 and cluster.calls == 1
    assert inputs(applied)["partition"] == "a"
    assert "已填入建议资源" in applied.text
    assert_no_records(config)


def test_apply_user_override_then_create_uses_latest_user_values(browser, config, form, cluster):
    preview = post(browser, "/new/recommend", form)
    applied = post(browser, "/new/apply", {"choice": choice(preview)})
    values = inputs(applied)
    edited = {**form, **{k: v for k, v in values.items() if k in DEFAULT_FORM}, "cpus_per_task": "6"}
    created = post(browser, "/new", edited)
    assert created.status_code == 303 and cluster.calls == 1
    with JobRepository(config.database_path) as repo:
        record = repo.get(created.headers["location"].split("/")[-1])
        assert record.job_spec.resources.cpus_per_task == 6
        assert record.job_spec.resources.partition == "a"
        assert record.job_spec.run_step.args == ["hello.py", "case ; literal"]
        assert "--cpus-per-task=6" in record.rendered_script
        assert len(repo.list()) == 1 and record.slurm_job_id is None


@pytest.mark.parametrize("kind", ["python", "compiled", "installed"])
def test_three_run_types_can_request_preview_without_full_project(browser, form, kind, config):
    form.update(run_type=kind, name="", project_dir="", work_dir="", entrypoint="", executable="")
    response = post(browser, "/new/recommend", form)
    assert response.status_code == 200 and '推荐方案' in response.text
    assert_no_records(config)  # advisory request is not a renderable JobSpec


@pytest.mark.parametrize("changes, message", [
    ({"time_limit_seconds": "bad"}, "time_limit_seconds"), ({"memory_mib": ""}, "memory_mib"),
    ({"environment": "unknown"}, "运行环境"), ({"preference": "PRIORITY"}, "UserPreference"),
    ({"unresolved": '[{"field":"gpu","reason":"unknown"}]'}, "unresolved"),
    ({"username": "someone"}, "不支持"),
])
def test_invalid_advisory_input_keeps_form_before_query(browser, cluster, form, config, changes, message):
    response = post(browser, "/new/recommend", {**form, **changes})
    assert response.status_code == 400 and message in response.text
    assert "advisory example" in response.text and cluster.calls == 0
    assert_no_records(config)


def test_no_candidates_explains_rejection_and_does_not_lower_request(browser, cluster, form, config):
    form["gpu_count"] = "999"
    response = post(browser, "/new/recommend", form)
    assert response.status_code == 200 and '未找到兼容的资源配置' in response.text
    assert "GPU" in response.text and 'action="/new/apply"' not in response.text
    assert inputs(response)["gpu_count"] == "999"
    assert_no_records(config)


def test_cluster_failure_preserves_manual_creation(browser, cluster, form, config):
    cluster.failure = ClusterUnavailableError("private server data")
    failed = post(browser, "/new/recommend", form)
    assert failed.status_code == 503 and "仍可手动填写" in failed.text
    assert "private server data" not in failed.text
    assert_no_records(config)
    created = post(browser, "/new", {**form, "partition": "manual_partition"})
    assert created.status_code == 303 and cluster.calls == 1


def test_recommender_failure_preserves_manual_creation(config, registry, cluster, form):
    class Broken:
        def recommend(self, **kwargs):
            raise RuntimeError("private traceback detail")
    app = create_app(config, profiles=registry, cluster_service=cluster, recommender=Broken())
    with TestClient(app, base_url="http://localhost") as browser:
        response = post(browser, "/new/recommend", form)
        assert response.status_code == 503 and "private traceback" not in response.text
        assert post(browser, "/new", {**form, "partition": "a"}).status_code == 303


def test_top_three_only_and_explicit_partition_is_fixed(browser, cluster, form):
    cluster.snapshot = snapshot(tuple(node(f"n{i}", name) for i, name in enumerate(("a", "b", "c", "d"))))
    response = post(browser, "/new/recommend", form)
    assert response.text.count('action="/new/apply"') == 3
    response = post(browser, "/new/recommend", {**form, "partition": "b"})
    assert response.text.count('action="/new/apply"') == 1
    assert inputs(post(browser, "/new/apply", {"choice": choice(response)}))["partition"] == "b"


@pytest.mark.parametrize("path", ["/new/recommend", "/new/apply"])
def test_new_posts_enforce_csrf_and_origin_before_queries(browser, cluster, form, path):
    assert browser.post(path, data=form).status_code == 403
    assert browser.post(path, data={**form, "csrf_token": token(browser)}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert cluster.calls == 0


def test_apply_rejects_tampering_and_cross_session_without_query(browser, config, form, cluster):
    signed = choice(post(browser, "/new/recommend", form))
    assert post(browser, "/new/apply", {"choice": signed + "tamper"}).status_code == 400
    with TestClient(browser.app, base_url="http://localhost") as other:
        assert post(other, "/new/apply", {"choice": signed}).status_code == 400
    assert cluster.calls == 1
    assert_no_records(config)


def test_apply_expired_choice_requires_new_preview(browser, form, monkeypatch):
    from itsdangerous import TimestampSigner
    signed = choice(post(browser, "/new/recommend", form))
    original = TimestampSigner.get_timestamp
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: original(self) + 901)
    assert post(browser, "/new/apply", {"choice": signed}).status_code == 400


def test_snapshot_warnings_and_form_values_are_escaped(browser, cluster, form):
    attack = '<script>alert("x")</script>'
    cluster.snapshot = replace(cluster.snapshot, warnings=(attack,))
    response = post(browser, "/new/recommend", {**form, "name": attack})
    assert response.status_code == 200 and "<script>" not in response.text
    assert attack in unescape(response.text)
