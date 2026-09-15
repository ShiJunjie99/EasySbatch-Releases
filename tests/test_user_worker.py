"""M10-B3 remote Worker protocol tests; all I/O is in memory."""

from io import BytesIO
import json
import pwd
import socket
import threading
from uuid import uuid4

import pytest

import sbatch_agent.user_worker as worker


class Sink:
    def __init__(self):
        self.sent = bytearray()

    def sendall(self, value):
        self.sent.extend(value)


class LifecycleSocket:
    def __init__(self):
        self.shutdown_how = None

    def shutdown(self, how):
        self.shutdown_how = how


def control(operation, *, extra=None):
    value = {"request_id": str(uuid4()), "operation": operation}
    if extra:
        value.update(extra)
    payload = json.dumps(value, separators=(",", ":")).encode()
    return worker.FRAME_HEADER.pack(
        worker.FRAME_MAGIC, worker.PROTOCOL_VERSION,
        worker.FrameType.CONTROL_REQUEST, worker.CONTROL_STREAM_ID.bytes,
        len(payload),
    ) + payload


def replies(sink):
    stream = BytesIO(bytes(sink.sent))
    result = []
    while stream.tell() < len(sink.sent):
        frame_type, stream_id, payload = worker._read_frame(stream)
        assert frame_type == worker.FrameType.CONTROL_RESPONSE
        assert stream_id == worker.CONTROL_STREAM_ID
        result.append(json.loads(payload))
    return result


def test_identity_comes_from_effective_uid_and_pwd(monkeypatch):
    entry = pwd.struct_passwd(("alice", "x", 1001, 1002, "", "/home/alice", "/bin/bash"))
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(worker.os, "getegid", lambda: 1002)
    monkeypatch.setattr(worker.pwd, "getpwuid", lambda uid: entry)
    monkeypatch.setattr(worker.socket, "gethostname", lambda: "example-cluster")
    assert worker._identity() == {
        "username": "alice", "uid": 1001, "gid": 1002,
        "home": "/home/alice", "hostname": "example-cluster",
    }


def test_root_worker_is_rejected(monkeypatch):
    entry = pwd.struct_passwd(("root", "x", 0, 0, "", "/root", "/bin/bash"))
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    monkeypatch.setattr(worker.os, "getegid", lambda: 0)
    monkeypatch.setattr(worker.pwd, "getpwuid", lambda uid: entry)
    with pytest.raises(worker.WorkerFailure):
        worker._identity()


def test_allowlisted_identity_ping_shutdown_protocol(monkeypatch):
    identity = {
        "username": "alice", "uid": 1001, "gid": 1001,
        "home": "/home/alice", "hostname": "example-cluster",
    }
    monkeypatch.setattr(worker, "_identity", lambda: dict(identity))
    stream = BytesIO(control("identity") + control("ping") + control("shutdown"))
    sink = Sink()
    worker._serve(sink, stream, identity, BytesIO(), threading.Lock())
    result = replies(sink)
    assert result[0]["result"] == identity
    assert result[1]["result"] == {"status": "ok"}
    assert result[2]["result"] == {"status": "closed"}
    assert all(set(item) == {"request_id", "ok", "result"} for item in result)


@pytest.mark.parametrize("payload", [
    control("command"),
    control("identity", extra={"path": "/tmp"}),
    worker.FRAME_HEADER.pack(b"NOPE", 2, 1, bytes(16), 0),
    worker.FRAME_HEADER.pack(worker.FRAME_MAGIC, 99, 1, bytes(16), 0),
    b'{"version":1,"version":1}\n',
    b"not-json\n",
])
def test_unknown_shell_path_version_and_malformed_protocol_fail_closed(payload):
    with pytest.raises(worker.WorkerFailure):
        worker._serve(Sink(), BytesIO(payload), {}, BytesIO(), threading.Lock())


def test_worker_input_path_is_fixed_and_main_does_not_echo_input(capsys):
    assert worker.main(["--socket", "/tmp/not-allowed.sock"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""
    with pytest.raises(worker.WorkerFailure):
        worker.run("/tmp/not-allowed.sock")


def test_ssh_input_eof_closes_worker_channel_after_valid_hello():
    connection = LifecycleSocket()
    connection.sent = bytearray()
    connection.sendall = connection.sent.extend
    hello = worker.FRAME_HEADER.pack(
        worker.FRAME_MAGIC, worker.PROTOCOL_VERSION,
        worker.FrameType.LAUNCHER_HELLO, bytes(16), len(worker.HELLO_PAYLOAD),
    ) + worker.HELLO_PAYLOAD
    worker._relay_ssh_input(connection, BytesIO(hello), threading.Lock())
    assert connection.shutdown_how == socket.SHUT_RDWR
    assert bytes(connection.sent) == hello


def test_safe_parser_never_echoes_untrusted_argument(capsys):
    with pytest.raises(SystemExit) as caught:
        worker.main(["--unknown", "M10B3_BOOTSTRAP_SECRET_DO_NOT_LOG"])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert "M10B3_BOOTSTRAP_SECRET_DO_NOT_LOG" not in output.err
    assert output.err == "WORKER_INPUT_INVALID\n"
