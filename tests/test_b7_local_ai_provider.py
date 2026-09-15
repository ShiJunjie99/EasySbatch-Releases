"""M10-B7 local credential and structured Provider RPC regression tests."""

import io
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
from urllib.error import URLError
from uuid import uuid4

import pytest

from sbatch_agent.credential_store import (
    AICredentialManager, CredentialBackend, CredentialStoreError,
    KeyringAISecretStore, SessionAISecretStore,
)
from sbatch_agent.local_ai_protocol import (
    LocalAIErrorCode, decode_provider_response, encode_provider_request,
    error_response, success_response, validate_provider_request,
)
from sbatch_agent.local_ai_provider import LocalDeepSeekProviderClient
from sbatch_agent.local_user_model_client import LocalUserProviderConfig, LocalUserProviderModelClient
from sbatch_agent.local_user_provider_session import LocalUserProviderSession
from sbatch_agent.analysis_context import AnalysisContext
from sbatch_agent.model_client import ModelErrorCode, ModelUnavailableError
from sbatch_agent.launcher_ai_stream import LauncherAIEgressAgent
from sbatch_agent.worker_broker import WorkerBrokerError, WorkerConnection
from sbatch_agent.launcher import _ai_credential_location


SECRET = "local-test-secret-value"


def test_session_only_cli_does_not_claim_persistent_secure_storage():
    """The documented Linux fallback is explicitly memory-only."""
    # Keep this regression close to the credential tests: the command's user-facing
    # wording must not imply that a session-only key survived process shutdown.
    text = _ai_credential_location(True)
    assert "本次运行使用" in text
    assert "系统安全凭据管理器" not in text
    assert "系统安全凭据管理器" in _ai_credential_location(False)


class FakeVault:
    __module__ = "keyring.backends.SecretService"

    def __init__(self):
        self.value = None

    def get_password(self, service, account):
        assert (service, account) == ("EasySbatch", "deepseek:default")
        return self.value

    def set_password(self, service, account, value):
        self.value = value

    def delete_password(self, service, account):
        self.value = None


def test_secure_store_set_get_delete_and_redacted_repr():
    vault = FakeVault()
    store = KeyringAISecretStore(backend=vault, platform="linux", os_name="posix")
    manager = AICredentialManager(secure_store=store)
    assert manager.backend == CredentialBackend.SECRET_SERVICE
    manager.set(SECRET)
    assert manager.exists() and manager.get().reveal() == SECRET
    assert SECRET not in repr(manager) and SECRET not in repr(manager.get())
    assert manager.delete() and not manager.exists()


def test_session_only_explicitly_overrides_persistent_entry_without_plaintext_file():
    vault = FakeVault()
    manager = AICredentialManager(secure_store=KeyringAISecretStore(
        backend=vault, platform="linux", os_name="posix"))
    manager.set("persistent-secret")
    manager.set("session-secret", session_only=True)
    assert manager.backend == CredentialBackend.SESSION_ONLY
    assert manager.get().reveal() == "session-secret"
    manager.delete()
    assert vault.value is None and not manager.exists()


def test_unapproved_backend_never_falls_back_to_plaintext():
    class FileBackend:
        def get_password(self, *args): return None
        def set_password(self, *args): pass
        def delete_password(self, *args): pass
    store = KeyringAISecretStore(backend=FileBackend(), platform="linux", os_name="posix")
    assert store.backend == CredentialBackend.UNAVAILABLE and not store.available
    manager = AICredentialManager(secure_store=store)
    with pytest.raises(CredentialStoreError):
        manager.set(SECRET)
    session = SessionAISecretStore()
    session.set(SECRET)
    assert session.backend == CredentialBackend.SESSION_ONLY and session.get().reveal() == SECRET


def request_value(**overrides):
    value = {
        "request_id": str(uuid4()), "provider": "deepseek", "model": "deepseek-chat",
        "messages": [{"role": "system", "content": "system"},
                     {"role": "user", "content": "user"}],
        "parameters": {"response_format": {"type": "json_object"}, "max_tokens": 8,
                       "thinking": {"type": "disabled"}, "temperature": 0, "stream": False},
        "timeout_seconds": 10, "metadata": {"prepare_request_id": None},
    }
    value.update(overrides)
    return value


class FakeResponse:
    def __init__(self, status=200, body=b'{}'):
        self.status = status
        self.body = body

    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, size=-1):
        body, self.body = self.body, b""
        return body


def provider_client(response):
    vault = FakeVault()
    manager = AICredentialManager(secure_store=KeyringAISecretStore(
        backend=vault, platform="linux", os_name="posix"))
    manager.set(SECRET)
    return LocalDeepSeekProviderClient(
        credentials=manager,
        opener_factory=lambda context: type("Opener", (), {
            "open": lambda self, request, timeout: response,
        })(),
        ssl_context_factory=lambda: type("TLS", (), {
            "check_hostname": True, "verify_mode": __import__("ssl").CERT_REQUIRED,
        })(),
    )


def valid_provider_body(content="{}"):
    return {"id": "resp_1", "choices": [{"finish_reason": "stop",
        "message": {"content": content}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def test_local_provider_posts_fixed_endpoint_and_returns_provider_response():
    client = provider_client(FakeResponse(body=json.dumps(valid_provider_body()).encode()))
    result = client.request(request_value())
    assert result["status"] == "ok" and result["provider_response"]["choices"]


@pytest.mark.parametrize("status,category", [(401, "AI_PROVIDER_AUTH_FAILED"),
                                               (429, "AI_PROVIDER_RATE_LIMITED"),
                                               (503, "AI_PROVIDER_UNAVAILABLE")])
def test_local_provider_http_error_taxonomy(status, category):
    result = provider_client(FakeResponse(status=status)).request(request_value())
    assert result["error_category"] == category and result["provider_response"] is None


def test_local_provider_malformed_and_oversized_response_are_bounded():
    malformed = provider_client(FakeResponse(body=b"not-json")).request(request_value())
    assert malformed["error_category"] == LocalAIErrorCode.RESPONSE_INVALID
    oversized = provider_client(FakeResponse(body=b"x" * (128 * 1024 + 1))).request(request_value())
    assert oversized["error_category"] == LocalAIErrorCode.RESPONSE_INVALID


def test_local_provider_urllib_timeout_is_classified_without_raw_exception():
    class Opener:
        def open(self, request, timeout):
            raise URLError(TimeoutError("secret provider detail"))
    client = provider_client(FakeResponse())
    client._opener_factory = lambda context: Opener()
    result = client.request(request_value())
    assert result["error_category"] == LocalAIErrorCode.TIMEOUT
    assert "secret provider detail" not in repr(result)


def test_missing_local_key_does_not_contact_provider():
    called = []
    vault = FakeVault()
    client = LocalDeepSeekProviderClient(
        credentials=AICredentialManager(secure_store=KeyringAISecretStore(
            backend=vault, platform="linux", os_name="posix")),
        opener_factory=lambda context: called.append(True),
    )
    result = client.request(request_value())
    assert result["error_category"] == LocalAIErrorCode.NOT_CONFIGURED and not called


def test_request_protocol_rejects_other_provider_and_url_surface():
    value = request_value(provider="openai")
    with pytest.raises(ValueError):
        validate_provider_request(value)
    value = request_value(metadata={"prepare_request_id": None, "url": "https://evil"})
    with pytest.raises(ValueError):
        encode_provider_request(value)


def test_provider_request_size_limit_is_independent_of_scanner_context_limit():
    value = request_value(messages=[{"role": "system", "content": "s" * 120000},
                                   {"role": "user", "content": "u" * 60001}])
    with pytest.raises(ValueError):
        encode_provider_request(value)


def test_provider_response_request_id_and_secret_are_not_reflected():
    request = request_value()
    response = success_response(request["request_id"], valid_provider_body())
    assert decode_provider_response(json.dumps(response, separators=(",", ":")).encode(),
                                   request_id=request["request_id"])["status"] == "ok"
    with pytest.raises(ValueError):
        decode_provider_response(json.dumps(response).encode(), request_id=str(uuid4()))


def test_provider_rpc_uses_current_worker_channel_and_reports_local_status():
    class Provider:
        def status(self):
            return {"provider": "deepseek", "configured": True,
                    "backend": "Session only", "availability": "configured"}
        def request(self, value):
            return success_response(value["request_id"], valid_provider_body())
    server, launcher = socket.socketpair()
    connection = WorkerConnection(server, on_disconnect=lambda: None)
    agent = LauncherAIEgressAgent(launcher, launcher, provider_client=Provider())
    agent.start()
    try:
        assert agent.wait_ready(1) and connection.wait_ai_ready(1)
        connection.wait_local_provider_status(1)
        assert connection.local_provider_configured
        request = request_value()
        response = connection.request_ai_provider(request, timeout=2)
        assert response["status"] == "ok"
    finally:
        agent.close(); connection.close(); launcher.close()


class _SessionConnection:
    error_type = WorkerBrokerError

    def __init__(self, *, configured=True):
        self.connected = True
        self.local_provider_configured = configured
        self.local_provider_status = {
            "provider": "deepseek", "configured": configured,
            "backend": "Session only", "availability": "configured" if configured else "not_configured",
        }
        self.requests = []

    def request_ai_provider(self, request, *, timeout):
        self.requests.append(request)
        return success_response(request["request_id"], valid_provider_body('{"draft":{}}'))


def test_exact_session_binding_and_same_user_sessions_never_cross_route(caplog):
    a_conn, b_conn = _SessionConnection(), _SessionConnection()
    a = LocalUserProviderSession.create(worker_session_id=str(uuid4()), username="alice",
                                        connection=a_conn)
    b = LocalUserProviderSession.create(worker_session_id=str(uuid4()), username="alice",
                                        connection=b_conn)
    with pytest.raises(WorkerBrokerError) as mismatch:
        a.assert_binding(worker_session_id=b.worker_session_id, username=b.username)
    assert mismatch.value.code == "AI_PROVIDER_IDENTITY_MISMATCH"
    with caplog.at_level(logging.INFO, logger="sbatch_agent.local_ai_audit"):
        response = a.request_provider(request_value(), timeout=2)
    assert response["status"] == "ok"
    assert len(a_conn.requests) == 1 and not b_conn.requests
    assert SECRET not in caplog.text


def test_server_model_client_builds_prompt_but_never_receives_or_serializes_key():
    connection = _SessionConnection()
    session = LocalUserProviderSession.create(worker_session_id=str(uuid4()), username="alice",
                                              connection=connection)
    client = LocalUserProviderModelClient(LocalUserProviderConfig(), session=session,
                                          username="alice")
    response = client.generate_structured(
        context=AnalysisContext("server rules", "untrusted project text", (), ()),
        schema={"type": "object"},
    )
    assert response.data == {"draft": {}}
    serialized = json.dumps(connection.requests[0])
    assert "untrusted project text" in serialized and SECRET not in serialized


def test_local_session_error_mapping_preserves_timeout_and_disconnect_taxonomy():
    class TimeoutConnection(_SessionConnection):
        def request_ai_provider(self, request, *, timeout):
            raise WorkerBrokerError("AI_PROVIDER_TIMEOUT")
    session = LocalUserProviderSession.create(worker_session_id=str(uuid4()), username="u",
                                              connection=TimeoutConnection())
    client = LocalUserProviderModelClient(LocalUserProviderConfig(), session=session)
    with pytest.raises(ModelUnavailableError) as caught:
        client.generate_structured(context=AnalysisContext("s", "u", (), ()), schema={})
    assert caught.value.code == ModelErrorCode.TIMEOUT


def test_disconnected_exact_launcher_session_fails_closed_without_cross_session_fallback():
    class DisconnectedConnection(_SessionConnection):
        def request_ai_provider(self, request, *, timeout):
            raise WorkerBrokerError("AI_CLIENT_DISCONNECTED")
    session = LocalUserProviderSession.create(worker_session_id=str(uuid4()), username="u",
                                              connection=DisconnectedConnection())
    with pytest.raises(WorkerBrokerError) as caught:
        session.request_provider(request_value(), timeout=1)
    assert caught.value.code == "AI_CLIENT_DISCONNECTED"


def test_server_local_transport_config_contains_no_api_key_environment(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("b7_start_web", root / "scripts/start_web.py")
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    config = tmp_path / "local.toml"
    config.write_text("""[ai]
provider = "deepseek"
model = "deepseek-chat"
endpoint = "https://api.deepseek.com/chat/completions"
transport_mode = "local_user_provider"
timeout = 60

[web]
port = 8000
database_path = ".sbatch-agent/jobs.sqlite3"
runs_root = ".sbatch-agent/runs"
profiles_path = ""
authentication_enabled = false
ssh_host = "cluster.example.edu"
ssh_port = 22
session_idle_timeout_seconds = 1800
session_cookie_secure = false
deployment_mode = "loopback_legacy"
public_base_url = ""
""", encoding="utf-8")
    config.chmod(0o600)
    values, _ = startup.load_settings(config, tmp_path)
    assert values["SBATCH_AGENT_AI_TRANSPORT_MODE"] == "local_user_provider"
    assert "SBATCH_AGENT_AI_API_KEY_ENV" not in values
    assert not any("KEY" in key and "AI" in key for key in values)
    monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", "OLD_AI_KEY")
    monkeypatch.setenv("OLD_AI_KEY", "must-not-enter-server")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-enter-server")
    monkeypatch.setattr(startup, "serve", lambda *args: None)
    assert startup.main(["--config", str(config)]) == 0
    assert "SBATCH_AGENT_AI_API_KEY_ENV" not in os.environ
    assert "OLD_AI_KEY" not in os.environ
    assert "DEEPSEEK_API_KEY" not in os.environ


def test_smart_prepare_uses_exact_local_session_analyzer(tmp_path, monkeypatch):
    """The primary AI UI must select a session Analyzer, not the empty global one."""
    from dataclasses import replace
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import sbatch_agent.web as web_module
    from test_smart_service import setup_smart
    from test_ssh_first_web import BASE_URL, FakeBroker, FakeWorkerContext, bootstrap, config
    from test_web import FakeSlurmClient, post

    for name in ("PROVIDER", "MODEL", "ENDPOINT", "API_KEY_ENV", "TIMEOUT"):
        monkeypatch.delenv("SBATCH_AGENT_AI_" + name, raising=False)
    monkeypatch.setenv("SBATCH_AGENT_AI_TRANSPORT_MODE", "local_user_provider")
    monkeypatch.setattr(web_module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_name="alice"))
    smart, root, model, cluster = setup_smart(tmp_path)
    captured = []

    def model_for_session(settings, **kwargs):
        captured.append(kwargs)
        return model

    monkeypatch.setattr(web_module, "LocalUserProviderModelClient", model_for_session)

    class Context(FakeWorkerContext):
        def attach_local_ai_provider(self, **kwargs):
            self.ai_egress = LocalUserProviderSession.create(
                worker_session_id=str(uuid4()), username=self.identity.username,
                connection=_SessionConnection(),
            )
            return self.ai_egress

    broker, slurm = FakeBroker(), FakeSlurmClient()
    app = web_module.create_app(
        replace(config(tmp_path), workspace_root=tmp_path), profiles=smart.profiles,
        cluster_service=cluster, slurm_client=slurm, worker_broker=broker,
    )
    context = Context("alice", 1001)
    with TestClient(app, base_url=BASE_URL) as client:
        response, _ = bootstrap(client, broker, str(uuid4()), SECRET, context)
        assert response.status_code == 303
        response = post(client, "/new/prepare", {"project_dir": str(root), "task_intent": "run synthetic demo"})
        assert response.status_code == 303, response.text
        assert len(captured) == len(model.calls) == 1
        assert captured[0]["session"] is context.ai_egress
        assert captured[0]["username"] == "alice"
        assert not slurm.submit_calls
