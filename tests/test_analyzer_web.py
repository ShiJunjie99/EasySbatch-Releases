"""Scan -> signed evidence -> fake inference, no record or execution effects."""

from html import unescape
import re
import socket
import subprocess

from fastapi.testclient import TestClient
import pytest

from sbatch_agent import JobRepository, SlurmClient, SubmissionService, SubprocessRunner
from sbatch_agent.analyzer import AIProjectAnalyzer
from sbatch_agent.model_client import ModelConfig, ModelErrorCode, ModelUnavailableError, OpenAICompatibleClient
from sbatch_agent.cluster import ClusterService
from sbatch_agent.recommender import ResourceRecommender
from sbatch_agent.scanner import ProjectScanner
from sbatch_agent.web import WebConfig, create_app
from test_analyzer import FakeModelClient, python_output, project, evidence, registered_profiles, proposed, ref
from test_web import form, profiles, post
from test_model_client import transport, envelope


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("AI Web must not query/execute anything outside the injected model")
    for owner, names in ((subprocess, ["Popen"]), (SubprocessRunner, ["run"]), (SlurmClient, ["submit", "get_status"]),
                         (ClusterService, ["get_snapshot"]), (ResourceRecommender, ["recommend"]),
                         (socket, ["create_connection"]), (socket.socket, ["connect"])):
        for name in names:
            monkeypatch.setattr(owner, name, forbidden)
    for key in ("PROVIDER", "MODEL", "ENDPOINT", "API_KEY_ENV", "TIMEOUT"):
        monkeypatch.delenv("SBATCH_AGENT_AI_" + key, raising=False)


@pytest.fixture
def config(tmp_path):
    return WebConfig(tmp_path / "jobs.sqlite3", tmp_path / "runs")


def fields(page):
    return {key: unescape(re.search('name="' + key + '" value="([^"]*)"', page.text)[1])
            for key in ("scan_token", "csrf_token")}


def scan(browser, project, form=None):
    response = post(browser, "/new/scan", {**(form or {}), "project_dir": str(project)})
    assert response.status_code == 200
    return response


def analyze(browser, scanned, intent="运行 case01"):
    return browser.post("/new/analyze", data={**fields(scanned), "task_intent": intent})


def assert_empty(config):
    with JobRepository(config.database_path) as repo:
        assert repo.list() == []
    assert not config.runs_root.exists()


def test_analysis_shows_fields_refs_without_auto_fill_or_jobs(config, evidence, project, profiles, form, monkeypatch):
    fake = FakeModelClient(python_output(evidence))
    app = create_app(config, profiles=profiles, project_analyzer=AIProjectAnalyzer(model_client=fake, profiles=profiles))
    with TestClient(app, base_url="http://127.0.0.1") as browser:
        scanned = scan(browser, project, {**form, "entrypoint": "manually-selected.py"})
        assert '分析项目' in scanned.text and not fake.calls
        def forbidden(*args, **kwargs):
            pytest.fail("Analyze must not rescan or touch repository/lifecycle")
        with monkeypatch.context() as patch:
            patch.setattr(ProjectScanner, "scan", forbidden)
            patch.setattr(JobRepository, "__init__", forbidden)
            patch.setattr(SubmissionService, "create_job", forbidden)
            page = analyze(browser, scanned)
        assert page.status_code == 200, page.text
        for text in ('AI 分析结果', "DIRECT", "UNRESOLVED", "README.md:2", "run.py", "inputs/case01.json", '环境匹配'):
            assert text in page.text
        assert 'value="manually-selected.py"' in page.text
        assert len(fake.calls) == 1
        assert 'AI 分析结果' not in browser.get("/new").text
    assert_empty(config)


@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE provider detail"), TimeoutError("PRIVATE timeout"), None])
def test_unavailable_or_bad_schema_keeps_manual_create(config, project, profiles, form, failure):
    fake = FakeModelClient({"shell_script": "unsafe"}, failure=failure)
    app = create_app(config, profiles=profiles, project_analyzer=AIProjectAnalyzer(model_client=fake))
    with TestClient(app, base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project, form))
        assert page.status_code == 503 and '手动' in page.text
        assert "PRIVATE" not in page.text and "Traceback" not in page.text
        assert_empty(config)
        # Manual M5 flow is independent of AI; fake is not called again.
        response = post(browser, "/new", form)
        assert response.status_code == 303 and len(fake.calls) == 1


def test_provider_not_configured_friendly(config, project, profiles):
    with TestClient(create_app(config, profiles=profiles), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project))
        assert page.status_code == 503 and '手动' in page.text
        assert 'AI 未配置' in page.text
    assert_empty(config)


def test_unmatched_task_unresolved_is_a_valid_analysis_page(config, project):
    fake = FakeModelClient({"draft": {}, "notes": ["当前项目没有与 hello world 对应的入口。"]})
    with TestClient(create_app(config, project_analyzer=AIProjectAnalyzer(model_client=fake)), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project), intent="只输出 hello world")
        assert page.status_code == 200
        assert 'AI 分析结果' in page.text and "UNRESOLVED" in page.text
        assert "当前项目没有与 hello world 对应的入口。" in page.text
        assert 'AI 分析暂不可用' not in page.text
    assert len(fake.calls) == 1
    assert_empty(config)


def test_web_displays_safe_schema_field_diagnostic(config, project, evidence, caplog):
    output = python_output(evidence)
    output["draft"]["entrypoint"] = {"value": None, "status": "DIRECT", "evidence_refs": [], "reason": "PRIVATE"}
    fake = FakeModelClient(output)
    with TestClient(create_app(config, project_analyzer=AIProjectAnalyzer(model_client=fake)), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project))
        assert page.status_code == 503
        assert "stage=schema field=entrypoint reason=inconsistent_proposal" in page.text
        assert "PRIVATE" not in page.text and "PRIVATE" not in caplog.text
    assert "field=entrypoint" in caplog.text and len(fake.calls) == 1
    assert_empty(config)


@pytest.mark.parametrize("kind", ["tamper", "other_session", "restart", "csrf", "origin", "extra"])
def test_analysis_tokens_and_origin_reject_before_model(config, project, kind):
    fake = FakeModelClient()
    factory = lambda: create_app(config, project_analyzer=AIProjectAnalyzer(model_client=fake))
    app = factory()
    with TestClient(app, base_url="http://localhost") as browser:
        data = {**fields(scan(browser, project)), "task_intent": "运行模拟"}
        headers = {}
        if kind == "tamper":
            data["scan_token"] += "x"
        elif kind == "csrf":
            data["csrf_token"] = "bad"
        elif kind == "origin":
            headers = {"Origin": "null"}
        elif kind == "extra":
            data["endpoint"] = "https://attacker.invalid"
        if kind in {"other_session", "restart"}:
            with TestClient(app if kind == "other_session" else factory(), base_url="http://localhost") as other:
                csrf = re.search('name="csrf_token" value="([^"]*)"', other.get("/new").text)[1]
                response = other.post("/new/analyze", data={**data, "csrf_token": csrf})
        else:
            response = browser.post("/new/analyze", data=data, headers=headers)
        assert response.status_code in {400, 403}
    assert not fake.calls
    assert_empty(config)


def test_conflict_and_model_text_are_escaped(config, project, evidence):
    output = python_output(evidence)
    output["notes"] = ['<script>alert("x")</script>']
    output["conflicts"] = [{"field": "entrypoint", "reason": "需确认入口",
                           "evidence_refs": ref(evidence, "command_text") + ref(evidence, "python_script")}]
    with TestClient(create_app(config, project_analyzer=AIProjectAnalyzer(model_client=FakeModelClient(output))), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project))
        assert page.status_code == 200 and '配置冲突' in page.text
        assert "<script>" not in page.text and "&lt;script&gt;" in page.text
    assert_empty(config)


def test_invalid_intent_no_model_call(config, project):
    fake = FakeModelClient()
    with TestClient(create_app(config, project_analyzer=AIProjectAnalyzer(model_client=fake)), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project), intent="")
        assert page.status_code == 400 and "Task description" in page.text
    assert not fake.calls


def test_ai_bad_configuration_does_not_prevent_app_startup(config, monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_AI_MODEL", "incomplete")
    with TestClient(create_app(config), base_url="http://localhost") as browser:
        assert browser.get("/new").status_code == 200


def test_ai_context_failure_does_not_break_scan(config, project, monkeypatch):
    from sbatch_agent.analysis_context import AnalysisContextBuilder, AnalysisInputError
    def unavailable(*args, **kwargs):
        raise AnalysisInputError("unsupported context")
    monkeypatch.setattr(AnalysisContextBuilder, "build", unavailable)
    with TestClient(create_app(config), base_url="http://localhost") as browser:
        page = scan(browser, project)
        assert "run.py" in page.text and "name=\"scan_token\"" not in page.text
    assert_empty(config)


def test_expired_evidence_requires_explicit_rescan(config, project, monkeypatch):
    from itsdangerous import TimestampSigner
    fake = FakeModelClient()
    with TestClient(create_app(config, project_analyzer=AIProjectAnalyzer(model_client=fake)), base_url="http://localhost") as browser:
        scanned = scan(browser, project)
        now = TimestampSigner("test").get_timestamp()
        monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: now + 901)
        assert analyze(browser, scanned).status_code == 400
    assert not fake.calls
    assert_empty(config)


def configure_provider(monkeypatch, *, key="backend-only-fixture", model="configured-model"):
    values = {"PROVIDER": "openai-compatible", "MODEL": model,
              "ENDPOINT": "https://provider.invalid/v1/chat/completions", "API_KEY_ENV": "WEB_TEST_CREDENTIAL"}
    for name, value in values.items():
        monkeypatch.setenv("SBATCH_AGENT_AI_" + name, value)
    if key is not None:
        monkeypatch.setenv("WEB_TEST_CREDENTIAL", key)
    else:
        monkeypatch.delenv("WEB_TEST_CREDENTIAL", raising=False)


@pytest.mark.parametrize("case,label", [("missing", '未配置'), ("partial", '配置无效'),
                                      ("no_key", '凭据未就绪'), ("ready", '可用')])
def test_provider_configuration_visible_without_network_probe(config, monkeypatch, case, label):
    if case in {"no_key", "ready"}:
        configure_provider(monkeypatch, key=None if case == "no_key" else "backend-only-fixture")
    elif case == "partial":
        monkeypatch.setenv("SBATCH_AGENT_AI_MODEL", "incomplete")
    calls = transport(monkeypatch, envelope())
    with TestClient(create_app(config), base_url="http://localhost") as browser:
        page = browser.get("/new")
        assert page.status_code == 200 and label in page.text
        assert '扫描项目' in page.text and '推荐资源' in page.text
        assert "backend-only-fixture" not in page.text and "provider.invalid" not in page.text
        assert not calls


@pytest.mark.parametrize("provider", ["openai-compatible", "deepseek"])
def test_real_adapter_factory_routes_strict_response_through_analyzer(config, project, evidence, profiles, monkeypatch, caplog, provider):
    import json
    configure_provider(monkeypatch)
    monkeypatch.setenv("SBATCH_AGENT_AI_PROVIDER", provider)
    calls = transport(monkeypatch, envelope(content=json.dumps(python_output(evidence))))
    with TestClient(create_app(config, profiles=profiles), base_url="http://localhost") as browser:
        assert not calls
        scanned = scan(browser, project)
        assert not calls
        page = analyze(browser, scanned)
        assert page.status_code == 200 and 'AI 分析结果' in page.text
        assert "run.py" in page.text and "DIRECT" in page.text and "UNRESOLVED" in page.text
        assert "configured-model" in page.text and "backend-only-fixture" not in page.text
        assert "Authorization" not in page.text
    assert "backend-only-fixture" not in caplog.text
    body = json.loads(calls[0][0].data)
    assert len(calls) == 1
    if provider == "deepseek":
        assert body["response_format"] == {"type": "json_object"}
    else:
        assert body["response_format"]["json_schema"]["strict"] is True
    assert "UNTRUSTED PROJECT CONTENT" in body["messages"][0]["content"]
    assert "tools" not in body
    assert_empty(config)


@pytest.mark.parametrize("invalid", ["schema", "path", "evidence"])
def test_deepseek_json_mode_still_rejects_untrusted_output(config, project, evidence, monkeypatch, invalid):
    import json
    configure_provider(monkeypatch)
    monkeypatch.setenv("SBATCH_AGENT_AI_PROVIDER", "deepseek")
    output = python_output(evidence)
    if invalid == "schema":
        output = {"shell_script": "do not execute"}
    elif invalid == "path":
        output["draft"]["entrypoint"]["value"]["value"] = "nonexistent.py"
    else:
        output["draft"]["entrypoint"]["evidence_refs"] = ["made-up-reference"]
    calls = transport(monkeypatch, envelope(content=json.dumps(output)))
    with TestClient(create_app(config), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project))
        assert page.status_code == 503 and '手动' in page.text
    assert len(calls) == 1
    assert_empty(config)


def test_fake_injection_wins_over_real_environment(config, project, evidence, monkeypatch):
    configure_provider(monkeypatch)
    fake = FakeModelClient(python_output(evidence))
    def forbidden(*args, **kwargs):
        pytest.fail("Injected fake must bypass real provider construction and environment config")
    monkeypatch.setattr(ModelConfig, "from_env", forbidden)
    monkeypatch.setattr(OpenAICompatibleClient, "__init__", forbidden)
    with TestClient(create_app(config, project_analyzer=AIProjectAnalyzer(model_client=fake)), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project))
        assert page.status_code == 200 and "offline-test" in page.text
    assert len(fake.calls) == 1


def test_model_label_escaped_without_exposing_config_or_credential(config, monkeypatch):
    configure_provider(monkeypatch, model='<script>alert("label")</script>')
    with TestClient(create_app(config), base_url="http://localhost") as browser:
        page = browser.get("/new")
        assert "<script>" not in page.text and "&lt;script&gt;" in page.text
        assert "backend-only-fixture" not in page.text and "provider.invalid" not in page.text


@pytest.mark.parametrize("code", [ModelErrorCode.CREDENTIAL_MISSING, ModelErrorCode.AUTHENTICATION,
                                ModelErrorCode.TIMEOUT, ModelErrorCode.RATE_LIMIT, ModelErrorCode.UNAVAILABLE,
                                ModelErrorCode.TLS_CERTIFICATE])
def test_web_reports_error_category_not_exception_secrets(config, project, code, caplog):
    fake = FakeModelClient(failure=ModelUnavailableError("PRIVATE Authorization detail", code=code))
    with TestClient(create_app(config, project_analyzer=AIProjectAnalyzer(model_client=fake, retry_sleeper=lambda _: None)), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project))
        assert page.status_code == 503 and '手动' in page.text
        assert "PRIVATE" not in page.text and "PRIVATE" not in caplog.text
    assert code.value in caplog.text
    assert len(fake.calls) == (2 if code in {ModelErrorCode.TIMEOUT, ModelErrorCode.RATE_LIMIT} else 1)
    assert_empty(config)


def test_valid_provider_missing_credential_no_request_manual_still_works(config, project, form, profiles, monkeypatch):
    configure_provider(monkeypatch, key=None)
    calls = transport(monkeypatch, envelope())
    with TestClient(create_app(config, profiles=profiles), base_url="http://localhost") as browser:
        page = analyze(browser, scan(browser, project, form))
        assert page.status_code == 503 and '凭据未就绪' in page.text and not calls
        assert_empty(config)
        assert post(browser, "/new", form).status_code == 303
