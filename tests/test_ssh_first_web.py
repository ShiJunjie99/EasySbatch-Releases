"""M10-B3 Web bootstrap/session binding with a fake kernel-verified broker."""

import logging
import re

from fastapi.testclient import TestClient

from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.ssh_poc import IdentityProbeResult
from sbatch_agent.web import WebConfig, create_app
from sbatch_agent.web_sessions import SSH_FIRST_COOKIE_NAME
from sbatch_agent.worker_broker import WorkerBrokerError
from sbatch_agent.ai_egress import AIEgressError, AIEgressErrorCode, AIEgressState


SECRET = "M10B3_BOOTSTRAP_SECRET_DO_NOT_LOG_0123456789"
BASE_URL = "http://127.0.0.1:51234"


class FakeWorkerContext:
    def __init__(self, username, uid):
        self.identity = IdentityProbeResult(
            username, uid, uid, (), f"/home/{username}", f"/home/{username}", "example-cluster",
        )
        self.connected = True
        self.closed = False
        self.verify_calls = 0
        self._disconnect_callback = None

    def set_disconnect_callback(self, callback):
        assert self._disconnect_callback is None
        self._disconnect_callback = callback

    def verify_identity(self):
        if not self.connected:
            raise WorkerBrokerError("WORKER_DISCONNECTED")
        self.verify_calls += 1
        return self.identity

    def disconnect(self):
        if self.connected:
            self.connected = False
            callback = self._disconnect_callback
            if callback:
                callback()

    def close(self):
        self.closed = True
        self.disconnect()


class FakeEgress:
    def __init__(self, worker_id, username, port, credential, *, healthy=True):
        self.egress_id = "99999999-9999-4999-8999-999999999999"
        self.worker_session_id = worker_id
        self.username = username
        self.remote_proxy_port = port
        self.credential = credential
        self.available = False
        self.state = AIEgressState.PENDING
        self.healthy = healthy

    def health_check(self):
        if not self.healthy:
            self.state = AIEgressState.UNAVAILABLE
            raise AIEgressError(AIEgressErrorCode.UNAVAILABLE)
        self.available = True
        self.state = AIEgressState.READY

    def invalidate(self):
        self.available = False
        self.state = AIEgressState.INVALID
        self.credential = ""


class EgressWorkerContext(FakeWorkerContext):
    def __init__(self, username, uid, worker_id, *, healthy=True):
        super().__init__(username, uid)
        self.worker_id = worker_id
        self.ai_egress = None
        self.healthy = healthy

    def attach_ai_egress(self, *, remote_proxy_port, credential):
        self.ai_egress = FakeEgress(
            self.worker_id, self.identity.username, remote_proxy_port,
            credential, healthy=self.healthy,
        )
        return self.ai_egress

    def attach_structured_ai_egress(self):
        self.ai_egress = FakeEgress(
            self.worker_id, self.identity.username, 0, "",
            healthy=self.healthy,
        )
        return self.ai_egress

    def close(self):
        if self.ai_egress is not None:
            self.ai_egress.invalidate()
        super().close()


class FakeBroker:
    def __init__(self):
        self.pending = {}
        self.consumed = set()
        self.started = False
        self.stopped = False

    def add(self, worker_id, token, context):
        self.pending[(worker_id, token)] = context

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def consume_bootstrap(self, worker_id, token):
        key = (worker_id, token)
        if key in self.consumed:
            raise WorkerBrokerError("BOOTSTRAP_REPLAYED")
        context = self.pending.pop(key, None)
        if context is None:
            raise WorkerBrokerError("BOOTSTRAP_INVALID")
        self.consumed.add(key)
        return context


def config(tmp_path):
    return WebConfig(
        tmp_path / "jobs.sqlite3", tmp_path / "runs",
        authentication_enabled=True, deployment_mode="ssh_first",
        session_cookie_secure=False,
        worker_broker_socket="/tmp/easysbatch-1000/broker.sock",
    )


def csrf(client, path="/session"):
    response = client.get(path)
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def bootstrap(client, broker, worker_id, token, context):
    broker.add(worker_id, token, context)
    first = client.get("/login")
    assert first.status_code == 200
    old = client.cookies.get(SSH_FIRST_COOKIE_NAME)
    response = client.post(
        "/auth/ssh-bootstrap",
        data={"worker_id": worker_id, "bootstrap_token": token},
        headers={"Origin": BASE_URL}, follow_redirects=False,
    )
    return response, old


def test_ssh_first_config_requires_auth_broker_and_loopback_cookie(tmp_path):
    value = config(tmp_path)
    assert value.session_cookie_name == SSH_FIRST_COOKIE_NAME
    assert value.trusted_hosts == ("localhost", "127.0.0.1")
    for changes in (
        {"authentication_enabled": False}, {"session_cookie_secure": True},
        {"worker_broker_socket": None},
        {"worker_broker_socket": "/tmp/arbitrary.sock"},
        {"public_base_url": "http://127.0.0.1:8000"},
    ):
        values = value.__dict__ | changes
        try:
            WebConfig(**values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted unsafe SSH-first config: {changes}")


def test_login_page_requires_launcher_and_has_no_password_form(tmp_path):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert "EasySbatch Launcher" in response.text
        assert 'type="password"' not in response.text
        assert client.post("/login", data={}).status_code == 404
    assert broker.started and broker.stopped


def test_valid_bootstrap_rotates_cookie_binds_worker_and_never_echoes_token(tmp_path, caplog):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    worker_id = "11111111-1111-4111-8111-111111111111"
    context = FakeWorkerContext("alice", 1001)
    with caplog.at_level(logging.INFO), TestClient(app, base_url=BASE_URL) as client:
        response, old = bootstrap(client, broker, worker_id, SECRET, context)
        new = client.cookies.get(SSH_FIRST_COOKIE_NAME)
        assert response.status_code == 303 and response.headers["location"] == "/session"
        assert old != new and len(new) >= 43
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "secure" not in cookie and "domain=" not in cookie
        assert SECRET not in response.text and SECRET not in response.headers
        assert SECRET not in caplog.text and worker_id not in response.headers["location"]
        page = client.get("/session")
        assert "alice" in page.text and "/home/alice" in page.text
        record, status = app.state.session_manager.resolve(new, touch=False)
        assert status == "active" and record.ssh_context is context


def test_invalid_and_replayed_bootstrap_fail_closed(tmp_path):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    worker_id = "22222222-2222-4222-8222-222222222222"
    with TestClient(app, base_url=BASE_URL) as first:
        response, _ = bootstrap(
            first, broker, worker_id, "T" * 43, FakeWorkerContext("alice", 1001),
        )
        assert response.status_code == 303
    with TestClient(app, base_url=BASE_URL) as replay:
        response = replay.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": worker_id, "bootstrap_token": "T" * 43},
            headers={"Origin": BASE_URL},
        )
        assert response.status_code == 401
        assert "启动凭据已失效" in response.text
        record, _ = app.state.session_manager.resolve(
            replay.cookies.get(SSH_FIRST_COOKIE_NAME), touch=False,
        )
        assert record is not None and not record.authenticated


def test_bootstrap_rejects_cross_origin_duplicate_and_extra_fields(tmp_path):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": "x", "bootstrap_token": "T" * 43},
            headers={"Origin": "http://evil.test"},
        )
        assert response.status_code == 403
        response = client.post(
            "/auth/ssh-bootstrap",
            content="worker_id=x&worker_id=y&bootstrap_token=" + "T" * 43,
            headers={"Origin": BASE_URL, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 400
        response = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": "x", "bootstrap_token": "T" * 43, "username": "alice"},
            headers={"Origin": BASE_URL},
        )
        assert response.status_code == 400


def test_cookie_and_browser_username_tampering_cannot_change_identity(tmp_path):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    context = FakeWorkerContext("alice", 1001)
    with TestClient(app, base_url=BASE_URL) as client:
        assert bootstrap(client, broker, "33333333-3333-4333-8333-333333333333",
                         "U" * 43, context)[0].status_code == 303
        rejected = client.post(
            "/session/verify",
            data={"csrf_token": csrf(client), "username": "mallory"},
            follow_redirects=False,
        )
        assert rejected.status_code == 400 and context.verify_calls == 0
        assert "/home/alice" in client.get("/session?username=mallory").text

        token = client.cookies.get(SSH_FIRST_COOKIE_NAME)
        client.cookies.set(SSH_FIRST_COOKIE_NAME, token[:-1] + ("A" if token[-1] != "A" else "B"))
        response = client.get("/session", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_two_worker_sessions_alternate_and_logout_is_isolated(tmp_path):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    alice, bob = FakeWorkerContext("alice", 1001), FakeWorkerContext("bob", 1002)
    with TestClient(app, base_url=BASE_URL) as a:
        b = TestClient(app, base_url=BASE_URL)
        assert bootstrap(a, broker, "44444444-4444-4444-8444-444444444444",
                         "A" * 43, alice)[0].status_code == 303
        assert bootstrap(b, broker, "55555555-5555-4555-8555-555555555555",
                         "B" * 43, bob)[0].status_code == 303
        for client, own, other in ((a, "alice", "bob"), (b, "bob", "alice")) * 2:
            page = client.get("/session")
            assert f"/home/{own}" in page.text and f"/home/{other}" not in page.text

        logged_out = a.post(
            "/logout", data={"csrf_token": csrf(a)}, follow_redirects=False,
        )
        assert logged_out.status_code == 303 and alice.closed
        assert f"/home/bob" in b.get("/session").text and bob.connected


def test_worker_disconnect_immediately_invalidates_only_bound_web_session(tmp_path):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    alice = FakeWorkerContext("alice", 1001)
    with TestClient(app, base_url=BASE_URL) as client:
        bootstrap(client, broker, "66666666-6666-4666-8666-666666666666",
                  "C" * 43, alice)
        alice.disconnect()
        response = client.get("/session", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")
        assert app.state.session_manager.active_count == 1  # fresh anonymous request session only


def test_identity_verify_uses_bound_worker_not_a_new_ssh_login(tmp_path):
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    context = FakeWorkerContext("alice", 1001)
    with TestClient(app, base_url=BASE_URL) as client:
        bootstrap(client, broker, "77777777-7777-4777-8777-777777777777",
                  "D" * 43, context)
        response = client.post(
            "/session/verify", data={"csrf_token": csrf(client)}, follow_redirects=False,
        )
        assert response.status_code == 303 and context.verify_calls == 1
        assert app.state.ssh_authenticator is None


def configure_b5(monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_AI_TRANSPORT_MODE", "per_user_ssh_egress")
    monkeypatch.setenv("SBATCH_AGENT_AI_PROVIDER", "deepseek")
    monkeypatch.setenv("SBATCH_AGENT_AI_MODEL", "deepseek-test")
    monkeypatch.setenv("SBATCH_AGENT_AI_ENDPOINT", "https://api.deepseek.com/chat/completions")
    monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", "M10_B5_SERVER_KEY")
    monkeypatch.setenv("M10_B5_SERVER_KEY", "server-only")


def configure_b5b(monkeypatch):
    configure_b5(monkeypatch)
    monkeypatch.setenv("SBATCH_AGENT_AI_TRANSPORT_MODE", "structured_ssh_egress")


def test_b5b_bootstrap_binds_structured_stream_without_proxy_fields(
        tmp_path, monkeypatch):
    configure_b5b(monkeypatch)
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    worker_id = "89898989-8989-4989-8989-898989898989"
    context = EgressWorkerContext("alice", 1001, worker_id)
    broker.add(worker_id, "V" * 43, context)
    with TestClient(app, base_url=BASE_URL) as client:
        login = client.get("/login")
        assert login.headers["X-EasySbatch-AI-Egress"] == "v2"
        response = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": worker_id, "bootstrap_token": "V" * 43},
            headers={"Origin": BASE_URL}, follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["X-EasySbatch-AI-Status"] == "available"
        record, status = app.state.session_manager.resolve(
            client.cookies.get(SSH_FIRST_COOKIE_NAME), touch=False,
        )
        assert status == "active"
        assert record.identity.username == record.ai_egress.username == "alice"
        assert record.ssh_context.worker_id == record.ai_egress.worker_session_id
        assert record.ai_egress.credential == ""


def test_b5b_ai_connect_failure_does_not_close_worker_or_web(tmp_path, monkeypatch):
    configure_b5b(monkeypatch)
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    worker_id = "90909090-9090-4090-8090-909090909090"
    context = EgressWorkerContext("alice", 1001, worker_id, healthy=False)
    broker.add(worker_id, "W" * 43, context)
    with TestClient(app, base_url=BASE_URL) as client:
        client.get("/login")
        response = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": worker_id, "bootstrap_token": "W" * 43},
            headers={"Origin": BASE_URL}, follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["X-EasySbatch-AI-Status"] == "unavailable"
        assert context.connected and not context.closed
        page = client.get("/session")
        assert "已连接" in page.text and "当前不可用" in page.text


def test_b5_bootstrap_advertises_capability_binds_egress_and_redacts_secret(
        tmp_path, monkeypatch, caplog):
    configure_b5(monkeypatch)
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    worker_id = "88888888-8888-4888-8888-888888888888"
    proxy_secret = "E" * 43
    context = EgressWorkerContext("alice", 1001, worker_id)
    broker.add(worker_id, "T" * 43, context)
    with caplog.at_level(logging.INFO), TestClient(app, base_url=BASE_URL) as client:
        login = client.get("/login")
        assert login.headers["X-EasySbatch-AI-Egress"] == "v1"
        response = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": worker_id, "bootstrap_token": "T" * 43,
                  "ai_egress_port": "54321",
                  "ai_egress_credential": proxy_secret},
            headers={"Origin": BASE_URL}, follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["X-EasySbatch-AI-Status"] == "available"
        assert proxy_secret not in response.text and proxy_secret not in repr(response.headers)
        assert proxy_secret not in caplog.text
        page = client.get("/session")
        assert "AI 服务" in page.text and "可用" in page.text
        assert proxy_secret not in page.text
        token = client.cookies.get(SSH_FIRST_COOKIE_NAME)
        record, status = app.state.session_manager.resolve(token, touch=False)
        assert status == "active" and record.ai_egress is context.ai_egress
        assert record.identity.username == record.ai_egress.username == "alice"
        assert record.ssh_context.worker_id == record.ai_egress.worker_session_id


def test_b5_egress_failure_degrades_ai_without_closing_worker(tmp_path, monkeypatch):
    configure_b5(monkeypatch)
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    worker_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    context = EgressWorkerContext("alice", 1001, worker_id, healthy=False)
    broker.add(worker_id, "T" * 43, context)
    with TestClient(app, base_url=BASE_URL) as client:
        client.get("/login")
        response = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": worker_id, "bootstrap_token": "T" * 43,
                  "ai_egress_port": "54321", "ai_egress_credential": "E" * 43},
            headers={"Origin": BASE_URL}, follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["X-EasySbatch-AI-Status"] == "unavailable"
        assert context.connected and not context.closed
        page = client.get("/session")
        assert "集群连接" in page.text and "已连接" in page.text
        assert "AI 服务" in page.text and "当前不可用" in page.text


def test_b5_bootstrap_rejects_partial_egress_protocol_but_accepts_cluster_only(
        tmp_path, monkeypatch):
    configure_b5(monkeypatch)
    broker = FakeBroker()
    app = create_app(config(tmp_path), profiles=StaticProfiles(), worker_broker=broker)
    with TestClient(app, base_url=BASE_URL) as client:
        client.get("/login")
        malformed = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": "x", "bootstrap_token": "T" * 43,
                  "ai_egress_port": "54321"},
            headers={"Origin": BASE_URL},
        )
        assert malformed.status_code == 400

        worker_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        context = EgressWorkerContext("alice", 1001, worker_id)
        broker.add(worker_id, "U" * 43, context)
        cluster_only = client.post(
            "/auth/ssh-bootstrap",
            data={"worker_id": worker_id, "bootstrap_token": "U" * 43},
            headers={"Origin": BASE_URL}, follow_redirects=False,
        )
        assert cluster_only.status_code == 303
        assert cluster_only.headers["X-EasySbatch-AI-Status"] == "unavailable"
        assert context.connected and context.ai_egress is None
