"""M8-C: fake inference/relay, real local Harness; no SSH, API or Slurm."""

from copy import deepcopy
from io import BytesIO
import json
import logging
import os
import socket
import subprocess
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from sbatch_agent.ai_relay import create_relay_app
from sbatch_agent.analyzer import AIProjectAnalyzer
from sbatch_agent.model_client import AnalysisOutputValidationError, ModelErrorCode, ModelResponse, ModelUnavailableError, network_error
from sbatch_agent.model_reliability import RetryingModelClient, RetryPolicy
from sbatch_agent.prepare_errors import error_code, is_retryable_model_error
from sbatch_agent.request_trace import current_trace, current_attempt, request_scope, remaining_timeout, attempt_scope, read_bounded
from sbatch_agent.relay_client import TOKEN_ENV, relay_failure
from sbatch_agent.server_catalog import CatalogError
from sbatch_agent.smart_models import PreparationValues
from sbatch_agent.web import WebConfig, create_app
from test_smart_service import setup_smart
from test_smart_web import web, count, hidden
from test_presentation import hx, assert_partial, Elements
from test_relay import bridge, Upstream, TOKEN, KEY, CONTEXT, SCHEMA, payload


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reliability unit tests never use real network/processes")
    for owner, attr in ((socket.socket, "connect"), (socket, "create_connection"), (subprocess, "Popen")):
        monkeypatch.setattr(owner, attr, forbidden)
    for name in tuple(os.environ):
        if name.startswith("SBATCH_AGENT_AI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv("RELAY_TEST_PROVIDER_KEY", KEY)
    monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", "RELAY_TEST_PROVIDER_KEY")
    monkeypatch.setattr("sbatch_agent.web.model_client_from_env", lambda: None)


def unavailable(code=ModelErrorCode.TIMEOUT, **kw):
    return ModelUnavailableError(KEY + TOKEN, code=code, **kw)


class SequenceModel(Upstream):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.ids, self.attempts, self.budgets = [], [], []

    def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        self.ids.append(current_trace().request_id if current_trace() else None)
        self.attempts.append(current_attempt())
        self.budgets.append(remaining_timeout(120))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return ModelResponse(deepcopy(result), "provider-response-id")


def scripted_smart(tmp_path, failures, *, cluster_failure=None):
    smart, root, original, cluster = setup_smart(tmp_path, cluster_failure=cluster_failure)
    model = SequenceModel([*failures, original.output])
    sleeps = []
    smart.analyzer = AIProjectAnalyzer(model_client=model, profiles=smart.profiles, retry_sleeper=sleeps.append)
    real_scan = smart.scanner.scan
    scans = []
    def scan(path):
        scans.append(path)
        return real_scan(path)
    smart.scanner.scan = scan
    return smart, root, model, cluster, scans, sleeps


@pytest.mark.parametrize("failure", [unavailable(), unavailable(ModelErrorCode.RATE_LIMIT, http_status=429),
    unavailable(ModelErrorCode.UNAVAILABLE, http_status=500), unavailable(ModelErrorCode.UNAVAILABLE, http_status=502),
    network_error(ConnectionResetError(KEY)), network_error(ConnectionRefusedError(KEY), origin="relay")])
def test_transient_retry_success_scans_context_snapshot_once(tmp_path, failure, caplog):
    caplog.set_level(logging.INFO, logger="sbatch_agent.prepare")
    smart, root, model, cluster, scans, sleeps = scripted_smart(tmp_path, [failure])
    result = smart.prepare(project_dir=root, task_intent="运行 case01")
    assert result.state == "READY_TO_SUBMIT"
    assert len(scans) == cluster.calls == 1 and len(model.calls) == 2 and sleeps == [1]
    assert model.calls[0]["context"] is model.calls[1]["context"]
    assert model.calls[0]["schema"] is model.calls[1]["schema"]
    assert model.ids == [result.prepare_request_id] * 2 and model.attempts == [1, 2]
    records = trace_records(caplog)
    assert any(r["phase"] == "retry_backoff" and r["error_code"] == error_code(failure) for r in records)
    assert all(r["prepare_request_id"] == result.prepare_request_id for r in records)
    assert KEY not in caplog.text and TOKEN not in caplog.text and str(root) not in caplog.text


@pytest.mark.parametrize("code", [ModelErrorCode.TIMEOUT, ModelErrorCode.RATE_LIMIT])
def test_two_transient_failures_stop_before_cluster(tmp_path, code):
    smart, root, model, cluster, scans, sleeps = scripted_smart(tmp_path, [unavailable(code), unavailable(code)])
    with pytest.raises(ModelUnavailableError) as caught:
        smart.prepare(project_dir=root, task_intent="run")
    assert caught.value.code == code and len(model.calls) == 2 and sleeps == [1]
    assert len(scans) == 1 and cluster.calls == 0


@pytest.mark.parametrize("failure", [
    unavailable(ModelErrorCode.AUTHENTICATION), unavailable(ModelErrorCode.CREDENTIAL_MISSING),
    unavailable(ModelErrorCode.INVALID_CONFIG), unavailable(ModelErrorCode.NOT_CONFIGURED),
    unavailable(ModelErrorCode.TLS_CERTIFICATE), unavailable(ModelErrorCode.UNAVAILABLE),
    unavailable(ModelErrorCode.UNAVAILABLE, origin="relay", http_status=503),
    AnalysisOutputValidationError(KEY, stage="schema"),
    AnalysisOutputValidationError(KEY, stage="schema", reason="inconsistent_proposal"),
    AnalysisOutputValidationError(KEY, stage="evidence", reason="unobserved_path"),
    RuntimeError(KEY),
])
def test_only_positive_transient_allowlist_retries(failure):
    model = SequenceModel([failure, {"status": "ok"}])
    sleeps = []
    wrapper = RetryingModelClient(model, sleeper=sleeps.append)
    with pytest.raises(type(failure)):
        wrapper.generate_structured(context=CONTEXT, schema=SCHEMA)
    assert len(model.calls) == 1 and not sleeps and not is_retryable_model_error(failure)


@pytest.mark.parametrize("kind", ["inconsistent_proposal", "hallucinated_path", "unknown_evidence", "invalid_type"])
def test_actual_harness_rejects_after_one_model_call(tmp_path, kind):
    smart, root, model, cluster, scans, sleeps = scripted_smart(tmp_path, [])
    output = model.responses[0]
    proposal = output["draft"]["entrypoint"]
    if kind == "inconsistent_proposal":
        proposal["status"] = "UNRESOLVED"  # A value with unresolved is illegal.
    elif kind == "hallucinated_path":
        proposal["value"]["value"] = "nonexistent.py"
    elif kind == "unknown_evidence":
        proposal["evidence_refs"] = ["ev-never-observed"]
    else:
        output["draft"]["parallelism"] = {"threads": {"value": 8, "status": "DIRECT", "reason": "bad type", "evidence_refs": proposal["evidence_refs"]}}
    with pytest.raises(AnalysisOutputValidationError) as caught:
        smart.prepare(project_dir=root, task_intent="run")
    assert error_code(caught.value) == ("AI_OUTPUT_INVALID" if kind == "invalid_type" else "AI_OUTPUT_REJECTED")
    assert len(model.calls) == len(scans) == 1 and cluster.calls == 0 and not sleeps


@pytest.mark.parametrize("bad", ["project", "missing_intent", "long_intent"])
def test_invalid_user_input_never_calls_model(tmp_path, bad):
    smart, root, model, cluster, scans, _ = scripted_smart(tmp_path, [])
    with pytest.raises(ValueError):
        smart.prepare(project_dir=root / "absent" if bad == "project" else root,
                      task_intent="" if bad == "missing_intent" else "x" * 2001 if bad == "long_intent" else "run")
    assert not model.calls and cluster.calls == 0


def test_cluster_failure_is_fallback_not_second_ai_call(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="sbatch_agent.prepare")
    smart, root, model, cluster, _, _ = scripted_smart(tmp_path, [], cluster_failure=RuntimeError(KEY))
    result = smart.prepare(project_dir=root, task_intent="run")
    assert result.state == "NEEDS_INPUT" and {q.field for q in result.unresolved_fields} == {"partition"}
    ready = smart.finalize(prepared=result, user_values=PreparationValues(partition="a"))
    assert ready.state == "READY_TO_SUBMIT" and len(model.calls) == cluster.calls == 1
    assert any(r["error_code"] == "CLUSTER_UNAVAILABLE" for r in trace_records(caplog))


@pytest.mark.parametrize("changes", [{"max_attempts": 0}, {"max_attempts": 3}, {"max_attempts": True},
    {"initial_backoff_seconds": -1}, {"initial_backoff_seconds": 3}, {"max_backoff_seconds": 6},
    {"total_timeout_seconds": 121}, {"total_timeout_seconds": 0}, {"total_timeout_seconds": float("nan")}])
def test_retry_policy_rejects_unbounded_configuration(changes):
    with pytest.raises(ValueError):
        RetryPolicy(**changes)


def test_shared_budget_deducts_first_attempt_and_backoff():
    now, sleeps, budgets = [0.0], [], []
    class Timed(SequenceModel):
        def generate_structured(self, **kwargs):
            budgets.append(remaining_timeout(90))
            if not self.calls:
                now[0] += 60
            return super().generate_structured(**kwargs)
    model = Timed([unavailable(), {"status": "ok"}])
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    result = RetryingModelClient(model, clock=lambda: now[0], sleeper=sleep).generate_structured(context=CONTEXT, schema=SCHEMA)
    assert result.data == {"status": "ok"} and budgets == [90, 29] and sleeps == [1]


def test_budget_exhaustion_does_not_start_second_attempt():
    now = [0]
    class Slow(SequenceModel):
        def generate_structured(self, **kwargs):
            self.calls.append(kwargs)
            now[0] = 91
            raise unavailable()
    model = Slow([])
    with pytest.raises(ModelUnavailableError):
        RetryingModelClient(model, clock=lambda: now[0], sleeper=lambda _: pytest.fail("No remaining budget")).generate_structured(context=CONTEXT, schema=SCHEMA)
    assert len(model.calls) == 1


def test_chunk_read_checks_deadline_and_size():
    now = [0.0]
    class Trickle(BytesIO):
        def read1(self, size):
            now[0] += 2
            return super().read(1)
    with attempt_scope(1, 3, clock=lambda: now[0]), pytest.raises(TimeoutError):
        read_bounded(Trickle(b"abcdef"), 10, 90)
    assert len(read_bounded(BytesIO(b"a" * 100000), 10, 90)) == 11


def trace_records(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "sbatch_agent.prepare"]


@pytest.mark.parametrize("failure,code,status,retries", [
    (unavailable(), "AI_PROVIDER_TIMEOUT", 504, 2),
    (network_error(ConnectionRefusedError(KEY), origin="relay"), "AI_RELAY_UNAVAILABLE", 503, 2),
    (unavailable(ModelErrorCode.RATE_LIMIT), "AI_RATE_LIMITED", 429, 2),
    (AnalysisOutputValidationError(KEY, stage="schema"), "AI_OUTPUT_INVALID", 502, 1),
    (AnalysisOutputValidationError(KEY, stage="evidence", reason="unobserved_path"), "AI_OUTPUT_REJECTED", 422, 1),
    (AnalysisOutputValidationError(KEY, stage="schema", reason="inconsistent_proposal"), "AI_OUTPUT_REJECTED", 422, 1),
    (unavailable(ModelErrorCode.AUTHENTICATION), "AI_AUTHENTICATION_FAILED", 503, 1),
])
def test_safe_error_partial_request_id_and_manual_mode(tmp_path, caplog, monkeypatch, failure, code, status, retries):
    monkeypatch.setenv("SBATCH_AGENT_AI_INITIAL_BACKOFF", "0")
    app, config, root, model, cluster, slurm = web(tmp_path)
    model.failure = failure
    # Only the wait is zero for tests; attempts/classification remain unchanged.
    with TestClient(app, base_url="http://localhost") as browser:
        response = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run"})
        assert_partial(response, status)
        request_id = response.headers["x-prepare-request-id"]
        assert str(UUID(request_id)) == request_id and code in response.text and request_id in response.text
        assert "项目扫描已完成" in response.text
        assert "未创建或提交任何作业" in response.text
        assert ('data-analysis-retry' in response.text) == (retries == 2)
        assert '继续手动配置' in response.text
        assert browser.get("/new?mode=manual").status_code == 200
    assert len(model.calls) == retries and cluster.calls == 0 and not slurm.submit_calls and count(config) == 0
    assert KEY not in response.text + caplog.text and TOKEN not in response.text + caplog.text
    assert {r["prepare_request_id"] for r in trace_records(caplog)} == {request_id}


def test_web_success_new_request_ids_not_prompt_or_jobspec(tmp_path, caplog):
    app, config, root, model, _, slurm = web(tmp_path)
    with TestClient(app, base_url="http://localhost") as browser:
        responses = [hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run"}) for _ in range(2)]
    ids = [r.headers["x-prepare-request-id"] for r in responses]
    assert len(set(ids)) == 2 and all(r.status_code == 200 for r in responses)
    assert all(rid not in repr(model.calls) for rid in ids)
    assert count(config) == 0 and not slurm.submit_calls


def test_web_normal_error_fallback_and_duplicate_button_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_AI_INITIAL_BACKOFF", "0")
    app, _, root, model, _, _ = web(tmp_path)
    model.failure = unavailable()
    with TestClient(app, base_url="http://localhost") as browser:
        page = browser.get("/new")
        form = next(a for tag, a in Elements(page.text).items if tag == "form" and a.get("id") == "smart-prepare-form")
        assert form["hx-sync"] == "this:drop" and "button" in form["hx-disabled-elt"]
        assert "data-analysis-retry" in form["hx-disabled-elt"] and form["hx-indicator"] == "#prepare-loading"
        response = browser.post("/new/prepare", data={"project_dir": str(root), "task_intent": "run", "csrf_token": hidden(page, "csrf_token")})
        assert response.status_code == 504 and "<html" in response.text
        assert 'form="smart-prepare-form"' in response.text


def test_correlation_web_model_and_relay_same_uuid(tmp_path, monkeypatch, caplog):
    smart, root, original, cluster = setup_smart(tmp_path)
    upstream = SequenceModel([unavailable(), original.output])
    remote, _, calls, _ = bridge(monkeypatch, upstream=upstream)
    analyzer = AIProjectAnalyzer(model_client=remote, profiles=smart.profiles, retry_sleeper=lambda _: None)
    config = WebConfig(tmp_path / "db.sqlite3", tmp_path / "runs", workspace_root=tmp_path)
    app = create_app(config, profiles=smart.profiles, project_analyzer=analyzer, cluster_service=cluster)
    with TestClient(app, base_url="http://localhost") as browser:
        response = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run"})
    assert_partial(response)
    request_id = response.headers["x-prepare-request-id"]
    assert upstream.ids == [request_id, request_id] and upstream.attempts == [1, 2]
    assert all(c[3]["X-Request-ID"] == request_id for c in calls)
    assert all(request_id not in c[2].decode() for c in calls)
    assert len(upstream.calls) == 2 and cluster.calls == 1  # No nested relay retries.
    records = trace_records(caplog)
    assert {r["prepare_request_id"] for r in records} == {request_id}
    assert sum(r["phase"] == "relay" and r["status"] == "success" for r in records) == 1
    assert all(KEY not in str(r) and TOKEN not in str(r) for r in records)


@pytest.mark.parametrize("identifier", ["evil\nvalue", "x" * 1000, TOKEN, KEY, "not-a-uuid"])
def test_incoming_correlation_never_logs_arbitrary_text(identifier, caplog):
    from sbatch_agent.request_trace import emit
    with request_scope(fresh=True, incoming_id=identifier) as trace:
        emit("relay", status="success")
    assert trace.request_id != identifier
    assert all(identifier not in r.getMessage() for r in caplog.records)


def test_uuid_shaped_secret_is_not_accepted_as_request_id(monkeypatch, caplog):
    credential = str(uuid4())
    monkeypatch.setenv("RELAY_TEST_PROVIDER_KEY", credential)
    with request_scope(fresh=True, incoming_id=credential) as trace:
        assert trace.request_id != credential


@pytest.mark.parametrize("code", list(ModelErrorCode))
def test_relay_error_metadata_allowlist_preserves_provider_classification(code):
    status = 504 if code == ModelErrorCode.TIMEOUT else 429 if code == ModelErrorCode.RATE_LIMIT else 502
    error = relay_failure(status, json.dumps({"error": code.value, "http_status": 503 if code == ModelErrorCode.UNAVAILABLE else None,
                                            "failure_kind": "http"}).encode())
    assert error.code == code and error.origin == "provider"
    assert is_retryable_model_error(error) == (code in {ModelErrorCode.TIMEOUT, ModelErrorCode.RATE_LIMIT, ModelErrorCode.UNAVAILABLE})


@pytest.mark.parametrize("body", [{"error": "relay_internal_error"}, {"retryable": True},
    {"error": "provider_unavailable", "http_status": 503, "failure_kind": KEY},
    {"error": "provider_unavailable", "http_status": "503", "failure_kind": "http"}])
def test_unknown_or_malformed_relay_5xx_never_retry(body):
    error = relay_failure(502, json.dumps(body).encode())
    assert not is_retryable_model_error(error) and KEY not in str(error)


def test_runtime_catalog_error_partial_not_guessed_environment(tmp_path, caplog):
    smart, root, _, cluster = setup_smart(tmp_path)
    def fail(*args, **kwargs):
        raise CatalogError(KEY + TOKEN)
    smart.analyzer.environment_resolver.resolve = fail
    config = WebConfig(tmp_path / "db.sqlite3", tmp_path / "runs", workspace_root=tmp_path)
    app = create_app(config, profiles=smart.profiles, project_analyzer=smart.analyzer, cluster_service=cluster)
    with TestClient(app, base_url="http://localhost") as browser:
        response = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run"})
        assert_partial(response, 503)
        assert '服务器软件目录不可用' in response.text
        assert "Try analysis again" not in response.text and cluster.calls == 0
        assert '手动配置' in browser.get("/new?mode=manual").text
    assert KEY not in response.text + caplog.text and TOKEN not in response.text + caplog.text


def test_prepare_unexpected_error_is_not_traceback(tmp_path, monkeypatch, caplog):
    from sbatch_agent.smart_service import SmartJobService
    app, _, root, _, _, _ = web(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError(KEY + TOKEN)
    monkeypatch.setattr(SmartJobService, "prepare", fail)
    with TestClient(app, base_url="http://localhost") as browser:
        response = hx(browser, "/new/prepare", {"project_dir": str(root), "task_intent": "run"})
    assert_partial(response, 500)
    assert "PREPARE_INTERNAL_ERROR" in response.text and "Project scan was completed" not in response.text
    assert KEY not in response.text + caplog.text and TOKEN not in response.text + caplog.text
