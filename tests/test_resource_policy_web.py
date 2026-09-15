"""Resource modes through real Web contracts with fake model/cluster/Slurm."""

from html import unescape

import pytest
from fastapi.testclient import TestClient

from sbatch_agent.persistence import JobRepository
from sbatch_agent.web_forms import DEFAULT_FORM
from test_presentation import Elements, hx
from test_smart_web import web, hidden, act, count, offline
from test_web import post, form, profiles
from test_resource_policy import rule


def start(browser, root, **values):
    response = post(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run",
        "memory_mode": "cluster_default", "walltime_mode": "cluster_default", **values})
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_smart_and_manual_share_three_modes_with_empty_inactive_inputs(tmp_path):
    app, _, _, model, cluster, _ = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        page = browser.get("/new")
        elements = Elements(page.text).items
        modes = [a for t, a in elements if t == "select" and a.get("name") in {"memory_mode", "walltime_mode"}]
        assert len(modes) == 4
        assert page.text.count('<option value="cluster_default" selected>使用集群默认</option>') == 4
        assert page.text.count('<option value="recommended">智能推荐</option>') == 4
        assert page.text.count('<option value="explicit">手动指定</option>') == 4
        for a in [a for t, a in elements if "data-policy-input" in a]:
            assert a["value"] == "" and "disabled" in a and a["placeholder"].startswith("例如：")
        assert "512 MiB / CPU" not in page.text
        assert "无限运行" not in page.text
    assert not model.calls and cluster.calls == 0


def test_prepare_defaults_final_review_sources_and_shell_without_submission(tmp_path):
    app, config, root, model, cluster, fake = web(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    with TestClient(app, base_url="http://localhost") as browser:
        url = start(browser, root)
        p = app.state.prepared_store.entries[url.rsplit("/", 1)[1]].prepared
        page = browser.get(url)
        assert "提交前确认" in page.text and 'id="unresolved"' not in page.text
        assert page.text.count("集群默认") >= 4
        assert "#SBATCH --mem" not in p.rendered_script and "#SBATCH --time" not in p.rendered_script
        assert p.job_spec.resources.memory_policy.mode == p.job_spec.resources.walltime_policy.mode == "cluster_default"
        assert 'name="memory_mode"' in page.text and 'name="walltime_mode"' in page.text
        assert 'name="memory_mib"' in page.text and "None MiB" not in page.text
    assert count(config) == 0 and not fake.submit_calls and len(model.calls) == cluster.calls == 1


@pytest.mark.parametrize("mode_key,key", [("memory_mode", "memory_mib"), ("walltime_mode", "time_limit_seconds")])
def test_smart_unavailable_recommendation_keeps_choice_and_blocks_review(tmp_path, mode_key, key):
    app, config, root, model, cluster, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        url = start(browser, root, **{mode_key: "recommended"})
        page = browser.get(url)
        assert page.status_code == 200 and 'id="unresolved"' in page.text
        assert '确认并提交' not in page.text and 'value="recommended" selected' in page.text
        assert "请选择使用集群默认或手动指定" in page.text
        assert act(browser, url, "continue", {mode_key: "cluster_default"}).status_code == 303
        assert "提交前确认" in browser.get(url).text
    assert len(model.calls) == cluster.calls == 1 and count(config) == 0 and not fake.submit_calls


@pytest.mark.parametrize("memory_mode,walltime_mode,expected", [
    ("recommended", "cluster_default", (4096, None)),
    ("explicit", "recommended", (8192, 120)),
    ("recommended", "recommended", (4096, 120)),
])
def test_smart_mixed_chinese_sources_and_user_edit(tmp_path, memory_mode, walltime_mode, expected):
    app, config, root, model, cluster, fake = web(tmp_path, extra_profile={"resource_rules": [rule()]})
    with TestClient(app, base_url="http://localhost") as browser:
        url = start(browser, root, memory_mode=memory_mode, walltime_mode=walltime_mode,
                    **({"memory_mib": "8192 MiB"} if memory_mode == "explicit" else {}))
        p = app.state.prepared_store.entries[url.rsplit("/", 1)[1]].prepared
        assert (p.values.memory_mib, p.values.time_limit_seconds) == expected
        page = browser.get(url)
        assert "智能推荐" in page.text and "fixture profile" in page.text and "查看依据" in page.text
        if memory_mode == "explicit":
            assert "用户指定" in page.text and "8192 MiB" in page.text
        assert act(browser, url, "continue", {"memory_mode": "explicit", "memory_mib": "8192 MiB"}).status_code == 303
        p = app.state.prepared_store.entries[url.rsplit("/", 1)[1]].prepared
        assert p.job_spec.resources.memory_policy.mode == "explicit" and p.job_spec.resources.memory_mib == 8192
    assert count(config) == 0 and not fake.submit_calls and len(model.calls) == cluster.calls == 1


def test_recommendation_value_cannot_be_edited_while_claiming_recommended(tmp_path):
    app, config, root, _, _, fake = web(tmp_path, extra_profile={"resource_rules": [rule()]})
    with TestClient(app, base_url="http://localhost") as browser:
        url = start(browser, root, memory_mode="recommended")
        assert act(browser, url, "continue", {"memory_mode": "recommended", "memory_mib": "999999"}).status_code == 303
        p = app.state.prepared_store.entries[url.rsplit("/", 1)[1]].prepared
        assert p.job_spec.resources.memory_mib == 4096 and p.job_spec.resources.memory_policy.mode == "recommended"
        assert act(browser, url, "continue", {"memory_policy": "forged"}).status_code == 400
    assert count(config) == 0 and not fake.submit_calls


@pytest.mark.parametrize("values", [{"memory_mode": "invalid"}, {"walltime_mode": "invalid"},
    {"memory_mode": "explicit", "memory_mib": "0"},
    {"walltime_mode": "explicit", "time_limit_seconds": "00:80:00"}])
def test_bad_policy_input_never_calls_model_or_cluster(tmp_path, values):
    app, config, root, model, cluster, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        page = post(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run",
            "memory_mode": "cluster_default", "walltime_mode": "cluster_default", **values})
        assert 400 <= page.status_code < 500
    assert not model.calls and cluster.calls == 0 and count(config) == 0 and not fake.submit_calls


@pytest.mark.parametrize("memory_mode,walltime_mode", [("cluster_default", "cluster_default"),
    ("explicit", "cluster_default"), ("cluster_default", "explicit"), ("explicit", "explicit")])
def test_manual_modes_same_formal_and_shell_semantics(tmp_path, form, memory_mode, walltime_mode):
    app, config, _, model, cluster, fake = web(tmp_path)
    values = {**form, "environment": '["python", "1"]', "memory_mode": memory_mode, "walltime_mode": walltime_mode,
        "memory_mib": "8192 MiB", "time_limit_seconds": "00:02:00"}
    with TestClient(app, base_url="http://localhost") as browser:
        result = post(browser, "/new", values)
        assert result.status_code == 303, result.text
        with JobRepository(config.database_path) as repo:
            saved = repo.list()[0]
        assert saved.job_spec.resources.memory_policy.mode == memory_mode
        assert saved.job_spec.resources.walltime_policy.mode == walltime_mode
        assert ("#SBATCH --mem=8192M" in saved.rendered_script) == (memory_mode == "explicit")
        assert ("#SBATCH --time=00:02:00" in saved.rendered_script) == (walltime_mode == "explicit")
        page = browser.get(result.headers["location"])
        assert page.status_code == 200 and "None MiB" not in page.text
        assert ("集群默认" in page.text) == ("cluster_default" in {memory_mode, walltime_mode})
    assert not fake.submit_calls and not model.calls and cluster.calls == 0


@pytest.mark.parametrize("mode_key", ["memory_mode", "walltime_mode"])
def test_manual_recommendation_unavailable_is_clear_400_not_500(tmp_path, form, mode_key):
    app, config, _, model, cluster, fake = web(tmp_path)
    values = {**form, "environment": '["python", "1"]', "memory_mode": "cluster_default",
              "walltime_mode": "cluster_default", mode_key: "recommended"}
    with TestClient(app, base_url="http://localhost") as browser:
        page = post(browser, "/new", values)
        assert page.status_code == 400 and "请选择使用集群默认或手动指定" in page.text
        assert 'value="recommended" selected' in page.text
        assert count(config) == 0
    assert not fake.submit_calls and not model.calls and cluster.calls == 0


def test_manual_rule_preview_and_creation_revalidate_trusted_values(tmp_path, form):
    app, config, root, model, cluster, fake = web(tmp_path, extra_profile={"resource_rules": [rule()]})
    values = {**form, "environment": '["python", "1"]', "executable": "python",
        "args": "run.py --input inputs/case01.json", "memory_mode": "recommended", "walltime_mode": "recommended",
        "memory_mib": "999999", "time_limit_seconds": "999999"}
    with TestClient(app, base_url="http://localhost") as browser:
        page = hx(browser, "/new/resource-policies", values)
        assert page.status_code == 200 and "4096 MiB" in page.text and "00:02:00" in page.text
        assert "fixture profile" in page.text and "<html" not in page.text
        assert count(config) == 0
        response = post(browser, "/new", values)
        assert response.status_code == 303, response.text
        with JobRepository(config.database_path) as repo:
            spec = repo.list()[0].job_spec
        assert spec.resources.memory_mib == 4096 and spec.resources.time_limit_seconds == 120
        assert spec.resources.memory_policy.mode == spec.resources.walltime_policy.mode == "recommended"
        page = browser.get(response.headers["location"])
        assert "智能推荐" in page.text and "fixture profile" in page.text
        assert post(browser, "/new", {**values, "cpus_per_task": "2"}).status_code == 400
    assert not fake.submit_calls and not model.calls and cluster.calls == 0


def test_policy_partial_rejects_csrf_and_forged_evidence(tmp_path):
    app, config, _, model, cluster, _ = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        assert browser.post("/new/resource-policies", data=DEFAULT_FORM).status_code == 403
        assert hx(browser, "/new/resource-policies", {**DEFAULT_FORM, "evidence_source": "forged"}).status_code == 400
    assert count(config) == 0 and not model.calls and cluster.calls == 0
