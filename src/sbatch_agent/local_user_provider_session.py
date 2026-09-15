"""Server-side handle for one exact Worker/Launcher local AI provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import secrets
import threading
import time
from uuid import UUID, uuid4

from .local_ai_protocol import validate_provider_request, validate_provider_response


AUDIT = logging.getLogger("sbatch_agent.local_ai_audit")


def _emit(*, audit_session_id, username, request, response, duration_ms):
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "AI_PROVIDER_REQUEST_COMPLETED",
        "audit_session_id": audit_session_id if isinstance(audit_session_id, str) else None,
        "username": username,
        "request_id": request["request_id"],
        "model": request["model"],
        "duration_ms": max(0, round(duration_ms)),
        "success": response.get("status") == "ok",
        "error_category": response.get("error_category"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    AUDIT.info(json.dumps(record, separators=(",", ":"), sort_keys=True))


@dataclass(repr=False)
class LocalUserProviderSession:
    provider_session_id: str
    worker_session_id: str
    username: str
    connection: object = field(repr=False)
    audit_session_id: str | None = None
    _invalid: bool = field(default=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self):
        if (str(UUID(self.provider_session_id)) != self.provider_session_id or
                str(UUID(self.worker_session_id)) != self.worker_session_id or
                not isinstance(self.username, str) or not self.username or
                not callable(getattr(self.connection, "request_ai_provider", None))):
            raise ValueError("Invalid local AI provider session")

    @classmethod
    def create(cls, *, worker_session_id, username, connection,
               audit_session_id=None):
        return cls(str(uuid4()), worker_session_id, username, connection,
                   audit_session_id)

    @property
    def available(self):
        with self._lock:
            return (not self._invalid and bool(getattr(self.connection, "connected", False))
                    and bool(getattr(self.connection, "local_provider_configured", False)))

    @property
    def status(self):
        return getattr(self.connection, "local_provider_status", {
            "provider": "deepseek", "configured": False,
            "backend": "Unavailable", "availability": "unavailable",
        })

    @property
    def state(self):
        # Compatibility with the existing Web/Session presentation boundary.
        class _State:
            value = "ready" if self.available else "unavailable"
        return _State()

    def assert_binding(self, *, worker_session_id, username):
        with self._lock:
            if (self._invalid or
                    not secrets.compare_digest(self.worker_session_id, worker_session_id) or
                    not secrets.compare_digest(self.username, username)):
                error_type = getattr(self.connection, "error_type", RuntimeError)
                raise error_type("AI_PROVIDER_IDENTITY_MISMATCH")

    def request_provider(self, request, *, timeout):
        validate_provider_request(request)
        with self._lock:
            if self._invalid:
                error_type = getattr(self.connection, "error_type", RuntimeError)
                raise error_type("AI_CLIENT_DISCONNECTED")
        started = time.monotonic()
        response = self.connection.request_ai_provider(request, timeout=timeout)
        validate_provider_response(response, request_id=request["request_id"])
        _emit(audit_session_id=self.audit_session_id, username=self.username,
              request=request, response=response,
              duration_ms=(time.monotonic() - started) * 1000)
        return response

    def invalidate(self):
        with self._lock:
            self._invalid = True

    def __repr__(self):
        return (f"LocalUserProviderSession(provider_session_id={self.provider_session_id!r}, "
                f"worker_session_id={self.worker_session_id!r}, username={self.username!r}, "
                f"available={self.available!r})")
