"""Evidence preview in /new, independent of jobs, profiles and cluster access."""

from html import unescape
from html.parser import HTMLParser
import re
import subprocess

from fastapi.testclient import TestClient
import pytest

from sbatch_agent import JobRepository, SlurmClient, StaticProfiles, SubmissionService, SubprocessRunner
from sbatch_agent.cluster import ClusterService
from sbatch_agent.recommender import ResourceRecommender
from sbatch_agent.scanner import ProjectScanError, ProjectScanner
from sbatch_agent.scanner_models import ScanConfig
from sbatch_agent.web import WebConfig, create_app
from sbatch_agent.web_forms import DEFAULT_FORM
from test_scanner import write


@pytest.fixture(autouse=True)
def forbid_execution_and_other_services(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Scan preview must not access jobs, Slurm or recommendation services")
    for owner, names in ((subprocess, ["Popen"]), (SubprocessRunner, ["run"]),
                         (SlurmClient, ["submit", "get_status"]), (ClusterService, ["get_snapshot"]),
                         (ResourceRecommender, ["recommend"]),
                         (SubmissionService, ["create_job", "submit_job", "refresh_status"])):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)


@pytest.fixture
def config(tmp_path):
    return WebConfig(tmp_path / "database.sqlite3", tmp_path / "runs")


@pytest.fixture
def browser(config):
    # Scanning does not require even one registered profile or a valid JobSpec.
    with TestClient(create_app(config, profiles=StaticProfiles()), base_url="http://127.0.0.1") as client:
        yield client


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    write(root, "README.md", "python run.py --input inputs/a.json\n")
    write(root, "run.py", "if __name__ == '__main__':\n    pass\n")
    write(root, "inputs/a.json", "{}")
    return root


def token(browser):
    return re.search(r'name="csrf_token" value="([^"]+)"', browser.get("/new").text)[1]


def post(browser, form):
    return browser.post("/new/scan", data={**form, "csrf_token": token(browser)})


def assert_empty(config):
    with JobRepository(config.database_path) as repo:
        assert repo.list() == []
    assert not config.runs_root.exists()


def test_scan_shows_evidence_without_job_or_auto_fill(browser, config, project):
    page = browser.get("/new")
    assert 'formaction="/new/scan" formnovalidate' in page.text and '扫描项目' in page.text
    form = {**DEFAULT_FORM, "project_dir": str(project), "entrypoint": "my-manual-entry.py",
            "partition": "user-choice", "run_type": "compiled", "memory_mib": "bad-but-not-relevant-to-scan"}
    response = post(browser, form)
    assert response.status_code == 200
    for text in ('项目扫描结果', '候选入口', "run.py", "python", '扫描时间：', "README.md:1", "DIRECT", "SHA-256"):
        assert text in response.text
    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}
        def handle_starttag(self, tag, attrs):
            if tag == "input":
                item = dict(attrs)
                self.values[item.get("name")] = item.get("value", "")
    inputs = Inputs()
    inputs.feed(response.text)
    assert inputs.values["entrypoint"] == "my-manual-entry.py"
    assert inputs.values["partition"] == "user-choice" and inputs.values["memory_mib"] == form["memory_mib"]
    assert 'option value="compiled" selected' in response.text
    assert_empty(config)


def test_no_repository_access_during_scan(browser, project, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Scan action must not even open JobRepository")
    monkeypatch.setattr(JobRepository, "__init__", forbidden)
    assert post(browser, {"project_dir": str(project)}).status_code == 200


@pytest.mark.parametrize("kind,fragment", [("missing", "not found"), ("file", "普通目录"),
                                          ("relative", "绝对路径"), ("empty", "不能为空"), ("traversal", "'..'")])
def test_invalid_path_preserves_form_without_record(browser, config, project, kind, fragment):
    paths = {"missing": str(project / "missing"), "file": str(project / "README.md"),
             "relative": "relative/project", "empty": "", "traversal": str(project / "../outside")}
    response = post(browser, {"project_dir": paths[kind], "name": "keep-my-name"})
    assert response.status_code == 400 and fragment in unescape(response.text)
    assert "keep-my-name" in response.text and "Traceback" not in response.text
    assert_empty(config)


def test_scan_enforces_csrf_and_origin(browser, project):
    data = {"project_dir": str(project)}
    assert browser.post("/new/scan", data=data).status_code == 403
    assert browser.post("/new/scan", data={**data, "csrf_token": token(browser)},
                        headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.get("/new/scan").status_code == 405


def test_unknown_fields_cannot_change_scanner_config(browser, config, project):
    response = post(browser, {"project_dir": str(project), "max_files": "99999999"})
    assert response.status_code == 400
    assert_empty(config)


@pytest.mark.parametrize("failure,status,fragment", [(ProjectScanError("Permission denied"), 400, "Permission denied"),
                                                      (RuntimeError("private detail"), 503, "仍可手动")])
def test_scanner_failure_is_friendly(config, project, failure, status, fragment):
    class BrokenScanner:
        def scan(self, project_dir):
            raise failure
    with TestClient(create_app(config, profiles=StaticProfiles(), project_scanner=BrokenScanner()),
                    base_url="http://localhost") as browser:
        response = post(browser, {"project_dir": str(project)})
        assert response.status_code == status and fragment in response.text
        assert "Traceback" not in response.text and "private detail" not in response.text
    assert_empty(config)


def test_project_text_and_filenames_are_html_escaped(browser, project):
    attack = '<script>alert("unsafe")</script>'
    write(project, "README.md", f'python run.py --label \'{attack}\'\n')
    write(project, "<img onerror=alert(1)>.py", "# python")
    response = post(browser, {"project_dir": str(project)})
    assert response.status_code == 200 and attack in unescape(response.text)
    assert "<script>" not in response.text and "<img " not in response.text
    assert "&lt;script&gt;" in response.text


def test_limits_and_empty_projects_are_successful_partial_previews(config, project):
    scanner = ProjectScanner(ScanConfig(max_files=1))
    with TestClient(create_app(config, profiles=StaticProfiles(), project_scanner=scanner), base_url="http://localhost") as browser:
        response = post(browser, {"project_dir": str(project)})
        assert response.status_code == 200 and '已达到部分扫描上限' in response.text and 'max_files' in response.text
        empty = project / "empty"
        empty.mkdir()
        response = post(browser, {"project_dir": str(empty)})
        assert response.status_code == 200 and "No relevant project evidence found" in response.text
    assert_empty(config)


def test_result_is_not_kept_on_next_get(browser, config, project):
    assert '项目扫描结果' in post(browser, {"project_dir": str(project)}).text
    assert '项目扫描结果' not in browser.get("/new").text
    assert_empty(config)


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8000", "http://localhost:8000"])
def test_browser_same_origin_form_policy_and_scan(browser, config, project, base_url):
    # Fetch's Origin algorithm turns navigation POST origins into null under
    # no-referrer. Use a policy that preserves same-origin form requests, even
    # though TestClient itself does not implement browser referrer semantics.
    with TestClient(browser.app, base_url=base_url) as client:
        page = client.get("/new")
        assert page.headers["referrer-policy"] == "same-origin"
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        response = client.post("/new/scan", data={"project_dir": str(project), "csrf_token": csrf}, headers={
            "Origin": base_url, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        })
        assert response.status_code == 200 and '项目扫描结果' in response.text
    assert_empty(config)


@pytest.mark.parametrize("headers", [
    {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://127.0.0.1:9000", "Sec-Fetch-Site": "same-site"},
    {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
])
def test_opaque_or_other_origin_still_rejected_before_scan(browser, config, project, headers, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid origin must be rejected before filesystem access")
    monkeypatch.setattr(ProjectScanner, "scan", forbidden)
    response = browser.post("/new/scan", data={"project_dir": str(project), "csrf_token": token(browser)}, headers=headers)
    assert response.status_code == 403
    assert_empty(config)
