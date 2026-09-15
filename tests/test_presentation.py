"""M8-B offline presentation regression; no real network, programs or Slurm."""

from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
import hashlib
import json
import re
import socket
import subprocess

import pytest
from fastapi.testclient import TestClient

from sbatch_agent.presentation import build_file_tree, field_source_badge, status_badge
from sbatch_agent.scanner import ProjectScanner
from sbatch_agent.scanner_models import ScannedFile
from sbatch_agent.model_client import ModelUnavailableError
from sbatch_agent.slurm import SlurmClient, JobState
from sbatch_agent.web import create_app, WebConfig
from sbatch_agent.web_forms import DEFAULT_FORM
from test_smart_web import web, hidden, prepare, act, count
from test_web import observation
from test_server_catalog import catalog, registry


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Presentation tests cannot access real network, programs or Slurm")
    for obj, name in ((subprocess, "Popen"), (socket.socket, "connect"), (socket, "create_connection"),
                      (SlurmClient, "submit"), (SlurmClient, "get_status")):
        monkeypatch.setattr(obj, name, forbidden)
    monkeypatch.setattr("sbatch_agent.web.model_client_from_env", lambda: None)


def hx(browser, url, values):
    csrf = hidden(browser.get("/new"), "csrf_token")
    return browser.post(url, data={"csrf_token": csrf, **values}, headers={"HX-Request": "true"}, follow_redirects=False)


def assert_partial(response, status=200):
    assert response.status_code == status, response.text
    assert "<html" not in response.text and "<!doctype" not in response.text
    assert "HX-Request" in response.headers["vary"].split(", ")
    assert response.headers["cache-control"] == "no-store"


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.items = []
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        self.items.append((tag, dict(attrs)))


@pytest.mark.parametrize("path", ["/", "/new", "/jobs", "/cluster"])
def test_layout_local_assets_active_navigation(tmp_path, path):
    app, _, _, _, _, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        page = browser.get(path)
        assert page.status_code == 200
        items = Elements(page.text).items
        nav = [a for tag, a in items if tag == "a" and "nav-link" in a.get("class", "")]
        assert [a["href"] for a in nav] == ["/new", "/jobs", "/cluster"]
        assert [a["href"] for a in nav if a.get("aria-current")] == ([path] if path != "/" else [])
        assets = [a.get("src", a.get("href")) for tag, a in items if tag in {"script", "link"}]
        assert len(assets) == 4 and all(url.startswith("/static/") for url in assets)
        for url in assets:
            assert browser.get(url).status_code == 200
        assert not fake.submit_calls


def test_csp_no_eval_inline_history_and_no_polling(tmp_path):
    app, *_ = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        page = browser.get("/new")
        config = next(a for tag, a in Elements(page.text).items if tag == "meta" and a.get("name") == "htmx-config")
        flags = json.loads(config["content"])
        assert all(flags[k] is False for k in ("allowEval", "allowScriptTags", "historyEnabled", "historyRestoreAsHxRequest", "includeIndicatorStyles"))
        assert flags["selfRequestsOnly"] is True
        csp = page.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "connect-src 'self'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        js = browser.get("/static/web.js").text
        assert all(word not in js for word in ("eval(", "new Function", "setInterval", "localStorage", "innerHTML"))
        assert 'hx-trigger="every' not in page.text and "hx-boost" not in page.text


def test_hx_prepare_ready_partial_no_record_and_normal_fallback(tmp_path):
    app, config, root, model, cluster, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        response = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "运行 case01"})
        assert_partial(response)
        assert '提交前确认' in response.text and '需要确认' not in response.text
        assert "data-prepared-id" in response.text
        assert count(config) == 0 and not fake.submit_calls and len(model.calls) == cluster.calls == 1
        url = prepare(browser, root)
        assert "<html" in browser.get(url).text


def test_hx_unresolved_continue_sources_review_and_no_submit(tmp_path):
    app, config, root, model, cluster, fake = web(tmp_path, omit=("memory_mib", "time_limit_seconds"))
    with TestClient(app, base_url="http://localhost") as browser:
        page = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "运行 case01"})
        assert_partial(page)
        assert '需要确认 2 项' in page.text
        assert page.text.index('id="unresolved"') < page.text.index('aria-label="任务配置"')
        identifier = re.search('data-prepared-id="([^"]+)"', page.text)[1]
        done = hx(browser, f"/new/prepared/{identifier}/continue", {"revision": hidden(page, "revision"), "memory_mib": "256", "time_limit_seconds": "120"})
        assert_partial(done)
        assert '需要确认' not in done.text and '提交前确认' in done.text
        for label in ('有明确依据', '已自动匹配', '已推荐', '用户指定', '任务', '软件与环境', '计算资源'):
            assert label in done.text
        items = Elements(done.text).items
        collapsed = [a for tag, a in items if tag == "details" and a.get("id") in {"shell-preview", "jobspec-summary", "advanced-settings"}]
        assert len(collapsed) == 3 and all("open" not in a for a in collapsed)
        confirm = next(a for tag, a in items if tag == "form" and a.get("action", "").endswith("/confirm"))
        assert confirm["method"] == "post" and not any(k.startswith("hx-") for k in confirm)
        assert '此操作将提交真实 Slurm 作业。' in done.text
        assert count(config) == 0 and not fake.submit_calls and len(model.calls) == cluster.calls == 1


@pytest.mark.parametrize("failure", ["model", "validation", "expired", "csrf"])
def test_hx_errors_are_sanitized_partials_keep_safety(tmp_path, failure):
    app, config, root, model, _, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        if failure == "model":
            def fail(**kwargs):
                raise ModelUnavailableError("PRIVATE_TOKEN")
            model.generate_structured = fail
            response = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "test"})
            expected = 503
        elif failure == "csrf":
            response = browser.post("/new/prepare", data={"csrf_token": "invalid"}, headers={"HX-Request": "true"})
            expected = 403
        else:
            url = prepare(browser, root)
            revision = hidden(browser.get(url), "revision")
            response = hx(browser, url + "/continue", {"revision": "wrong" if failure == "expired" else revision, "memory_mib": "bad"})
            expected = 409 if failure == "expired" else 400
        assert_partial(response, expected)
        assert 'role="alert"' in response.text and "PRIVATE_TOKEN" not in response.text
        assert '确认并提交' not in response.text
        assert count(config) == 0 and not fake.submit_calls


def test_hx_scan_analyze_no_nested_forms_or_domain_mutation(tmp_path):
    app, config, root, model, cluster, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        scan = hx(browser, "/new/scan", {**DEFAULT_FORM, "project_dir": str(root)})
        assert_partial(scan)
        assert "data-file-tree" in scan.text and '分析项目' in scan.text
        assert not model.calls and cluster.calls == 0
        analysis = hx(browser, "/new/analyze", {"scan_token": hidden(scan, "scan_token"), "task_intent": "运行 case01"})
        assert_partial(analysis)
        assert 'AI 分析结果' in analysis.text and "DIRECT" in analysis.text
        assert len(model.calls) == 1 and cluster.calls == 0 and not fake.submit_calls and count(config) == 0
        stack = 0
        for tag in re.findall(r"</?form\b", browser.get("/new").text):
            stack += -1 if tag.startswith("</") else 1
            assert stack in {0, 1}
        assert stack == 0


@pytest.mark.parametrize("state", [JobState.PENDING, JobState.RUNNING, JobState.COMPLETED, JobState.UNKNOWN])
def test_hx_status_partial_does_not_submit(tmp_path, state):
    app, config, root, _, _, fake = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        url = prepare(browser, root)
        submitted = act(browser, url, "confirm")  # fake only; validates real guard
        job = submitted.headers["location"]
        fake.states = [observation(state)]
        result = hx(browser, job + "/refresh", {})
        assert_partial(result)
        assert state.value in result.text and 'hx-target="#job-status"' not in result.text
        assert f'action="{job}/refresh"' in browser.get("/jobs").text
        assert len(fake.submit_calls) == 1 and fake.status_calls == ["123"]
        assert "script-snapshot" not in result.text and '确认并提交' not in result.text
        assert act(browser, url, "confirm").headers["location"] == job and len(fake.submit_calls) == 1


def test_offline_snapshot_label_cannot_be_hidden(tmp_path):
    app, _, root, _, cluster, _ = web(tmp_path)
    original = cluster.get_snapshot
    cluster.get_snapshot = lambda: replace(original(), warnings=("Based on TEST/OFFLINE cluster snapshot",))
    with TestClient(app, base_url="http://localhost") as browser:
        response = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "运行 case01"})
        assert "Based on TEST/OFFLINE cluster snapshot" in response.text
        assert '基于集群快照 · 采集于' not in response.text
        assert "Based on TEST/OFFLINE cluster snapshot" in browser.get("/cluster").text


def test_catalog_choices_compact_but_verification_preserved(tmp_path):
    app = create_app(WebConfig(tmp_path / "db.sqlite3", tmp_path / "runs"), profiles=registry(), catalog=catalog())
    with TestClient(app, base_url="http://localhost") as browser:
        page = browser.get("/new")
        options = re.findall(r"<option[^>]*>(.*?)</option>", page.text)
        assert any('GROMACS / 1 · 已复核' in option for option in options)
        assert all("/opt/example/" not in option and '上次复核' not in option for option in options)
        assert "上次复核：" in page.text


@pytest.fixture
def tree_evidence(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    for path, content in {"README.md": "python src/main.py --input config/input.json\n", "src/main.py": 'if __name__ == "__main__":\n    pass\n',
                          "src/utils.py": "# helpers\n", "config/input.json": "{}", "tests/check.txt": "fixture", "requirements.txt": "numpy\n", "Makefile": "all:\n\ttrue\n", "run.sbatch": "#!/bin/bash\n#SBATCH --mem=256M\n"}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    (root / "outputs").mkdir()
    return ProjectScanner().scan(root)


def flatten(node):
    return [node, *(item for child in node.children for item in flatten(child))]


def test_tree_hierarchy_tags_expansion_relative_and_no_mutation(tree_evidence):
    before = tree_evidence.model_dump_json()
    tree = build_file_tree(tree_evidence)
    nodes = {node.relative_path: node for node in flatten(tree.root)}
    assert tree.root.expanded and nodes["src"].expanded and nodes["config"].expanded
    assert not nodes["tests"].expanded
    for path, tag in (("README.md", "Evidence"), ("src/main.py", "Entrypoint"), ("config/input.json", "Input"), ("run.sbatch", "SBATCH"), ("Makefile", "Build"), ("requirements.txt", "Environment")):
        assert tag in nodes[path].tags
    assert "Entrypoint" not in nodes["README.md"].tags
    assert all(not path.startswith("/") for path in nodes)
    assert tree_evidence.model_dump_json() == before


@pytest.mark.parametrize("limit", [1, 5, 80, 240])
def test_tree_large_output_is_bounded_and_counts_omissions(tree_evidence, limit):
    files = [ScannedFile(path=f"ordinary/file{i}.txt", size_bytes=0, status="metadata_only") for i in range(3000)]
    evidence = tree_evidence.model_copy(update={"files": (*tree_evidence.files, *files), "skipped_directories": ("outputs",)})
    tree = build_file_tree(evidence, max_nodes=limit)
    nodes = flatten(tree.root)
    assert len(nodes) == tree.rendered_nodes <= limit
    assert sum(not n.is_directory for n in nodes) + tree.omitted_files == tree.root.file_count == len(evidence.files)


def test_large_tree_html_and_debug_details_are_bounded(tmp_path, tree_evidence):
    app, config, root, _, _, fake = web(tmp_path)
    evidence = tree_evidence.model_copy(update={"files": tuple(ScannedFile(path=f"ordinary/f{i}.txt", size_bytes=0, status="metadata_only") for i in range(3000))})
    # Only the presentation route's injected scanner; no extra filesystem reads.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ProjectScanner, "scan", lambda *args: evidence)
        with TestClient(app, base_url="http://localhost") as browser:
            result = hx(browser, "/new/scan", {**DEFAULT_FORM, "project_dir": str(root)})
            assert_partial(result)
            assert result.text.count("data-tree-node=") <= 240
            assert len(result.content) < 250_000
            assert '个文件未展示' in result.text
            assert count(config) == 0 and not fake.submit_calls


def test_skipped_directory_is_not_empty_or_expanded(tree_evidence):
    tree = build_file_tree(tree_evidence.model_copy(update={"skipped_directories": ("outputs",)}))
    skipped = next(n for n in flatten(tree.root) if n.relative_path == "outputs")
    assert skipped.is_skipped and "Skipped" in skipped.tags and not skipped.expanded


@pytest.mark.parametrize("path", ["/private/not-relative", "../escape", "a/../../bad", "/"])
def test_invalid_tree_paths_are_not_displayed(tree_evidence, path):
    item = ScannedFile(path=path, size_bytes=0, status="skipped")
    tree = build_file_tree(tree_evidence.model_copy(update={"files": (item,), "skipped_directories": ()}))
    assert not tree.root.children


@pytest.mark.parametrize("source,label", [("SERVER_CATALOG", '软件目录'), ("AI_DIRECT", '有明确依据'), ("AI_INFERRED", '系统推断'), ("RESOURCE_RECOMMENDER", '已推荐'), ("USER", '用户指定'), ("PROJECT_EVIDENCE", '项目依据'), ("UNRESOLVED", '需要确认')])
def test_source_labels_do_not_promote_inference(source, label):
    badge = field_source_badge(source)
    assert badge.label == label
    assert "Verified" not in badge.label
    if source == "AI_INFERRED":
        assert badge.tone != field_source_badge("AI_DIRECT").tone


@pytest.mark.parametrize("state,tone", [("COMPLETED", "success"), ("RUNNING", "primary"), ("PENDING", "warning"), ("FAILED", "danger"), ("UNKNOWN", "secondary"), ("SUBMISSION_UNKNOWN", "warning")])
def test_status_badge_is_text_and_consistent(state, tone):
    labels = {"COMPLETED": "已完成", "RUNNING": "运行中", "PENDING": "排队中",
              "FAILED": "运行失败", "UNKNOWN": "状态未知", "SUBMISSION_UNKNOWN": "提交结果未知"}
    assert status_badge(state).label == labels[state] and status_badge(state).tone == tone
    assert status_badge(state).description == state


def test_large_branch_does_not_hide_other_relevant_files(tree_evidence):
    files = tuple(ScannedFile(path=f"src/ordinary{i}.txt", size_bytes=0, status="metadata_only") for i in range(1000))
    tree = build_file_tree(tree_evidence.model_copy(update={"files": (*tree_evidence.files, *files)}), max_nodes=30)
    paths = {n.relative_path for n in flatten(tree.root)}
    assert {'README.md', 'src/main.py', 'config/input.json'} <= paths


def test_smart_and_manual_dom_ids_labels_are_unambiguous(tmp_path):
    app, _, root, _, _, _ = web(tmp_path, omit=("memory_mib",))
    with TestClient(app, base_url="http://localhost") as browser:
        base = browser.get('/new')
        smart = hx(browser, '/new/prepare', {'project_dir': str(root), 'task_intent': '运行 case01'})
        scan = hx(browser, '/new/scan', {**DEFAULT_FORM, 'project_dir': str(root)})
        elements = Elements(base.text + smart.text + scan.text).items
        ids = [a['id'] for _, a in elements if 'id' in a]
        assert len(ids) == len(set(ids))
        for tag, attrs in elements:
            if tag == 'label' and 'for' in attrs:
                assert attrs['for'] in ids


def test_tree_html_escapes_project_file_names(tmp_path, tree_evidence, monkeypatch):
    app, _, root, _, _, _ = web(tmp_path)
    evidence = tree_evidence.model_copy(update={'files': (ScannedFile(path='<script>alert(1)</script>.txt', size_bytes=0, status='metadata_only'),)})
    monkeypatch.setattr(ProjectScanner, 'scan', lambda *args: evidence)
    with TestClient(app, base_url='http://localhost') as browser:
        page = hx(browser, '/new/scan', {**DEFAULT_FORM, 'project_dir': str(root)})
        assert_partial(page)
        assert '<script>' not in page.text and '&lt;script&gt;' in page.text


def test_hx_status_failure_keeps_saved_state_and_no_submit(tmp_path):
    app, _, root, _, _, fake = web(tmp_path)
    with TestClient(app, base_url='http://localhost') as browser:
        url = prepare(browser, root)
        job = act(browser, url, 'confirm').headers['location']
        fake.states = [observation(JobState.RUNNING), RuntimeError('query failure')]
        assert_partial(hx(browser, job + '/refresh', {}))
        failed = hx(browser, job + '/refresh', {})
        assert_partial(failed, 503)
        assert 'RUNNING' in failed.text and '保留最近一次结果' in failed.text
        assert len(fake.submit_calls) == 1 and len(fake.status_calls) == 2


@pytest.mark.parametrize('name,digest', [
    ('tabler-1.5.0/tabler.min.css', '4cdeade29286540dff94acfeb6ea9ea6a16bad4a64ff5604f659414b7c954cd5'),
    ('htmx-2.0.10/htmx.min.js', '71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de'),
])
def test_vendored_assets_are_versioned_unmodified_and_licensed(name, digest):
    root = Path(__file__).parents[1] / 'src/sbatch_agent/static/vendor'
    asset = root / name
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == digest
    assert (asset.parent / 'LICENSE').is_file()
    assert sum(p.stat().st_size for p in root.rglob('*') if p.is_file()) < 800_000
