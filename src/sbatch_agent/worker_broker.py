"""Kernel-verified per-user worker registry for the SSH-first Alpha.

The broker accepts a deliberately tiny versioned protocol over an AF_UNIX
socket.  A worker's claimed identity is never authoritative: Linux
SO_PEERCRED plus the server's pwd database define the authenticated identity.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import queue
import re
import secrets
import select
import socket
import stat
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from .ai_egress import AIEgressSession
from .ai_stream_protocol import (
    CONNECT_TIMEOUT_SECONDS, CONTROL_STREAM_ID, Frame, FrameType,
    HELLO_PAYLOAD, MAX_ACTIVE_AI_STREAMS_PER_SESSION, MAX_BUFFERED_BYTES,
    MAX_FRAME_SIZE, OPEN_PAYLOAD, PROTOCOL_VERSION, ProtocolError,
    READ_TIMEOUT_SECONDS,
    WRITE_TIMEOUT_SECONDS, read_frame, write_frame,
)
from .local_ai_protocol import (
    LocalAIErrorCode, decode_provider_response, decode_provider_status,
    encode_provider_request, MAX_PROVIDER_REQUEST_BYTES,
)
from .local_user_provider_session import LocalUserProviderSession
from .ssh_poc import IdentityProbeResult
from .structured_ai_egress import StructuredAIEgressSession, emit_ai_stream_event
from . import user_worker


WORKER_FILENAME = f"user-worker-v{PROTOCOL_VERSION}.py"
LEGACY_WORKER_FILENAME = "user-worker-v1.py"
LEGACY_WORKER_GUARD_SOURCE = (
    b'import sys\n'
    b'sys.stdout.write("EASYSBATCH_WORKER_READY_V2 {}\\n")\n'
    b'sys.stdout.flush()\n'
    b'raise SystemExit(2)\n'
)
MAX_MESSAGE_BYTES = 32 * 1024
BOOTSTRAP_TTL_SECONDS = 45
BROKER_ERROR_CODES = frozenset({
    "BROKER_UNAVAILABLE", "BROKER_PATH_INVALID", "BROKER_PEER_UNSUPPORTED",
    "WORKER_PROTOCOL_INVALID", "WORKER_PROTOCOL_UNSUPPORTED", "WORKER_IDENTITY_MISMATCH",
    "WORKER_UNKNOWN_UID", "WORKER_DUPLICATE", "WORKER_DISCONNECTED", "WORKER_TIMEOUT",
    "BOOTSTRAP_INVALID", "BOOTSTRAP_EXPIRED", "BOOTSTRAP_REPLAYED",
    "BOOTSTRAP_WORKER_MISMATCH", "BOOTSTRAP_WORKER_UNAVAILABLE",
    "AI_EGRESS_UNAVAILABLE", "AI_EGRESS_AUTH_FAILED",
    "AI_EGRESS_IDENTITY_MISMATCH", "AI_EGRESS_TARGET_REJECTED",
    "AI_EGRESS_TLS_FAILED", "AI_EGRESS_TIMEOUT",
    "AI_PROVIDER_TIMEOUT", "AI_PROVIDER_IDENTITY_MISMATCH",
    "AI_CLIENT_DISCONNECTED", "AI_PROVIDER_RESPONSE_INVALID",
})
WORKER_OPERATIONS = frozenset({"identity", "ping", "shutdown"})
WORKER_AUDIT_EVENTS = frozenset({
    "WORKER_CONNECTED", "SSH_BOOTSTRAP_CREATED", "WEB_SESSION_BOUND",
    "WORKER_DISCONNECTED", "WEB_SESSION_INVALIDATED",
})
AUDIT = logging.getLogger("sbatch_agent.worker_audit")


class WorkerBrokerError(RuntimeError):
    """A bounded category that never includes protocol or credential content."""

    def __init__(self, code):
        self.code = code if code in BROKER_ERROR_CODES else "BROKER_UNAVAILABLE"
        super().__init__(self.code)


def emit_worker_event(event, *, audit_id=None, username=None, uid=None, result,
                      error_code=None):
    if event not in WORKER_AUDIT_EVENTS or result not in {"SUCCESS", "FAIL", "CLOSED"}:
        raise ValueError("Invalid worker audit event")
    AUDIT.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "audit_id": audit_id,
        "username": username,
        "uid": uid,
        "result": result,
        "error_code": error_code,
    }, separators=(",", ":"), sort_keys=True))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _request_id(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    UUID(value)
    return value


def _decode_object(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID") from None
    if not isinstance(value, dict):
        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
    return value


def _recv_line(connection, *, timeout):
    connection.settimeout(timeout)
    data = bytearray()
    try:
        while True:
            chunk = connection.recv(min(4096, MAX_MESSAGE_BYTES + 1 - len(data)))
            if not chunk:
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            data.extend(chunk)
            newline = data.find(b"\n")
            if newline >= 0:
                if newline != len(data) - 1 or newline > MAX_MESSAGE_BYTES:
                    raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                return bytes(data[:newline])
            if len(data) > MAX_MESSAGE_BYTES:
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
    except TimeoutError:
        raise WorkerBrokerError("WORKER_TIMEOUT") from None
    finally:
        connection.settimeout(None)


def _send_object(connection, value):
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError
        connection.sendall(encoded)
    except (OSError, TypeError, ValueError):
        raise WorkerBrokerError("WORKER_DISCONNECTED") from None


@dataclass(frozen=True)
class PeerCredential:
    pid: int
    uid: int
    gid: int


@dataclass(frozen=True, repr=False)
class _BootstrapRecord:
    worker_id: str
    expires_at: float


class BootstrapTokenRegistry:
    """Stores only token digests; raw bootstrap values never appear in repr."""

    def __init__(self, *, ttl_seconds=BOOTSTRAP_TTL_SECONDS, clock=time.monotonic,
                 token_factory=None):
        if type(ttl_seconds) not in (int, float) or not 30 <= ttl_seconds <= 60:
            raise ValueError("Invalid bootstrap TTL")
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._records = {}
        self._consumed = {}
        self._lock = threading.Lock()

    def _purge_consumed(self):
        now = self._clock()
        for digest, expires_at in tuple(self._consumed.items()):
            if now > expires_at:
                self._consumed.pop(digest, None)

    @staticmethod
    def _digest(token):
        if (not isinstance(token, str) or len(token) < 43 or len(token) > 256 or
                re.fullmatch(r"[A-Za-z0-9_-]+", token) is None):
            raise WorkerBrokerError("BOOTSTRAP_INVALID")
        return hashlib.sha256(token.encode("ascii")).digest()

    def create(self, worker_id):
        _request_id(worker_id)
        with self._lock:
            self._purge_consumed()
            for _ in range(4):
                token = self._token_factory()
                try:
                    digest = self._digest(token)
                except WorkerBrokerError:
                    continue
                if digest not in self._records and digest not in self._consumed:
                    self._records[digest] = _BootstrapRecord(
                        worker_id=worker_id, expires_at=self._clock() + self.ttl_seconds,
                    )
                    return token
        raise RuntimeError("Unable to create bootstrap token")

    def consume(self, token, worker_id):
        digest = self._digest(token)
        _request_id(worker_id)
        with self._lock:
            self._purge_consumed()
            if digest in self._consumed:
                raise WorkerBrokerError("BOOTSTRAP_REPLAYED")
            record = self._records.pop(digest, None)
            if record is None:
                raise WorkerBrokerError("BOOTSTRAP_INVALID")
            self._consumed[digest] = record.expires_at
            if self._clock() > record.expires_at:
                raise WorkerBrokerError("BOOTSTRAP_EXPIRED")
            if not secrets.compare_digest(record.worker_id, worker_id):
                raise WorkerBrokerError("BOOTSTRAP_WORKER_MISMATCH")
            return record.worker_id

    def revoke_worker(self, worker_id):
        with self._lock:
            for digest, record in tuple(self._records.items()):
                if record.worker_id == worker_id:
                    self._records.pop(digest, None)

    @property
    def active_count(self):
        with self._lock:
            return len(self._records)

    def __repr__(self):
        return f"BootstrapTokenRegistry(active_count={self.active_count})"


@dataclass
class WorkerSession:
    worker_id: str
    audit_id: str
    identity: IdentityProbeResult
    peer: PeerCredential
    connection: "WorkerConnection"
    connected_at: float
    bound: bool = False
    disconnect_callback: object | None = field(default=None, repr=False)

    @property
    def connected(self):
        return self.connection.connected

    def __repr__(self):
        return (f"WorkerSession(worker_id={self.worker_id!r}, username={self.identity.username!r}, "
                f"uid={self.identity.uid!r}, connected={self.connected!r}, bound={self.bound!r})")


class WorkerConnection:
    """Full-duplex allowlisted request channel with one dedicated reader."""

    def __init__(self, connection, *, on_disconnect, start=True):
        self._socket = connection
        self._on_disconnect = on_disconnect
        self._pending = {}
        self._provider_pending = {}
        self._provider_expired = []
        self._ai_streams = {}
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._connected = True
        self._last_error_code = None
        self._ai_ready = threading.Event()
        self._local_provider_status = {
            "provider": "deepseek", "configured": False,
            "backend": "Unavailable", "availability": "unavailable",
        }
        self._local_provider_status_event = threading.Event()
        self._audit_id = None
        self._username = None
        self._reader = threading.Thread(target=self._read_responses,
                                        name="easysbatch-worker-reader", daemon=True)
        self._started = False
        if start:
            self.start()

    def start(self):
        with self._state_lock:
            if not self._connected:
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            if self._started:
                return
            self._started = True
        self._reader.start()

    @property
    def connected(self):
        with self._state_lock:
            return self._connected

    @property
    def ai_ready(self):
        return self.connected and self._ai_ready.is_set()

    @property
    def last_error_code(self):
        with self._state_lock:
            return self._last_error_code

    @property
    def local_provider_status(self):
        with self._state_lock:
            return dict(self._local_provider_status)

    @property
    def local_provider_configured(self):
        with self._state_lock:
            return bool(self._local_provider_status.get("configured"))

    def wait_local_provider_status(self, timeout=2):
        return self._local_provider_status_event.wait(timeout)

    @property
    def error_type(self):
        return WorkerBrokerError

    def wait_ai_ready(self, timeout=5):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("Invalid AI readiness timeout")
        return self._ai_ready.wait(timeout) and self.connected

    def bind_audit(self, audit_id, username):
        with self._state_lock:
            if self._audit_id is not None:
                raise RuntimeError("Worker audit binding already set")
            self._audit_id = audit_id
            self._username = username

    def _send_frame(self, frame):
        with self._write_lock:
            write_frame(self._socket, frame)

    def request(self, operation, *, timeout=5):
        if operation not in WORKER_OPERATIONS or type(timeout) not in (int, float) or not 0.1 <= timeout <= 10:
            raise ValueError("Invalid worker operation")
        request_id = str(uuid4())
        result_queue = queue.Queue(maxsize=1)
        with self._state_lock:
            if not self._connected or not self._started:
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            self._pending[request_id] = result_queue
        try:
            payload = json.dumps({
                "request_id": request_id, "operation": operation,
            }, separators=(",", ":"), sort_keys=True).encode("utf-8")
            self._send_frame(Frame(
                FrameType.CONTROL_REQUEST, CONTROL_STREAM_ID, payload,
            ))
            try:
                response = result_queue.get(timeout=timeout)
            except queue.Empty:
                self.close()
                raise WorkerBrokerError("WORKER_TIMEOUT") from None
            if isinstance(response, WorkerBrokerError):
                raise response
            if response.get("ok") is not True:
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
            return response["result"]
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def request_ai_provider(self, request, *, timeout=120):
        """Send one bounded local-provider request over this exact Worker."""
        try:
            payload = encode_provider_request(request)
            request_id = request["request_id"]
        except (ValueError, TypeError, RecursionError):
            raise WorkerBrokerError("AI_PROVIDER_RESPONSE_INVALID") from None
        if type(timeout) not in (int, float) or not 0.1 <= timeout <= 120:
            raise ValueError("Invalid provider request timeout")
        try:
            stream_id = UUID(request_id)
        except (ValueError, TypeError):
            raise WorkerBrokerError("AI_PROVIDER_RESPONSE_INVALID") from None
        result_queue = queue.Queue(maxsize=1)
        with self._state_lock:
            if not self._connected or not self._started:
                raise WorkerBrokerError("AI_CLIENT_DISCONNECTED")
            if self._provider_pending:
                raise WorkerBrokerError("AI_PROVIDER_TIMEOUT")
            self._provider_pending[request_id] = result_queue
        try:
            self._send_frame(Frame(FrameType.AI_PROVIDER_REQUEST, stream_id, payload))
            try:
                response = result_queue.get(timeout=timeout)
            except queue.Empty:
                with self._state_lock:
                    self._provider_pending.pop(request_id, None)
                    self._provider_expired.append(request_id)
                    del self._provider_expired[:-16]
                raise WorkerBrokerError("AI_PROVIDER_TIMEOUT") from None
            if isinstance(response, WorkerBrokerError):
                raise response
            return response
        finally:
            with self._state_lock:
                self._provider_pending.pop(request_id, None)

    def _read_responses(self):
        error = WorkerBrokerError("WORKER_DISCONNECTED")
        try:
            while True:
                frame = read_frame(self._socket)
                if frame.frame_type == FrameType.CONTROL_RESPONSE:
                    response = _decode_object(frame.payload)
                    if (set(response) != {"request_id", "ok", "result"} or
                            response.get("ok") is not True or not isinstance(response.get("result"), dict)):
                        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                    request_id = _request_id(response["request_id"])
                    with self._state_lock:
                        target = self._pending.get(request_id)
                    if target is None:
                        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                    target.put_nowait(response)
                elif frame.frame_type == FrameType.LAUNCHER_HELLO:
                    if self._ai_ready.is_set() or frame.payload != HELLO_PAYLOAD:
                        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                    self._ai_ready.set()
                    self._send_frame(Frame(
                        FrameType.LAUNCHER_HELLO_ACK, CONTROL_STREAM_ID,
                    ))
                elif frame.frame_type == FrameType.AI_PROVIDER_STATUS:
                    status = decode_provider_status(frame.payload)
                    with self._state_lock:
                        self._local_provider_status = status
                    self._local_provider_status_event.set()
                elif frame.frame_type == FrameType.AI_PROVIDER_RESPONSE:
                    request_id = str(frame.stream_id)
                    response = decode_provider_response(frame.payload,
                                                        request_id=request_id)
                    with self._state_lock:
                        target = self._provider_pending.get(request_id)
                        expired = request_id in self._provider_expired
                        if expired:
                            self._provider_expired.remove(request_id)
                    if target is not None:
                        target.put_nowait(response)
                    elif not expired:
                        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                elif frame.frame_type in {
                        FrameType.AI_OPEN_OK, FrameType.AI_OPEN_ERROR,
                        FrameType.AI_DATA, FrameType.AI_EOF, FrameType.AI_CLOSE,
                        FrameType.AI_ERROR}:
                    with self._state_lock:
                        stream = self._ai_streams.get(frame.stream_id)
                    if stream is None:
                        raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                    stream.receive(frame)
                else:
                    raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
        except (EOFError, OSError, ValueError, queue.Full, ProtocolError,
                WorkerBrokerError) as exc:
            error = exc if isinstance(exc, WorkerBrokerError) else WorkerBrokerError("WORKER_PROTOCOL_INVALID")
        self._mark_disconnected(error)

    def open_ai_socket(self, *, timeout=CONNECT_TIMEOUT_SECONDS):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("Invalid AI stream timeout")
        with self._state_lock:
            if not self._connected or not self._started or not self._ai_ready.is_set():
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            if len(self._ai_streams) >= MAX_ACTIVE_AI_STREAMS_PER_SESSION:
                raise WorkerBrokerError("WORKER_TIMEOUT")
            stream_id = uuid4()
            stream = _CentralAIStream(self, stream_id)
            self._ai_streams[stream_id] = stream
        try:
            self._send_frame(Frame(FrameType.AI_OPEN, stream_id, OPEN_PAYLOAD))
            return stream.wait_open(timeout)
        except Exception:
            stream.close(notify=False)
            raise

    def _forget_stream(self, stream_id):
        with self._state_lock:
            self._ai_streams.pop(stream_id, None)

    def _mark_disconnected(self, error):
        with self._state_lock:
            if not self._connected:
                return
            self._connected = False
            self._last_error_code = getattr(error, "code", "WORKER_DISCONNECTED")
            pending = tuple(self._pending.values())
            streams = tuple(self._ai_streams.values())
            self._ai_streams.clear()
            pending_provider = tuple(self._provider_pending.values())
            self._provider_pending.clear()
            self._provider_expired.clear()
            self._ai_ready.clear()
        for target in pending:
            try:
                target.put_nowait(error)
            except queue.Full:
                pass
        for target in pending_provider:
            try:
                target.put_nowait(error)
            except queue.Full:
                pass
        for stream in streams:
            stream.close(notify=False)
        try:
            self._socket.close()
        finally:
            self._on_disconnect()

    def close(self):
        self._mark_disconnected(WorkerBrokerError("WORKER_DISCONNECTED"))


class _CentralAIStream:
    """One bounded socketpair bridge from the server to one Launcher socket."""

    def __init__(self, connection, stream_id):
        self.connection = connection
        self.stream_id = stream_id
        self._client_socket, self._relay_socket = socket.socketpair()
        self._relay_socket.settimeout(READ_TIMEOUT_SECONDS)
        self._opened = queue.Queue(maxsize=1)
        self._incoming = queue.Queue(maxsize=max(1, MAX_BUFFERED_BYTES // MAX_FRAME_SIZE))
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._started = False
        self._open_result_seen = False
        self._lock = threading.Lock()
        self._created_at = time.monotonic()
        self._bytes_in = 0
        self._bytes_out = 0
        self._local_eof = False
        self._remote_eof = False
        emit_ai_stream_event(
            "AI_EGRESS_STREAM_OPENED", audit_session_id=connection._audit_id,
            username=connection._username, stream_id=stream_id,
        )

    def wait_open(self, timeout):
        try:
            result = self._opened.get(timeout=timeout)
        except queue.Empty:
            raise WorkerBrokerError("WORKER_TIMEOUT") from None
        if result is not True:
            code = {
                1: "AI_EGRESS_UNAVAILABLE", 2: "AI_EGRESS_AUTH_FAILED",
                3: "AI_EGRESS_IDENTITY_MISMATCH", 4: "AI_EGRESS_TARGET_REJECTED",
                5: "AI_EGRESS_TLS_FAILED", 6: "AI_EGRESS_TIMEOUT",
            }.get(result, "AI_EGRESS_UNAVAILABLE")
            raise WorkerBrokerError(code)
        with self._lock:
            if self._closed.is_set():
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            self._started = True
        emit_ai_stream_event(
            "AI_EGRESS_STREAM_CONNECTED", audit_session_id=self.connection._audit_id,
            username=self.connection._username, stream_id=self.stream_id,
        )
        threading.Thread(target=self._write_incoming,
                         name="easysbatch-ai-stream-in", daemon=True).start()
        threading.Thread(target=self._read_outgoing,
                         name="easysbatch-ai-stream-out", daemon=True).start()
        return self._client_socket

    def receive(self, frame):
        if self._closed.is_set():
            raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
        if frame.frame_type == FrameType.AI_OPEN_OK:
            with self._lock:
                if self._open_result_seen:
                    raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                self._open_result_seen = True
            try:
                self._opened.put_nowait(True)
            except queue.Full:
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID") from None
        elif frame.frame_type == FrameType.AI_OPEN_ERROR:
            with self._lock:
                if self._open_result_seen:
                    raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
                self._open_result_seen = True
            emit_ai_stream_event(
                "AI_EGRESS_STREAM_FAILED", audit_session_id=self.connection._audit_id,
                username=self.connection._username, stream_id=self.stream_id,
                duration_ms=round((time.monotonic() - self._created_at) * 1000),
                error_category=("AI_EGRESS_TIMEOUT" if frame.payload[0] == 6 else
                                "AI_EGRESS_TARGET_REJECTED" if frame.payload[0] == 4 else
                                "AI_EGRESS_UNAVAILABLE"),
            )
            try:
                self._opened.put_nowait(frame.payload[0])
            except queue.Full:
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID") from None
        elif frame.frame_type == FrameType.AI_DATA:
            if not self._started:
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
            try:
                self._incoming.put_nowait(frame.payload)
                self._bytes_in += len(frame.payload)
            except queue.Full:
                self.close()
        elif frame.frame_type == FrameType.AI_EOF:
            if not self._started:
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
            try:
                self._incoming.put_nowait(None)
                self._remote_eof = True
                self._close_if_complete()
            except queue.Full:
                self.close()
        else:
            self.close(notify=False)

    def _write_incoming(self):
        try:
            self._relay_socket.settimeout(WRITE_TIMEOUT_SECONDS)
            while not self._closed.is_set():
                payload = self._incoming.get()
                if self._closed.is_set():
                    return
                if payload is None:
                    self._relay_socket.shutdown(socket.SHUT_WR)
                    return
                _, writable, _ = select.select(
                    (), (self._relay_socket,), (), WRITE_TIMEOUT_SECONDS,
                )
                if not writable:
                    raise TimeoutError
                self._relay_socket.sendall(payload)
        except (OSError, queue.Empty):
            self.close()

    def _read_outgoing(self):
        try:
            while not self._closed.is_set():
                payload = self._relay_socket.recv(MAX_FRAME_SIZE)
                if self._closed.is_set():
                    return
                if not payload:
                    self.connection._send_frame(Frame(FrameType.AI_EOF, self.stream_id))
                    self._local_eof = True
                    self._close_if_complete()
                    return
                self.connection._send_frame(Frame(FrameType.AI_DATA, self.stream_id, payload))
                self._bytes_out += len(payload)
        except (OSError, ProtocolError):
            self.close()

    def _close_if_complete(self):
        if self._local_eof and self._remote_eof:
            # Bidirectional EOF is the complete graceful-close handshake.
            # Avoid symmetric AI_CLOSE frames after both peers have removed
            # the stream; unknown stream IDs remain fail-closed elsewhere.
            self.close(notify=False)

    def close(self, *, notify=True):
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
        try:
            self._incoming.put_nowait(None)
        except queue.Full:
            pass
        emit_ai_stream_event(
            "AI_EGRESS_STREAM_CLOSED", audit_session_id=self.connection._audit_id,
            username=self.connection._username, stream_id=self.stream_id,
            bytes_in=self._bytes_in, bytes_out=self._bytes_out,
            duration_ms=round((time.monotonic() - self._created_at) * 1000),
        )
        if notify and self.connection.connected:
            try:
                self.connection._send_frame(Frame(FrameType.AI_CLOSE, self.stream_id))
            except (OSError, ProtocolError, WorkerBrokerError):
                pass
        for candidate in (self._client_socket, self._relay_socket):
            try:
                candidate.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            candidate.close()
        self.connection._forget_stream(self.stream_id)


class WorkerBoundContext:
    """SessionManager-compatible context backed by one kernel-verified worker."""

    def __init__(self, broker, session):
        self._broker = broker
        self._session = session
        self.identity = session.identity
        self._callback = None
        self._web_invalidated = False
        self._ai_egress = None
        self._lock = threading.Lock()

    @property
    def worker_id(self):
        return self._session.worker_id

    @property
    def ai_egress(self):
        with self._lock:
            return self._ai_egress

    @property
    def structured_ai_ready(self):
        return self.connected and self._session.connection.ai_ready

    def attach_ai_egress(self, *, remote_proxy_port, credential,
                         session_factory=AIEgressSession.create):
        """Bind one memory-only egress credential to this exact Worker session."""
        with self._lock:
            if not self.connected:
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            if self._ai_egress is not None:
                raise RuntimeError("AI egress already attached")
            egress = session_factory(
                worker_session_id=self._session.worker_id,
                username=self.identity.username,
                remote_proxy_port=remote_proxy_port,
                credential=credential,
            )
            self._ai_egress = egress
            return egress

    def attach_structured_ai_egress(
            self, *, session_factory=StructuredAIEgressSession.create):
        """Bind framed AI transport to this exact kernel-verified Worker."""
        with self._lock:
            if not self.connected:
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            if not self._session.connection.ai_ready:
                raise WorkerBrokerError("WORKER_TIMEOUT")
            if self._ai_egress is not None:
                raise RuntimeError("AI egress already attached")
            egress = session_factory(
                worker_session_id=self._session.worker_id,
                username=self.identity.username,
                connection=self._session.connection,
            )
            self._ai_egress = egress
            return egress

    def attach_local_ai_provider(
            self, *, session_factory=LocalUserProviderSession.create,
            audit_session_id=None):
        """Bind the current Launcher's local credential capability to this Worker."""
        with self._lock:
            if not self.connected:
                raise WorkerBrokerError("WORKER_DISCONNECTED")
            waiter = getattr(self._session.connection, "wait_local_provider_status", None)
            if waiter is not None:
                waiter(1)
            if self._ai_egress is not None:
                raise RuntimeError("AI provider already attached")
            provider = session_factory(
                worker_session_id=self._session.worker_id,
                username=self.identity.username,
                connection=self._session.connection,
                audit_session_id=audit_session_id,
            )
            self._ai_egress = provider
            return provider

    @property
    def connected(self):
        return self._session.connected

    def set_disconnect_callback(self, callback):
        with self._lock:
            if self._callback is not None:
                raise RuntimeError("Disconnect callback already set")
            self._callback = callback
            connected = self.connected
        if not connected:
            callback()

    def _notify_disconnect(self):
        with self._lock:
            callback = self._callback
            egress = self._ai_egress
        if egress is not None:
            egress.invalidate()
        if callback is not None:
            callback()

    def verify_identity(self):
        result = self._session.connection.request("identity")
        try:
            identity = IdentityProbeResult(
                username=result["username"], uid=result["uid"], gid=result["gid"],
                groups=(), home=result["home"], pwd=result["home"], hostname=result["hostname"],
            )
        except (KeyError, TypeError):
            self.close()
            raise WorkerBrokerError("WORKER_PROTOCOL_INVALID") from None
        if (set(result) != {"username", "uid", "gid", "home", "hostname"} or
                identity != self.identity):
            self.close()
            raise WorkerBrokerError("WORKER_IDENTITY_MISMATCH")
        return identity

    def close(self):
        with self._lock:
            egress = self._ai_egress
            if not self._web_invalidated:
                self._web_invalidated = True
                emit = True
            else:
                emit = False
        if egress is not None:
            egress.invalidate()
        if emit:
            emit_worker_event(
                "WEB_SESSION_INVALIDATED", audit_id=self._session.audit_id,
                username=self.identity.username, uid=self.identity.uid, result="CLOSED",
            )
        self._broker.close_worker(self._session.worker_id)

    def __repr__(self):
        egress = self.ai_egress
        return (f"WorkerBoundContext(username={self.identity.username!r}, "
                f"uid={self.identity.uid!r}, connected={self.connected!r}, "
                f"ai_egress_state={egress.state.value if egress else None!r})")


class WorkerBroker:
    """Own the UDS listener, kernel identity assertions and worker lifecycle."""

    def __init__(self, socket_path, *, token_registry=None, account_lookup=pwd.getpwuid,
                 clock=time.monotonic, handshake_timeout=5, worker_source=None):
        self.socket_path = Path(socket_path)
        self.tokens = token_registry or BootstrapTokenRegistry(clock=clock)
        self._account_lookup = account_lookup
        self._clock = clock
        self._handshake_timeout = handshake_timeout
        self._listener = None
        self._listener_identity = None
        self._accept_thread = None
        self._workers = {}
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        source = (Path(user_worker.__file__).read_bytes()
                  if worker_source is None else worker_source)
        if (user_worker.PROTOCOL_VERSION != PROTOCOL_VERSION or
                not isinstance(source, bytes) or not source or len(source) > 64 * 1024):
            raise ValueError("Invalid maintained Worker source")
        self._worker_source = source
        self.worker_entrypoint = self.socket_path.parent / WORKER_FILENAME
        self.legacy_worker_entrypoint = self.socket_path.parent / LEGACY_WORKER_FILENAME
        self._validate_configured_path()

    def _validate_configured_path(self):
        value = str(self.socket_path)
        if (not self.socket_path.is_absolute() or self.socket_path.name != "broker.sock" or
                re.fullmatch(r"/tmp/easysbatch-[0-9]+/broker\.sock", value) is None):
            raise ValueError("Invalid SSH-first broker socket path")

    def _prepare_directory(self):
        directory = self.socket_path.parent
        created = False
        try:
            directory.mkdir(mode=0o711)
            created = True
        except FileExistsError:
            pass
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise WorkerBrokerError("BROKER_PATH_INVALID")
        # mkdir is filtered by the service umask.  Only normalize a directory
        # created by this call; an unexpected pre-existing mode stays fail closed.
        if created and stat.S_IMODE(metadata.st_mode) != 0o711:
            try:
                os.chmod(directory, 0o711, follow_symlinks=False)
            except OSError:
                raise WorkerBrokerError("BROKER_PATH_INVALID") from None
            metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or
                stat.S_IMODE(metadata.st_mode) != 0o711):
            raise WorkerBrokerError("BROKER_PATH_INVALID")

    def _prepare_socket_path(self):
        try:
            metadata = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid()):
            raise WorkerBrokerError("BROKER_PATH_INVALID")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
        except ConnectionRefusedError:
            current = self.socket_path.lstat()
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise WorkerBrokerError("BROKER_PATH_INVALID")
            self.socket_path.unlink()
        except OSError:
            raise WorkerBrokerError("BROKER_PATH_INVALID") from None
        else:
            raise WorkerBrokerError("BROKER_UNAVAILABLE")
        finally:
            probe.close()

    def _publish_source(self, path, source):
        """Atomically publish one maintained, read-only remote entrypoint."""
        try:
            current = path.lstat()
        except FileNotFoundError:
            current = None
        if current is not None:
            if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.geteuid() or
                    stat.S_IMODE(current.st_mode) != 0o555):
                raise WorkerBrokerError("BROKER_PATH_INVALID")
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "rb") as stream:
                    if stream.read(64 * 1024 + 1) == source:
                        return
            except OSError:
                raise WorkerBrokerError("BROKER_PATH_INVALID") from None

        temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        created_identity = None
        try:
            descriptor = os.open(temporary, flags, 0o500)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o555)
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or
                        stat.S_IMODE(metadata.st_mode) != 0o555):
                    raise WorkerBrokerError("BROKER_PATH_INVALID")
                created_identity = (metadata.st_dev, metadata.st_ino)
            os.replace(temporary, path)
            published = path.lstat()
            if (not stat.S_ISREG(published.st_mode) or published.st_uid != os.geteuid() or
                    stat.S_IMODE(published.st_mode) != 0o555 or
                    path.read_bytes() != source):
                raise WorkerBrokerError("BROKER_PATH_INVALID")
        except WorkerBrokerError:
            raise
        except OSError:
            raise WorkerBrokerError("BROKER_PATH_INVALID") from None
        finally:
            try:
                leftover = temporary.lstat()
                if (created_identity is not None and
                        (leftover.st_dev, leftover.st_ino) == created_identity and
                        stat.S_ISREG(leftover.st_mode) and leftover.st_uid == os.geteuid()):
                    temporary.unlink()
            except FileNotFoundError:
                pass

    def _publish_worker(self):
        """Publish protocol v2 and a v1 fail-fast upgrade guard."""
        self._publish_source(self.worker_entrypoint, self._worker_source)
        self._publish_source(self.legacy_worker_entrypoint, LEGACY_WORKER_GUARD_SOURCE)

    def start(self):
        if not hasattr(socket, "SO_PEERCRED"):
            raise WorkerBrokerError("BROKER_PEER_UNSUPPORTED")
        with self._lock:
            if self._listener is not None:
                return
            self._prepare_directory()
            self._publish_worker()
            self._prepare_socket_path()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            created_identity = None
            try:
                listener.bind(str(self.socket_path))
                created = self.socket_path.lstat()
                if not stat.S_ISSOCK(created.st_mode) or created.st_uid != os.geteuid():
                    raise WorkerBrokerError("BROKER_PATH_INVALID")
                created_identity = (created.st_dev, created.st_ino)
                os.chmod(self.socket_path, 0o666, follow_symlinks=False)
                listener.listen(16)
                listener.settimeout(0.5)
                metadata = self.socket_path.lstat()
                if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.geteuid() or
                        stat.S_IMODE(metadata.st_mode) != 0o666):
                    raise WorkerBrokerError("BROKER_PATH_INVALID")
            except Exception:
                listener.close()
                try:
                    current = self.socket_path.lstat()
                    if (created_identity is not None and
                            (current.st_dev, current.st_ino) == created_identity and
                            stat.S_ISSOCK(current.st_mode) and current.st_uid == os.geteuid()):
                        self.socket_path.unlink()
                except (FileNotFoundError, OSError):
                    pass
                raise
            self._listener = listener
            self._listener_identity = (metadata.st_dev, metadata.st_ino)
            self._stopping.clear()
            self._accept_thread = threading.Thread(target=self._accept_loop,
                                                   name="easysbatch-worker-broker", daemon=True)
            self._accept_thread.start()

    def _accept_loop(self):
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._register, args=(connection,),
                             name="easysbatch-worker-register", daemon=True).start()

    @staticmethod
    def _peer_credential(connection):
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                        struct.calcsize("3i"))
            pid, uid, gid = struct.unpack("3i", raw)
        except (AttributeError, OSError, struct.error):
            raise WorkerBrokerError("BROKER_PEER_UNSUPPORTED") from None
        if pid <= 0 or uid <= 0 or gid < 0:
            raise WorkerBrokerError("WORKER_IDENTITY_MISMATCH")
        return PeerCredential(pid, uid, gid)

    def _verified_identity(self, connection, claims):
        if (not isinstance(claims, dict) or
                set(claims) != {"username", "uid", "gid", "home", "hostname"} or
                not isinstance(claims.get("username"), str) or
                type(claims.get("uid")) is not int or type(claims.get("gid")) is not int or
                not isinstance(claims.get("home"), str) or
                not isinstance(claims.get("hostname"), str)):
            raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
        peer = self._peer_credential(connection)
        try:
            account = self._account_lookup(peer.uid)
        except (KeyError, OSError):
            raise WorkerBrokerError("WORKER_UNKNOWN_UID") from None
        expected = {
            "username": account.pw_name, "uid": peer.uid, "gid": peer.gid,
            "home": account.pw_dir, "hostname": socket.gethostname(),
        }
        if (account.pw_name == "root" or account.pw_gid != peer.gid or claims != expected):
            raise WorkerBrokerError("WORKER_IDENTITY_MISMATCH")
        return peer, IdentityProbeResult(
            username=account.pw_name, uid=peer.uid, gid=peer.gid, groups=(),
            home=account.pw_dir, pwd=account.pw_dir, hostname=socket.gethostname(),
        )

    def _register(self, connection):
        request_id = None
        worker_id = None
        worker = None
        registered = False
        try:
            request = _decode_object(_recv_line(connection, timeout=self._handshake_timeout))
            if set(request) != {"version", "request_id", "operation", "claims"}:
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
            request_id = _request_id(request["request_id"])
            if request.get("version") != PROTOCOL_VERSION:
                raise WorkerBrokerError("WORKER_PROTOCOL_UNSUPPORTED")
            if request.get("operation") != "hello":
                raise WorkerBrokerError("WORKER_PROTOCOL_INVALID")
            peer, identity = self._verified_identity(connection, request["claims"])
            worker_id, audit_id = str(uuid4()), str(uuid4())
            with self._lock:
                worker = WorkerSession(
                    worker_id=worker_id, audit_id=audit_id, identity=identity, peer=peer,
                    connection=None, connected_at=self._clock(),
                )
                worker.connection = WorkerConnection(
                    connection, on_disconnect=lambda: self._worker_disconnected(worker_id),
                    start=False,
                )
                binder = getattr(worker.connection, "bind_audit", None)
                if binder is not None:
                    binder(audit_id, identity.username)
                self._workers[worker_id] = worker
                registered = True
            token = self.tokens.create(worker_id)
            _send_object(connection, {
                "request_id": request_id, "ok": True,
                "result": {
                    "worker_id": worker_id, "bootstrap_token": token,
                    "username": identity.username, "uid": identity.uid,
                    "ttl_seconds": int(self.tokens.ttl_seconds),
                },
            })
            worker.connection.start()
            emit_worker_event("WORKER_CONNECTED", audit_id=audit_id,
                              username=identity.username, uid=identity.uid, result="SUCCESS")
            emit_worker_event("SSH_BOOTSTRAP_CREATED", audit_id=audit_id,
                              username=identity.username, uid=identity.uid, result="SUCCESS")
        except WorkerBrokerError as exc:
            if request_id is not None:
                try:
                    _send_object(connection, {
                        "request_id": request_id, "ok": False, "error_code": exc.code,
                    })
                except WorkerBrokerError:
                    pass
            if registered and worker is not None:
                worker.connection.close()
            else:
                connection.close()

    def _worker_disconnected(self, worker_id):
        with self._lock:
            worker = self._workers.pop(worker_id, None)
            if worker is None:
                return
        self.tokens.revoke_worker(worker_id)
        callback = worker.disconnect_callback
        emit_worker_event("WORKER_DISCONNECTED", audit_id=worker.audit_id,
                          username=worker.identity.username, uid=worker.identity.uid,
                          result="CLOSED")
        if callback is not None:
            callback()

    def consume_bootstrap(self, worker_id, token):
        resolved = self.tokens.consume(token, worker_id)
        with self._lock:
            worker = self._workers.get(resolved)
            if worker is None or not worker.connected:
                raise WorkerBrokerError("BOOTSTRAP_WORKER_UNAVAILABLE")
            if worker.bound:
                raise WorkerBrokerError("BOOTSTRAP_REPLAYED")
            worker.bound = True
            context = WorkerBoundContext(self, worker)
            worker.disconnect_callback = context._notify_disconnect
        emit_worker_event("WEB_SESSION_BOUND", audit_id=worker.audit_id,
                          username=worker.identity.username, uid=worker.identity.uid,
                          result="SUCCESS")
        return context

    def close_worker(self, worker_id):
        with self._lock:
            worker = self._workers.get(worker_id)
        if worker is None:
            return
        if worker.connected:
            try:
                worker.connection.request("shutdown", timeout=2)
            except (WorkerBrokerError, ValueError):
                pass
        worker.connection.close()

    def stop(self):
        self._stopping.set()
        with self._lock:
            listener, self._listener = self._listener, None
            workers = tuple(self._workers)
        if listener is not None:
            listener.close()
        for worker_id in workers:
            self.close_worker(worker_id)
        thread = self._accept_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        try:
            metadata = self.socket_path.lstat()
            if ((metadata.st_dev, metadata.st_ino) == self._listener_identity and
                    stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid()):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._listener_identity = None

    @property
    def active_count(self):
        with self._lock:
            return len(self._workers)

    def __repr__(self):
        return f"WorkerBroker(socket_path={str(self.socket_path)!r}, active_count={self.active_count})"
