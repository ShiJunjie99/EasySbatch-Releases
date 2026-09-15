"""Request-local, payload-free diagnostics; never a persistent analysis history."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import math
import os
import re
import time
from uuid import UUID, uuid4


logger = logging.getLogger("sbatch_agent.prepare")
_trace = ContextVar("prepare_trace", default=None)
_deadline = ContextVar("model_deadline", default=None)
_attempt = ContextVar("model_attempt", default=0)
PHASES = {"web", "prepare", "scanner", "analyzer", "model", "model_attempt", "retry_backoff",
          "post_validation", "environment", "catalog", "cluster", "recommendation", "finalization", "relay"}


@dataclass
class RequestTrace:
    request_id: str
    scan_completed: bool = False
    provider: str = "unknown"
    model: str = "configured"
    protected: tuple[str, ...] = ()


def configure_logging():
    # Only this payload-free logger, not HTTP client/access/debug logging.
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def current_trace():
    return _trace.get()


def current_attempt():
    return _attempt.get()


def protected_values():
    names = {"SBATCH_AGENT_AI_RELAY_TOKEN", "SBATCH_AGENT_LOCAL_AI_KEY",
             os.environ.get("SBATCH_AGENT_AI_API_KEY_ENV", "")}
    return tuple(os.environ[n] for n in names if n and os.environ.get(n))


def safe_label(value):
    trace = current_trace()
    protected = protected_values() + (trace.protected if trace else ())
    return value if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value)
                     and not any(secret in value for secret in protected if secret)) else "configured"


@contextmanager
def request_scope(*, fresh=False, incoming_id=None, protected=()):
    existing = current_trace()
    if existing is not None and not fresh:
        yield existing
        return
    # Relay accepts only authenticated UUIDs, never arbitrary correlation text.
    request_id = str(uuid4())
    if isinstance(incoming_id, str) and len(incoming_id) == 36:
        try:
            if (str(UUID(incoming_id)) == incoming_id and UUID(incoming_id).version == 4
                    and not any(secret and secret in incoming_id for secret in (*protected_values(), *protected))):
                request_id = incoming_id
        except ValueError:
            pass
    trace = RequestTrace(request_id, protected=protected)
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        _trace.reset(token)


def set_model(client):
    trace = current_trace()
    if trace:
        trace.provider = safe_label(getattr(client, "provider", "unknown"))
        trace.model = safe_label(getattr(client, "model", "configured"))


def emit(phase, *, status, duration_ms=0, exc=None, code=None):
    from .model_client import AnalysisOutputValidationError, ModelUnavailableError
    from .prepare_errors import PrepareErrorCode, error_code
    trace = current_trace()
    if trace is None:
        return
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "prepare_request_id": trace.request_id,
              "phase": phase if phase in PHASES else "prepare", "attempt": current_attempt(),
              "provider": safe_label(trace.provider), "model": safe_label(trace.model),
              "duration_ms": max(0, round(duration_ms)),
              "status": status if status in {"success", "failure", "retry", "start"} else "failure",
              "error_code": (PrepareErrorCode(code).value if code is not None else error_code(exc).value) if exc or code else None}
    if isinstance(exc, ModelUnavailableError):
        record.update(origin=exc.origin, failure_kind=exc.failure_kind, http_status=exc.http_status)
    elif isinstance(exc, AnalysisOutputValidationError):
        record["diagnostic"] = exc.safe_diagnostic()
    # A final exact-value guard includes custom configured key names. No record
    # takes exception messages, headers, project paths, bodies or provider IDs.
    text = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
    if any(secret and secret in text for secret in (*protected_values(), *trace.protected)):
        return
    logger.info("%s", text)


@contextmanager
def phase(name, *, failure_code=None):
    started = time.monotonic()
    emit(name, status="start")
    try:
        yield
    except Exception as exc:
        emit(name, status="failure", duration_ms=(time.monotonic() - started) * 1000, exc=exc, code=failure_code)
        raise
    else:
        emit(name, status="success", duration_ms=(time.monotonic() - started) * 1000)


def traced(name):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with request_scope(), phase(name):
                return function(*args, **kwargs)
        return call
    return decorate


@contextmanager
def attempt_scope(attempt, seconds, *, clock=time.monotonic):
    limit = clock() + seconds
    previous = _deadline.get()
    token = _deadline.set((min(limit, previous[0]) if previous else limit, clock))
    number = _attempt.set(attempt)
    try:
        yield
    finally:
        _attempt.reset(number)
        _deadline.reset(token)


def remaining_timeout(configured):
    deadline = _deadline.get()
    if deadline is None:
        return configured
    remaining = deadline[0] - deadline[1]()
    if remaining <= 0:
        raise TimeoutError("Model request budget exhausted")
    return min(configured, remaining)


def relay_headers():
    trace = current_trace()
    headers = {}
    if trace:
        headers["X-Request-ID"] = trace.request_id
        headers["X-Model-Attempt"] = str(current_attempt() or 1)
    if _deadline.get() is not None:
        headers["X-Model-Budget-Ms"] = str(max(1, math.floor(remaining_timeout(120) * 1000)))
    return headers


def read_bounded(response, limit, timeout, *, connection=None):
    """Read in bounded chunks, reapplying the remaining socket budget.

    Standard-library DNS/OS calls are not forcibly interrupted. No background
    inference worker is abandoned when the budget expires.
    """
    chunks, count = [], 0
    while count <= limit:
        remaining = remaining_timeout(timeout)
        sock = getattr(connection, "sock", None) if connection else None
        if sock is None:
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(remaining)
        read = getattr(response, "read1", response.read)
        data = read(min(16384, limit + 1 - count))
        remaining_timeout(timeout)
        if not data:
            break
        chunks.append(data)
        count += len(data)
    return b"".join(chunks)
