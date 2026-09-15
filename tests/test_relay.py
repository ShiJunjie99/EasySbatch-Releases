"""All relay tests are offline: in-process HTTP and fake upstream only."""

from copy import deepcopy
from io import BytesIO
import http.client
import importlib.util
import json
import logging
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sbatch_agent.ai_relay import create_relay_app
from sbatch_agent.analysis_context import AnalysisContext
from sbatch_agent.analyzer import AIProjectAnalyzer
from sbatch_agent.model_client import (
    AnalysisOutputValidationError, ModelAvailability, ModelConfig, ModelErrorCode,
    ModelResponse, ModelUnavailableError, OpenAICompatibleClient,
)
from sbatch_agent.model_factory import model_client_from_env
from sbatch_agent.relay_client import (
    REQUEST_LIMIT, RESPONSE_LIMIT, TOKEN_ENV, RelayConfig, RemoteRelayModelClient,
)
import sbatch_agent.relay_client as transport_module
from sbatch_agent.runner import SubprocessRunner
from sbatch_agent.web import WebConfig, create_app
from test_analyzer import project, evidence, python_output
from test_smart_web import hidden


TOKEN = "offline-relay-token-" + "x" * 32
KEY = "offline-provider-credential-not-real"
SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}}
CONTEXT = AnalysisContext("Return structured JSON only.", "测试 status ok", ("server-only-ref",), ("server-only-warning",))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Relay tests cannot access network, SSH, Slurm or real model")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(OpenAICompatibleClient, "generate_structured", forbidden)
    for name in tuple(os.environ):
        if name.startswith("SBATCH_AGENT_AI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv("RELAY_TEST_PROVIDER_KEY", KEY)


class Upstream:
    provider, model = "deepseek", "offline-model"
    config = SimpleNamespace(api_key_env="RELAY_TEST_PROVIDER_KEY")
    def __init__(self, output=None, failure=None):
        self.output = {"status": "ok"} if output is None else output
        self.failure, self.calls = failure, []
    def availability(self):
        return ModelAvailability("available", self.model)
    def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return ModelResponse(deepcopy(self.output), "upstream-id-1")


def bridge(monkeypatch, *, upstream=None, raw=None, status=200, failure=None):
    upstream = upstream or Upstream()
    app = create_relay_app(model_client=upstream)
    browser = TestClient(app, base_url="http://127.0.0.1:18081")
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("127.0.0.1", 18080) and 0 < timeout <= 90
        def request(self, method, path, body, headers):
            calls.append((method, path, body, headers))
            if failure:
                raise failure
            self.response = browser.request(method, path, content=body, headers=headers)
        def getresponse(self):
            result = BytesIO(raw if raw is not None else self.response.content)
            result.status = status if raw is not None else self.response.status_code
            return result
        def close(self):
            pass
    monkeypatch.setattr(transport_module.http.client, "HTTPConnection", Connection)
    client = RemoteRelayModelClient(RelayConfig("offline-model", "http://127.0.0.1:18080"))
    return client, upstream, calls, browser


def infer(client):
    return client.generate_structured(context=CONTEXT, schema=SCHEMA)


def payload():
    return {"system": CONTEXT.system, "user": CONTEXT.user, "schema": SCHEMA}


def test_roundtrip_preserves_model_contract_not_local_metadata(monkeypatch):
    client, upstream, calls, _ = bridge(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://unrelated.invalid:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.invalid:9999")
    result = infer(client)
    assert result == ModelResponse({"status": "ok"}, "upstream-id-1")
    assert json.loads(calls[0][2]) == payload()
    assert calls[0][0:2] == ("POST", "/v1/analyze")
    assert calls[0][3]["Authorization"] == "Bearer " + TOKEN
    sent = upstream.calls[0]
    assert sent["schema"] == SCHEMA and sent["context"].system == CONTEXT.system
    assert sent["context"].user == CONTEXT.user and sent["context"].evidence_refs == ()
    assert TOKEN not in repr(sent) and KEY not in repr(sent)
    assert os.environ["HTTP_PROXY"] == "http://unrelated.invalid:9999"


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1:18080", "http://localhost:18080",
    "http://0.0.0.0:18080", "http://example.org:18080", "http://127.0.0.1",
    "http://127.0.0.1:18080/v1/analyze", "http://user:secret@127.0.0.1:18080",
    "http://127.0.0.1:18080?token=secret", "http://127.0.0.1:18080/#fragment",
    "http://127.0.0.1:99999", "http://127.0.0.1:018080"])
def test_only_literal_loopback_endpoint_allowed(endpoint):
    with pytest.raises(ValueError, match="Invalid loopback"):
        RelayConfig("offline-model", endpoint)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 121])
def test_bounded_timeout(timeout):
    with pytest.raises(ValueError):
        RelayConfig("offline-model", "http://127.0.0.1:18080", timeout)


@pytest.mark.parametrize("token", ["", "short", "x" * 129, "x" * 32 + "\n", "Bearer " + "x" * 32])
def test_invalid_token_never_connects(monkeypatch, token):
    client, _, calls, _ = bridge(monkeypatch)
    monkeypatch.setenv(TOKEN_ENV, token)
    assert client.availability().state == "credential_missing"
    with pytest.raises(ModelUnavailableError) as caught:
        infer(client)
    assert caught.value.code == ModelErrorCode.CREDENTIAL_MISSING and not calls


@pytest.mark.parametrize("failure,code", [
    (TimeoutError(KEY + TOKEN), ModelErrorCode.TIMEOUT),
    (ConnectionRefusedError(KEY + TOKEN), ModelErrorCode.UNAVAILABLE),
    (OSError(KEY + TOKEN), ModelErrorCode.UNAVAILABLE),
    (http.client.RemoteDisconnected(KEY + TOKEN), ModelErrorCode.UNAVAILABLE),
])
def test_unreachable_timeout_disconnect_no_fallback(monkeypatch, failure, code):
    client, upstream, calls, _ = bridge(monkeypatch, failure=failure)
    with pytest.raises(ModelUnavailableError) as caught:
        infer(client)
    assert caught.value.code == code and len(calls) == 1 and not upstream.calls
    assert KEY not in str(caught.value) and TOKEN not in str(caught.value)


@pytest.mark.parametrize("status,code", [(401, ModelErrorCode.AUTHENTICATION), (403, ModelErrorCode.AUTHENTICATION),
    (429, ModelErrorCode.UNAVAILABLE), (504, ModelErrorCode.UNAVAILABLE), (302, ModelErrorCode.UNAVAILABLE),
    (500, ModelErrorCode.UNAVAILABLE)])
def test_untrusted_http_errors_never_reflected_or_redirected(monkeypatch, status, code):
    client, _, calls, _ = bridge(monkeypatch, raw=(KEY + TOKEN).encode(), status=status)
    with pytest.raises(ModelUnavailableError) as caught:
        infer(client)
    assert caught.value.code == code and len(calls) == 1
    assert KEY not in str(caught.value) and TOKEN not in str(caught.value)


@pytest.mark.parametrize("raw", [b"markdown", b"[]", b"{}", b'{"data":[],"request_id":null}',
    b'{"data":{},"request_id":"bad id"}', b'{"data":{},"request_id":null,"extra":1}',
    b'{"data":{"bad":NaN},"request_id":null}', b"x" * (RESPONSE_LIMIT + 1),
    json.dumps({"data": {"protected": TOKEN}, "request_id": None}).encode()])
def test_invalid_wire_schema_is_rejected(monkeypatch, raw):
    client, _, _, _ = bridge(monkeypatch, raw=raw)
    with pytest.raises(AnalysisOutputValidationError) as caught:
        infer(client)
    assert TOKEN not in str(caught.value) and KEY not in str(caught.value)


@pytest.mark.parametrize("failure,code", [
    (ModelUnavailableError(KEY + TOKEN, code=ModelErrorCode.AUTHENTICATION), ModelErrorCode.AUTHENTICATION),
    (ModelUnavailableError(KEY + TOKEN, code=ModelErrorCode.TIMEOUT), ModelErrorCode.TIMEOUT),
    (ModelUnavailableError(KEY + TOKEN, code=ModelErrorCode.RATE_LIMIT), ModelErrorCode.RATE_LIMIT),
    (ModelUnavailableError(KEY + TOKEN, code=ModelErrorCode.TLS_CERTIFICATE), ModelErrorCode.TLS_CERTIFICATE),
    (RuntimeError(KEY + TOKEN), ModelErrorCode.UNAVAILABLE),
])
def test_upstream_error_mapping_and_log_redaction(monkeypatch, caplog, failure, code):
    caplog.set_level(logging.INFO)
    client, _, _, browser = bridge(monkeypatch, upstream=Upstream(failure=failure))
    response = browser.post("/v1/analyze", json=payload(), headers={"Authorization": "Bearer " + TOKEN})
    with pytest.raises(ModelUnavailableError) as caught:
        infer(client)
    assert caught.value.code == code
    for text in (response.text, str(caught.value), caplog.text):
        assert KEY not in text and TOKEN not in text and CONTEXT.user not in text


def test_invalid_upstream_response_is_not_downgraded(monkeypatch):
    client, _, _, _ = bridge(monkeypatch, upstream=Upstream(failure=AnalysisOutputValidationError(KEY + TOKEN)))
    with pytest.raises(AnalysisOutputValidationError) as caught:
        infer(client)
    assert KEY not in str(caught.value) and TOKEN not in str(caught.value)


@pytest.mark.parametrize("output", [{"value": KEY}, {KEY: "nested"}, {"nested": [{"value": TOKEN}]},
                                      {"value": float("nan")}, "text", {"value": "x" * RESPONSE_LIMIT}])
def test_no_secret_in_response_analysis_exception_logs(monkeypatch, caplog, output):
    caplog.set_level(logging.INFO)
    client, _, _, browser = bridge(monkeypatch, upstream=Upstream(output))
    response = browser.post("/v1/analyze", json=payload(), headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 502 and response.json() == {"error": "invalid_structured_response"}
    with pytest.raises(AnalysisOutputValidationError) as caught:
        infer(client)
    assert all(secret not in text for secret in (KEY, TOKEN)
               for text in (response.text, str(caught.value), caplog.text))


def test_health_and_minimal_routes(monkeypatch):
    _, upstream, _, browser = bridge(monkeypatch)
    response = browser.get("/health")
    assert response.json() == {"status": "ok"} and not upstream.calls
    assert response.headers["cache-control"] == "no-store"
    for path in ("/docs", "/openapi.json", "/proxy", "/shell", "/files"):
        assert browser.get(path).status_code == 404
    assert browser.get("/health", headers={"Host": "evil.invalid"}).status_code == 400
    assert browser.request("CONNECT", "/").status_code in {404, 405}


@pytest.mark.parametrize("change", ["auth", "origin", "fetch", "type", "extra", "schema", "context", "body", "key", "token"])
def test_invalid_requests_do_not_call_provider(monkeypatch, change):
    _, upstream, _, browser = bridge(monkeypatch)
    headers = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
    data = payload()
    if change == "auth": headers["Authorization"] = "Bearer incorrect"
    if change == "origin": headers["Origin"] = "http://localhost"
    if change == "fetch": headers["Sec-Fetch-Site"] = "same-origin"
    if change == "type": headers["Content-Type"] = "text/plain"
    if change == "extra": data["url"] = "https://unrelated.invalid"
    if change == "schema": data["schema"] = []
    if change == "context": data["user"] = "x" * 32001
    if change == "key": data["user"] = KEY
    if change == "token": data["user"] = TOKEN
    body = b"x" * (REQUEST_LIMIT + 1) if change == "body" else json.dumps(data).encode()
    response = browser.post("/v1/analyze", content=body, headers=headers)
    assert 400 <= response.status_code < 500 and not upstream.calls
    assert KEY not in response.text and TOKEN not in response.text


def test_server_harness_still_runs_after_relay(monkeypatch, evidence):
    output = python_output(evidence)
    client, _, _, _ = bridge(monkeypatch, upstream=Upstream(output))
    result = AIProjectAnalyzer(model_client=client).analyze(evidence=evidence, task_intent="运行 case01")
    assert result.draft.entrypoint.value.value == "run.py"
    assert result.model_metadata.provider == "relay"
    assert TOKEN not in result.model_dump_json() and KEY not in result.model_dump_json()
    for invalid in ("path", "evidence", "proposal"):
        bad = deepcopy(output)
        if invalid == "path": bad["draft"]["entrypoint"]["value"]["value"] = "../escape.py"
        if invalid == "evidence": bad["draft"]["entrypoint"]["evidence_refs"] = ["invented-ref"]
        if invalid == "proposal": bad["draft"]["entrypoint"]["status"] = "UNRESOLVED"
        client, _, _, _ = bridge(monkeypatch, upstream=Upstream(bad))
        with pytest.raises(AnalysisOutputValidationError):
            AIProjectAnalyzer(model_client=client).analyze(evidence=evidence, task_intent="运行 case01")


def test_factory_relay_does_not_read_direct_config(monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_AI_PROVIDER", "relay")
    monkeypatch.setenv("SBATCH_AGENT_AI_MODEL", "offline-model")
    monkeypatch.setenv("SBATCH_AGENT_AI_RELAY_URL", "http://127.0.0.1:18080")
    monkeypatch.setattr(ModelConfig, "from_env", lambda: pytest.fail("No direct fallback/config lookup"))
    client = model_client_from_env()
    assert isinstance(client, RemoteRelayModelClient)
    assert client.availability().state == "available"


def test_web_tunnel_failure_leaves_manual_mode_and_no_secret_html(monkeypatch, tmp_path, project):
    client, _, _, _ = bridge(monkeypatch, failure=ConnectionRefusedError(KEY + TOKEN))
    import sbatch_agent.web as web
    monkeypatch.setattr(web, "model_client_from_env", lambda: client)
    config = WebConfig(tmp_path / "jobs.sqlite3", tmp_path / "runs", workspace_root=tmp_path)
    with TestClient(create_app(config), base_url="http://localhost") as browser:
        new = browser.get("/new")
        failed = browser.post("/new/prepare", data={"csrf_token": hidden(new, "csrf_token"),
            "project_dir": str(project), "task_intent": "运行 case01"})
        assert failed.status_code == 503
        assert browser.get("/new").status_code == 200
        assert all(secret not in text for secret in (KEY, TOKEN) for text in (new.text, failed.text))


def test_slurm_environment_strips_provider_and_relay_credentials(monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", "RELAY_TEST_PROVIDER_KEY")
    monkeypatch.setenv("SBATCH_AGENT_AI_PROVIDER", "relay")
    monkeypatch.setenv("SBATCH_AGENT_AI_RELAY_URL", "http://127.0.0.1:18080")
    seen = []
    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    SubprocessRunner().run(["sinfo", "--version"], timeout=1)
    assert os.environ[TOKEN_ENV] == TOKEN and os.environ["RELAY_TEST_PROVIDER_KEY"] == KEY
    assert not any(name.startswith("SBATCH_AGENT_") for name in seen[0])
    assert "RELAY_TEST_PROVIDER_KEY" not in seen[0]
    assert TOKEN not in repr(seen[0]) and KEY not in repr(seen[0])


def test_production_relay_only_uses_existing_deepseek_client(monkeypatch):
    import sbatch_agent.ai_relay as relay
    seen = []
    config = ModelConfig("deepseek", "offline-model", "https://api.deepseek.com/chat/completions", "RELAY_TEST_PROVIDER_KEY")
    monkeypatch.setattr(ModelConfig, "from_env", lambda: config)
    monkeypatch.setattr(relay, "OpenAICompatibleClient", lambda value: seen.append(value) or Upstream())
    app = create_relay_app()
    assert seen == [config] and app is not None
    monkeypatch.setattr(ModelConfig, "from_env", lambda: None)
    with pytest.raises(ValueError):
        create_relay_app()


def test_relay_cannot_reuse_provider_credential_as_token(monkeypatch):
    monkeypatch.setenv("RELAY_TEST_PROVIDER_KEY", TOKEN)
    with pytest.raises(ValueError) as caught:
        create_relay_app(model_client=Upstream())
    assert TOKEN not in str(caught.value)


def test_launcher_binds_loopback_disables_access_logs_and_no_ssh(monkeypatch):
    path = Path(__file__).parents[1] / "scripts/start_ai_relay.py"
    spec = importlib.util.spec_from_file_location("relay_launcher", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    import uvicorn
    sentinel, calls = object(), []
    monkeypatch.setattr(launcher, "create_relay_app", lambda: sentinel)
    monkeypatch.setattr(launcher.resource, "setrlimit", lambda *args: None)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert launcher.main([]) == 0
    assert calls == [((sentinel,), {"host": "127.0.0.1", "port": 18081,
                                  "proxy_headers": False, "access_log": False})]


def test_busy_relay_rejects_second_inference_without_queue(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    started, finish = Event(), Event()
    upstream = Upstream()
    original = upstream.generate_structured
    def blocking(**kwargs):
        started.set()
        assert finish.wait(5)
        return original(**kwargs)
    upstream.generate_structured = blocking
    _, _, _, browser = bridge(monkeypatch, upstream=upstream)
    def request():
        return browser.post("/v1/analyze", json=payload(), headers={"Authorization": "Bearer " + TOKEN})
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(request)
        try:
            assert started.wait(5)
            assert request().status_code == 429
        finally:
            finish.set()
        assert first.result(timeout=5).status_code == 200
    assert len(upstream.calls) == 1
