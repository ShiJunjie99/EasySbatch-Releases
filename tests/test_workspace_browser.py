"""Metadata-only folder picker: no network, model, Slurm or file execution."""

import errno
from pathlib import Path
import os
import socket
import subprocess

import pytest
from fastapi.testclient import TestClient

from sbatch_agent.workspace_browser import WorkspaceBrowser, FolderAccessError
from sbatch_agent.scanner import ProjectScanner
from sbatch_agent.web import WebConfig, create_app
from test_smart_web import web, hidden, count
from test_presentation import Elements


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("Folder tests must be offline and must not execute any command")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    (root / "project-a/input").mkdir(parents=True)
    (root / "project-b").mkdir()
    (root / "project-a/README.md").write_text("# Example")
    (root / "project-a/run.py").write_text("never_execute()")
    (root / "project-a/input/input.json").write_text("PRIVATE FILE CONTENTS")
    (root / "forbidden-link").symlink_to(tmp_path, target_is_directory=True)
    (root / "internal-link").symlink_to(root / "project-a", target_is_directory=True)
    (root / ".private").mkdir()
    (root / ".env").write_text("PRIVATE CREDENTIAL")
    return root


def test_navigation_sorting_and_current_selection(workspace):
    browser = WorkspaceBrowser(workspace)
    listing = browser.browse()
    assert [e.name for e in listing.entries] == ["project-a", "project-b", "forbidden-link", "internal-link"]
    assert listing.parent is None and listing.current.display_path == '工作区'
    assert not any(e.is_accessible for e in listing.entries if e.is_symlink)
    a = browser.browse("project-a")
    assert [e.name for e in a.entries] == ["input", "README.md", "run.py"]
    nested = browser.browse("project-a/input")
    assert nested.breadcrumbs == (('工作区', "."), ("project-a", "project-a"), ("input", "project-a/input"))
    assert browser.browse(nested.parent) == a
    selected = browser.select("project-a/input")
    assert selected.absolute_path == workspace / "project-a/input"
    assert selected.display_path == "project-a / input"
    assert str(workspace) not in repr(selected)


@pytest.mark.parametrize("value", ["..", "../", "../../", "project-a/../../", "%2e%2e", "%252e%252e",
    "project-a/%2e%2e", "/etc", "project-a\\..", "project-a/./input", "project-a//input", "", ".private",
    "project-a/\x00", "a/" * 33, "x" * 2049, "forbidden-link", "internal-link", "project-a/README.md"])
@pytest.mark.parametrize("method", ["browse", "select"])
def test_untrusted_navigation_and_selection_rejected(workspace, value, method):
    with pytest.raises(FolderAccessError):
        getattr(WorkspaceBrowser(workspace), method)(value)


def test_absolute_advanced_input_same_boundary(workspace):
    browser = WorkspaceBrowser(workspace)
    assert browser.from_absolute(str(workspace / "project-a")).relative_path == "project-a"
    for value in ("/etc", str(workspace / "../workspace/project-a"), "project-a", str(workspace / "internal-link")):
        with pytest.raises(FolderAccessError):
            browser.from_absolute(value)


def test_root_and_ancestor_symlink_rejected(workspace, tmp_path):
    for root in (Path("/"), Path("relative"), tmp_path / "missing", workspace / "internal-link"):
        with pytest.raises(ValueError):
            WorkspaceBrowser(root)
    linked_parent = tmp_path / "alias"
    linked_parent.symlink_to(workspace)
    with pytest.raises(ValueError):
        WorkspaceBrowser(linked_parent / "project-a")


@pytest.mark.parametrize("limit", [0, -1, 501, True, "200"])
def test_invalid_limit_rejected(workspace, limit):
    with pytest.raises(ValueError):
        WorkspaceBrowser(workspace, max_entries_per_directory=limit)


def test_changed_directory_link_and_replaced_root_rejected(workspace, tmp_path):
    browser = WorkspaceBrowser(workspace)
    browser.select("project-b")
    (workspace / "project-b").rmdir()
    (workspace / "project-b").symlink_to(tmp_path)
    with pytest.raises(FolderAccessError):
        browser.select("project-b")
    workspace.rename(tmp_path / "old-workspace")
    workspace.mkdir()
    with pytest.raises(FolderAccessError):
        browser.browse()


def test_empty_and_listing_limits(workspace):
    browser = WorkspaceBrowser(workspace, max_entries_per_directory=12)
    assert browser.browse("project-b").entries == ()
    for i in range(100):
        (workspace / "project-b" / f"file-{i:03d}").touch()
    listing = browser.browse("project-b")
    assert listing.truncated and len(listing.entries) == 12
    assert [e.name for e in listing.entries] == [f"file-{i:03d}" for i in range(12)]
    browser.ENUMERATION_LIMIT = 50
    listing = browser.browse("project-b")
    assert listing.enumeration_limited and not listing.entries
    assert browser.select("project-b").relative_path == "project-b"


def test_permission_failure_redacted(workspace, monkeypatch):
    browser = WorkspaceBrowser(workspace)
    monkeypatch.setattr(os, "scandir", lambda *a: (_ for _ in ()).throw(PermissionError(errno.EACCES, "PRIVATE")))
    with pytest.raises(FolderAccessError, match='权限') as error:
        browser.browse("project-a")
    assert error.value.status == 403 and "PRIVATE" not in str(error.value)


def test_browse_does_not_read_files_or_scan(workspace, monkeypatch):
    browser = WorkspaceBrowser(workspace)
    def forbidden(*a, **k):
        pytest.fail("Navigation must not read file contents or scan projects")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(ProjectScanner, "scan", forbidden)
    assert browser.browse("project-a/input").entries[0].name == "input.json"
    assert browser.select("project-b")


@pytest.fixture
def client(workspace, tmp_path):
    config = WebConfig(tmp_path / "db.sqlite3", tmp_path / "runs", workspace_root=workspace,
                       max_entries_per_directory=10)
    with TestClient(create_app(config), base_url="http://localhost") as browser:
        yield browser


def select(client, path, *, htmx=True, **extra):
    token = hidden(client.get("/new"), "csrf_token")
    return client.post("/ui/folders/select", data={"csrf_token": token, "path": path, **extra},
                       headers={"HX-Request": "true"} if htmx else {})


def test_picker_partial_selection_change_and_no_absolute_paths(client, workspace):
    first = client.get("/ui/folders", headers={"HX-Request": "true"})
    assert first.status_code == 200 and "<html" not in first.text
    assert '选择工作目录' in first.text and "project-a" in first.text
    assert str(workspace) not in first.text and ".env" not in first.text
    assert 'data-folder-cancel' in first.text and 'aria-label="打开 project-a"' in first.text
    links = [attrs for tag, attrs in Elements(first.text).items if attrs.get("aria-label", "").startswith("打开 ")]
    assert links and all(a["hx-include"] == "unset" and a["hx-params"] == "none" for a in links)
    assert all(a["hx-sync"] == "#folder-picker:queue last" for a in links)
    select_form = next(a for tag, a in Elements(first.text).items if tag == "form")
    assert select_form["hx-sync"] == "#folder-picker:queue last"
    assert "hx-include" not in select_form  # Task is required only for Analyze, not folder confirmation.
    selected = select(client, "project-a/input")
    assert selected.status_code == 200 and "<html" not in selected.text
    assert hidden(selected, "folder_path") == "project-a/input"
    assert "project-a / input" in selected.text and ">更换</a>" in selected.text
    assert str(workspace) not in selected.text
    # Browsing has no persisted selection; only the select partial replaces it.
    page = client.get("/ui/folders?path=project-b", headers={"HX-Request": "true"})
    assert 'name="folder_path"' not in page.text
    assert hidden(select(client, "project-b"), "folder_path") == "project-b"
    assert hidden(select(client, "."), "folder_path") == "."


@pytest.mark.parametrize("url", ["/ui/folders?path=../", "/ui/folders?path=%2e%2e", "/ui/folders?path=%252e%252e",
    "/ui/folders?path=/etc", "/ui/folders?path=forbidden-link", "/ui/folders?path=project-a/README.md",
    "/ui/folders?path=.&path=project-a", "/ui/folders?url=https://example.com"])
def test_route_rejects_traversal_and_file_read(client, url):
    response = client.get(url, headers={"HX-Request": "true"})
    assert response.status_code == 400 and "<html" not in response.text
    assert "Traceback" not in response.text and "root:" not in response.text


def test_select_failure_does_not_replace_selection(client):
    response = select(client, "forbidden-link")
    assert response.status_code == 400 and response.headers["HX-Retarget"] == "#folder-picker"
    assert 'data-folder-confirmed' not in response.text
    assert select(client, "project-a", extra="bad").status_code == 400


def test_csrf_before_browser_and_cross_origin(client, monkeypatch):
    def forbidden(*a):
        pytest.fail("CSRF must run before selection")
    monkeypatch.setattr(client.app.state.workspace_browser, "select", forbidden)
    assert client.post("/ui/folders/select", data={"path": "project-a"}).status_code == 403
    token = hidden(client.get("/new"), "csrf_token")
    assert client.post("/ui/folders/select", data={"path": "project-a", "csrf_token": token},
                       headers={"Origin": "https://evil.example"}).status_code == 403


def test_folder_plain_form_fallback_and_task_preserved(client):
    response = select(client, "project-a", htmx=False, task_intent="Run input.json")
    assert response.status_code == 200 and "<html" in response.text
    assert hidden(response, "folder_path") == "project-a" and "Run input.json</textarea>" in response.text
    assert "<html" in client.get("/ui/folders").text


def test_large_directory_and_permission_partial(client, workspace, monkeypatch):
    for i in range(30):
        (workspace / "project-b" / f"item{i:02d}").touch()
    page = client.get("/ui/folders?path=project-b", headers={"HX-Request": "true"})
    assert "显示前 10 项" in page.text and len(page.content) < 12000
    assert "item09" in page.text and "item10" not in page.text
    def fail(*a):
        raise FolderAccessError('没有权限打开此文件夹。', 403)
    monkeypatch.setattr(client.app.state.workspace_browser, "browse", fail)
    page = client.get("/ui/folders", headers={"HX-Request": "true"})
    assert page.status_code == 403 and '权限' in page.text and "Traceback" not in page.text


def test_filename_autoescaping(client, workspace):
    (workspace / 'project-b/<img onerror="attack">').touch()
    page = client.get("/ui/folders?path=project-b", headers={"HX-Request": "true"})
    assert "&lt;img" in page.text and "<img" not in page.text


def test_simple_defaults_and_examples_not_values(client):
    page = client.get("/new")
    parsed = Elements(page.text)
    assert '<details id="manual-mode">' in page.text and 'id="analyze-prepare"' in page.text
    assert hidden(page, "folder_path") == ""
    for key, placeholder in (("smart-intent", "例如：使用 input.json 运行一次自洽场计算"),
                             ("memory_mib", "例如：4096 MiB"), ("time_limit_seconds", "例如：02:00:00"),
                             ("manual-args", '--input input.json')):
        attrs = next(attrs for tag, attrs in parsed.items if attrs.get("id") == key)
        assert attrs["placeholder"] == placeholder
        if key != "manual-args":
            assert attrs.get("value", "") == ""
    simple = page.text.split('aria-label="智能配置"')[1].split("</form>")[0]
    assert 'name="project_dir"' not in simple and 'name="partition"' not in simple
    assert 'form="smart-prepare-form"' in page.text


def test_real_scan_once_after_folder_selection_and_prepare(tmp_path, monkeypatch):
    app, config, root, model, cluster, slurm = web(tmp_path, omit=("memory_mib",))
    calls = []
    original = ProjectScanner.scan
    monkeypatch.setattr(ProjectScanner, "scan", lambda self, *a, **k: (calls.append(a), original(self, *a, **k))[1])
    with TestClient(app, base_url="http://localhost") as client:
        client.get("/ui/folders?path=project")
        chosen = select(client, "project")
        assert calls == [] and model.calls == [] and cluster.calls == 0
        result = client.post("/new/prepare", data={"csrf_token": hidden(client.get("/new"), "csrf_token"),
            "folder_path": hidden(chosen, "folder_path"), "project_dir": "", "task_intent": "运行 case01"},
            headers={"HX-Request": "true"})
        assert result.status_code == 200 and "<html" not in result.text
        assert '需要确认' in result.text and "scan-summary" in result.text
        assert "入口" in result.text and "输入" in result.text
        assert len(calls) == len(model.calls) == cluster.calls == 1
        entry = next(iter(app.state.prepared_store.entries.values())).prepared
        assert entry.values.work_dir == str(root) and entry.project_evidence.project_dir == str(root)
        final = client.post(f"/new/prepared/{entry.id}/continue", data={
            "csrf_token": hidden(result, "csrf_token"), "revision": hidden(result, "revision"), "memory_mib": "256"},
            headers={"HX-Request": "true"})
        assert final.status_code == 200 and "提交前确认" in final.text
        assert '<details id="shell-preview">' in final.text
        assert count(config) == 0 and not slurm.submit_calls
        assert len(calls) == len(model.calls) == cluster.calls == 1


@pytest.mark.parametrize("values", [{"folder_path": "../"}, {"folder_path": "/etc"},
    {"folder_path": "project", "project_dir": "/etc"}, {"project_dir": "/etc"},
    {"folder_path": "project", "task_intent": " "}, {"folder_path": "project", "unexpected": "x"}])
def test_invalid_prepare_inputs_call_no_scanner_or_model(tmp_path, monkeypatch, values):
    app, config, root, model, cluster, slurm = web(tmp_path)
    def forbidden(*a, **k):
        pytest.fail("Invalid selection/task must be rejected before Scanner")
    monkeypatch.setattr(ProjectScanner, "scan", forbidden)
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post("/new/prepare", data={"csrf_token": hidden(client.get("/new"), "csrf_token"),
            "task_intent": "Run example", **values}, headers={"HX-Request": "true"})
        assert result.status_code == 400 and "PREPARE_INPUT_INVALID" in result.text
        assert model.calls == [] and cluster.calls == 0 and count(config) == 0


def test_workspace_config_env_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("SBATCH_AGENT_FOLDER_MAX_ENTRIES", "12")
    config = WebConfig.from_env()
    assert config.workspace_root == tmp_path and config.max_entries_per_directory == 12
    monkeypatch.delenv("SBATCH_AGENT_WORKSPACE_ROOT")
    monkeypatch.delenv("SBATCH_AGENT_FOLDER_MAX_ENTRIES")
    assert WebConfig.from_env().workspace_root is None


def test_selection_revalidated_at_prepare(tmp_path):
    app, config, root, model, cluster, slurm = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        assert select(client, "project").status_code == 200
        root.rename(tmp_path / "renamed-project")
        root.symlink_to(tmp_path.parent)
        result = client.post("/new/prepare", data={"csrf_token": hidden(client.get("/new"), "csrf_token"),
            "folder_path": "project", "task_intent": "Run example"}, headers={"HX-Request": "true"})
        assert result.status_code == 400 and not model.calls and cluster.calls == 0


def test_explicit_working_folder_overrides_proposed_subdirectory(tmp_path, monkeypatch):
    from sbatch_agent.smart_service import SmartJobService
    original = SmartJobService.prepare
    def subdirectory(self, **kwargs):
        prepared = original(self, **kwargs)
        prepared.values = prepared.values.model_copy(update={"work_dir": str(Path(kwargs["project_dir"]) / "inputs")})
        return prepared
    monkeypatch.setattr(SmartJobService, "prepare", subdirectory)
    app, config, root, model, cluster, slurm = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post("/new/prepare", data={"csrf_token": hidden(client.get("/new"), "csrf_token"),
            "folder_path": "project", "task_intent": "Run example"}, headers={"HX-Request": "true"})
        assert result.status_code == 200
        prepared = next(iter(app.state.prepared_store.entries.values())).prepared
        assert prepared.values.work_dir == str(root) and prepared.job_spec.work_dir == str(root)
        assert prepared.resolved_fields["work_dir"].source == "USER"
        assert len(model.calls) == cluster.calls == 1


def test_select_csrf_failure_targets_picker_not_confirmed_value(client):
    response = client.post("/ui/folders/select", data={"path": "project-a"}, headers={"HX-Request": "true"})
    assert response.status_code == 403 and response.headers["HX-Retarget"] == "#folder-picker"
    assert "<html" not in response.text


def test_compact_review_keeps_actual_paths_and_provenance(tmp_path):
    from sbatch_agent.presentation import compact_review_values, field_source_badge
    from sbatch_agent.smart_web import form_values
    from test_smart_service import setup_smart
    smart, root, model, cluster = setup_smart(tmp_path)
    prepared = smart.prepare(project_dir=root, task_intent="Run example")
    original = form_values(prepared)
    display = compact_review_values(prepared, original, {})
    assert str(root) in original["required_inputs"] and str(root) not in display["required_inputs"]
    assert "inputs/case01.json" in display["required_inputs"]
    assert prepared.job_spec.work_dir == str(root)
    assert field_source_badge("AI_INFERRED").short_label == "系统推断"
    assert "尚未验证" in field_source_badge("AI_INFERRED").description
    assert field_source_badge("SERVER_CATALOG").short_label == "软件目录"
