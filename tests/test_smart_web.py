"""Browser Smart workflow; fake model + fake cluster + fake Slurm, real tmp DB."""

from html import unescape
import re
import socket
import subprocess

import pytest
from fastapi.testclient import TestClient

from sbatch_agent.persistence import JobRepository, SubmissionState
from sbatch_agent.runner import SubprocessRunner
from sbatch_agent.smart_web import PreparedStore, PreparedStateError, form_values
from sbatch_agent.web import create_app, WebConfig
from test_smart_service import setup_smart
from test_web import FakeSlurmClient, form, profiles, post


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("No network/process/API/Slurm in Web tests")
    for obj, key in ((subprocess, "Popen"), (SubprocessRunner, "run"), (socket.socket, "connect")):
        monkeypatch.setattr(obj, key, forbidden)
    for key in ("PROVIDER", "MODEL", "ENDPOINT", "API_KEY_ENV", "TIMEOUT"):
        monkeypatch.delenv("SBATCH_AGENT_AI_" + key, raising=False)


def web(tmp_path, **kwargs):
    smart, root, model, cluster = setup_smart(tmp_path, **kwargs)
    config = WebConfig(tmp_path / "jobs.sqlite3", tmp_path / "runs", workspace_root=tmp_path)
    slurm = FakeSlurmClient()
    app = create_app(config, profiles=smart.profiles, slurm_client=slurm, cluster_service=cluster,
                     project_analyzer=smart.analyzer)
    return app, config, root, model, cluster, slurm


def hidden(page, key):
    return unescape(re.search('name="' + key + '" value="([^"]*)"', page.text)[1])


def prepare(browser, root):
    response = post(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "运行 case01"})
    assert response.status_code == 303, response.text
    return response.headers["location"]


def act(browser, url, action, values=None):
    page = browser.get(url)
    # Replay the original browser form after Confirm removed all action forms.
    if 'name="revision"' in page.text:
        browser.smart_form = {"csrf_token": hidden(page, "csrf_token"), "revision": hidden(page, "revision")}
    return browser.post(url + "/" + action, data={**browser.smart_form, **(values or {})}, follow_redirects=False)


def count(config):
    with JobRepository(config.database_path) as repo:
        return len(repo.list())


def test_default_simple_then_final_review_and_confirm_once(tmp_path):
    app, config, root, model, cluster, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        new = browser.get("/new")
        assert '<details id="manual-mode">' in new.text
        simple = new.text.split('aria-label="智能配置"')[1].split("</form>")[0]
        assert '分析并准备' in simple and 'name="partition"' not in simple
        assert '<details id="manual-mode" open>' in browser.get("/new?mode=manual").text
        url = prepare(browser, root)
        page = browser.get(url)
        for text in ('提交前确认', '查看生成脚本', '任务配置详情（JobSpec）', "AI_DIRECT", "ENVIRONMENT_RESOLVER",
                     "RESOURCE_RECOMMENDER", '基于集群快照 · 采集于', "run.py", '确认并提交'):
            assert text in page.text
        assert count(config) == 0 and not fake.submit_calls
        first = act(browser, url, "confirm")
        second = act(browser, url, "confirm")
        assert first.status_code == second.status_code == 303
        assert first.headers["location"] == second.headers["location"]
        assert count(config) == 1 and len(fake.submit_calls) == 1
        assert "SUBMITTED" in browser.get(first.headers["location"]).text
        assert len(model.calls) == cluster.calls == 1
        assert '确认并提交' not in browser.get(url).text
        assert act(browser, url, "continue", {"memory_mib": "512"}).status_code == 409


@pytest.mark.parametrize("missing", [("time_limit_seconds",), ("time_limit_seconds", "memory_mib")])
def test_only_unresolved_questions_and_continue(tmp_path, missing):
    app, config, root, _, cluster, fake = web(tmp_path, omit=missing)
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        page = browser.get(url)
        section = page.text.split('<fieldset id="unresolved">')[1].split("</fieldset>")[0]
        modes = {"memory_mib": "memory_mode", "time_limit_seconds": "walltime_mode"}
        assert set(re.findall('name="([^"]+)"', section)) == set(missing) | {modes[key] for key in missing}
        assert '确认并提交' not in page.text
        response = act(browser, url, "continue", {k: "120" if k == "time_limit_seconds" else "256" for k in missing})
        assert response.status_code == 303
        assert '提交前确认' in browser.get(url).text and count(config) == 0
        assert cluster.calls == 1 and not fake.submit_calls


def test_environment_choices_and_user_override(tmp_path):
    app, config, root, _, _, fake = web(tmp_path, multiple=True)
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        page = browser.get(url)
        assert "python-alt" in page.text and "环境" in page.text
        assert act(browser, url, "continue", {"environment_profile": '["python-alt", "1"]'}).status_code == 303
        assert act(browser, url, "continue", {"partition": "b", "memory_mib": "512"}).status_code == 303
        response = act(browser, url, "confirm")
        with JobRepository(config.database_path) as repo:
            record = repo.get(response.headers["location"].split("/")[-1])
            assert record.job_spec.resources.partition == "b" and record.job_spec.resources.memory_mib == 512
        assert len(fake.submit_calls) == 1


def test_unknown_submission_safety_and_duplicate(tmp_path):
    app, config, root, _, _, fake = web(tmp_path)
    fake.failure = TimeoutError("uncertain")
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        response = act(browser, url, "confirm")
        assert response.status_code == 303
        page = browser.get(response.headers["location"])
        assert "SUBMISSION_UNKNOWN" in page.text and "Retry" not in page.text
        assert act(browser, url, "confirm").status_code == 303
        assert len(fake.submit_calls) == 1


def test_cluster_failure_then_manual_partition(tmp_path):
    app, config, root, _, _, fake = web(tmp_path, cluster_failure=RuntimeError("PRIVATE"))
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        assert "run.py" in browser.get(url).text and "PRIVATE" not in browser.get(url).text
        assert act(browser, url, "continue", {"partition": "a"}).status_code == 303
        assert '提交前确认' in browser.get(url).text and not fake.submit_calls


def test_ai_unavailable_manual_mode_still_creates(tmp_path, form, profiles):
    app, config, root, model, _, fake = web(tmp_path)
    model.failure = RuntimeError("PRIVATE provider diagnostic")
    with TestClient(app, base_url="http://localhost") as browser:
        page = post(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run"})
        assert page.status_code == 503 and '继续手动配置' in page.text
        assert "PRIVATE" not in page.text and count(config) == 0
        manual = {**form, "environment": '["python", "1"]'}
        assert post(browser, "/new", manual).status_code == 303
        assert count(config) == 1 and not fake.submit_calls


@pytest.mark.parametrize("stage", ["continue", "confirm"])
def test_fingerprint_blocks_web_and_keeps_db_empty(tmp_path, stage):
    app, config, root, _, _, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        (root / "run.py").write_text("changed")
        page = act(browser, url, stage)
        assert page.status_code == 400 and "re-analyze" in page.text
        assert '确认并提交' not in page.text
        assert count(config) == 0 and not fake.submit_calls


def test_stale_revision_client_injection_and_other_session_rejected(tmp_path):
    app, config, root, _, _, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        old = browser.get(url)
        assert act(browser, url, "continue", {"memory_mib": "512"}).status_code == 303
        response = browser.post(url + "/confirm", data={"csrf_token": hidden(old, "csrf_token"), "revision": hidden(old, "revision")})
        assert response.status_code == 409
        assert act(browser, url, "confirm", {"job_spec": "{}"}).status_code == 400
        with TestClient(app, base_url="http://localhost") as other:
            other.get("/new")
            assert other.get(url).status_code == 409
        assert not fake.submit_calls and count(config) == 0


def test_browser_unchanged_advanced_values_retain_provenance(tmp_path):
    app, _, root, _, _, _ = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        entry = app.state.prepared_store.entries[url.split("/")[-1]]
        before = dict(entry.prepared.resolved_fields)
        assert act(browser, url, "continue", form_values(entry.prepared)).status_code == 303
        assert entry.prepared.resolved_fields == before


def test_store_expiry_size_and_restart(tmp_path):
    smart, root, _, _ = setup_smart(tmp_path)
    p = smart.prepare(project_dir=root, task_intent="run")
    now = [0]
    store = PreparedStore(ttl=10, max_entries=1, clock=lambda: now[0])
    store.add(p, "owner")
    with pytest.raises(PreparedStateError):
        store.add(p, "owner")
    with pytest.raises(PreparedStateError):
        with store.use(p.id, "other"):
            pass
    now[0] = 11
    with pytest.raises(PreparedStateError):
        with store.use(p.id, "owner"):
            pass
    with pytest.raises(PreparedStateError):
        PreparedStore(max_bytes=10).add(p, "owner")
    with pytest.raises(PreparedStateError):
        with PreparedStore().use(p.id, "owner"):
            pass
