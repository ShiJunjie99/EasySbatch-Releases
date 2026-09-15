"""M10-B5B bounded binary framing and standalone Worker relay tests."""

from io import BytesIO
import struct
import threading
from uuid import UUID, uuid4

import pytest

from sbatch_agent.ai_stream_protocol import (
    CONTROL_STREAM_ID, Frame, FrameType, HEADER, HELLO_PAYLOAD, MAGIC,
    MAX_FRAME_SIZE, OPEN_PAYLOAD, PROTOCOL_VERSION, ProtocolError,
    encode_frame, read_frame, write_frame,
)
import sbatch_agent.user_worker as worker


class Fragmented:
    def __init__(self, payload, widths):
        self.payload = bytearray(payload)
        self.widths = iter(widths)

    def read(self, size):
        if not self.payload:
            return b""
        width = min(size, next(self.widths, size), len(self.payload))
        result = bytes(self.payload[:width])
        del self.payload[:width]
        return result


class PartialWriter:
    def __init__(self):
        self.data = bytearray()
        self.flushed = False

    def write(self, value):
        chunk = bytes(value[:3])
        self.data.extend(chunk)
        return len(chunk)

    def flush(self):
        self.flushed = True


def test_valid_data_partial_reads_fragmented_writes_and_coalesced_frames():
    first = Frame(FrameType.AI_DATA, uuid4(), b"opaque-tls-record")
    second = Frame(FrameType.AI_CLOSE, first.stream_id)
    encoded = encode_frame(first) + encode_frame(second)
    source = Fragmented(encoded, [1, 2, 5, 3, 1, 8] * 20)
    assert read_frame(source) == first
    assert read_frame(source) == second
    writer = PartialWriter()
    write_frame(writer, first)
    assert bytes(writer.data) == encode_frame(first) and writer.flushed


@pytest.mark.parametrize("payload", [
    HEADER.pack(b"NOPE", PROTOCOL_VERSION, FrameType.AI_DATA, uuid4().bytes, 0),
    HEADER.pack(MAGIC, 99, FrameType.AI_DATA, uuid4().bytes, 0),
    HEADER.pack(MAGIC, PROTOCOL_VERSION, 255, uuid4().bytes, 0),
    HEADER.pack(MAGIC, PROTOCOL_VERSION, FrameType.AI_DATA,
                uuid4().bytes, MAX_FRAME_SIZE + 1),
    HEADER.pack(MAGIC, PROTOCOL_VERSION, FrameType.AI_DATA, uuid4().bytes, 5) + b"x",
])
def test_invalid_magic_version_type_oversize_and_truncated_length_rejected(payload):
    with pytest.raises((ProtocolError, EOFError)):
        read_frame(BytesIO(payload))


@pytest.mark.parametrize("frame", [
    lambda: Frame(FrameType.AI_DATA, CONTROL_STREAM_ID, b"x"),
    lambda: Frame(FrameType.LAUNCHER_HELLO, CONTROL_STREAM_ID, b"wrong"),
    lambda: Frame(FrameType.AI_OPEN, uuid4(), b"example.com\0\x01\xbb"),
    lambda: Frame(FrameType.AI_OPEN_OK, uuid4(), b"unexpected"),
    lambda: Frame(FrameType.AI_ERROR, uuid4(), b"unbounded error text"),
])
def test_frame_schema_and_fixed_destination_fail_closed(frame):
    with pytest.raises(ProtocolError):
        frame()


def test_worker_relays_only_directionally_valid_frames_and_stdout_has_no_log_text():
    stream_id = uuid4()
    broker_frames = encode_frame(Frame(FrameType.AI_OPEN, stream_id, OPEN_PAYLOAD))
    broker_frames += encode_frame(Frame(FrameType.AI_CLOSE, stream_id))
    output = BytesIO()
    with pytest.raises(EOFError):
        worker._serve(
            type("Sink", (), {"sendall": lambda self, value: None})(),
            BytesIO(broker_frames), {}, output, threading.Lock(),
        )
    assert output.getvalue() == broker_frames
    assert b"worker started" not in output.getvalue().lower()


def test_worker_launcher_relay_accepts_hello_and_rejects_server_direction_frame():
    class Connection:
        def __init__(self):
            self.sent = bytearray()
            self.shutdown_value = None
        def sendall(self, value): self.sent.extend(value)
        def shutdown(self, value): self.shutdown_value = value

    valid = encode_frame(Frame(
        FrameType.LAUNCHER_HELLO, CONTROL_STREAM_ID, HELLO_PAYLOAD,
    ))
    connection = Connection()
    worker._relay_ssh_input(connection, BytesIO(valid), threading.Lock())
    assert bytes(connection.sent) == valid
    invalid = encode_frame(Frame(FrameType.AI_OPEN, uuid4(), OPEN_PAYLOAD))
    connection = Connection()
    worker._relay_ssh_input(connection, BytesIO(invalid), threading.Lock())
    assert bytes(connection.sent) == b""
