"""Bounded binary framing for the M10-B5B structured AI byte stream.

Control payloads are small JSON objects.  TLS records are carried as raw
bytes, never base64 encoded and never included in logs or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
from uuid import UUID

from .local_ai_protocol import (
    MAX_PROVIDER_REQUEST_BYTES, MAX_PROVIDER_RESPONSE_BYTES,
    MAX_PROVIDER_STATUS_BYTES,
)


PROTOCOL_VERSION = 2
MAGIC = b"ESAI"
HEADER = struct.Struct("!4sBB16sI")
HEADER_SIZE = HEADER.size
MAX_FRAME_SIZE = 64 * 1024
MAX_CONTROL_SIZE = 8 * 1024
MAX_BUFFERED_BYTES = 256 * 1024
MAX_ACTIVE_AI_STREAMS_PER_SESSION = 1
READ_TIMEOUT_SECONDS = 120.0
WRITE_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 8.0
DEEPSEEK_HOST = "api.deepseek.com"
DEEPSEEK_PORT = 443
CONTROL_STREAM_ID = UUID(int=0)
HELLO_PAYLOAD = b"structured-ai-egress-v2"
OPEN_PAYLOAD = DEEPSEEK_HOST.encode("ascii") + b"\0" + struct.pack("!H", DEEPSEEK_PORT)


class FrameType(IntEnum):
    CONTROL_REQUEST = 1
    CONTROL_RESPONSE = 2
    AI_OPEN = 10
    AI_OPEN_OK = 11
    AI_OPEN_ERROR = 12
    AI_DATA = 13
    AI_EOF = 14
    AI_CLOSE = 15
    AI_ERROR = 16
    LAUNCHER_HELLO = 20
    LAUNCHER_HELLO_ACK = 21
    AI_PROVIDER_REQUEST = 30
    AI_PROVIDER_RESPONSE = 31
    AI_PROVIDER_STATUS = 32


CONTROL_TYPES = frozenset({
    FrameType.CONTROL_REQUEST, FrameType.CONTROL_RESPONSE,
    FrameType.LAUNCHER_HELLO, FrameType.LAUNCHER_HELLO_ACK,
    FrameType.AI_PROVIDER_STATUS,
})
EMPTY_TYPES = frozenset({
    FrameType.AI_OPEN_OK, FrameType.AI_EOF, FrameType.AI_CLOSE,
    FrameType.LAUNCHER_HELLO_ACK,
})


class ProtocolError(RuntimeError):
    """Fixed exception category: frame content is deliberately omitted."""

    def __init__(self, code="AI_STREAM_PROTOCOL_INVALID"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class Frame:
    frame_type: FrameType
    stream_id: UUID
    payload: bytes = b""

    def __post_init__(self):
        if not isinstance(self.frame_type, FrameType) or not isinstance(self.stream_id, UUID):
            raise ProtocolError()
        if not isinstance(self.payload, bytes) or len(self.payload) > _payload_limit(self.frame_type):
            raise ProtocolError("AI_STREAM_FRAME_TOO_LARGE")
        if self.frame_type in CONTROL_TYPES:
            limit = (MAX_PROVIDER_STATUS_BYTES if self.frame_type == FrameType.AI_PROVIDER_STATUS
                     else MAX_CONTROL_SIZE)
            if self.stream_id != CONTROL_STREAM_ID or len(self.payload) > limit:
                raise ProtocolError()
        elif self.stream_id == CONTROL_STREAM_ID:
            raise ProtocolError()
        if self.frame_type in EMPTY_TYPES and self.payload:
            raise ProtocolError()
        if self.frame_type == FrameType.LAUNCHER_HELLO and self.payload != HELLO_PAYLOAD:
            raise ProtocolError("AI_STREAM_PROTOCOL_UNSUPPORTED")
        if self.frame_type == FrameType.AI_OPEN and self.payload != OPEN_PAYLOAD:
            raise ProtocolError("AI_EGRESS_TARGET_REJECTED")
        if self.frame_type in {FrameType.AI_OPEN_ERROR, FrameType.AI_ERROR} and (
                len(self.payload) != 1 or self.payload[0] not in range(1, 7)):
            raise ProtocolError()

    def __repr__(self):
        return (f"Frame(frame_type={self.frame_type.name!r}, "
                f"stream_id={str(self.stream_id)!r}, byte_count={len(self.payload)!r})")


def _payload_limit(frame_type):
    if frame_type == FrameType.AI_PROVIDER_REQUEST:
        return MAX_PROVIDER_REQUEST_BYTES
    if frame_type == FrameType.AI_PROVIDER_RESPONSE:
        return MAX_PROVIDER_RESPONSE_BYTES
    if frame_type == FrameType.AI_PROVIDER_STATUS:
        return MAX_PROVIDER_STATUS_BYTES
    return MAX_FRAME_SIZE

def _read_exact(source, size):
    data = bytearray()
    while len(data) < size:
        reader = getattr(source, "recv", None) or getattr(source, "read", None)
        if reader is None:
            raise ProtocolError()
        chunk = reader(size - len(data))
        if not chunk:
            if data:
                raise ProtocolError("AI_STREAM_UNEXPECTED_EOF")
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def encode_frame(frame: Frame) -> bytes:
    header = HEADER.pack(
        MAGIC, PROTOCOL_VERSION, int(frame.frame_type),
        frame.stream_id.bytes, len(frame.payload),
    )
    return header + frame.payload


def read_frame(source) -> Frame:
    raw_header = _read_exact(source, HEADER_SIZE)
    try:
        magic, version, raw_type, raw_stream_id, size = HEADER.unpack(raw_header)
        frame_type = FrameType(raw_type)
    except (ValueError, struct.error):
        raise ProtocolError() from None
    if magic != MAGIC:
        raise ProtocolError()
    if version != PROTOCOL_VERSION:
        raise ProtocolError("AI_STREAM_PROTOCOL_UNSUPPORTED")
    if size > _payload_limit(frame_type):
        raise ProtocolError("AI_STREAM_FRAME_TOO_LARGE")
    payload = _read_exact(source, size) if size else b""
    return Frame(frame_type, UUID(bytes=raw_stream_id), payload)


def write_frame(destination, frame: Frame):
    encoded = encode_frame(frame)
    sender = getattr(destination, "sendall", None)
    if sender is not None:
        sender(encoded)
        return
    writer = getattr(destination, "write", None)
    if writer is None:
        raise ProtocolError()
    view = memoryview(encoded)
    while view:
        written = writer(view)
        if written is None:
            break
        if not isinstance(written, int) or written <= 0:
            raise ProtocolError("AI_STREAM_WRITE_FAILED")
        view = view[written:]
    flush = getattr(destination, "flush", None)
    if flush is not None:
        flush()
