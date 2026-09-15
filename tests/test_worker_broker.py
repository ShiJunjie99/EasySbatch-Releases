"""M10-B3 broker and bootstrap security regressions; no real Unix socket."""

import json
import logging
import os
from pathlib import Path
import pwd
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest

import sbatch_agent.worker_broker as broker_module
from sbatch_agent.worker_broker import (
    BootstrapTokenRegistry,
    WorkerBroker,
    WorkerBrokerError,
)
from sbatch_agent.ai_egress import AIEgressState


def account(username="alice", uid=1001, gid=1001):
    return pwd.struct_passwd((username, "x", uid, gid, "", f"/home/{username}", "/bin/bash"))


def claims(username="alice", uid=1001, gid=1001, hostname=None):
    return {
        "username": username,
        "uid": uid,
        "gid": gid,
        "home": f"/home/{username}",
        "hostname": hostname or broker_module.socket.gethostname(),
    }


class PeerSocket:
    def __init__(self, *, pid=501, uid=1001, gid=1001):
        self.peer = struct.pack("3i", pid, uid, gid)

    def getsockopt(self, level, option, size):
        assert level == broker_module.socket.SOL_SOCKET
        assert option == broker_module.socket.SO_PEERCRED
        assert size == struct.calcsize("3i")
        return self.peer


class HandshakeSocket(PeerSocket):
    def __init__(self, message, **peer):
        super().__init__(**peer)
        self.incoming = bytearray(
            json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        self.sent = bytearray()
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        if not self.incoming:
            return b""
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True


class FakeWorkerConnection:
    instances = []

    def __init__(self, connection, *, on_disconnect, start=True):
        self.socket = connection
        self.on_disconnect = on_disconnect
        self.connected = True
        self.ai_ready = True
        self.started = False
        self.identity = None
        self.__class__.instances.append(self)
        if start:
            self.start()

    def start(self):
        self.started = True

    def request(self, operation, *, timeout=5):
        assert operation in {"identity", "ping", "shutdown"}
        if operation == "identity":
            return dict(self.identity)
        if operation == "shutdown":
            self.close()
            return {"status": "closed"}
        return {"status": "ok"}

    def close(self):
        if self.connected:
            self.connected = False
            self.socket.close()
            self.on_disconnect()


def message(*, version=broker_module.PROTOCOL_VERSION, value=None):
    return {
        "version": version,
        "request_id": str(uuid4()),
        "operation": "hello",
        "claims": value or claims(),
    }


def response(connection):
    return json.loads(bytes(connection.sent).decode("utf-8"))


def make_broker(tmp_id="12345", **kwargs):
    return WorkerBroker(f"/tmp/easysbatch-{tmp_id}/broker.sock", **kwargs)


def test_bootstrap_is_high_entropy_single_use_and_repr_redacts_secret():
    registry = BootstrapTokenRegistry()
    worker_id = str(uuid4())
    token = registry.create(worker_id)
    assert len(token) >= 43 and token not in repr(registry)
    assert registry.consume(token, worker_id) == worker_id
    with pytest.raises(WorkerBrokerError, match="BOOTSTRAP_REPLAYED"):
        registry.consume(token, worker_id)


def test_bootstrap_ttl_wrong_binding_invalid_and_worker_revocation():
    now = [10.0]
    registry = BootstrapTokenRegistry(clock=lambda: now[0])
    worker_id, other = str(uuid4()), str(uuid4())
    expired = registry.create(worker_id)
    now[0] += 46
    with pytest.raises(WorkerBrokerError, match="BOOTSTRAP_EXPIRED"):
        registry.consume(expired, worker_id)

    wrong = registry.create(worker_id)
    with pytest.raises(WorkerBrokerError, match="BOOTSTRAP_WORKER_MISMATCH"):
        registry.consume(wrong, other)
    with pytest.raises(WorkerBrokerError, match="BOOTSTRAP_REPLAYED"):
        registry.consume(wrong, worker_id)

    revoked = registry.create(worker_id)
    registry.revoke_worker(worker_id)
    with pytest.raises(WorkerBrokerError, match="BOOTSTRAP_INVALID"):
        registry.consume(revoked, worker_id)
    for invalid in ("short", "contains+symbol", None):
        with pytest.raises(WorkerBrokerError, match="BOOTSTRAP_INVALID"):
            registry.consume(invalid, worker_id)


def test_peer_identity_uses_kernel_uid_and_pwd_not_worker_claim(monkeypatch):
    broker = make_broker(account_lookup=lambda uid: account(uid=uid))
    peer, identity = broker._verified_identity(PeerSocket(), claims())
    assert (peer.pid, peer.uid, peer.gid) == (501, 1001, 1001)
    assert (identity.username, identity.uid, identity.home) == ("alice", 1001, "/home/alice")

    for forged in (
        claims(username="mallory"), claims(uid=1002), claims(gid=1002),
        claims(hostname="other-host"),
    ):
        with pytest.raises(WorkerBrokerError, match="WORKER_IDENTITY_MISMATCH"):
            broker._verified_identity(PeerSocket(), forged)


def test_unknown_and_privileged_peer_fail_closed():
    unknown = make_broker(account_lookup=lambda uid: (_ for _ in ()).throw(KeyError(uid)))
    with pytest.raises(WorkerBrokerError, match="WORKER_UNKNOWN_UID"):
        unknown._verified_identity(PeerSocket(), claims())
    root = make_broker(account_lookup=lambda uid: account("root", 0, 0))
    with pytest.raises(WorkerBrokerError, match="WORKER_IDENTITY_MISMATCH"):
        root._verified_identity(PeerSocket(pid=1, uid=0, gid=0),
                                claims("root", 0, 0))


def test_valid_registration_bootstrap_identity_and_cleanup(monkeypatch):
    FakeWorkerConnection.instances.clear()
    monkeypatch.setattr(broker_module, "WorkerConnection", FakeWorkerConnection)
    broker = make_broker(account_lookup=lambda uid: account(uid=uid))
    connection = HandshakeSocket(message())
    broker._register(connection)
    report = response(connection)
    assert report["ok"] is True
    result = report["result"]
    assert result["username"] == "alice" and result["uid"] == 1001
    assert broker.active_count == 1
    fake = FakeWorkerConnection.instances[0]
    fake.identity = claims()
    assert fake.started

    context = broker.consume_bootstrap(result["worker_id"], result["bootstrap_token"])
    assert context.verify_identity().username == "alice"
    context.close()
    assert broker.active_count == 0 and connection.closed


def test_same_uid_workers_are_independent_and_protocol_failures_are_bounded(monkeypatch):
    FakeWorkerConnection.instances.clear()
    monkeypatch.setattr(broker_module, "WorkerConnection", FakeWorkerConnection)
    broker = make_broker(account_lookup=lambda uid: account(uid=uid))
    first = HandshakeSocket(message())
    broker._register(first)
    second = HandshakeSocket(message())
    broker._register(second)
    first_result = response(first)["result"]
    second_result = response(second)["result"]
    assert first_result["worker_id"] != second_result["worker_id"]
    assert first_result["bootstrap_token"] != second_result["bootstrap_token"]
    assert not first.closed and not second.closed and broker.active_count == 2
    FakeWorkerConnection.instances[0].close()
    assert broker.active_count == 1 and not second.closed

    unsupported = HandshakeSocket(message(version=99, value=claims(uid=1002, gid=1002)),
                                  uid=1002, gid=1002)
    broker._register(unsupported)
    assert response(unsupported)["error_code"] == "WORKER_PROTOCOL_UNSUPPORTED"
    assert unsupported.closed

    malformed_message = message(value=claims())
    malformed_message["unexpected"] = True
    malformed = HandshakeSocket(malformed_message)
    broker._register(malformed)
    assert bytes(malformed.sent) == b"" and malformed.closed
    FakeWorkerConnection.instances[1].close()
    assert broker.active_count == 0


def test_same_uid_worker_contexts_get_distinct_egress_and_disconnect_isolated(monkeypatch):
    FakeWorkerConnection.instances.clear()
    monkeypatch.setattr(broker_module, "WorkerConnection", FakeWorkerConnection)
    broker = make_broker(account_lookup=lambda uid: account(uid=uid))
    connections = [HandshakeSocket(message()), HandshakeSocket(message())]
    for connection in connections:
        broker._register(connection)
    reports = [response(connection)["result"] for connection in connections]
    contexts = [broker.consume_bootstrap(item["worker_id"], item["bootstrap_token"])
                for item in reports]

    class Egress:
        def __init__(self, **values):
            self.__dict__.update(values)
            self.egress_id = str(uuid4())
            self.state = AIEgressState.PENDING
            self.credential = values["credential"]
        def invalidate(self):
            self.state = AIEgressState.INVALID
            self.credential = ""

    egresses = [context.attach_ai_egress(
        remote_proxy_port=54000 + index, credential=chr(65 + index) * 43,
        session_factory=lambda **values: Egress(**values),
    ) for index, context in enumerate(contexts)]
    assert egresses[0].worker_session_id != egresses[1].worker_session_id
    assert egresses[0].egress_id != egresses[1].egress_id
    assert egresses[0].credential not in repr(contexts[0])
    FakeWorkerConnection.instances[0].close()
    assert egresses[0].state == AIEgressState.INVALID
    assert egresses[1].state == AIEgressState.PENDING
    assert contexts[1].connected
    contexts[1].close()


def test_broker_publishes_owned_read_only_stable_worker_entrypoint():
    directory = Path(f"/tmp/easysbatch-{uuid4().int}")
    source = b"print('maintained worker')\n"
    broker = WorkerBroker(str(directory / "broker.sock"), worker_source=source)
    try:
        broker._prepare_directory()
        broker._publish_worker()
        metadata = broker.worker_entrypoint.lstat()
        assert broker.worker_entrypoint.name == "user-worker-v2.py"
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_mode & 0o777 == 0o555
        assert broker.worker_entrypoint.read_bytes() == source
        assert broker.legacy_worker_entrypoint.name == "user-worker-v1.py"
        assert broker.legacy_worker_entrypoint.stat().st_mode & 0o777 == 0o555
        assert (b"EASYSBATCH_WORKER_READY_V2" in
                broker.legacy_worker_entrypoint.read_bytes())
        broker._publish_worker()
        assert broker.worker_entrypoint.read_bytes() == source
    finally:
        broker.worker_entrypoint.unlink(missing_ok=True)
        broker.legacy_worker_entrypoint.unlink(missing_ok=True)
        directory.rmdir()


def test_legacy_worker_guard_gives_old_launcher_a_versioned_upgrade_signal(capsys):
    with pytest.raises(SystemExit) as caught:
        exec(compile(
            broker_module.LEGACY_WORKER_GUARD_SOURCE,
            "user-worker-v1.py", "exec",
        ), {})
    assert caught.value.code == 2
    assert capsys.readouterr().out == "EASYSBATCH_WORKER_READY_V2 {}\n"


def test_broker_rejects_unsafe_preexisting_worker_entrypoint():
    directory = Path(f"/tmp/easysbatch-{uuid4().int}")
    broker = WorkerBroker(str(directory / "broker.sock"), worker_source=b"safe\n")
    try:
        broker._prepare_directory()
        broker.worker_entrypoint.write_bytes(b"untrusted\n")
        broker.worker_entrypoint.chmod(0o755)
        with pytest.raises(WorkerBrokerError, match="BROKER_PATH_INVALID"):
            broker._publish_worker()
    finally:
        broker.worker_entrypoint.unlink(missing_ok=True)
        directory.rmdir()


def test_invalid_broker_paths_and_missing_peer_credential_fail_closed(monkeypatch):
    for path in ("broker.sock", "/tmp/broker.sock", "/tmp/easysbatch-a/broker.sock",
                 "/tmp/easysbatch-123/other.sock"):
        with pytest.raises(ValueError):
            WorkerBroker(path)
    monkeypatch.delattr(broker_module.socket, "SO_PEERCRED", raising=False)
    with pytest.raises(WorkerBrokerError, match="BROKER_PEER_UNSUPPORTED"):
        make_broker().start()


def test_new_broker_directory_normalizes_restrictive_umask():
    directory = Path(f"/tmp/easysbatch-{uuid4().int}")
    broker = WorkerBroker(str(directory / "broker.sock"))
    previous = os.umask(0o077)
    try:
        broker._prepare_directory()
        assert directory.stat().st_mode & 0o777 == 0o711
    finally:
        os.umask(previous)
        directory.rmdir()


def test_worker_audit_schema_never_logs_bootstrap_secret(caplog):
    secret = "M10B3_BOOTSTRAP_SECRET_DO_NOT_LOG"
    with caplog.at_level(logging.INFO, logger="sbatch_agent.worker_audit"):
        broker_module.emit_worker_event(
            "SSH_BOOTSTRAP_CREATED", audit_id=str(uuid4()), username="alice",
            uid=1001, result="SUCCESS",
        )
    assert secret not in caplog.text
    payload = json.loads(caplog.records[-1].message)
    assert set(payload) == {
        "audit_id", "error_code", "event", "result", "timestamp", "uid", "username",
    }
    assert "token" not in caplog.records[-1].message.lower()
