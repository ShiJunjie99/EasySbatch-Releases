"""Provider transport is faked; never contacts an endpoint or reads real keys."""

import io
import json
import socket
import ssl
from urllib.error import HTTPError, URLError

import pytest

from sbatch_agent.analysis_context import AnalysisContext
from sbatch_agent.analyzer import structured_output_schema
from sbatch_agent.model_client import (
    AnalysisOutputValidationError, ModelConfig, ModelErrorCode, ModelUnavailableError, OpenAICompatibleClient, _NoRedirect,
)
import sbatch_agent.model_client as module


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Real model network access is forbidden in pytest")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setenv("TEST_MODEL_CREDENTIAL", "offline-placeholder")


def config(**kwargs):
    return ModelConfig(**{"provider": "openai-compatible", "model": "explicit-test-model",
                          "endpoint": "https://provider.invalid/v1/chat/completions",
                          "api_key_env": "TEST_MODEL_CREDENTIAL", **kwargs})


def envelope(**kwargs):
    return {"id": "request-test-1", "choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"draft": {}}), **kwargs,
    }}]}


def transport(monkeypatch, value, failure=None):
    calls = []
    class FakeOpener:
        def open(self, request, **kwargs):
            calls.append((request, kwargs))
            if failure:
                raise failure
            return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
    monkeypatch.setattr(module, "build_opener", lambda *args: FakeOpener())
    return calls


def generate(client):
    return client.generate_structured(context=AnalysisContext("rules", "data", (), ()), schema=structured_output_schema())


def test_provider_one_strict_request_no_tools_or_retry(monkeypatch):
    calls = transport(monkeypatch, envelope())
    response = generate(OpenAICompatibleClient(config()))
    assert response.data == {"draft": {}} and response.request_id == "request-test-1"
    assert len(calls) == 1
    req, kwargs = calls[0]
    body = json.loads(req.data)
    assert req.method == "POST" and kwargs["timeout"] == 30
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["store"] is False and body["stream"] is False
    assert "tools" not in body and "tool_choice" not in body
    assert "offline-placeholder" not in str(body) and "offline-placeholder" not in repr(config())


def test_deepseek_json_request_preserves_schema_security_and_uses_supported_options(monkeypatch):
    from sbatch_agent.analysis_context import SYSTEM_INSTRUCTION
    calls = transport(monkeypatch, envelope())
    schema = structured_output_schema()
    client = OpenAICompatibleClient(config(provider="deepseek"))
    client.generate_structured(context=AnalysisContext(SYSTEM_INSTRUCTION, "untrusted data", (), ()), schema=schema)
    assert len(calls) == 1
    req, options = calls[0]
    body = json.loads(req.data)
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 8192 and body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0 and not body["stream"]
    assert not {"max_completion_tokens", "store", "n", "tools", "tool_choice"} & body.keys()
    assert "UNTRUSTED PROJECT CONTENT" in body["messages"][0]["content"]
    assert json.loads(body["messages"][0]["content"].split("OUTPUT JSON SCHEMA:\n", 1)[1]) == schema
    assert body["messages"][1]["content"] == "untrusted data"
    assert "offline-placeholder" not in str(body) and options["timeout"] == 30


@pytest.mark.parametrize("content", ["", "```json\n{}\n```", "[1]", "not JSON"])
def test_deepseek_never_repairs_bad_response_or_retries(monkeypatch, content):
    calls = transport(monkeypatch, envelope(content=content))
    with pytest.raises(AnalysisOutputValidationError):
        generate(OpenAICompatibleClient(config(provider="deepseek")))
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"not JSON", {"choices": []}, envelope(content="```json\n{}\n```"),
                                  envelope(content="[]"), envelope(content=None), envelope(refusal="no"),
                                  envelope(tool_calls=[{"function": "shell"}])])
def test_malformed_refused_tool_or_text_output_rejected(monkeypatch, body):
    calls = transport(monkeypatch, body)
    with pytest.raises(AnalysisOutputValidationError):
        generate(OpenAICompatibleClient(config()))
    assert len(calls) == 1


def test_truncated_completion_rejected(monkeypatch):
    body = envelope()
    body["choices"][0]["finish_reason"] = "length"
    transport(monkeypatch, body)
    with pytest.raises(AnalysisOutputValidationError) as caught:
        generate(OpenAICompatibleClient(config()))
    assert caught.value.safe_diagnostic() == "stage=response field=output reason=truncated_output"


def test_diagnostic_metadata_is_allowlisted_not_provider_text():
    error = AnalysisOutputValidationError("PRIVATE", stage="PRIVATE", field="PRIVATE", reason="PRIVATE")
    assert "PRIVATE" not in error.safe_diagnostic()


@pytest.mark.parametrize("failure", [TimeoutError("credential-should-not-appear"), URLError("credential-should-not-appear"),
                                     HTTPError("https://private.invalid", 401, "credential-should-not-appear", {}, io.BytesIO(b"credential-should-not-appear"))])
def test_transport_failures_are_sanitized_no_retry(monkeypatch, failure):
    calls = transport(monkeypatch, {}, failure)
    with pytest.raises(ModelUnavailableError) as caught:
        generate(OpenAICompatibleClient(config()))
    assert "credential-should-not-appear" not in str(caught.value) and len(calls) == 1


def test_missing_key_does_not_contact_provider(monkeypatch):
    monkeypatch.delenv("TEST_MODEL_CREDENTIAL")
    calls = transport(monkeypatch, envelope())
    with pytest.raises(ModelUnavailableError):
        generate(OpenAICompatibleClient(config()))
    assert calls == []


def test_response_size_cap(monkeypatch):
    transport(monkeypatch, b"x" * 100)
    with pytest.raises(AnalysisOutputValidationError):
        generate(OpenAICompatibleClient(config(max_response_bytes=64)))


@pytest.mark.parametrize("override", [{"endpoint": "http://provider.invalid"}, {"endpoint": "https://user:pass@provider.invalid"},
    {"endpoint": "https://provider.invalid?api_key=x"}, {"endpoint": "https://provider.invalid/#fragment"},
    {"provider": "unknown"}, {"model": ""}, {"api_key_env": "literal key"}, {"timeout": 0}, {"timeout": float("nan")}, {"timeout": 121}])
def test_explicit_provider_configuration_validation(override):
    with pytest.raises(ValueError):
        config(**override)


def test_no_redirect_credential_forwarding():
    assert _NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid") is None


def test_config_disabled_and_partial_config(monkeypatch):
    for key in ("PROVIDER", "MODEL", "ENDPOINT", "API_KEY_ENV", "TIMEOUT"):
        monkeypatch.delenv("SBATCH_AGENT_AI_" + key, raising=False)
    assert ModelConfig.from_env() is None
    monkeypatch.setenv("SBATCH_AGENT_AI_MODEL", "configured")
    with pytest.raises(ValueError, match="incomplete"):
        ModelConfig.from_env()


@pytest.mark.parametrize("status,category", [
    (401, ModelErrorCode.AUTHENTICATION), (403, ModelErrorCode.AUTHENTICATION),
    (429, ModelErrorCode.RATE_LIMIT), (408, ModelErrorCode.TIMEOUT), (504, ModelErrorCode.TIMEOUT),
    (400, ModelErrorCode.INVALID_CONFIG), (404, ModelErrorCode.INVALID_CONFIG),
    (500, ModelErrorCode.UNAVAILABLE), (503, ModelErrorCode.UNAVAILABLE), (302, ModelErrorCode.UNAVAILABLE),
])
def test_http_error_categories_without_response_body(monkeypatch, status, category):
    failure = HTTPError("https://provider.invalid", status, "offline-placeholder", {}, io.BytesIO(b"offline-placeholder"))
    calls = transport(monkeypatch, {}, failure)
    with pytest.raises(ModelUnavailableError) as caught:
        generate(OpenAICompatibleClient(config()))
    assert caught.value.code == category and caught.value.http_status == status
    assert "offline-placeholder" not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("failure,category", [
    (TimeoutError("private"), ModelErrorCode.TIMEOUT),
    (URLError(TimeoutError("private")), ModelErrorCode.TIMEOUT),
    (URLError("DNS private"), ModelErrorCode.UNAVAILABLE),
    (ConnectionError("private"), ModelErrorCode.UNAVAILABLE),
    (URLError(ssl.SSLCertVerificationError("private")), ModelErrorCode.TLS_CERTIFICATE),
    (ssl.SSLCertVerificationError("private"), ModelErrorCode.TLS_CERTIFICATE),
])
def test_network_error_categories(monkeypatch, failure, category):
    transport(monkeypatch, {}, failure)
    with pytest.raises(ModelUnavailableError) as caught:
        generate(OpenAICompatibleClient(config()))
    assert caught.value.code == category and caught.value.http_status is None
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("key", ["", " ", "<your-key>", "x\n", " x", "非ASCII"])
def test_readiness_requires_backend_credential_without_network(monkeypatch, key):
    monkeypatch.setenv("TEST_MODEL_CREDENTIAL", key)
    calls = transport(monkeypatch, envelope())
    client = OpenAICompatibleClient(config())
    assert client.availability().state == "credential_missing"
    assert client.availability().model == "explicit-test-model"
    with pytest.raises(ModelUnavailableError) as caught:
        generate(client)
    assert caught.value.code == ModelErrorCode.CREDENTIAL_MISSING and not calls


def test_available_is_configuration_readiness_not_api_probe(monkeypatch):
    calls = transport(monkeypatch, envelope())
    client = OpenAICompatibleClient(config())
    assert client.availability().state == "available" and not calls
    assert "offline-placeholder" not in repr(client.availability())
    monkeypatch.delenv("TEST_MODEL_CREDENTIAL")
    assert client.availability().state == "credential_missing" and not calls


@pytest.mark.parametrize("response", [envelope(content=json.dumps({"draft": {}, "notes": ["offline-placeholder"]})),
                                      {**envelope(), "id": "offline-placeholder"}])
def test_provider_cannot_reflect_credential_into_output(monkeypatch, response):
    transport(monkeypatch, response)
    with pytest.raises(AnalysisOutputValidationError) as caught:
        generate(OpenAICompatibleClient(config()))
    assert "offline-placeholder" not in str(caught.value)
