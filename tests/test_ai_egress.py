"""M10-B5 restricted per-session AI egress security and isolation tests."""

from io import BytesIO
import json
import logging
import os
import re
import socket
import ssl
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sbatch_agent.ai_egress import (
    AIEgressSession, AIEgressState, AuditedAIEgressModelClient,
)
from sbatch_agent.restricted_egress import (
    AIEgressError, AIEgressErrorCode, RestrictedConnectEgressAgent,
    _parse_connect_request, _public_addresses, new_egress_credential,
)
from sbatch_agent.launcher import LauncherConfig, SSHLauncherProcess, ssh_arguments
from sbatch_agent.model_client import (
    ModelConfig, ModelErrorCode, ModelResponse, ModelUnavailableError,
    OpenAICompatibleClient,
)
from sbatch_agent.model_factory import model_client_from_env
from sbatch_agent.model_reliability import RetryingModelClient, RetryPolicy


HOST = "api.deepseek.com"


def connect_request(credential, *, target=f"{HOST}:443", host=f"{HOST}:443"):
    return (f"CONNECT {target} HTTP/1.1\r\nHost: {host}\r\n"
            f"Proxy-Authorization: Bearer {credential}\r\n"
            "Connection: close\r\n\r\n").encode("ascii")


def session(username="alice", worker_id=None, credential=None, **testing):
    value = AIEgressSession.create(
        worker_session_id=worker_id or str(uuid4()), username=username,
        remote_proxy_port=54321,
        credential=credential or new_egress_credential(), **testing,
    )
    value._set_result(None)
    return value


def test_credential_is_per_session_memory_only_and_invalidated():
    first, second = session(), session()
    secret = first.credential
    assert first.egress_id != second.egress_id
    assert first.worker_session_id != second.worker_session_id
    assert len(secret) >= 43 and secret != second.credential
    assert secret not in repr(first)
    first.invalidate()
    assert first.state == AIEgressState.INVALID and first.credential == ""
    with pytest.raises(AIEgressError, match="AI_EGRESS_UNAVAILABLE"):
        first.open_tls_tunnel(timeout=1)


@pytest.mark.parametrize("raw, status", [
    (lambda key: connect_request(key, target="example.com:443"), 403),
    (lambda key: connect_request(key, target="google.com:443"), 403),
    (lambda key: connect_request(key, target="127.0.0.1:443"), 403),
    (lambda key: connect_request(key, target="10.0.0.1:443"), 403),
    (lambda key: connect_request(key, target=f"{HOST}:80"), 403),
    (lambda key: b"GET https://api.deepseek.com/ HTTP/1.1\r\nHost: api.deepseek.com\r\n\r\n", 403),
    (lambda key: b"malformed\r\n\r\n", 403),
    (lambda key: connect_request("X" * 43), 407),
])
def test_connect_protocol_is_authenticated_and_not_a_generic_proxy(raw, status):
    key = new_egress_credential()
    assert _parse_connect_request(raw(key), key) == status
    assert _parse_connect_request(connect_request(key), key) == 200


def test_launcher_agent_binds_loopback_and_relays_only_opaque_bytes():
    key = new_egress_credential()
    upstream_for_agent, upstream_observer = socket.socketpair()
    agent = RestrictedConnectEgressAgent(key, connector=lambda timeout: upstream_for_agent)
    try:
        assert agent.host == "127.0.0.1" and agent.port > 1024
        agent.start()
        with socket.create_connection((agent.host, agent.port), timeout=2) as client:
            client.sendall(connect_request(key))
            assert client.recv(4096).startswith(b"HTTP/1.1 200")
            client.sendall(b"opaque-tls-record")
            upstream_observer.settimeout(2)
            assert upstream_observer.recv(4096) == b"opaque-tls-record"
    finally:
        agent.close()
        upstream_observer.close()
    assert not agent.running


def test_other_local_user_probe_and_old_credential_replay_are_rejected():
    old = new_egress_credential()
    agent = RestrictedConnectEgressAgent(
        new_egress_credential(),
        connector=lambda timeout: pytest.fail("bad auth must not reach Internet"),
    )
    try:
        agent.start()
        with socket.create_connection((agent.host, agent.port), timeout=2) as client:
            client.sendall(connect_request(old))
            assert client.recv(4096).startswith(b"HTTP/1.1 407")
    finally:
        agent.close()


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1",
    "169.254.169.254", "0.0.0.0", "::1", "fc00::1", "fe80::1", "ff02::1",
])
def test_launcher_dns_resolution_rejects_non_global_results(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    def private(*args, **kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]
    with pytest.raises(AIEgressError, match="AI_EGRESS_TARGET_REJECTED"):
        _public_addresses(resolver=private)


def test_launcher_dns_allows_multiple_public_addresses_but_rejects_mixed_private():
    public = lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
    ]
    assert len(_public_addresses(resolver=public)) == 2
    mixed = lambda *a, **k: public() + [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ]
    with pytest.raises(AIEgressError, match="AI_EGRESS_TARGET_REJECTED"):
        _public_addresses(resolver=mixed)


class ProxySocket:
    def __init__(self, response=b"HTTP/1.1 200 Connection Established\r\n\r\n"):
        self.response = bytearray(response)
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, value):
        self.sent.extend(value)

    def recv(self, size):
        value = bytes(self.response[:size])
        del self.response[:size]
        return value

    def close(self):
        self.closed = True


class VerifiedContext:
    check_hostname = True
    verify_mode = ssl.CERT_REQUIRED

    def __init__(self):
        self.server_hostname = None

    def wrap_socket(self, connection, *, server_hostname):
        self.server_hostname = server_hostname
        return connection


def test_server_creates_tls_with_system_verification_and_deepseek_sni():
    proxy, context = ProxySocket(), VerifiedContext()
    value = session(
        _socket_create_connection=lambda address, timeout: proxy,
        _ssl_context_factory=lambda: context,
    )
    secured = value.open_tls_tunnel(timeout=2)
    assert secured is proxy and context.server_hostname == HOST
    assert connect_request(value.credential) == bytes(proxy.sent)
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED

    bad = session(
        _socket_create_connection=lambda address, timeout: ProxySocket(),
        _ssl_context_factory=lambda: SimpleNamespace(
            check_hostname=False, verify_mode=ssl.CERT_NONE,
        ),
    )
    with pytest.raises(AIEgressError, match="AI_EGRESS_TLS_FAILED"):
        bad.health_check(timeout=2)
    assert bad.state == AIEgressState.UNAVAILABLE


def provider_response():
    return json.dumps({
        "id": "request-1",
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"ok": True}),
        }}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }).encode()


def model_config():
    return ModelConfig(
        "deepseek", "deepseek-test", "https://api.deepseek.com/chat/completions",
        "M10_B5_TEST_KEY", timeout=2,
    )


def test_deepseek_request_is_created_server_side_with_injected_transport(monkeypatch):
    calls = []

    class Transport:
        available = True
        def request(self, **kwargs):
            calls.append(kwargs)
            return 200, provider_response()

    monkeypatch.setenv("M10_B5_TEST_KEY", "server-only-key")
    client = OpenAICompatibleClient(model_config(), transport=Transport())
    result = client.generate_structured(
        context=SimpleNamespace(system="server system prompt", user="server user prompt"),
        schema={"type": "object"},
    )
    assert result.data == {"ok": True} and (result.input_tokens, result.output_tokens) == (3, 2)
    assert len(calls) == 1 and calls[0]["endpoint"].startswith("https://api.deepseek.com/")
    assert b"server user prompt" in calls[0]["body"]
    assert calls[0]["headers"]["Authorization"] == "Bearer server-only-key"


def configure_per_user(monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_AI_TRANSPORT_MODE", "per_user_ssh_egress")
    monkeypatch.setenv("SBATCH_AGENT_AI_PROVIDER", "deepseek")
    monkeypatch.setenv("SBATCH_AGENT_AI_MODEL", "deepseek-test")
    monkeypatch.setenv("SBATCH_AGENT_AI_ENDPOINT", "https://api.deepseek.com/chat/completions")
    monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", "M10_B5_TEST_KEY")
    monkeypatch.setenv("M10_B5_TEST_KEY", "server-only-key")


def test_ab_and_same_user_multi_launcher_transport_binding_no_fallback(monkeypatch):
    configure_per_user(monkeypatch)
    worker_a, worker_b, worker_a2 = str(uuid4()), str(uuid4()), str(uuid4())
    a = session("alice", worker_a)
    b = session("bob", worker_b)
    a2 = session("alice", worker_a2)
    clients = [
        model_client_from_env(ai_egress=value, worker_session_id=worker,
                              username=user, audit_session_id=str(uuid4()))
        for value, worker, user in ((a, worker_a, "alice"), (b, worker_b, "bob"),
                                    (a2, worker_a2, "alice"), (a, worker_a, "alice"))
    ]
    ids = [client.client.transport.egress_id for client in clients]
    assert ids == [a.egress_id, b.egress_id, a2.egress_id, a.egress_id]
    assert a.egress_id != b.egress_id != a2.egress_id

    with pytest.raises(ModelUnavailableError) as mismatch:
        model_client_from_env(ai_egress=a, worker_session_id=worker_b,
                              username="bob", audit_session_id=str(uuid4()))
    assert mismatch.value.code == ModelErrorCode.EGRESS_IDENTITY
    with pytest.raises(ModelUnavailableError) as unavailable:
        model_client_from_env(ai_egress=None, worker_session_id=worker_a,
                              username="alice", audit_session_id=str(uuid4()))
    assert unavailable.value.code == ModelErrorCode.EGRESS_UNAVAILABLE


def test_one_retry_uses_the_exact_same_transport(monkeypatch):
    monkeypatch.setenv("M10_B5_TEST_KEY", "server-only-key")

    class Transport:
        available = True
        def __init__(self):
            self.calls = []
        def request(self, **kwargs):
            self.calls.append(id(self))
            if len(self.calls) == 1:
                raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
            return 200, provider_response()

    transport = Transport()
    raw = OpenAICompatibleClient(model_config(), transport=transport)
    retried = RetryingModelClient(
        raw, policy=RetryPolicy(initial_backoff_seconds=0), sleeper=lambda _: None,
    )
    result = retried.generate_structured(
        context=SimpleNamespace(system="system", user="user"), schema={},
    )
    assert result.data == {"ok": True}
    assert transport.calls == [id(transport), id(transport)]


def test_audit_and_repr_never_include_prompt_response_or_credentials(caplog):
    value = session()
    secret = value.credential

    class Client:
        provider, model = "deepseek", "deepseek-test"
        def availability(self): return SimpleNamespace(state="available")
        def generate_structured(self, *, context, schema):
            return ModelResponse({"secret_response": "not-for-audit"}, "safe-request", 9, 4)

    audited = AuditedAIEgressModelClient(
        Client(), session=value, audit_session_id=str(uuid4()), username="alice",
    )
    with caplog.at_level(logging.INFO, logger="sbatch_agent.ai_egress_audit"):
        audited.generate_structured(
            context=SimpleNamespace(user="secret prompt"), schema={},
        )
    assert "safe-request" in caplog.text and '"input_tokens":9' in caplog.text
    assert "AI_PROVIDER_REQUEST_SUCCESS" in caplog.text
    assert secret not in caplog.text and "secret prompt" not in caplog.text
    assert "secret_response" not in caplog.text


def test_no_global_proxy_state_and_no_secret_in_ssh_argv(monkeypatch):
    before = {name: os.environ.get(name) for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    key = new_egress_credential()
    cfg = LauncherConfig(
        name="example-cluster", host="cluster.example.edu", port=22, remote_web_port=8000,
        broker_socket="/tmp/easysbatch-1000/broker.sock",
        worker_entrypoint="/tmp/easysbatch-1000/user-worker-v2.py",
    )
    argv = ssh_arguments(
        cfg, "alice", 51234, ssh_executable="/usr/bin/ssh",
        ai_egress_local_port=52345,
    )
    assert argv[argv.index("-R") + 1] == "127.0.0.1:0:127.0.0.1:52345"
    assert "GatewayPorts=no" in argv and "ExitOnForwardFailure=no" in argv
    assert key not in repr(argv)
    assert before == {name: os.environ.get(name) for name in before}


class FakeProcess:
    def __init__(self, stderr):
        self.stdout = BytesIO()
        self.stderr = BytesIO(stderr)
        self.returncode = None
    def poll(self): return self.returncode
    def wait(self, timeout=None): self.returncode = 0; return 0
    def terminate(self): self.returncode = -15
    def kill(self): self.returncode = -9


def test_openssh_dynamic_remote_port_allocation_is_bounded_and_parsed():
    process = SSHLauncherProcess(
        ["/usr/bin/ssh"], process_factory=lambda *args, **kwargs:
        FakeProcess(b"Allocated port 54321 for remote forward to 127.0.0.1:51200\r\n"),
    )
    process._stderr_thread.join(1)
    assert process.wait_remote_forward(timeout=0.1) == 54321
    assert "54321" not in repr(process.__dict__) or process._remote_forward_port == 54321
    process.close()
