"""Small Launcher-safe restricted CONNECT agent for per-user AI egress."""

from enum import StrEnum
import ipaddress
import math
import re
import secrets
import select
import socket
import socketserver
import threading
import time


DEEPSEEK_EGRESS_HOST = "api.deepseek.com"
DEEPSEEK_EGRESS_PORT = 443
REMOTE_PROXY_HOST = "127.0.0.1"
MAX_CONNECT_HEADER_BYTES = 8192
CONNECT_TIMEOUT_SECONDS = 8.0
RELAY_IDLE_TIMEOUT_SECONDS = 30.0
RELAY_LIFETIME_SECONDS = 120.0


class AIEgressErrorCode(StrEnum):
    UNAVAILABLE = "AI_EGRESS_UNAVAILABLE"
    AUTH_FAILED = "AI_EGRESS_AUTH_FAILED"
    IDENTITY_MISMATCH = "AI_EGRESS_IDENTITY_MISMATCH"
    TARGET_REJECTED = "AI_EGRESS_TARGET_REJECTED"
    TLS_FAILED = "AI_EGRESS_TLS_FAILED"
    TIMEOUT = "AI_EGRESS_TIMEOUT"


class AIEgressError(RuntimeError):
    """A fixed, payload-free failure category."""

    def __init__(self, code=AIEgressErrorCode.UNAVAILABLE):
        self.code = AIEgressErrorCode(code)
        super().__init__(self.code.value)


def new_egress_credential() -> str:
    """Return 256 bits of url-safe entropy without persistence."""
    return secrets.token_urlsafe(32)


def _validate_credential(value: str) -> str:
    if (not isinstance(value, str) or
            re.fullmatch(r"[A-Za-z0-9_-]{43,128}", value) is None):
        raise ValueError("Invalid AI egress credential")
    return value


def _parse_connect_request(raw: bytes, credential: str):
    try:
        header, marker, remainder = raw.partition(b"\r\n\r\n")
        if not marker or remainder or not header.isascii():
            raise ValueError
        lines = header.decode("ascii").split("\r\n")
        if lines[0] != f"CONNECT {DEEPSEEK_EGRESS_HOST}:{DEEPSEEK_EGRESS_PORT} HTTP/1.1":
            return 403
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            lowered = name.lower()
            if (not separator or lowered in headers or
                    lowered not in {"host", "proxy-authorization", "connection"} or
                    not value.startswith(" ") or not value[1:] or
                    any(ord(character) < 32 or ord(character) > 126 for character in value[1:])):
                raise ValueError
            headers[lowered] = value[1:]
        if headers.get("host") != f"{DEEPSEEK_EGRESS_HOST}:{DEEPSEEK_EGRESS_PORT}":
            return 403
        supplied = headers.get("proxy-authorization", "")
        expected = "Bearer " + credential
        if not secrets.compare_digest(supplied.encode("ascii"), expected.encode("ascii")):
            return 407
        return 200
    except (UnicodeError, ValueError):
        return 400


def _public_addresses(*, resolver=socket.getaddrinfo):
    try:
        candidates = resolver(
            DEEPSEEK_EGRESS_HOST, DEEPSEEK_EGRESS_PORT,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise AIEgressError(AIEgressErrorCode.UNAVAILABLE) from None
    results = []
    for family, socktype, protocol, _, address in candidates:
        if family not in {socket.AF_INET, socket.AF_INET6}:
            continue
        try:
            ip = ipaddress.ip_address(address[0])
        except (ValueError, TypeError, IndexError):
            raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED) from None
        if (not ip.is_global or ip.is_loopback or ip.is_private or ip.is_link_local or
                ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED)
        results.append((family, socktype, protocol, address))
    if not results:
        raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED)
    return results


def _connect_public_deepseek(*, resolver=socket.getaddrinfo,
                             socket_factory=socket.socket,
                             timeout=CONNECT_TIMEOUT_SECONDS):
    last_timeout = False
    for family, socktype, protocol, address in _public_addresses(resolver=resolver):
        candidate = socket_factory(family, socktype, protocol)
        try:
            candidate.settimeout(timeout)
            candidate.connect(address)
            return candidate
        except TimeoutError:
            last_timeout = True
            candidate.close()
        except OSError:
            candidate.close()
    raise AIEgressError(
        AIEgressErrorCode.TIMEOUT if last_timeout else AIEgressErrorCode.UNAVAILABLE,
    )


class _RestrictedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = False


class RestrictedConnectEgressAgent:
    """Launcher-side CONNECT agent pinned to one hostname, port and credential."""

    def __init__(self, credential: str, *, connector=None,
                 connect_timeout=CONNECT_TIMEOUT_SECONDS,
                 idle_timeout=RELAY_IDLE_TIMEOUT_SECONDS,
                 lifetime=RELAY_LIFETIME_SECONDS):
        self._credential = _validate_credential(credential)
        for value in (connect_timeout, idle_timeout, lifetime):
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or not 0 < value <= 300):
                raise ValueError("Invalid restricted egress timeout")
        self.connect_timeout = float(connect_timeout)
        self.idle_timeout = float(idle_timeout)
        self.lifetime = float(lifetime)
        self._connector = connector or (lambda timeout: _connect_public_deepseek(timeout=timeout))
        agent = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                agent._handle(self.request)

        try:
            self._server = _RestrictedServer(("127.0.0.1", 0), Handler)
        except OSError:
            raise AIEgressError(AIEgressErrorCode.UNAVAILABLE) from None
        self.host, self.port = self._server.server_address
        if self.host != "127.0.0.1":
            self._server.server_close()
            raise AIEgressError(AIEgressErrorCode.TARGET_REJECTED)
        self._thread = None
        self._closed = False

    def __repr__(self):
        return (f"RestrictedConnectEgressAgent(host={self.host!r}, port={self.port!r}, "
                f"running={self.running!r})")

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._closed)

    def start(self):
        if self._closed:
            raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="easysbatch-restricted-ai-egress", daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _response(connection, status):
        line = {
            200: b"HTTP/1.1 200 Connection Established\r\n\r\n",
            400: b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n",
            403: b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n",
            407: (b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                  b"Proxy-Authenticate: Bearer\r\nConnection: close\r\n\r\n"),
            502: b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n",
            504: b"HTTP/1.1 504 Gateway Timeout\r\nConnection: close\r\n\r\n",
        }.get(status, b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        try:
            connection.sendall(line)
        except OSError:
            pass

    def _handle(self, client):
        upstream = None
        raw = bytearray()
        try:
            client.settimeout(self.connect_timeout)
            while b"\r\n\r\n" not in raw:
                chunk = client.recv(min(1024, MAX_CONNECT_HEADER_BYTES + 1 - len(raw)))
                if not chunk:
                    return
                raw.extend(chunk)
                if len(raw) > MAX_CONNECT_HEADER_BYTES:
                    self._response(client, 400)
                    return
            header, marker, remainder = bytes(raw).partition(b"\r\n\r\n")
            if remainder:
                self._response(client, 400)
                return
            status = _parse_connect_request(header + marker, self._credential)
            if status != 200:
                self._response(client, status)
                return
            try:
                upstream = self._connector(self.connect_timeout)
            except AIEgressError as exc:
                self._response(client, 504 if exc.code == AIEgressErrorCode.TIMEOUT else
                               403 if exc.code == AIEgressErrorCode.TARGET_REJECTED else 502)
                return
            except (OSError, TimeoutError):
                self._response(client, 502)
                return
            self._response(client, 200)
            self._relay(client, upstream)
        finally:
            raw.clear()
            if upstream is not None:
                upstream.close()

    def _relay(self, client, upstream):
        deadline = time.monotonic() + self.lifetime
        last_activity = time.monotonic()
        sockets = (client, upstream)
        while time.monotonic() < deadline:
            wait = min(0.5, deadline - time.monotonic(),
                       self.idle_timeout - (time.monotonic() - last_activity))
            if wait <= 0:
                return
            try:
                readable, _, _ = select.select(sockets, (), (), wait)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            for source in readable:
                target = upstream if source is client else client
                try:
                    payload = source.recv(16384)
                    if not payload:
                        return
                    target.sendall(payload)
                    last_activity = time.monotonic()
                except OSError:
                    return

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._credential = ""
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
