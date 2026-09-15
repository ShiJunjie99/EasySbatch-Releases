"""Server-side TLS endpoint over one session-bound structured SSH stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import re
import secrets
import ssl
import threading
from uuid import UUID, uuid4

from .ai_egress import AIEgressState
from .restricted_egress import (
    AIEgressError, AIEgressErrorCode, DEEPSEEK_EGRESS_HOST,
)


AI_STREAM_AUDIT = logging.getLogger("sbatch_agent.ai_stream_audit")
AI_STREAM_EVENTS = frozenset({
    "AI_EGRESS_STREAM_OPENED", "AI_EGRESS_STREAM_CONNECTED",
    "AI_EGRESS_STREAM_FAILED", "AI_EGRESS_STREAM_CLOSED",
})


def emit_ai_stream_event(event, *, audit_session_id, username, stream_id,
                         bytes_in=0, bytes_out=0, duration_ms=0,
                         error_category=None):
    if (event not in AI_STREAM_EVENTS or
            not all(type(value) is int and value >= 0
                    for value in (bytes_in, bytes_out, duration_ms))):
        raise ValueError("Invalid AI stream audit event")
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "audit_session_id": audit_session_id if isinstance(audit_session_id, str) else None,
        "username": username if isinstance(username, str) else None,
        "stream_id": str(stream_id) if isinstance(stream_id, UUID) else None,
        "bytes_in": bytes_in,
        "bytes_out": bytes_out,
        "duration_ms": duration_ms,
        "error_category": error_category if error_category in {
            "AI_EGRESS_UNAVAILABLE", "AI_EGRESS_TARGET_REJECTED",
            "AI_EGRESS_TIMEOUT", "AI_STREAM_PROTOCOL_INVALID",
        } else None,
    }
    AI_STREAM_AUDIT.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))


@dataclass(repr=False)
class StructuredAIEgressSession:
    """One WebSession/WorkerSession binding; contains no proxy credential."""

    egress_id: str
    worker_session_id: str
    username: str
    connection: object = field(repr=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_health_at: datetime | None = None
    state: AIEgressState = AIEgressState.PENDING
    last_error: AIEgressErrorCode | None = None
    _ssl_context_factory: object = field(default=ssl.create_default_context, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self):
        if (not isinstance(self.egress_id, str) or str(UUID(self.egress_id)) != self.egress_id or
                not isinstance(self.worker_session_id, str) or
                str(UUID(self.worker_session_id)) != self.worker_session_id or
                not isinstance(self.username, str) or self.username == "root" or
                re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.username) is None or
                not isinstance(self.created_at, datetime) or
                self.created_at.utcoffset() is None or
                not callable(getattr(self.connection, "open_ai_socket", None))):
            raise ValueError("Invalid structured AI egress session")

    @classmethod
    def create(cls, *, worker_session_id, username, connection, **testing):
        return cls(
            egress_id=str(uuid4()), worker_session_id=worker_session_id,
            username=username, connection=connection, **testing,
        )

    def __repr__(self):
        return (f"StructuredAIEgressSession(egress_id={self.egress_id!r}, "
                f"worker_session_id={self.worker_session_id!r}, "
                f"username={self.username!r}, state={self.state.value!r})")

    @property
    def available(self):
        with self._lock:
            return self.state == AIEgressState.READY

    def assert_binding(self, *, worker_session_id, username):
        with self._lock:
            if (self.state == AIEgressState.INVALID or
                    not secrets.compare_digest(self.worker_session_id, worker_session_id) or
                    not secrets.compare_digest(self.username, username)):
                raise AIEgressError(AIEgressErrorCode.IDENTITY_MISMATCH)

    def _set_result(self, error=None):
        with self._lock:
            if self.state == AIEgressState.INVALID:
                return
            self.last_health_at = datetime.now(timezone.utc)
            self.state = AIEgressState.READY if error is None else AIEgressState.UNAVAILABLE
            self.last_error = None if error is None else error.code

    def open_tls_tunnel(self, *, timeout):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise ValueError("Invalid AI egress timeout")
        with self._lock:
            if self.state == AIEgressState.INVALID:
                raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
        raw_socket = None
        try:
            raw_socket = self.connection.open_ai_socket(timeout=min(timeout, 30))
            raw_socket.settimeout(timeout)
            context = self._ssl_context_factory()
            if (not getattr(context, "check_hostname", False) or
                    getattr(context, "verify_mode", ssl.CERT_NONE) != ssl.CERT_REQUIRED):
                raise AIEgressError(AIEgressErrorCode.TLS_FAILED)
            secured = context.wrap_socket(
                raw_socket, server_hostname=DEEPSEEK_EGRESS_HOST,
            )
            raw_socket = None
            return secured
        except AIEgressError:
            raise
        except TimeoutError:
            raise AIEgressError(AIEgressErrorCode.TIMEOUT) from None
        except (ssl.SSLCertVerificationError, ssl.CertificateError, ssl.SSLError):
            raise AIEgressError(AIEgressErrorCode.TLS_FAILED) from None
        except Exception as exc:
            code = getattr(exc, "code", "")
            category = {
                "WORKER_TIMEOUT": AIEgressErrorCode.TIMEOUT,
                **{value.value: value for value in AIEgressErrorCode},
            }.get(code, AIEgressErrorCode.UNAVAILABLE)
            raise AIEgressError(category) from None
        finally:
            if raw_socket is not None:
                raw_socket.close()

    def health_check(self, *, timeout=8):
        secured = None
        try:
            secured = self.open_tls_tunnel(timeout=timeout)
        except AIEgressError as exc:
            self._set_result(exc)
            raise
        self._set_result()
        secured.close()
        return True

    def mark_unavailable(self, code=AIEgressErrorCode.UNAVAILABLE):
        self._set_result(AIEgressError(code))

    def invalidate(self):
        with self._lock:
            self.state = AIEgressState.INVALID
            self.last_error = AIEgressErrorCode.UNAVAILABLE
