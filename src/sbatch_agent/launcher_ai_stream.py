"""Launcher-side raw TCP agent for the bounded structured AI protocol."""

from __future__ import annotations

import queue
import select
import socket
import threading
import math

from .ai_stream_protocol import (
    CONNECT_TIMEOUT_SECONDS, CONTROL_STREAM_ID, Frame, FrameType,
    HELLO_PAYLOAD, MAX_ACTIVE_AI_STREAMS_PER_SESSION, MAX_BUFFERED_BYTES,
    MAX_FRAME_SIZE, ProtocolError, READ_TIMEOUT_SECONDS, WRITE_TIMEOUT_SECONDS, read_frame,
    write_frame,
)
from .restricted_egress import (
    AIEgressError, AIEgressErrorCode, _connect_public_deepseek,
)
from .local_ai_protocol import (
    LocalAIErrorCode, decode_provider_request, encode_provider_response,
    encode_provider_status, error_response, validate_provider_response,
)
from .local_ai_provider import LocalDeepSeekProviderClient


_ERROR_BYTE = {
    AIEgressErrorCode.UNAVAILABLE: 1,
    AIEgressErrorCode.AUTH_FAILED: 2,
    AIEgressErrorCode.IDENTITY_MISMATCH: 3,
    AIEgressErrorCode.TARGET_REJECTED: 4,
    AIEgressErrorCode.TLS_FAILED: 5,
    AIEgressErrorCode.TIMEOUT: 6,
}


class LauncherAIEgressAgent:
    """One bounded AI protocol: legacy TLS bytes plus local provider RPC."""

    def __init__(self, source, destination, *, connector=None,
                 provider_client=None,
                 connect_timeout=CONNECT_TIMEOUT_SECONDS,
                 read_timeout=READ_TIMEOUT_SECONDS,
                 write_timeout=WRITE_TIMEOUT_SECONDS):
        if source is None or destination is None:
            raise ValueError("SSH protocol streams required")
        for value in (connect_timeout, read_timeout, write_timeout):
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or not 0 < value <= 300):
                raise ValueError("Invalid structured egress timeout")
        self._source = source
        self._destination = destination
        self._connector = connector or (
            lambda timeout: _connect_public_deepseek(timeout=timeout)
        )
        self.provider_client = provider_client or LocalDeepSeekProviderClient()
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._write_timeout = float(write_timeout)
        self._streams = {}
        self._provider_requests = set()
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._ready = threading.Event()
        self._reader = None

    def __repr__(self):
        with self._lock:
            count = len(self._streams)
        return (f"LauncherAIEgressAgent(ready={self.ready!r}, active_streams={count!r}, "
                f"local_ai_configured={self.provider_client.status()['configured']!r})")

    @property
    def ready(self):
        return self._ready.is_set() and not self._closed.is_set()

    def _send(self, frame):
        if self._closed.is_set():
            raise ProtocolError("AI_STREAM_CLOSED")
        with self._write_lock:
            write_frame(self._destination, frame)

    def start(self):
        if self._reader is not None:
            return
        self._reader = threading.Thread(
            target=self._read_loop, name="easysbatch-ai-egress-frames", daemon=True,
        )
        self._reader.start()
        self._send(Frame(
            FrameType.LAUNCHER_HELLO, CONTROL_STREAM_ID, HELLO_PAYLOAD,
        ))

    def wait_ready(self, timeout=5):
        ready = self._ready.wait(timeout) and not self._closed.is_set()
        if ready:
            try:
                self._send_provider_status()
            except (OSError, ProtocolError):
                self.close()
                return False
        return ready

    def _read_loop(self):
        try:
            while not self._closed.is_set():
                frame = read_frame(self._source)
                if frame.frame_type == FrameType.LAUNCHER_HELLO_ACK:
                    if self._ready.is_set():
                        raise ProtocolError()
                    self._ready.set()
                elif frame.frame_type == FrameType.AI_OPEN:
                    if not self._ready.is_set():
                        raise ProtocolError()
                    self._open(frame)
                elif frame.frame_type == FrameType.AI_PROVIDER_REQUEST:
                    if not self._ready.is_set():
                        raise ProtocolError()
                    self._request_provider(frame)
                elif frame.frame_type in {FrameType.AI_DATA, FrameType.AI_EOF,
                                          FrameType.AI_CLOSE, FrameType.AI_ERROR}:
                    with self._lock:
                        stream = self._streams.get(frame.stream_id)
                    if stream is None:
                        raise ProtocolError()
                    stream.receive(frame)
                else:
                    raise ProtocolError()
        except (EOFError, OSError, ValueError, ProtocolError):
            self.close()

    def _open(self, frame):
        with self._lock:
            if (frame.stream_id in self._streams or
                    len(self._streams) >= MAX_ACTIVE_AI_STREAMS_PER_SESSION):
                self._send(Frame(FrameType.AI_OPEN_ERROR, frame.stream_id, b"\x01"))
                return
            stream = _LauncherAIStream(self, frame.stream_id)
            self._streams[frame.stream_id] = stream
        stream.open()

    def _forget(self, stream_id):
        with self._lock:
            self._streams.pop(stream_id, None)

    def _send_provider_status(self):
        self._send(Frame(
            FrameType.AI_PROVIDER_STATUS, CONTROL_STREAM_ID,
            encode_provider_status(self.provider_client.status()),
        ))

    def refresh_provider_status(self):
        if self.ready:
            self._send_provider_status()
        return self.provider_client.status()

    def _request_provider(self, frame):
        with self._lock:
            if frame.stream_id in self._provider_requests or self._provider_requests:
                response = error_response(
                    str(frame.stream_id), LocalAIErrorCode.UNAVAILABLE,
                )
                self._send(Frame(
                    FrameType.AI_PROVIDER_RESPONSE, frame.stream_id,
                    encode_provider_response(response),
                ))
                return
            self._provider_requests.add(frame.stream_id)

        def perform():
            try:
                try:
                    request = decode_provider_request(frame.payload)
                    if request["request_id"] != str(frame.stream_id):
                        raise ValueError
                    response = self.provider_client.request(request)
                    validate_provider_response(response, request_id=str(frame.stream_id))
                except (ValueError, TypeError, RecursionError):
                    response = error_response(
                        str(frame.stream_id), LocalAIErrorCode.RESPONSE_INVALID,
                    )
                self._send(Frame(
                    FrameType.AI_PROVIDER_RESPONSE, frame.stream_id,
                    encode_provider_response(response),
                ))
                self._send_provider_status()
            except (OSError, ProtocolError):
                pass
            finally:
                with self._lock:
                    self._provider_requests.discard(frame.stream_id)

        threading.Thread(
            target=perform, name="easysbatch-local-ai-provider", daemon=True,
        ).start()

    def close(self):
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
        self._ready.clear()
        with self._lock:
            streams = tuple(self._streams.values())
            self._streams.clear()
            self._provider_requests.clear()
        for stream in streams:
            stream.close(notify=False)


class _LauncherAIStream:
    def __init__(self, agent, stream_id):
        self.agent = agent
        self.stream_id = stream_id
        self._socket = None
        self._incoming = queue.Queue(maxsize=max(1, MAX_BUFFERED_BYTES // MAX_FRAME_SIZE))
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._connected = threading.Event()
        self._local_eof = False
        self._remote_eof = False

    def open(self):
        threading.Thread(target=self._connect,
                         name="easysbatch-ai-egress-connect", daemon=True).start()

    def _connect(self):
        try:
            candidate = self.agent._connector(self.agent._connect_timeout)
            if not isinstance(candidate, socket.socket):
                raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
            self._socket = candidate
            candidate.settimeout(self.agent._read_timeout)
            self._connected.set()
            self.agent._send(Frame(FrameType.AI_OPEN_OK, self.stream_id))
            threading.Thread(target=self._write_socket,
                             name="easysbatch-ai-egress-write", daemon=True).start()
            self._read_socket()
        except AIEgressError as exc:
            try:
                self.agent._send(Frame(
                    FrameType.AI_OPEN_ERROR, self.stream_id,
                    bytes((_ERROR_BYTE.get(exc.code, 1),)),
                ))
            except (OSError, ProtocolError):
                pass
            self.close(notify=False)
        except (OSError, TimeoutError, ProtocolError):
            try:
                self.agent._send(Frame(
                    FrameType.AI_OPEN_ERROR, self.stream_id, b"\x01",
                ))
            except (OSError, ProtocolError):
                pass
            self.close(notify=False)

    def receive(self, frame):
        if self._closed.is_set() or not self._connected.is_set():
            raise ProtocolError()
        if frame.frame_type == FrameType.AI_DATA:
            try:
                self._incoming.put_nowait(frame.payload)
            except queue.Full:
                self.close()
        elif frame.frame_type == FrameType.AI_EOF:
            try:
                self._incoming.put_nowait(None)
                self._remote_eof = True
                self._close_if_complete()
            except queue.Full:
                self.close()
        else:
            self.close(notify=False)

    def _write_socket(self):
        candidate = self._socket
        if candidate is None:
            return
        try:
            while not self._closed.is_set():
                payload = self._incoming.get()
                if self._closed.is_set():
                    return
                if payload is None:
                    candidate.shutdown(socket.SHUT_WR)
                    return
                _, writable, _ = select.select(
                    (), (candidate,), (), self.agent._write_timeout,
                )
                if not writable:
                    raise TimeoutError
                candidate.sendall(payload)
        except (OSError, queue.Empty):
            self.close()

    def _read_socket(self):
        candidate = self._socket
        if candidate is None:
            return
        try:
            while not self._closed.is_set():
                payload = candidate.recv(MAX_FRAME_SIZE)
                if self._closed.is_set():
                    return
                if not payload:
                    self.agent._send(Frame(FrameType.AI_EOF, self.stream_id))
                    self._local_eof = True
                    self._close_if_complete()
                    return
                self.agent._send(Frame(FrameType.AI_DATA, self.stream_id, payload))
        except (OSError, ProtocolError):
            self.close()

    def _close_if_complete(self):
        if self._local_eof and self._remote_eof:
            # Both directions have already acknowledged EOF.  Sending an
            # additional AI_CLOSE from both peers races with stream removal:
            # each late close then looks like an unknown stream and tears down
            # the whole Worker channel.  EOF completion needs no extra close.
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
        if notify and self.agent.ready:
            try:
                self.agent._send(Frame(FrameType.AI_CLOSE, self.stream_id))
            except (OSError, ProtocolError):
                pass
        candidate, self._socket = self._socket, None
        if candidate is not None:
            try:
                candidate.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            candidate.close()
        self.agent._forget(self.stream_id)
