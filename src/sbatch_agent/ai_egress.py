"""Central per-session DeepSeek transport over a restricted Launcher egress.

The Central process creates the provider request and TLS session. The Launcher
sees only the fixed CONNECT destination and opaque TLS records. Credentials
are memory-only and deliberately absent from repr, URLs, argv and logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import http.client
import json
import logging
import math
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .request_trace import current_trace, read_bounded, remaining_timeout
from .restricted_egress import (
    AIEgressError, AIEgressErrorCode, CONNECT_TIMEOUT_SECONDS,
    DEEPSEEK_EGRESS_HOST, DEEPSEEK_EGRESS_PORT, REMOTE_PROXY_HOST,
    RestrictedConnectEgressAgent, _parse_connect_request, _public_addresses,
    _validate_credential, new_egress_credential,
)


AI_EGRESS_AUDIT = logging.getLogger("sbatch_agent.ai_egress_audit")
AI_EGRESS_AUDIT.setLevel(logging.INFO)


def configure_ai_egress_audit():
    """Make the safe usage schema visible under normal Uvicorn startup."""
    if not AI_EGRESS_AUDIT.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        AI_EGRESS_AUDIT.addHandler(handler)
    AI_EGRESS_AUDIT.setLevel(logging.INFO)


class AIEgressState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


def _validate_uuid(value: str) -> str:
    if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value:
        raise ValueError("Invalid AI egress identifier")
    return value


def _validate_username(value: str) -> str:
    if (not isinstance(value, str) or value == "root" or
            re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value) is None):
        raise ValueError("Invalid AI egress username")
    return value


def _deepseek_endpoint(endpoint: str):
    if not isinstance(endpoint, str):
        raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED)
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port or DEEPSEEK_EGRESS_PORT
    except (TypeError, ValueError):
        raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED) from None
    if (parsed.scheme != "https" or parsed.hostname != DEEPSEEK_EGRESS_HOST or
            port != DEEPSEEK_EGRESS_PORT or parsed.username is not None or
            parsed.password is not None or parsed.query or parsed.fragment or
            not parsed.path.startswith("/") or not endpoint.isprintable()):
        raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED)
    return parsed


def _read_connect_response(connection: socket.socket, *, timeout: float) -> int:
    connection.settimeout(timeout)
    data = bytearray()
    try:
        while b"\r\n\r\n" not in data:
            chunk = connection.recv(min(1024, 8193 - len(data)))
            if not chunk:
                raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
            data.extend(chunk)
            if len(data) > 8192:
                raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
        header, marker, remainder = bytes(data).partition(b"\r\n\r\n")
        if not marker or remainder:
            raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
        lines = header.split(b"\r\n")
        match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3}) [\x20-\x7e]{1,80}", lines[0])
        if match is None or any(b":" not in line for line in lines[1:]):
            raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
        return int(match.group(1))
    finally:
        data.clear()


@dataclass(repr=False)
class AIEgressSession:
    egress_id: str
    worker_session_id: str
    username: str
    remote_proxy_host: str
    remote_proxy_port: int
    credential: str = field(repr=False)
    created_at: datetime
    last_health_at: datetime | None = None
    state: AIEgressState = AIEgressState.PENDING
    last_error: AIEgressErrorCode | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _socket_create_connection: object = field(default=socket.create_connection, repr=False)
    _ssl_context_factory: object = field(default=ssl.create_default_context, repr=False)

    def __post_init__(self):
        _validate_uuid(self.egress_id)
        _validate_uuid(self.worker_session_id)
        _validate_username(self.username)
        if (self.remote_proxy_host != REMOTE_PROXY_HOST or
                type(self.remote_proxy_port) is not int or
                not 1024 <= self.remote_proxy_port <= 65535 or
                not isinstance(self.created_at, datetime) or
                self.created_at.utcoffset() is None):
            raise ValueError("Invalid AI egress session")
        _validate_credential(self.credential)

    @classmethod
    def create(cls, *, worker_session_id: str, username: str,
               remote_proxy_port: int, credential: str, **testing):
        return cls(
            egress_id=str(uuid4()), worker_session_id=worker_session_id,
            username=username, remote_proxy_host=REMOTE_PROXY_HOST,
            remote_proxy_port=remote_proxy_port, credential=credential,
            created_at=datetime.now(timezone.utc), **testing,
        )

    def __repr__(self):
        return (f"AIEgressSession(egress_id={self.egress_id!r}, "
                f"worker_session_id={self.worker_session_id!r}, "
                f"username={self.username!r}, remote_proxy_host={self.remote_proxy_host!r}, "
                f"remote_proxy_port={self.remote_proxy_port!r}, state={self.state.value!r})")

    @property
    def available(self):
        with self._lock:
            return self.state == AIEgressState.READY

    def assert_binding(self, *, worker_session_id: str, username: str):
        with self._lock:
            if (self.state == AIEgressState.INVALID or
                    not secrets.compare_digest(self.worker_session_id, worker_session_id) or
                    not secrets.compare_digest(self.username, username)):
                raise AIEgressError(AIEgressErrorCode.IDENTITY_MISMATCH)

    def _credential(self):
        with self._lock:
            if self.state == AIEgressState.INVALID or not self.credential:
                raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
            return self.credential

    def _set_result(self, error: AIEgressError | None):
        with self._lock:
            if self.state == AIEgressState.INVALID:
                return
            self.last_health_at = datetime.now(timezone.utc)
            self.state = AIEgressState.READY if error is None else AIEgressState.UNAVAILABLE
            self.last_error = None if error is None else error.code

    def open_tls_tunnel(self, *, timeout: float):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise ValueError("Invalid AI egress timeout")
        credential = self._credential()
        connection = None
        try:
            connection = self._socket_create_connection(
                (self.remote_proxy_host, self.remote_proxy_port), timeout=timeout,
            )
            connection.settimeout(timeout)
            request = (
                f"CONNECT {DEEPSEEK_EGRESS_HOST}:{DEEPSEEK_EGRESS_PORT} HTTP/1.1\r\n"
                f"Host: {DEEPSEEK_EGRESS_HOST}:{DEEPSEEK_EGRESS_PORT}\r\n"
                f"Proxy-Authorization: Bearer {credential}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            connection.sendall(request)
            request = b""
            credential = None
            status = _read_connect_response(connection, timeout=timeout)
            if status == 407:
                raise AIEgressError(AIEgressErrorCode.AUTH_FAILED)
            if status == 403:
                raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED)
            if status == 504:
                raise AIEgressError(AIEgressErrorCode.TIMEOUT)
            if status != 200:
                raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
            context = self._ssl_context_factory()
            if (not getattr(context, "check_hostname", False) or
                    getattr(context, "verify_mode", ssl.CERT_NONE) != ssl.CERT_REQUIRED):
                raise AIEgressError(AIEgressErrorCode.TLS_FAILED)
            secured = context.wrap_socket(
                connection, server_hostname=DEEPSEEK_EGRESS_HOST,
            )
            connection = None
            return secured
        except AIEgressError:
            raise
        except (ssl.SSLCertVerificationError, ssl.CertificateError, ssl.SSLError):
            raise AIEgressError(AIEgressErrorCode.TLS_FAILED) from None
        except TimeoutError:
            raise AIEgressError(AIEgressErrorCode.TIMEOUT) from None
        except OSError:
            raise AIEgressError(AIEgressErrorCode.UNAVAILABLE) from None
        finally:
            credential = None
            if connection is not None:
                connection.close()

    def health_check(self, *, timeout=CONNECT_TIMEOUT_SECONDS):
        secured = None
        try:
            secured = self.open_tls_tunnel(timeout=timeout)
        except AIEgressError as exc:
            self._set_result(exc)
            raise
        else:
            self._set_result(None)
            return True
        finally:
            if secured is not None:
                secured.close()

    def mark_unavailable(self, code=AIEgressErrorCode.UNAVAILABLE):
        self._set_result(AIEgressError(code))

    def invalidate(self):
        with self._lock:
            self.state = AIEgressState.INVALID
            self.last_error = AIEgressErrorCode.UNAVAILABLE
            self.credential = ""


def _session_transport_interface(session):
    if (session is None or not isinstance(getattr(session, "egress_id", None), str) or
            not isinstance(getattr(session, "username", None), str) or
            not callable(getattr(session, "open_tls_tunnel", None)) or
            not callable(getattr(session, "assert_binding", None)) or
            not callable(getattr(session, "mark_unavailable", None))):
        raise ValueError("AI egress session required")
    return session


class AIEgressHTTPTransport:
    """Server-side HTTPS transport pinned to one AIEgressSession."""

    def __init__(self, session: AIEgressSession):
        self._session = _session_transport_interface(session)

    def __repr__(self):
        return (f"AIEgressHTTPTransport(egress_id={self._session.egress_id!r}, "
                f"username={self._session.username!r})")

    @property
    def egress_id(self):
        return self._session.egress_id

    @property
    def available(self):
        return self._session.available

    def request(self, *, endpoint, body, headers, timeout, max_response_bytes):
        parsed = _deepseek_endpoint(endpoint)
        secured = connection = None
        try:
            secured = self._session.open_tls_tunnel(timeout=remaining_timeout(timeout))
            connection = http.client.HTTPConnection(
                DEEPSEEK_EGRESS_HOST, DEEPSEEK_EGRESS_PORT,
                timeout=remaining_timeout(timeout),
            )
            connection.sock = secured
            secured = None
            safe_headers = dict(headers)
            if any(key.lower() in {"proxy-authorization", "proxy-connection"}
                   for key in safe_headers):
                raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED)
            safe_headers["Host"] = DEEPSEEK_EGRESS_HOST
            connection.request("POST", parsed.path, body=body, headers=safe_headers)
            response = connection.getresponse()
            raw = read_bounded(
                response, max_response_bytes, timeout, connection=connection,
            )
            return response.status, raw
        except AIEgressError as exc:
            self._session.mark_unavailable(exc.code)
            raise
        except (ssl.SSLCertVerificationError, ssl.CertificateError, ssl.SSLError):
            self._session.mark_unavailable(AIEgressErrorCode.TLS_FAILED)
            raise AIEgressError(AIEgressErrorCode.TLS_FAILED) from None
        except TimeoutError:
            self._session.mark_unavailable(AIEgressErrorCode.TIMEOUT)
            raise AIEgressError(AIEgressErrorCode.TIMEOUT) from None
        except OSError:
            self._session.mark_unavailable(AIEgressErrorCode.UNAVAILABLE)
            raise AIEgressError(AIEgressErrorCode.UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()
            elif secured is not None:
                secured.close()


def emit_ai_usage(*, audit_session_id, username, request_id, model,
                  duration_ms, success, error_category=None,
                  input_tokens=None, output_tokens=None):
    """Write an allowlisted attribution record without prompt/response content."""
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": ("AI_PROVIDER_REQUEST_SUCCESS" if success else
                  "AI_PROVIDER_REQUEST_FAILED"),
        "audit_session_id": _validate_uuid(audit_session_id),
        "username": _validate_username(username),
        "request_id": request_id if isinstance(request_id, str) and
                      re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id) else None,
        "model": model if isinstance(model, str) and
                 re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", model) else "configured",
        "duration_ms": max(0, round(duration_ms)),
        "success": bool(success),
        "error_category": error_category if error_category in {
            *(code.value for code in AIEgressErrorCode), "AI_PROVIDER_UNAVAILABLE",
            "AI_PROVIDER_TIMEOUT", "AI_AUTHENTICATION_FAILED", "AI_RATE_LIMITED",
            "AI_TLS_ERROR", "AI_OUTPUT_INVALID",
        } else None,
        "input_tokens": input_tokens if type(input_tokens) is int and input_tokens >= 0 else None,
        "output_tokens": output_tokens if type(output_tokens) is int and output_tokens >= 0 else None,
    }
    AI_EGRESS_AUDIT.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


class AuditedAIEgressModelClient:
    """Safe usage attribution around a session-bound model client."""

    def __init__(self, client, *, session: AIEgressSession,
                 audit_session_id: str, username: str):
        _session_transport_interface(session)
        session.assert_binding(
            worker_session_id=session.worker_session_id, username=username,
        )
        self.client = client
        self.session = session
        self.audit_session_id = _validate_uuid(audit_session_id)
        self.username = _validate_username(username)
        self.provider, self.model = client.provider, client.model

    def availability(self):
        return self.client.availability()

    def generate_structured(self, *, context, schema):
        started = time.monotonic()
        result = None
        failure = None
        try:
            result = self.client.generate_structured(context=context, schema=schema)
            return result
        except Exception as exc:
            failure = exc
            raise
        finally:
            trace = current_trace()
            request_id = (getattr(result, "request_id", None) or
                          (trace.request_id if trace is not None else str(uuid4())))
            raw_category = getattr(getattr(failure, "code", None), "value", None)
            category = {
                "ai_egress_unavailable": "AI_EGRESS_UNAVAILABLE",
                "ai_egress_authentication_failure": "AI_EGRESS_AUTH_FAILED",
                "ai_egress_identity_mismatch": "AI_EGRESS_IDENTITY_MISMATCH",
                "ai_egress_target_rejected": "AI_EGRESS_TARGET_REJECTED",
                "ai_egress_tls_failure": "AI_EGRESS_TLS_FAILED",
                "ai_egress_timeout": "AI_EGRESS_TIMEOUT",
                "provider_unavailable": "AI_PROVIDER_UNAVAILABLE",
                "timeout": "AI_PROVIDER_TIMEOUT",
                "authentication_failure": "AI_AUTHENTICATION_FAILED",
                "rate_limit_or_quota": "AI_RATE_LIMITED",
                "tls_certificate_error": "AI_TLS_ERROR",
            }.get(raw_category, raw_category)
            if category is None and failure is not None:
                category = "AI_OUTPUT_INVALID"
            emit_ai_usage(
                audit_session_id=self.audit_session_id, username=self.username,
                request_id=request_id, model=self.model,
                duration_ms=(time.monotonic() - started) * 1000,
                success=failure is None, error_category=category,
                input_tokens=getattr(result, "input_tokens", None),
                output_tokens=getattr(result, "output_tokens", None),
            )
