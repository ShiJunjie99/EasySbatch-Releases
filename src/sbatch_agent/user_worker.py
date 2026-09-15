"""Standalone SSH-first user worker with framed control and AI relay.

This file intentionally imports only the Python standard library.  The
Central Broker publishes it as a fixed, read-only server entrypoint which the
Launcher invokes through ``python3 -I``.  It has no shell, filesystem or Slurm
operation.
"""

from __future__ import annotations

import argparse
from enum import IntEnum
import json
import os
import pwd
import re
import socket
import struct
import sys
import threading
from uuid import UUID, uuid4


PROTOCOL_VERSION = 2
MAX_MESSAGE_BYTES = 32 * 1024
MAX_FRAME_SIZE = 64 * 1024
MAX_CONTROL_SIZE = 8 * 1024
MAX_PROVIDER_REQUEST_BYTES = 256 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 160 * 1024
MAX_PROVIDER_STATUS_BYTES = 1024
FRAME_MAGIC = b"ESAI"
FRAME_HEADER = struct.Struct("!4sBB16sI")
CONTROL_STREAM_ID = UUID(int=0)
HELLO_PAYLOAD = b"structured-ai-egress-v2"
OPEN_PAYLOAD = b"api.deepseek.com\0\x01\xbb"
WORKER_READY_PREFIX = "EASYSBATCH_WORKER_READY_V2 "
OPERATIONS = frozenset({"identity", "ping", "shutdown"})


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


LAUNCHER_TO_SERVER = frozenset({
    FrameType.LAUNCHER_HELLO, FrameType.AI_OPEN_OK, FrameType.AI_OPEN_ERROR,
    FrameType.AI_DATA, FrameType.AI_EOF, FrameType.AI_CLOSE, FrameType.AI_ERROR,
    FrameType.AI_PROVIDER_RESPONSE, FrameType.AI_PROVIDER_STATUS,
})
SERVER_TO_LAUNCHER = frozenset({
    FrameType.AI_OPEN, FrameType.AI_DATA, FrameType.AI_EOF,
    FrameType.AI_CLOSE, FrameType.AI_ERROR, FrameType.LAUNCHER_HELLO_ACK,
    FrameType.AI_PROVIDER_REQUEST,
})


class WorkerFailure(RuntimeError):
    pass


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


def _identity():
    uid, gid = os.geteuid(), os.getegid()
    account = pwd.getpwuid(uid)
    if uid <= 0 or gid < 0 or account.pw_name == "root":
        raise WorkerFailure
    return {
        "username": account.pw_name,
        "uid": uid,
        "gid": gid,
        "home": account.pw_dir,
        "hostname": socket.gethostname(),
    }


def _send(connection, value):
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise WorkerFailure
    connection.sendall(encoded)


def _read_line(stream):
    raw = stream.readline(MAX_MESSAGE_BYTES + 2)
    if not raw or len(raw) > MAX_MESSAGE_BYTES + 1 or not raw.endswith(b"\n"):
        raise WorkerFailure
    try:
        value = json.loads(raw[:-1].decode("utf-8", errors="strict"),
                           object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise WorkerFailure from None
    if not isinstance(value, dict):
        raise WorkerFailure
    return value


def _read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            if data:
                raise WorkerFailure
            raise EOFError
        data.extend(chunk)
    return bytes(data)


def _read_frame(stream):
    try:
        magic, version, raw_type, raw_stream_id, size = FRAME_HEADER.unpack(
            _read_exact(stream, FRAME_HEADER.size)
        )
        frame_type = FrameType(raw_type)
    except (ValueError, struct.error):
        raise WorkerFailure from None
    limit = (MAX_PROVIDER_REQUEST_BYTES if frame_type == FrameType.AI_PROVIDER_REQUEST else
             MAX_PROVIDER_RESPONSE_BYTES if frame_type == FrameType.AI_PROVIDER_RESPONSE else
             MAX_PROVIDER_STATUS_BYTES if frame_type == FrameType.AI_PROVIDER_STATUS else
             MAX_FRAME_SIZE)
    if magic != FRAME_MAGIC or version != PROTOCOL_VERSION or size > limit:
        raise WorkerFailure
    stream_id = UUID(bytes=raw_stream_id)
    payload = _read_exact(stream, size) if size else b""
    if frame_type in {FrameType.CONTROL_REQUEST, FrameType.CONTROL_RESPONSE,
                       FrameType.LAUNCHER_HELLO, FrameType.LAUNCHER_HELLO_ACK,
                       FrameType.AI_PROVIDER_STATUS}:
        control_limit = (MAX_PROVIDER_STATUS_BYTES
                         if frame_type == FrameType.AI_PROVIDER_STATUS else MAX_CONTROL_SIZE)
        if stream_id != CONTROL_STREAM_ID or size > control_limit:
            raise WorkerFailure
    elif stream_id == CONTROL_STREAM_ID:
        raise WorkerFailure
    if frame_type in {FrameType.AI_OPEN_OK, FrameType.AI_EOF, FrameType.AI_CLOSE,
                      FrameType.LAUNCHER_HELLO_ACK} and payload:
        raise WorkerFailure
    if frame_type == FrameType.LAUNCHER_HELLO and payload != HELLO_PAYLOAD:
        raise WorkerFailure
    if frame_type == FrameType.AI_OPEN and payload != OPEN_PAYLOAD:
        raise WorkerFailure
    if frame_type in {FrameType.AI_OPEN_ERROR, FrameType.AI_ERROR} and (
            len(payload) != 1 or payload[0] not in range(1, 7)):
        raise WorkerFailure
    return frame_type, stream_id, payload


def _send_frame(stream, frame_type, stream_id, payload=b""):
    limit = (MAX_PROVIDER_REQUEST_BYTES if frame_type == FrameType.AI_PROVIDER_REQUEST else
             MAX_PROVIDER_RESPONSE_BYTES if frame_type == FrameType.AI_PROVIDER_RESPONSE else
             MAX_PROVIDER_STATUS_BYTES if frame_type == FrameType.AI_PROVIDER_STATUS else
             MAX_FRAME_SIZE)
    if not isinstance(payload, bytes) or len(payload) > limit:
        raise WorkerFailure
    stream.sendall(FRAME_HEADER.pack(
        FRAME_MAGIC, PROTOCOL_VERSION, int(frame_type), stream_id.bytes, len(payload),
    ) + payload)


def _hello(connection, identity):
    request_id = str(uuid4())
    _send(connection, {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "operation": "hello",
        "claims": identity,
    })
    stream = connection.makefile("rb", buffering=0)
    response = _read_line(stream)
    if (response.get("request_id") != request_id or response.get("ok") is not True or
            set(response) != {"request_id", "ok", "result"} or
            not isinstance(response.get("result"), dict)):
        raise WorkerFailure
    result = response["result"]
    if (set(result) != {"worker_id", "bootstrap_token", "username", "uid", "ttl_seconds"} or
            result.get("username") != identity["username"] or
            result.get("uid") != identity["uid"] or
            type(result.get("ttl_seconds")) is not int or not 30 <= result["ttl_seconds"] <= 60 or
            not isinstance(result.get("bootstrap_token"), str) or
            re.fullmatch(r"[A-Za-z0-9_-]{43,256}", result["bootstrap_token"]) is None):
        raise WorkerFailure
    _request_id(result.get("worker_id"))
    return stream, result


def _control_request(payload, identity):
    if not payload or len(payload) > MAX_CONTROL_SIZE:
        raise WorkerFailure
    try:
        request = json.loads(payload.decode("utf-8", errors="strict"),
                             object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise WorkerFailure from None
    if not isinstance(request, dict) or set(request) != {"request_id", "operation"}:
        raise WorkerFailure
    request_id = _request_id(request.get("request_id"))
    operation = request.get("operation")
    if operation not in OPERATIONS:
        raise WorkerFailure
    if operation == "identity":
        result = _identity()
    elif operation == "ping":
        result = {"status": "ok"}
    else:
        result = {"status": "closed"}
    response = json.dumps(
        {"request_id": request_id, "ok": True, "result": result},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return response, operation == "shutdown"


def _serve(connection, stream, identity, ssh_output, write_lock):
    while True:
        frame_type, stream_id, payload = _read_frame(stream)
        if frame_type == FrameType.CONTROL_REQUEST:
            response, shutdown = _control_request(payload, identity)
            with write_lock:
                _send_frame(connection, FrameType.CONTROL_RESPONSE, CONTROL_STREAM_ID, response)
            if shutdown:
                return
        elif frame_type in SERVER_TO_LAUNCHER:
            encoded = FRAME_HEADER.pack(
                FRAME_MAGIC, PROTOCOL_VERSION, int(frame_type), stream_id.bytes, len(payload),
            ) + payload
            view = memoryview(encoded)
            while view:
                written = ssh_output.write(view)
                if written is None:
                    break
                if not isinstance(written, int) or written <= 0:
                    raise WorkerFailure
                view = view[written:]
            ssh_output.flush()
        else:
            raise WorkerFailure


def _relay_ssh_input(connection, stream, write_lock):
    """Relay validated Launcher frames; EOF closes this exact Worker channel."""
    try:
        while True:
            frame_type, stream_id, payload = _read_frame(stream)
            if frame_type not in LAUNCHER_TO_SERVER:
                raise WorkerFailure
            with write_lock:
                _send_frame(connection, frame_type, stream_id, payload)
    except (EOFError, OSError, ValueError, WorkerFailure):
        pass
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def run(socket_path):
    if (not isinstance(socket_path, str) or
            re.fullmatch(r"/tmp/easysbatch-[0-9]+/broker\.sock", socket_path) is None):
        raise WorkerFailure
    identity = _identity()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(8)
        connection.connect(socket_path)
        connection.settimeout(None)
        stream, result = _hello(connection, identity)
        write_lock = threading.Lock()
        threading.Thread(
            target=_relay_ssh_input,
            args=(connection, sys.stdin.buffer, write_lock),
            name="easysbatch-ssh-frame-relay",
            daemon=True,
        ).start()
        # This single line is consumed by the Launcher and never written to an
        # application log.  Do not add banners or diagnostics to stdout.
        sys.stdout.write(WORKER_READY_PREFIX + json.dumps({
            "version": PROTOCOL_VERSION,
            "event": "ready",
            "worker_id": result["worker_id"],
            "bootstrap_token": result["bootstrap_token"],
            "username": identity["username"],
            "uid": identity["uid"],
            "ttl_seconds": result["ttl_seconds"],
        }, separators=(",", ":"), sort_keys=True) + "\n")
        sys.stdout.flush()
        result.clear()
        _serve(connection, stream, identity, sys.stdout.buffer, write_lock)
    finally:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "WORKER_INPUT_INVALID\n")


def main(argv=None):
    parser = SafeParser(add_help=False)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    try:
        run(args.socket)
        return 0
    except (OSError, ValueError, WorkerFailure):
        # Fixed category only. Never echo broker responses or environment.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
