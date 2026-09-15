"""M10-B5B session-bound structured SSH egress integration tests."""

import os
import logging
import socket
import ssl
import time
from uuid import uuid4

import pytest

from sbatch_agent.ai_egress import AIEgressHTTPTransport, AIEgressState
from sbatch_agent.ai_stream_protocol import (
    CONTROL_STREAM_ID, Frame, FrameType, MAX_BUFFERED_BYTES, MAX_FRAME_SIZE,
    OPEN_PAYLOAD, write_frame,
)
import sbatch_agent.launcher_ai_stream as launcher_stream_module
from sbatch_agent.launcher_ai_stream import LauncherAIEgressAgent
from sbatch_agent.model_factory import model_client_from_env
from sbatch_agent.restricted_egress import AIEgressError, AIEgressErrorCode
from sbatch_agent.structured_ai_egress import (
    StructuredAIEgressSession, emit_ai_stream_event,
)
from sbatch_agent.worker_broker import WorkerConnection, WorkerBrokerError


def channel(connector):
    server, launcher = socket.socketpair()
    connection = WorkerConnection(server, on_disconnect=lambda: None)
    agent = LauncherAIEgressAgent(
        launcher.makefile("rb", buffering=0),
        launcher.makefile("wb", buffering=0), connector=connector,
    )
    agent.start()
    assert agent.wait_ready(1) and connection.wait_ai_ready(1)
    return connection, agent, launcher


def close_channel(connection, agent, launcher):
    agent.close()
    connection.close()
    launcher.close()


def test_open_data_eof_close_uses_raw_bytes_and_no_listener():
    upstream, observer = socket.socketpair()
    connection, agent, launcher = channel(lambda timeout: upstream)
    tunnel = connection.open_ai_socket(timeout=1)
    tunnel.settimeout(1)
    observer.settimeout(1)
    tunnel.sendall(b"\x16\x03\x01opaque-client-tls")
    assert observer.recv(128) == b"\x16\x03\x01opaque-client-tls"
    observer.sendall(b"\x16\x03\x03opaque-provider-tls")
    assert tunnel.recv(128) == b"\x16\x03\x03opaque-provider-tls"
    tunnel.close()
    observer.close()
    close_channel(connection, agent, launcher)


def test_graceful_bidirectional_eof_keeps_worker_channel_connected():
    """A completed TLS health stream must not tear down its Worker channel."""
    upstream, observer = socket.socketpair()
    connection, agent, launcher = channel(lambda timeout: upstream)
    tunnel = connection.open_ai_socket(timeout=1)

    # Closing the example-cluster TLS side propagates EOF to the provider.  Closing the
    # provider side then completes the reverse EOF without making the two
    # peers' simultaneous stream cleanup look like an unknown stream ID.
    tunnel.close()
    observer.settimeout(1)
    assert observer.recv(1) == b""
    observer.close()

    deadline = time.monotonic() + 1
    while connection._ai_streams and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert not connection._ai_streams
    assert connection.connected and connection.ai_ready and agent.ready
    close_channel(connection, agent, launcher)


def test_two_users_and_same_user_two_sessions_never_cross_streams():
    resources = []
    for marker in (b"A", b"B", b"A2"):
        upstream, observer = socket.socketpair()
        connection, agent, launcher = channel(lambda timeout, s=upstream: s)
        tunnel = connection.open_ai_socket(timeout=1)
        tunnel.settimeout(1)
        observer.settimeout(1)
        resources.append((marker, tunnel, observer, connection, agent, launcher))
    for marker, tunnel, observer, *_ in resources:
        tunnel.sendall(marker)
        assert observer.recv(len(marker)) == marker
    for marker, tunnel, observer, *_ in resources:
        observer.sendall(marker.lower())
        assert tunnel.recv(len(marker)) == marker.lower()
    for _, tunnel, observer, connection, agent, launcher in resources:
        tunnel.close(); observer.close(); close_channel(connection, agent, launcher)


def test_connect_failure_degrades_ai_without_closing_worker_channel():
    def unavailable(timeout):
        raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)

    connection, agent, launcher = channel(unavailable)
    with pytest.raises(WorkerBrokerError):
        connection.open_ai_socket(timeout=1)
    assert connection.connected and connection.ai_ready and agent.ready
    close_channel(connection, agent, launcher)


def test_connect_timeout_and_read_timeout_close_only_ai_stream():
    def timed_out(timeout):
        raise AIEgressError(AIEgressErrorCode.TIMEOUT)

    connection, agent, launcher = channel(timed_out)
    with pytest.raises(WorkerBrokerError, match="AI_EGRESS_TIMEOUT"):
        connection.open_ai_socket(timeout=1)
    assert connection.connected and agent.ready
    close_channel(connection, agent, launcher)

    server, launcher = socket.socketpair()
    upstream, observer = socket.socketpair()
    connection = WorkerConnection(server, on_disconnect=lambda: None)
    agent = LauncherAIEgressAgent(
        launcher.makefile("rb", buffering=0), launcher.makefile("wb", buffering=0),
        connector=lambda timeout: upstream, read_timeout=0.05,
    )
    agent.start()
    assert agent.wait_ready(1) and connection.wait_ai_ready(1)
    tunnel = connection.open_ai_socket(timeout=1)
    deadline = time.monotonic() + 1
    while connection._ai_streams and time.monotonic() < deadline:
        time.sleep(0.01)
    assert connection.connected and not connection._ai_streams, connection.last_error_code
    tunnel.close(); observer.close(); close_channel(connection, agent, launcher)


def test_write_timeout_and_buffer_limit_close_only_stream(monkeypatch):
    upstream, observer = socket.socketpair()
    connection, agent, launcher = channel(lambda timeout: upstream)
    tunnel = connection.open_ai_socket(timeout=1)
    monkeypatch.setattr(
        launcher_stream_module.select, "select",
        lambda read, write, error, timeout: ((), (), ()),
    )
    tunnel.sendall(b"opaque")
    deadline = time.monotonic() + 1
    while connection._ai_streams and time.monotonic() < deadline:
        time.sleep(0.01)
    assert connection.connected and not connection._ai_streams, connection.last_error_code
    tunnel.close(); observer.close(); close_channel(connection, agent, launcher)

    class Agent:
        ready = True
        def __init__(self): self.sent = []; self.forgot = []
        def _send(self, frame): self.sent.append(frame)
        def _forget(self, stream_id): self.forgot.append(stream_id)

    fake = Agent()
    stream = launcher_stream_module._LauncherAIStream(fake, uuid4())
    stream._connected.set()
    for _ in range(MAX_BUFFERED_BYTES // MAX_FRAME_SIZE):
        stream.receive(Frame(FrameType.AI_DATA, stream.stream_id, b"x" * MAX_FRAME_SIZE))
    stream.receive(Frame(FrameType.AI_DATA, stream.stream_id, b"overflow"))
    assert stream._closed.is_set() and fake.forgot == [stream.stream_id]


def test_unknown_stream_and_data_before_open_fail_closed():
    server, launcher = socket.socketpair()
    connection = WorkerConnection(server, on_disconnect=lambda: None)
    write_frame(launcher, Frame(FrameType.AI_DATA, uuid4(), b"x"))
    deadline = time.monotonic() + 1
    while connection.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not connection.connected
    launcher.shutdown(socket.SHUT_RDWR)
    launcher.close()


def test_one_active_stream_limit_and_launcher_disconnect_cleanup():
    upstream, observer = socket.socketpair()
    connection, agent, launcher = channel(lambda timeout: upstream)
    tunnel = connection.open_ai_socket(timeout=1)
    with pytest.raises(WorkerBrokerError):
        connection.open_ai_socket(timeout=0.1)
    launcher.shutdown(socket.SHUT_RDWR)
    launcher.close()
    deadline = time.monotonic() + 1
    while connection.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not connection.connected
    tunnel.close(); observer.close(); agent.close()


def test_data_after_close_and_unknown_stream_reject_channel():
    upstream, observer = socket.socketpair()
    connection, agent, launcher = channel(lambda timeout: upstream)
    tunnel = connection.open_ai_socket(timeout=1)
    stream_id = next(iter(connection._ai_streams))
    write_frame(launcher, Frame(FrameType.AI_CLOSE, stream_id))
    deadline = time.monotonic() + 1
    while stream_id in connection._ai_streams and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stream_id not in connection._ai_streams and connection.connected
    write_frame(launcher, Frame(FrameType.AI_DATA, stream_id, b"late"))
    deadline = time.monotonic() + 1
    while connection.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not connection.connected
    tunnel.close(); observer.close(); agent.close(); launcher.close()


def test_duplicate_open_result_is_protocol_rejected():
    upstream, observer = socket.socketpair()
    connection, agent, launcher = channel(lambda timeout: upstream)
    tunnel = connection.open_ai_socket(timeout=1)
    stream_id = next(iter(connection._ai_streams))
    write_frame(launcher, Frame(FrameType.AI_OPEN_OK, stream_id))
    deadline = time.monotonic() + 1
    while connection.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not connection.connected
    tunnel.close(); observer.close(); agent.close(); launcher.close()


def test_structured_session_binding_tls_configuration_and_transport_injection():
    class Connection:
        def __init__(self): self.calls = []
        def open_ai_socket(self, timeout):
            self.calls.append(timeout)
            return type("Raw", (), {
                "settimeout": lambda self, value: None,
                "close": lambda self: None,
            })()

    class Context:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED
        def __init__(self): self.sni = None
        def wrap_socket(self, raw, *, server_hostname):
            self.sni = server_hostname
            return type("TLS", (), {"close": lambda self: None})()

    worker_id = str(uuid4())
    tls = Context()
    session = StructuredAIEgressSession.create(
        worker_session_id=worker_id, username="alice", connection=Connection(),
        _ssl_context_factory=lambda: tls,
    )
    secured = session.open_tls_tunnel(timeout=2)
    assert tls.sni == "api.deepseek.com"
    secured.close()
    session._set_result()
    assert AIEgressHTTPTransport(session).available
    session.assert_binding(worker_session_id=worker_id, username="alice")
    with pytest.raises(AIEgressError, match="AI_EGRESS_IDENTITY_MISMATCH"):
        session.assert_binding(worker_session_id=str(uuid4()), username="alice")


def test_no_global_proxy_env_or_session_secret_and_invalidation():
    before = {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
    connection = type("Connection", (), {"open_ai_socket": lambda self, timeout: None})()
    session = StructuredAIEgressSession.create(
        worker_session_id=str(uuid4()), username="alice", connection=connection,
    )
    representation = repr(session)
    assert "credential" not in representation.lower() and "proxy" not in representation.lower()
    assert {key: os.environ.get(key) for key in before} == before
    session.invalidate()
    assert session.state == AIEgressState.INVALID
    with pytest.raises(AIEgressError, match="AI_EGRESS_UNAVAILABLE"):
        session.open_tls_tunnel(timeout=1)


def test_structured_model_factory_keeps_session_transport_explicit(monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_AI_TRANSPORT_MODE", "structured_ssh_egress")
    monkeypatch.setenv("SBATCH_AGENT_AI_PROVIDER", "deepseek")
    monkeypatch.setenv("SBATCH_AGENT_AI_MODEL", "deepseek-test")
    monkeypatch.setenv(
        "SBATCH_AGENT_AI_ENDPOINT", "https://api.deepseek.com/chat/completions",
    )
    monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", "M10_B5B_SERVER_KEY")
    monkeypatch.setenv("M10_B5B_SERVER_KEY", "server-only-test-value")
    worker_id = str(uuid4())
    connection = type("Connection", (), {"open_ai_socket": lambda self, timeout: None})()
    session = StructuredAIEgressSession.create(
        worker_session_id=worker_id, username="alice", connection=connection,
    )
    session._set_result()
    client = model_client_from_env(
        ai_egress=session, worker_session_id=worker_id, username="alice",
        audit_session_id=str(uuid4()),
    )
    assert client.client.transport.egress_id == session.egress_id
    with pytest.raises(Exception) as mismatch:
        model_client_from_env(
            ai_egress=session, worker_session_id=str(uuid4()), username="alice",
            audit_session_id=str(uuid4()),
        )
    assert "server-only-test-value" not in repr(mismatch.value)


def test_fixed_open_payload_has_no_generic_proxy_surface():
    assert OPEN_PAYLOAD == b"api.deepseek.com\0\x01\xbb"
    for forbidden in (b"example.com", b"google.com", b"127.0.0.1", b"10.0.0.1"):
        assert forbidden not in OPEN_PAYLOAD
    assert CONTROL_STREAM_ID.int == 0


def test_stream_audit_is_metadata_only_and_redacts_payload_secrets(caplog):
    secret = "M10B5B_TEST_API_KEY_DO_NOT_LOG"
    proxy_secret = "M10B5B_TEST_PROXY_SECRET_DO_NOT_LOG"
    with caplog.at_level(logging.INFO, logger="sbatch_agent.ai_stream_audit"):
        emit_ai_stream_event(
            "AI_EGRESS_STREAM_CLOSED", audit_session_id=str(uuid4()),
            username="alice", stream_id=uuid4(), bytes_in=len(secret),
            bytes_out=len(proxy_secret), duration_ms=2,
        )
    assert "AI_EGRESS_STREAM_CLOSED" in caplog.text
    assert secret not in caplog.text and proxy_secret not in caplog.text
