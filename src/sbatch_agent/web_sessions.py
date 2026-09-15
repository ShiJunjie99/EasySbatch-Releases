"""In-memory authenticated Web sessions bound to one execution context."""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from uuid import uuid4

from starlette.responses import HTMLResponse, RedirectResponse


AUDIT = logging.getLogger('sbatch_agent.authentication_audit')
COOKIE_NAME = 'easysbatch_session'
SSH_FIRST_COOKIE_NAME = 'easysbatch_ssh_first_session'
AUDIT_EVENTS = frozenset({
    'LOGIN_SUCCESS', 'LOGIN_FAILED', 'IDENTITY_MISMATCH', 'SESSION_EXPIRED',
    'LOGOUT', 'SSH_DISCONNECTED', 'WORKER_DISCONNECTED', 'IDENTITY_VERIFIED', 'SHUTDOWN',
})


def configure_authentication_audit():
    """Make the bounded authentication audit visible under Uvicorn logging."""
    if not AUDIT.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        AUDIT.addHandler(handler)
    AUDIT.setLevel(logging.INFO)


def emit_auth_event(event, *, audit_session_id=None, username=None, result,
                    correlation_id=None, duration_ms=None, error_code=None):
    """Log only an explicit safe schema; raw tokens and credentials have no slot."""
    if event not in AUDIT_EVENTS or result not in {'SUCCESS', 'FAIL', 'CLOSED'}:
        raise ValueError('Invalid authentication audit event')
    payload = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'event': event,
        'audit_session_id': audit_session_id,
        'username': username,
        'result': result,
        'correlation_id': correlation_id or str(uuid4()),
        'duration_ms': duration_ms,
        'error_code': error_code,
    }
    AUDIT.info(json.dumps(payload, separators=(',', ':'), sort_keys=True))


@dataclass
class ServerSession:
    audit_session_id: str
    created_at: float
    last_seen_at: float
    data: dict = field(default_factory=dict)
    ssh_context: object | None = None
    operation_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def authenticated(self):
        return self.ssh_context is not None

    @property
    def identity(self):
        return self.ssh_context.identity if self.ssh_context is not None else None

    @property
    def ai_egress(self):
        return (getattr(self.ssh_context, 'ai_egress', None)
                if self.ssh_context is not None else None)

    def __repr__(self):
        identity = self.identity
        return (f'ServerSession(audit_session_id={self.audit_session_id!r}, '
                f'username={identity.username if identity else None!r}, '
                f'authenticated={self.authenticated!r})')


class SessionManager:
    """Owns opaque tokens, authenticated execution contexts, and lifecycle."""

    def __init__(self, *, idle_timeout_seconds=1800, clock=time.monotonic,
                 token_factory=None):
        if (type(idle_timeout_seconds) not in (int, float) or
                not 60 <= idle_timeout_seconds <= 86400):
            raise ValueError('Invalid session idle timeout')
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._sessions = {}
        self._lock = threading.RLock()

    def _new_token(self):
        for _ in range(4):
            token = self._token_factory()
            if (isinstance(token, str) and len(token) >= 43 and
                    re.fullmatch(r'[A-Za-z0-9_-]+', token) and token not in self._sessions):
                return token
        raise RuntimeError('Unable to create opaque session token')

    def _new_record(self, *, context=None):
        now = self._clock()
        return ServerSession(
            audit_session_id=str(uuid4()), created_at=now, last_seen_at=now,
            data={'csrf': secrets.token_urlsafe(32)}, ssh_context=context,
        )

    def create_anonymous(self):
        with self._lock:
            token = self._new_token()
            record = self._new_record()
            self._sessions[token] = record
            return token, record

    def promote(self, anonymous_token, context, *, correlation_id=None, duration_ms=None):
        """Rotate the token and bind exactly one already-verified SSH context."""
        if context is None or not context.connected:
            raise ValueError('A connected verified SSH context is required')
        with self._lock:
            old = self._sessions.pop(anonymous_token, None) if anonymous_token else None
            token = self._new_token()
            record = self._new_record(context=context)
            self._sessions[token] = record
        setter = getattr(context, 'set_disconnect_callback', None)
        try:
            if setter is not None:
                setter(lambda: self.invalidate_context(context, event='WORKER_DISCONNECTED'))
                if not context.connected:
                    self.invalidate_context(context, event='WORKER_DISCONNECTED')
                    raise ValueError('Execution context disconnected during session binding')
        except Exception:
            with self._lock:
                self._sessions.pop(token, None)
            try:
                context.close()
            except Exception:
                pass
            if old is not None and old.ssh_context is not None:
                old.ssh_context.close()
            raise
        if old is not None and old.ssh_context is not None:
            old.ssh_context.close()
        emit_auth_event(
            'LOGIN_SUCCESS', audit_session_id=record.audit_session_id,
            username=record.identity.username, result='SUCCESS',
            correlation_id=correlation_id, duration_ms=duration_ms,
        )
        return token, record

    def resolve(self, token, *, touch=True):
        """Return (record, status); every invalid path fails closed."""
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43,256}', token):
            return None, 'missing'
        expired = []
        requested_expired = False
        disconnected = None
        with self._lock:
            now = self._clock()
            for candidate, record in tuple(self._sessions.items()):
                if now - record.last_seen_at > self.idle_timeout_seconds:
                    requested_expired = requested_expired or candidate == token
                    expired.append(self._sessions.pop(candidate))
            record = self._sessions.get(token)
            if record is not None and record.authenticated and not record.ssh_context.connected:
                disconnected = self._sessions.pop(token)
                record = None
            if record is not None and touch:
                record.last_seen_at = now
        for stale in expired:
            self._close_record(stale, 'SESSION_EXPIRED')
        if disconnected is not None:
            self._close_record(disconnected, 'SSH_DISCONNECTED')
            return None, 'disconnected'
        if requested_expired:
            return None, 'expired'
        return (record, 'active') if record is not None else (None, 'missing')

    def invalidate(self, token, *, event='LOGOUT', correlation_id=None):
        if event not in {'LOGOUT', 'SESSION_EXPIRED', 'SSH_DISCONNECTED', 'WORKER_DISCONNECTED'}:
            raise ValueError('Invalid session invalidation event')
        with self._lock:
            record = self._sessions.pop(token, None)
        if record is not None:
            self._close_record(record, event, correlation_id=correlation_id)
            return True
        return False

    def invalidate_context(self, context, *, event='WORKER_DISCONNECTED'):
        if event not in {'SSH_DISCONNECTED', 'WORKER_DISCONNECTED'}:
            raise ValueError('Invalid context invalidation event')
        with self._lock:
            matches = [
                (token, record) for token, record in self._sessions.items()
                if record.ssh_context is context
            ]
            for token, _ in matches:
                self._sessions.pop(token, None)
        for _, record in matches:
            self._close_record(record, event)
        return len(matches)

    def close_all(self):
        with self._lock:
            records = tuple(self._sessions.values())
            self._sessions.clear()
        for record in records:
            self._close_record(record, 'SHUTDOWN')

    def _close_record(self, record, event, *, correlation_id=None):
        identity = record.identity
        if record.ssh_context is not None:
            record.ssh_context.close()
        if identity is not None:
            emit_auth_event(
                event, audit_session_id=record.audit_session_id,
                username=identity.username, result='CLOSED',
                correlation_id=correlation_id,
            )

    @property
    def active_count(self):
        with self._lock:
            return len(self._sessions)


class ServerSessionMiddleware:
    """Expose server-side request.session while sending only an opaque cookie."""

    def __init__(self, app, *, manager, secure=False, cookie_name=COOKIE_NAME):
        self.app = app
        self.manager = manager
        self.secure = secure
        self.cookie_name = cookie_name

    @staticmethod
    def _cookie_from_scope(scope, name):
        raw = dict(scope.get('headers', ())).get(b'cookie', b'').decode('latin-1')
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
            return cookie[name].value if name in cookie else None
        except (CookieError, ValueError):
            return None

    def _set_cookie_header(self, token):
        pieces = [f'{self.cookie_name}={token}', 'Path=/', 'HttpOnly', 'SameSite=Strict']
        if self.secure:
            pieces.append('Secure')
        return '; '.join(pieces).encode('latin-1')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        token = self._cookie_from_scope(scope, self.cookie_name)
        record, status = self.manager.resolve(token)
        created = False
        if record is None:
            token, record = self.manager.create_anonymous()
            created = True
        scope['session'] = record.data
        scope['server_session'] = record
        scope['session_token'] = token
        scope['session_status'] = status

        async def send_with_cookie(message):
            override = scope.get('session_cookie_override')
            if message['type'] == 'http.response.start' and (created or override):
                headers = list(message.get('headers', ()))
                headers.append((b'set-cookie', self._set_cookie_header(override or token)))
                message['headers'] = headers
            await send(message)

        await self.app(scope, receive, send_with_cookie)


class AuthenticationBoundaryMiddleware:
    """Fail closed and keep non-owner identities out of local legacy routes."""

    AUTHENTICATED_PATHS = frozenset({'/session', '/session/verify', '/logout'})

    def __init__(self, app, *, enabled, process_username, bootstrap_enabled=False):
        self.app = app
        self.enabled = enabled
        self.process_username = process_username
        self.bootstrap_enabled = bootstrap_enabled

    @staticmethod
    async def _respond(response, scope, receive, send):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not self.enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get('path', '')
        if (path == '/login' or path.startswith('/static/') or
                (self.bootstrap_enabled and path == '/auth/ssh-bootstrap')):
            await self.app(scope, receive, send)
            return
        record = scope.get('server_session')
        if record is None or not record.authenticated:
            reason = scope.get('session_status')
            suffix = '?reason=' + reason if reason in {'expired', 'disconnected'} else ''
            await self._respond(RedirectResponse('/login' + suffix, status_code=303),
                                scope, receive, send)
            return
        if path not in self.AUTHENTICATED_PATHS and record.identity.username != self.process_username:
            if scope.get('method') in {'GET', 'HEAD'}:
                await self._respond(RedirectResponse('/session?legacy=blocked', status_code=303),
                                    scope, receive, send)
            else:
                await self._respond(HTMLResponse(
                    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<title>功能暂不可用</title><p>该功能正在进行多用户适配，当前账号暂不可使用。</p>',
                    status_code=403,
                ), scope, receive, send)
            return
        await self.app(scope, receive, send)
